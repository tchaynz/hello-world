import os
import base64
import json
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import anthropic
from datetime import datetime, timezone, timedelta

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.compose',
    'https://www.googleapis.com/auth/gmail.send',
]


def get_gmail_service():
    creds = None

    token_json = os.environ.get('GMAIL_TOKEN_JSON')
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    elif os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds:
        raise RuntimeError(
            "No Gmail credentials found. Run auth_setup.py locally to generate token.json, "
            "then add its contents as the GMAIL_TOKEN_JSON GitHub secret."
        )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        if not os.environ.get('GMAIL_TOKEN_JSON'):
            with open('token.json', 'w') as f:
                f.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)


def extract_body(payload):
    if payload.get('body', {}).get('data'):
        return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')

    for part in payload.get('parts', []):
        if part['mimeType'] == 'text/plain':
            data = part.get('body', {}).get('data', '')
            if data:
                return base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')

    for part in payload.get('parts', []):
        result = extract_body(part)
        if result:
            return result

    return ''


def get_recent_emails(service, hours=1):
    after = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    query = f'is:unread after:{after} -category:promotions -category:social -category:updates'

    results = service.users().messages().list(userId='me', q=query, maxResults=25).execute()
    messages = results.get('messages', [])

    emails = []
    for msg in messages:
        full = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        headers = {h['name']: h['value'] for h in full['payload']['headers']}

        emails.append({
            'id': msg['id'],
            'thread_id': full['threadId'],
            'subject': headers.get('Subject', '(no subject)'),
            'from': headers.get('From', ''),
            'to': headers.get('To', ''),
            'date': headers.get('Date', ''),
            'body': extract_body(full['payload'])[:3000],
            'list_unsubscribe': headers.get('List-Unsubscribe', ''),
            'list_id': headers.get('List-ID', ''),
            'precedence': headers.get('Precedence', ''),
        })

    return emails


def classify_and_draft(client, email):
    prompt = f"""You are an email assistant. Analyze this email and determine if it is an important
email that requires a personal reply from the recipient.

Email details:
- From: {email['from']}
- Subject: {email['subject']}
- Date: {email['date']}
- List-Unsubscribe header: {email['list_unsubscribe'] or 'None'}
- List-ID header: {email['list_id'] or 'None'}
- Precedence header: {email['precedence'] or 'None'}
- Body:
{email['body']}

Classify this email and respond with a JSON object only — no extra text:
{{
  "needs_reply": true or false,
  "reason": "brief explanation",
  "draft_reply": "full draft reply text if needs_reply is true, otherwise null"
}}

Mark needs_reply as FALSE for:
- Newsletters, digests, subscription emails
- Marketing or promotional content
- Automated notifications (receipts, order updates, shipping, bank alerts)
- Social media notifications
- Emails with List-Unsubscribe, List-ID, or Precedence: bulk headers
- Senders containing "noreply", "no-reply", "donotreply", "notifications", "alerts"
- Spam or unsolicited bulk email

Mark needs_reply as TRUE for:
- Direct personal email from a real person to the recipient
- A colleague, client, or partner asking a question or waiting on something
- Meeting requests or scheduling emails that need confirmation
- Business emails requiring a decision or action

If needs_reply is true, write a professional, helpful draft reply in first person.
Keep it concise and natural — leave placeholders like [NAME] only where truly needed."""

    response = client.messages.create(
        model='claude-opus-4-6',
        max_tokens=1024,
        messages=[{'role': 'user', 'content': prompt}],
    )

    try:
        return json.loads(response.content[0].text)
    except json.JSONDecodeError:
        return {'needs_reply': False, 'reason': 'Could not parse response', 'draft_reply': None}


def create_draft(service, email, draft_text):
    message = MIMEText(draft_text)
    message['To'] = email['from']
    message['Subject'] = f"Re: {email['subject']}"
    message['In-Reply-To'] = email['id']
    message['References'] = email['id']

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

    draft = service.users().drafts().create(
        userId='me',
        body={'message': {'raw': raw, 'threadId': email['thread_id']}},
    ).execute()

    return draft['id']


def send_notification(service, user_email, drafted_emails):
    lines = []
    for i, item in enumerate(drafted_emails, 1):
        lines.append(f"{i}. From:    {item['from']}")
        lines.append(f"   Subject: {item['subject']}")
        lines.append('')

    body = (
        f"Your AI email assistant created {len(drafted_emails)} draft reply(s) for your review.\n\n"
        + '\n'.join(lines)
        + "Review and send your drafts here:\nhttps://mail.google.com/mail/u/0/#drafts\n\n"
        + f"---\nRun completed at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

    message = MIMEText(body)
    message['To'] = user_email
    message['From'] = user_email
    message['Subject'] = f"[AI Assistant] {len(drafted_emails)} draft reply(s) ready"

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
    service.users().messages().send(userId='me', body={'raw': raw}).execute()


def main():
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        raise ValueError('ANTHROPIC_API_KEY environment variable is not set.')

    client = anthropic.Anthropic(api_key=api_key)
    service = get_gmail_service()

    profile = service.users().getProfile(userId='me').execute()
    user_email = profile['emailAddress']
    print(f'Checking emails for {user_email}...')

    hours = int(os.environ.get('CHECK_HOURS', '1'))
    emails = get_recent_emails(service, hours=hours)
    print(f'Found {len(emails)} unread emails to analyse')

    drafted_emails = []

    for email in emails:
        print(f"  Analysing: {email['subject'][:60]}")
        result = classify_and_draft(client, email)

        if result.get('needs_reply') and result.get('draft_reply'):
            draft_id = create_draft(service, email, result['draft_reply'])
            drafted_emails.append({
                'from': email['from'],
                'subject': email['subject'],
                'draft_id': draft_id,
            })
            print(f"    -> Draft created")
        else:
            print(f"    -> Skipped ({result.get('reason', 'no reply needed')})")

    if drafted_emails:
        send_notification(service, user_email, drafted_emails)
        print(f'\nDone. {len(drafted_emails)} draft(s) created. Notification sent to {user_email}.')
    else:
        print('\nDone. No drafts needed this run.')


if __name__ == '__main__':
    main()
