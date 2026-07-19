"""Gmail I/O: authentication, finding the daily email, extracting/cleaning its
body, and sending the translated result.
"""

import base64
import io
import os
import pickle
import re
import sys
import time
import webbrowser
from contextlib import contextmanager
from datetime import date
from email.mime.audio import MIMEAudio
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser

from urllib.parse import parse_qs, urlparse

import google_auth_httplib2
import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from ..config import APP_NAME, SCOPES
from ..storage.files import atomic_write


class GmailAuthenticationError(RuntimeError):
    """Gmail authorization cannot be completed in the current environment."""


def _has_graphical_session():
    """Whether opening an interactive system browser is expected to work.

    Linux terminal browsers such as w3m cannot complete Google's modern sign-in
    flow. A desktop Linux session exposes DISPLAY or WAYLAND_DISPLAY; macOS and
    Windows have native browser launchers that do not use those variables.
    """
    if sys.platform in {'darwin', 'win32'}:
        return True
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


@contextmanager
def _stderr_silenced():
    """Silence file descriptor 2 for the duration, children included.

    When no browser is installed, xdg-open probes every browser it knows and
    prints one "not found" line per miss — a dozen-plus lines of noise around
    the one line that matters, the authorization URL. The probes are child
    processes writing straight to fd 2, out of reach of Python-level
    redirection, so the silencing has to happen at the descriptor."""
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved = os.dup(2)
    os.dup2(devnull, 2)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(devnull)


# Where Google redirects the browser in the pasted-URL flow. Nothing ever
# listens here: the page fails to load by design, and the user copies the
# address — carrying the authorization code — back into the terminal. OAuth
# requires the value to match byte-for-byte between the authorization URL and
# the token exchange, hence one fixed literal rather than a random free port.
PASTE_REDIRECT_URI = 'http://localhost:8765/'


def _extract_auth_code(pasted, expected_state):
    """The authorization code from whatever the user pasted.

    Accepts the full redirected localhost URL (the instructed flow) or a bare
    code, for users who spot and copy just the parameter themselves."""
    text = pasted.strip()
    if not text:
        raise GmailAuthenticationError(
            'nothing was pasted. Run the auth command again.')
    if '://' not in text:
        return text
    params = parse_qs(urlparse(text).query)
    if 'error' in params:
        raise GmailAuthenticationError(
            f'Google reported: {params["error"][0]}.')
    if params.get('state', [None])[0] != expected_state:
        raise GmailAuthenticationError(
            'the pasted URL is from a different authorization attempt; use '
            'the link printed by this run.')
    code = params.get('code', [None])[0]
    if not code:
        raise GmailAuthenticationError(
            'the pasted URL has no code parameter; copy the full address of '
            'the localhost page the browser lands on after approval.')
    return code


def _authorize_by_pasted_url(cfg):
    """First-time authorization for terminal-only sessions.

    Prints the Google authorization URL for the user to open on any device
    with a browser. After consent, Google redirects that browser to
    PASTE_REDIRECT_URI; the page fails to load — nothing listens there — but
    the address bar keeps the URL, and the user pastes it back here. The code
    inside it is harmless on its own: what stops anyone else from redeeming
    it is the PKCE verifier held only in this process's memory (an installed
    app's client secret is not confidential — Google says to treat it so),
    plus the code being single-use and expiring within minutes."""
    flow = Flow.from_client_secrets_file(
        cfg.credentials_path, SCOPES,
        redirect_uri=PASTE_REDIRECT_URI,
        autogenerate_code_verifier=True)
    url, state = flow.authorization_url()
    print('\nOpen this link in a browser on any device and approve access:\n')
    print(f'  {url}\n')
    # One sentence per line: the terminal soft-wraps long lines itself, and
    # breaking at sentence boundaries reads naturally at any width.
    print('The browser will end up on a localhost page that fails to load — '
          'that is expected.')
    print('Copy the full URL from its address bar and paste it below.',
          flush=True)
    pasted = input('Pasted URL: ')
    code = _extract_auth_code(pasted, state)
    print('Exchanging the code for a token...', flush=True)
    # Broad on purpose: the exchange can fail as an oauthlib error (expired,
    # already-used, or mangled code) or as a network error, and every one of
    # them means the same thing to the person at the terminal.
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        raise GmailAuthenticationError(
            f'the code exchange failed ({e}). Authorization codes are '
            'single-use and expire within minutes; run the auth command '
            'again for a fresh link.') from e
    return flow.credentials


