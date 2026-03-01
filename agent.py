import os
import re
import base64
import json
import requests
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import anthropic
from datetime import datetime, timezone, timedelta

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.compose',
    'https://www.googleapis.com/auth/gmail.send',
]

# 7 triage categories with display labels
CATEGORIES = {
    'needs_reply':   'Needs Reply',
    'needs_action':  'Needs Action',
    'waiting_for':   'Waiting For',
    'delegate':      'Delegate',
    'read_later':    'Read Later',
    'newsletter':    'Newsletter',
    'no_action':     'No Action',
}

# Points toward cognitive load score (0-10)
COGNITIVE_WEIGHTS = {
    'needs_reply':  3,
    'needs_action': 2,
    'delegate':     1,
    'waiting_for':  1,
    'read_later':   0,
    'newsletter':   0,
    'no_action':    0,
}


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


def get_recent_emails(service, hours=8):
    after = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    query = f'is:unread after:{after} -category:promotions -category:social -category:updates'

    results = service.users().messages().list(userId='me', q=query, maxResults=50).execute()
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


def triage_email(client, email):
    prompt = f"""You are an expert email triage assistant. Classify this email into exactly one category.

Categories:
- needs_reply:  A real person sent this directly and expects a personal reply
- needs_action: Requires a task or decision from you, but no email reply needed
- waiting_for:  You sent this or are CC'd — you're waiting on the other party
- delegate:     Should be forwarded to or handled by someone else
- read_later:   Worth reading for context but no action required
- newsletter:   Subscribed newsletter, digest, or regular publication
- no_action:    Automated notification, receipt, alert, spam, or bulk mail

Email details:
- From: {email['from']}
- Subject: {email['subject']}
- Date: {email['date']}
- List-Unsubscribe: {email['list_unsubscribe'] or 'None'}
- List-ID: {email['list_id'] or 'None'}
- Precedence: {email['precedence'] or 'None'}
- Body:
{email['body']}

Respond with a JSON object only — no extra text:
{{
  "category": "<one of the 7 categories>",
  "reason": "one concise sentence",
  "urgency": "high|medium|low",
  "draft_reply": "full draft reply if category is needs_reply, otherwise null"
}}"""

    response = client.messages.create(
        model='claude-opus-4-6',
        max_tokens=1024,
        messages=[{'role': 'user', 'content': prompt}],
    )

    try:
        raw = response.content[0].text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        result = json.loads(raw)
        if result.get('category') not in CATEGORIES:
            result['category'] = 'no_action'
        return result
    except (json.JSONDecodeError, IndexError):
        return {'category': 'no_action', 'reason': 'Could not parse response', 'urgency': 'low', 'draft_reply': None}


def mark_as_read(service, email_id):
    service.users().messages().modify(
        userId='me',
        id=email_id,
        body={'removeLabelIds': ['UNREAD']},
    ).execute()


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


def log_to_notion(token, database_id, email, category, reason, urgency, draft_gmail_url=None):
    try:
        dt = parsedate_to_datetime(email['date'])
        date_iso = dt.astimezone(timezone.utc).isoformat()
    except Exception:
        date_iso = datetime.now(timezone.utc).isoformat()

    properties = {
        'Subject':  {'title':     [{'text': {'content': email['subject'][:2000]}}]},
        'From':     {'rich_text': [{'text': {'content': email['from'][:2000]}}]},
        'Category': {'select':    {'name': CATEGORIES[category]}},
        'Urgency':  {'select':    {'name': urgency.capitalize()}},
        'Reason':   {'rich_text': [{'text': {'content': reason[:2000]}}]},
        'Received': {'date':      {'start': date_iso}},
    }

    if draft_gmail_url:
        properties['Draft'] = {'url': draft_gmail_url}

    response = requests.post(
        'https://api.notion.com/v1/pages',
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Notion-Version': '2022-06-28',
        },
        json={'parent': {'database_id': database_id}, 'properties': properties},
        timeout=10,
    )
    return response.status_code == 200


