"""Gmail I/O: authentication, finding the daily email, extracting/cleaning its
body, and sending the translated result.
"""

import base64
import io
import os
import pickle
import re
import time
from datetime import date
from email.mime.audio import MIMEAudio
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser

import google_auth_httplib2
import httplib2
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from ..config import SCOPES


def _authorized_http(cfg, creds):
    """An httplib2 transport carrying the OAuth credentials, left at httplib2's
    defaults so it honors whatever proxy environment the launching process (e.g.
    a systemd unit) sets. AuthorizedHttp refreshes tokens over this same
    transport, so refreshes use the same connection settings."""
    http = httplib2.Http(timeout=180)
    return google_auth_httplib2.AuthorizedHttp(creds, http=http)


def get_gmail_service(cfg):
    creds = None
    if os.path.exists(cfg.token_path):
        with open(cfg.token_path, 'rb') as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(cfg.credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(cfg.token_path, 'wb') as f:
            pickle.dump(creds, f)
    return build('gmail', 'v1', http=_authorized_http(cfg, creds))


def find_espresso_email(cfg, service, lookback_hours=24):
    # Epoch seconds avoid Gmail's timezone-ambiguous date parsing: this is a
    # strict lookback window regardless of account/server timezone.
    since = int(time.time()) - lookback_hours * 3600
    query = f'{cfg.gmail_query} after:{since}'
    results = service.users().messages().list(userId='me', q=query, maxResults=5).execute()
    messages = results.get('messages', [])
    if messages:
        print(f'Found {len(messages)} message(s)')
        return messages[0]
    return None


def extract_body(payload):
    """Extract the largest inline HTML and plain-text MIME body candidates."""
    html_bodies = []
    plain_bodies = []

    def is_attachment(part):
        if part.get('filename', '').strip():
            return True
        for header in part.get('headers', []):
            if (header.get('name', '').lower() == 'content-disposition'
                    and 'attachment' in header.get('value', '').lower()):
                return True
        return False

    def decode(data):
        padded = data + '=' * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode('utf-8', errors='replace')

    def walk(part):
        mime = part.get('mimeType', '')
        if not is_attachment(part) and mime in {'text/html', 'text/plain'}:
            data = part.get('body', {}).get('data', '')
            if data:
                body = decode(data)
                if mime == 'text/html':
                    html_bodies.append(body)
                else:
                    plain_bodies.append(body)
        for sub in part.get('parts', []):
            walk(sub)

    walk(payload)
    html_body = max(html_bodies, key=len, default=None)
    plain_body = max(plain_bodies, key=len, default=None)
    return html_body, plain_body


class _HTMLTextExtractor(HTMLParser):
    HIDDEN_TAGS = {'head', 'script', 'style', 'noscript', 'template', 'svg'}
    BLOCK_TAGS = {
        'address', 'article', 'aside', 'blockquote', 'div', 'dl', 'dt', 'dd',
        'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3',
        'h4', 'h5', 'h6', 'header', 'hr', 'li', 'main', 'nav', 'ol', 'p',
        'pre', 'section', 'table', 'tr', 'ul',
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.HIDDEN_TAGS:
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            return
        if tag == 'br':
            self.parts.append('\n')
        elif tag in self.BLOCK_TAGS:
            self.parts.append('\n\n')

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.HIDDEN_TAGS:
            if self.hidden_depth:
                self.hidden_depth -= 1
            return
        if self.hidden_depth:
            return
        if tag in {'td', 'th'}:
            self.parts.append(' ')
        elif tag in self.BLOCK_TAGS:
            self.parts.append('\n\n')

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(re.sub(r'\s+', ' ', data.replace('\xa0', ' ')))

    def text(self):
        lines = []
        previous_blank = False
        for raw_line in ''.join(self.parts).splitlines():
            line = re.sub(r'\s+', ' ', raw_line).strip()
            if line:
                lines.append(line)
                previous_blank = False
            elif lines and not previous_blank:
                lines.append('')
                previous_blank = True
        return '\n'.join(lines).strip()


def html_to_text(html):
    """Extract readable text and structural line breaks from an HTML body."""
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def get_email_content(service, message_id):
    msg = service.users().messages().get(userId='me', id=message_id, format='full').execute()
    subject = ''
    sender = ''
    for header in msg['payload']['headers']:
        name = header['name'].lower()
        if name == 'subject':
            subject = header['value']
        elif name == 'from':
            sender = header['value']
    html_body, plain_body = extract_body(msg['payload'])
    if html_body:
        text = html_to_text(html_body)
    elif plain_body:
        text = plain_body
    else:
        text = ''
    return subject, sender, text


def send_email(cfg, service, original_subject, html_content, plain_content, audio_path=None):
    alternative = MIMEMultipart('alternative')
    alternative.attach(MIMEText(plain_content, 'plain', 'utf-8'))
    alternative.attach(MIMEText(html_content, 'html', 'utf-8'))

    if audio_path:
        msg = MIMEMultipart('mixed')
        msg.attach(alternative)
        with open(audio_path, 'rb') as f:
            audio = MIMEAudio(f.read(), _subtype='mpeg')
        audio.add_header('Content-Disposition', 'attachment',
                         filename=f'espresso-{date.today().isoformat()}.mp3')
        msg.attach(audio)
    else:
        msg = alternative

    msg['Subject'] = f'[译] {original_subject}'
    msg['From'] = cfg.reader_email
    msg['To'] = cfg.dest_email

    # Media upload handles large messages (audio attachments) that break the
    # JSON body's size limit. num_retries backs off and retries transient upload
    # failures (e.g. an intermittent SSL error on a large upload).
    media = MediaIoBaseUpload(io.BytesIO(msg.as_bytes()),
                              mimetype='message/rfc822', resumable=True)
    service.users().messages().send(
        userId='me', body={}, media_body=media).execute(num_retries=5)
    print(f'Sent: [译] {original_subject}')