def _authorize_in_terminal(cfg):
    """Authorization fallback when a local browser cannot be opened: the
    pasted-URL flow if someone is at the terminal, otherwise a clear error —
    an unattended run (systemd) can only ever use an existing token."""
    if not sys.stdin.isatty():
        raise GmailAuthenticationError(
            'first-time Gmail authorization needs a person at a terminal, '
            'and this session has none. Run the auth command in an '
            'interactive shell (any device with a browser can approve it), '
            f'or copy an existing token.pickle to {cfg.token_path}.')
    return _authorize_by_pasted_url(cfg)


def _authorized_http(cfg, creds):
    """An httplib2 transport carrying the OAuth credentials, left at httplib2's
    defaults so it honors whatever proxy environment the launching process (e.g.
    a systemd unit) sets. AuthorizedHttp refreshes tokens over this same
    transport, so refreshes use the same connection settings."""
    http = httplib2.Http(timeout=180)
    return google_auth_httplib2.AuthorizedHttp(creds, http=http)


def get_gmail_credentials(cfg, interactive=False):
    """Return valid Gmail credentials, refreshing the stored token as needed.

    Refreshing an expired token is silent and always allowed. Interactive
    (re-)authorization, by contrast, only happens when `interactive` is set —
    that is the dedicated auth command. A pipeline run instead fails fast with
    instructions: it must never sit waiting on a browser or a pasted URL,
    because its callers (a timer, a wrapper script) may have nobody watching.
    """
    creds = None
    if os.path.exists(cfg.token_path):
        with open(cfg.token_path, 'rb') as f:
            creds = pickle.load(f)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            atomic_write(cfg.token_path, pickle.dumps(creds))
            return creds
        except TransportError as e:
            raise GmailAuthenticationError(
                f'the stored Gmail token could not be refreshed because '
                f'Google could not be reached ({e}); check the network and '
                'try again.') from e
        except RefreshError as e:
            if not interactive:
                raise GmailAuthenticationError(
                    f'the stored Gmail token could not be refreshed ({e}); '
                    f'it may have been revoked. Run "{APP_NAME} auth" once '
                    'in an interactive terminal to authorize again.') from e
            # The auth command owns re-authorization, and a dead refresh
            # token is no better than no token: fall through and authorize
            # from scratch, or the very command the error message points at
            # would fail the same way forever.
            print('The stored token could not be refreshed; authorizing again.')
            creds = None
    if not interactive:
        raise GmailAuthenticationError(
            f'no usable Gmail token at {cfg.token_path}. Run "{APP_NAME} '
            'auth" once in an interactive terminal to authorize, or copy an '
            'existing token.pickle there.')
    if _has_graphical_session():
        flow = InstalledAppFlow.from_client_secrets_file(cfg.credentials_path, SCOPES)
        try:
            # Silenced: when no browser is installed, the xdg-open probe
            # floods stderr; the printed URL is the flow that matters then.
            with _stderr_silenced():
                creds = flow.run_local_server(port=0)
        except webbrowser.Error:
            # The graphical session was a false promise (e.g. a stale
            # DISPLAY): same situation as headless, same fallback.
            creds = _authorize_in_terminal(cfg)
    else:
        creds = _authorize_in_terminal(cfg)
    atomic_write(cfg.token_path, pickle.dumps(creds))
    return creds


