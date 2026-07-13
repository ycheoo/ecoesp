# Opening audio (optional)

If you place an `opening.pcm` file in this directory, it is prepended to the
start of every generated recording — use it for a short personal intro or
jingle. It is entirely optional: with no `opening.pcm`, recordings simply start
at the first bullet.

The file must be **raw signed 16-bit little-endian PCM, 24 kHz, mono** — the
same format Gemini TTS returns — so it can be concatenated with the synthesized
audio without re-encoding. To convert your own clip with `ffmpeg`:

```bash
ffmpeg -i my-opening.mp3 -f s16le -ar 24000 -ac 1 assets/opening.pcm
```

Nothing in this repository ships an opening clip; bring your own if you want one.