def compute_cognitive_load(results):
    raw = sum(COGNITIVE_WEIGHTS.get(r['category'], 0) for r in results)
    return min(10, raw)


def send_notification(service, user_email, drafted_emails, summary, cognitive_score):
    load_label = 'Low' if cognitive_score <= 3 else 'Medium' if cognitive_score <= 6 else 'High'

    lines = [
        f"Cognitive Load: {cognitive_score}/10 ({load_label})",
        '',
        'Triage Summary:',
    ]
    for key, label in CATEGORIES.items():
        count = summary.get(key, 0)
        if count > 0:
            lines.append(f"  {label}: {count}")

    if drafted_emails:
        lines += ['', '--- Drafts Ready ---']
        for i, item in enumerate(drafted_emails, 1):
            lines.append(f"{i}. From:    {item['from']}")
            lines.append(f"   Subject: {item['subject']}")
            lines.append('')
        lines.append('Review drafts: https://mail.google.com/mail/u/0/#drafts')

    lines.append(f"\n---\nRun completed at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    message = MIMEText('\n'.join(lines))
    message['To'] = user_email
    message['From'] = user_email
    message['Subject'] = (
        f"[AI Triage] Load {cognitive_score}/10 — "
        f"{summary.get('needs_reply', 0)} draft(s), "
        f"{summary.get('needs_action', 0)} action(s)"
    )

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
    service.users().messages().send(userId='me', body={'raw': raw}).execute()


def main():
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        raise ValueError('ANTHROPIC_API_KEY environment variable is not set.')

    notion_token = os.environ.get('NOTION_API_KEY')
    notion_db = os.environ.get('NOTION_DATABASE_ID')
    use_notion = bool(notion_token and notion_db)

    client = anthropic.Anthropic(api_key=api_key)
    service = get_gmail_service()

    profile = service.users().getProfile(userId='me').execute()
    user_email = profile['emailAddress']
    print(f'Checking emails for {user_email}...')

    hours = int(os.environ.get('CHECK_HOURS', '8'))
    emails = get_recent_emails(service, hours=hours)
    print(f'Found {len(emails)} unread emails to triage')

    results = []
    drafted_emails = []
    summary = {k: 0 for k in CATEGORIES}
    notion_failures = 0

    for email in emails:
        print(f"  Triaging: {email['subject'][:60]}")
        result = triage_email(client, email)

        category = result.get('category', 'no_action')
        reason = result.get('reason', '')
        urgency = result.get('urgency', 'low')
        draft_gmail_url = None

        if category == 'needs_reply' and result.get('draft_reply'):
            draft_id = create_draft(service, email, result['draft_reply'])
            draft_gmail_url = f"https://mail.google.com/mail/u/0/#drafts/{draft_id}"
            drafted_emails.append({'from': email['from'], 'subject': email['subject']})
            print(f"    -> [{CATEGORIES[category]}] Draft created")
        else:
            print(f"    -> [{CATEGORIES[category]}] {reason}")

        mark_as_read(service, email['id'])

        if use_notion:
            ok = log_to_notion(notion_token, notion_db, email, category, reason, urgency, draft_gmail_url)
            if not ok:
                notion_failures += 1

        summary[category] += 1
        results.append({'category': category})

    cognitive_score = compute_cognitive_load(results)

    if notion_failures:
        print(f'Warning: {notion_failures} Notion log(s) failed.')

    if emails:
        send_notification(service, user_email, drafted_emails, summary, cognitive_score)
        print(
            f'\nDone. Cognitive load: {cognitive_score}/10. '
            f'{len(drafted_emails)} draft(s) created. '
            f'Notification sent to {user_email}.'
        )
    else:
        print('\nDone. No new emails this run.')


if __name__ == '__main__':
    main()
