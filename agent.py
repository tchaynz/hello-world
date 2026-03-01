import os
import re
import base64
import json
import requests
from email.mime.text import MIMEText
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

CATEGORIES = {
    'needs_reply':   'Needs Reply',
    'needs_action':  'Needs Action',
    'waiting_for':   'Waiting For',
    'delegate':      'Delegate',
    'important':     'New Important Emails',
    'read_later':    'Read Later',
    'newsletter':    'Newsletter',
    'unimportant':   'Unimportant',
    'spam':          'Potential Spam',
    'no_action':     'No Action',
}

# Points toward cognitive load score (0-10)
COGNITIVE_WEIGHTS = {
    'needs_reply':  3,
    'needs_action': 2,
    'important':    1,
    'delegate':     1,
    'waiting_for':  1,
    'read_later':   0,
    'newsletter':   0,
    'unimportant':  0,
    'spam':         0,
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
- important:    Important email worth reading (bill, legal notice, account alert) but no immediate reply or action needed
- read_later:   Worth reading for context or reference but not urgent
- newsletter:   Subscribed newsletter, digest, or regular publication
- unimportant:  Marketing, promotional, social media notification, or other low-value bulk mail
- spam:         Unsolicited, suspicious, or likely spam
- no_action:    Automated receipt, system notification, or confirmation requiring no attention

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
  "category": "<one of the 10 categories>",
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


def push_briefing_to_notion(token, page_id, all_emails, summary, cognitive_score, drafted_count):
    """Create a briefing page under a Notion parent page."""
    load_label = 'Low' if cognitive_score <= 3 else 'Medium' if cognitive_score <= 6 else 'High'
    now = datetime.now(timezone.utc)
    title = f"Email Briefing — {now.strftime('%b %-d, %Y %H:%M UTC')}"
    total = len(all_emails)

    children = []

    # Executive Summary
    children.append(_notion_heading("Executive Summary"))
    stats = f"{total} email{'s' if total != 1 else ''} processed  •  Cognitive Load: {cognitive_score}/10 ({load_label})"
    if drafted_count > 0:
        stats += f"  •  {drafted_count} draft(s) created → https://mail.google.com/mail/u/0/#drafts"
    children.append(_notion_paragraph(stats))

    breakdown = " | ".join(
        f"{CATEGORIES[k]}: {v}" for k, v in summary.items() if v > 0
    )
    if breakdown:
        children.append(_notion_paragraph(breakdown))
    children.append(_notion_divider())

    # Action Required
    actionable_cats = ['needs_reply', 'needs_action', 'delegate', 'waiting_for']
    actionable = [e for e in all_emails if e['category'] in actionable_cats]
    if actionable:
        children.append(_notion_heading("Action Required", level=2))
        for cat in actionable_cats:
            items = [e for e in actionable if e['category'] == cat]
            if not items:
                continue
            children.append(_notion_heading(f"{CATEGORIES[cat]} ({len(items)})", level=3))
            for item in items:
                tag = f"[{item['urgency'].upper()}]" if item['urgency'] == 'high' else f"[{item['urgency'].capitalize()}]"
                children.append(_notion_paragraph(f"{tag} {item['from']} — {item['subject']}"))
                children.append(_notion_paragraph(f"  ↳ {item['reason']}", italic=True))
                if item.get('body'):
                    children.append(_notion_paragraph(item['body'][:400], italic=True))
        children.append(_notion_divider())

    # New Important Emails
    important = [e for e in all_emails if e['category'] == 'important']
    if important:
        children.append(_notion_heading(f"New Important Emails ({len(important)})", level=2))
        for item in important:
            children.append(_notion_paragraph(f"{item['from']} — {item['subject']}"))
            children.append(_notion_paragraph(f"  ↳ {item['reason']}", italic=True))
            if item.get('body'):
                children.append(_notion_paragraph(item['body'][:400], italic=True))
        children.append(_notion_divider())

    # Read Later
    read_later = [e for e in all_emails if e['category'] == 'read_later']
    if read_later:
        children.append(_notion_heading(f"Read Later ({len(read_later)})", level=2))
        for item in read_later:
            children.append(_notion_paragraph(f"{item['from']} — {item['subject']}"))
            children.append(_notion_paragraph(f"  ↳ {item['reason']}", italic=True))
            if item.get('body'):
                children.append(_notion_paragraph(item['body'][:300], italic=True))
        children.append(_notion_divider())

    # Unimportant Emails (newsletter + unimportant)
    unimportant = [e for e in all_emails if e['category'] in ('newsletter', 'unimportant')]
    if unimportant:
        children.append(_notion_heading(f"Unimportant Emails ({len(unimportant)})", level=2))
        for item in unimportant:
            children.append(_notion_paragraph(f"{item['from']} — {item['subject']}"))
            children.append(_notion_paragraph(f"  ↳ {item['reason']}", italic=True))
        children.append(_notion_divider())

    # Potential Spam
    spam = [e for e in all_emails if e['category'] == 'spam']
    if spam:
        children.append(_notion_heading(f"Potential Spam ({len(spam)})", level=2))
        for item in spam:
            children.append(_notion_paragraph(f"{item['from']} — {item['subject']}"))
            children.append(_notion_paragraph(f"  ↳ {item['reason']}", italic=True))
        children.append(_notion_divider())

    # No Action
    no_action = [e for e in all_emails if e['category'] == 'no_action']
    if no_action:
        children.append(_notion_heading(f"No Action ({len(no_action)})", level=2))
        for item in no_action:
            children.append(_notion_paragraph(f"{item['from']} — {item['subject']}"))
            children.append(_notion_paragraph(f"  ↳ {item['reason']}", italic=True))

    response = requests.post(
        'https://api.notion.com/v1/pages',
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Notion-Version': '2022-06-28',
        },
        json={
            'parent': {'page_id': page_id},
            'properties': {'title': {'title': [{'text': {'content': title}}]}},
            'children': children[:100],  # Notion API limit
        },
        timeout=15,
    )
    return response.status_code == 200


