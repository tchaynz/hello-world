"""
Run this script ONCE on your local machine to authorise Gmail access.
It will open a browser for you to log in, then save token.json.

Steps:
1. Place your credentials.json from Google Cloud Console in this folder
2. Run: python auth_setup.py
3. Copy the printed token JSON and add it as GMAIL_TOKEN_JSON in GitHub Secrets

Requirements: pip install google-auth-oauthlib
"""

import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.compose',
    'https://www.googleapis.com/auth/gmail.send',
]

flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
creds = flow.run_local_server(port=0)

token_json = creds.to_json()

with open('token.json', 'w') as f:
    f.write(token_json)

print('token.json saved successfully.\n')
print('=' * 60)
print('Copy everything below as your GMAIL_TOKEN_JSON GitHub secret:')
print('=' * 60)
print(token_json)
