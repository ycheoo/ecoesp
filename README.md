# ecoesp — Economist Espresso Translator

A small tool that turns the daily [Economist Espresso](https://www.economist.com/espresso) "The world in brief" email into a bilingual study pack. It reads the latest newsletter from your Gmail, uses Google's Gemini models to translate it into Chinese and annotate the tricky vocabulary, generates a spoken bilingual recording, and emails the HTML, plain-text, and MP3 back to you.

For each *Today's Top Stories* bullet the recording plays: English original → vocabulary explanation → English original → Chinese translation → English original once more — a rhythm for listening practice.

## What you need

- Linux x86_64 for the prebuilt binary, or Linux with Python 3.10+ to run from source
- `ffmpeg`
- A Google account whose Gmail receives the Espresso newsletter
- Gmail API OAuth credentials (Desktop app) — free
- One or more [Gemini API keys](https://aistudio.google.com/apikey) — the free tier is enough to try it

## Install

### Prebuilt binary

Download the archive for the latest version from
[GitHub Releases](https://github.com/ycheoo/ecoesp/releases). For example, after
downloading `ecoesp_v0.1.0_linux_amd64.tar.gz`:

```bash
sudo apt install ffmpeg
tar xzf ecoesp_v0.1.0_linux_amd64.tar.gz
install -Dm755 ecoesp ~/.local/bin/ecoesp
~/.local/bin/ecoesp --version
```

Replace `v0.1.0` with the version you downloaded. The binary includes Python and
the application's dependencies; `ffmpeg` remains a system dependency and must be
available on `PATH`. Add `~/.local/bin` to `PATH` if your distribution does not
already do so.

### Run from source

On Debian or Ubuntu:

```bash
sudo apt install python3 python3-venv ffmpeg
git clone https://github.com/ycheoo/ecoesp.git
cd ecoesp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 1. Gmail OAuth

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a project and **enable the Gmail API**.
2. Configure the OAuth consent screen; while it is in *Testing* mode, add your Gmail address as a test user.
3. Create an **OAuth Client ID** of type **Desktop app** and download the JSON.
4. Save it as `~/.config/ecoesp/credentials.json` and lock it down:

```bash
mkdir -p ~/.config/ecoesp
mv ~/Downloads/client_secret_*.json ~/.config/ecoesp/credentials.json
chmod 600 ~/.config/ecoesp/credentials.json
```

The tool requests read-only + send access to Gmail. The first run opens a browser once to authorize; the token is cached under `~/.local/state/ecoesp/`.

## 2. Configuration

Create the configuration file and restrict its permissions:

```bash
mkdir -p ~/.config/ecoesp
touch ~/.config/ecoesp/.env
chmod 600 ~/.config/ecoesp/.env
```

Open `~/.config/ecoesp/.env` in an editor and set at least these values:

```dotenv
# One or more Gemini API keys, comma-separated. More keys = more throughput and
# quota headroom for the audio step; give each key its own Google Cloud project.
GEMINI_API_KEY=your-key-1,your-key-2

# The Gmail account that receives the Espresso email (and sends the result).
READER_EMAIL=you@gmail.com

# Where to send the finished study pack (can be the same address).
DEST_EMAIL=you@gmail.com

# How to find the source email.
GMAIL_QUERY=from:noreply@e.economist.com subject:"world in brief"
```

Everything else is optional and documented in
[`.env.example`](.env.example) (model choices, TTS voice, timeouts). In a source
checkout, you can copy that file instead of creating an empty one.

## 3. Run

For the prebuilt binary:

```bash
ecoesp
```

From a source checkout:

```bash
.venv/bin/python -m ecoesp
```

It looks for a matching email from the last 24 hours, builds the translation, vocabulary, and audio, and emails the result. Each message is delivered only once; useful flags:

| Flag | Effect |
| --- | --- |
| `--force` | Rebuild and resend the latest matching message even if already delivered |
| `--require-audio` | Fail instead of sending a text-only email when audio generation fails |
| `--prepare-only` | Build the translation and scripts but skip TTS and sending |
| `--lookback-hours N` | Search the last `N` hours instead of 24 |
| `--version` | Print the version (release binaries report their tag; source runs report `dev`) |

By default, if audio generation fails (for example the Gemini TTS free-tier quota runs out), the HTML and plain-text email is still sent without the MP3.

## Optional: make it yours

Everything below is optional — the tool works out of the box without any of it.

**An opening jingle.** Drop a clip at `~/.local/share/ecoesp/opening.pcm` to play a personal intro before every recording. It must be raw signed 16-bit little-endian PCM, 24kHz, mono — the format Gemini TTS returns — so convert yours with:

```bash
ffmpeg -i my-opening.mp3 -f s16le -ar 24000 -ac 1 ~/.local/share/ecoesp/opening.pcm
```

**The narration and translation prompts.** Drop a file at `~/.config/ecoesp/prompts/<name>.md` to replace any shipped prompt — `tts_original.md`, `tts_vocab.md`, `tts_translation.md`, `text_translation.md`, `text_vocab.md`. Only the ones you supply are overridden; the rest keep shipping defaults, so they still improve when you upgrade.

> Keep the markdown headings the shipped `text_translation.md` produces (`## 一`, `#### 原文`, `#### 中文翻译`, `#### 生词注释`). The audio pipeline parses them to split each story, so a prompt that stops emitting them will break audio generation.

**The email template.** Drop a file at `~/.config/ecoesp/email.html`; `{body}` is replaced with the rendered story.

**Pacing and the subject line.** `SEGMENT_GAP_SECONDS`, `BULLET_GAP_SECONDS`, and `SUBJECT_PREFIX` in your `.env` — see `.env.example`.

## Optional: run it daily with systemd

Complete the browser authorization once in a terminal, then add a user service and timer so it runs each morning after the newsletter arrives. Create `~/.config/systemd/user/ecoesp.service`:

```ini
[Unit]
Description=Translate the latest Economist Espresso email

[Service]
Type=oneshot
ExecStart=%h/.local/bin/ecoesp
```

When running from source instead, add
`WorkingDirectory=/absolute/path/to/ecoesp` and use
`ExecStart=/absolute/path/to/ecoesp/.venv/bin/python -m ecoesp`.

And `~/.config/systemd/user/ecoesp.timer`:

```ini
[Unit]
Description=Run the Espresso translator daily

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Then enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now ecoesp.timer
journalctl --user -u ecoesp.service -n 100   # inspect logs
```

## Optional: build a single-file binary

To compile everything (Python included, ffmpeg excluded) into one Linux executable:

```bash
packaging/build.sh
```

The result is `dist/ecoesp`; it reads the same configuration from the same places, so `ExecStart` in the systemd unit can point at it instead of the venv. The script builds inside its own temporary venv, so it won't touch your environment. A binary runs only on systems whose glibc is at least as new as the build machine's.

## Where things live

The tool follows the XDG base-directory convention and never writes into the project folder:

| Path | Contents |
| --- | --- |
| `~/.config/ecoesp/` | `.env`, `credentials.json`, and any prompt or email-template overrides |
| `~/.local/share/ecoesp/` | Your own assets — currently `opening.pcm` |
| `~/.local/state/ecoesp/` | OAuth token, list of already-delivered messages |
| `~/.cache/ecoesp/` | Generated text and audio, grouped by Gmail message ID (auto-pruned after 7 days) |

## Troubleshooting

- **`credentials.json not found`** — make sure the OAuth JSON is at `~/.config/ecoesp/credentials.json` (or set `GOOGLE_CREDENTIALS_PATH`).
- **No email arrives** — check the terminal/journal output; confirm `GMAIL_QUERY` matches a message from the last 24 hours (`--lookback-hours` widens the window).
- **Email has no MP3** — usually TTS quota exhaustion or a missing `ffmpeg`; look for `Audio generation failed` in the output. Adding more `GEMINI_API_KEY` values gives the audio step more quota.

## License

[MIT](LICENSE).