def _notion_heading(text, level=1):
    key = f"heading_{level}"
    return {key: {'rich_text': [{'text': {'content': text}}]}}


def _notion_paragraph(text, italic=False):
    annotations = {'italic': True} if italic else {}
    return {'paragraph': {'rich_text': [{'text': {'content': text}, 'annotations': annotations}]}}


def _notion_divider():
    return {'divider': {}}


def compute_cognitive_load(results):
    raw = sum(COGNITIVE_WEIGHTS.get(r['category'], 0) for r in results)
    return min(10, raw)


def main():
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        raise ValueError('ANTHROPIC_API_KEY environment variable is not set.')

    notion_token = os.environ.get('NOTION_API_KEY')
    notion_page = os.environ.get('NOTION_PAGE_ID')
    use_notion = bool(notion_token and notion_page)

    client = anthropic.Anthropic(api_key=api_key)
    service = get_gmail_service()

    print('Checking emails...')

    hours = int(os.environ.get('CHECK_HOURS', '8'))
    emails = get_recent_emails(service, hours=hours)
    print(f'Found {len(emails)} unread emails to triage')

    all_emails = []
    drafted_count = 0
    summary = {k: 0 for k in CATEGORIES}

    for email in emails:
        print(f"  Triaging: {email['subject'][:60]}")
        result = triage_email(client, email)

        category = result.get('category', 'no_action')
        reason = result.get('reason', '')
        urgency = result.get('urgency', 'low')

        if category == 'needs_reply' and result.get('draft_reply'):
            create_draft(service, email, result['draft_reply'])
            drafted_count += 1
            print(f"    -> [{CATEGORIES[category]}] Draft created")
        else:
            print(f"    -> [{CATEGORIES[category]}] {reason}")

        mark_as_read(service, email['id'])

        all_emails.append({
            'category': category,
            'from': email['from'],
            'subject': email['subject'],
            'reason': reason,
            'urgency': urgency,
            'body': email['body'][:400],
        })

        summary[category] += 1

    cognitive_score = compute_cognitive_load(all_emails)

    if use_notion and emails:
        ok = push_briefing_to_notion(
            notion_token, notion_page, all_emails, summary, cognitive_score, drafted_count,
        )
        if ok:
            print('Briefing pushed to Notion.')
        else:
            print('Warning: Failed to push briefing to Notion.')

    if emails:
        print(
            f'\nDone. Cognitive load: {cognitive_score}/10. '
            f'{drafted_count} draft(s) created.'
        )
    else:
        print('\nDone. No new emails this run.')


if __name__ == '__main__':
    main()