def get_gmail_service(cfg):
    creds = get_gmail_credentials(cfg)
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


class _PromoBulletFinder(HTMLParser):
    """Locate Today's-Top-Stories promotional bullets so the caller can excise
    them from the raw HTML before it is flattened to text.

    Real news bullets lead with a bold run (``▸ <b>…</b> …``); the newsletter's
    event/promo bullets instead italicise the whole line (``▸ <i>…</i>``). That
    distinction is lost once the HTML is flattened, so this runs on the raw HTML
    and records the character span of every ``<p>`` whose text, right after the
    ▸ glyph, is led by an italic (``<i>``/``<em>``) run rather than bold. The
    signal is the leading tag specifically — a ``<b>``-led bullet that merely
    contains italics later (e.g. a book title) is left alone.
    """

    ITALIC_TAGS = {'i', 'em'}
    GLYPH = '▸'  # ▸

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self._html = html
        # Absolute offset of each line's start, matching HTMLParser's own
        # newline-based line counting, so getpos() maps back to a string index.
        self._line_starts = [0]
        for i, char in enumerate(html):
            if char == '\n':
                self._line_starts.append(i + 1)
        self.spans = []
        self._reset()

    def _reset(self):
        self._p_start = None         # offset of the current <p>'s '<'
        self._seen_glyph = False     # the ▸ has appeared in this <p>
        self._awaiting_lead = False  # ▸ seen, still waiting for the lead run
        self._is_promo = False

    def _offset(self):
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def _tag_end(self):
        """Offset just past the '>' of the tag currently being handled."""
        return self._html.index('>', self._offset()) + 1

    def handle_starttag(self, tag, attrs):
        if tag == 'p':
            self._reset()
            self._p_start = self._offset()
        elif self._awaiting_lead:
            # The first inline element after the ▸ decides: italic => promo.
            self._is_promo = tag in self.ITALIC_TAGS
            self._awaiting_lead = False

    def handle_data(self, data):
        if self._p_start is None:
            return
        if not self._seen_glyph:
            idx = data.find(self.GLYPH)
            if idx == -1:
                return
            self._seen_glyph = True
            # Non-whitespace text right after the ▸ (no tag) is a plain lead.
            self._awaiting_lead = not data[idx + 1:].strip()
        elif self._awaiting_lead and data.strip():
            self._awaiting_lead = False

    def handle_endtag(self, tag):
        if tag == 'p' and self._p_start is not None:
            if self._is_promo:
                self.spans.append((self._p_start, self._tag_end()))
            self._reset()


def strip_promo_bullets(html):
    """Remove Today's-Top-Stories promo bullets (see _PromoBulletFinder) from
    the raw HTML before it is flattened to text.

    Fails open: if no promo bullet is detected the HTML is returned unchanged,
    so a change to the newsletter's markup can only let a promo slip through —
    never drop a real story.
    """
    if not html:
        return html
    finder = _PromoBulletFinder(html)
    finder.feed(html)
    finder.close()
    for start, end in sorted(finder.spans, reverse=True):
        html = html[:start] + html[end:]
    return html


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
        text = html_to_text(strip_promo_bullets(html_body))
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

    subject = (f'{cfg.subject_prefix} {original_subject}'
               if cfg.subject_prefix else original_subject)
    msg['Subject'] = subject
    msg['From'] = cfg.reader_email
    msg['To'] = cfg.dest_email

    # Media upload handles large messages (audio attachments) that break the
    # JSON body's size limit. num_retries backs off and retries transient upload
    # failures (e.g. an intermittent SSL error on a large upload).
    media = MediaIoBaseUpload(io.BytesIO(msg.as_bytes()),
                              mimetype='message/rfc822', resumable=True)
    service.users().messages().send(
        userId='me', body={}, media_body=media).execute(num_retries=5)
    print(f'Sent: {subject}')
