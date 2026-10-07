# Local voice plugin

The bundled Voice Plugin uses eSpeak NG to generate WAV audio entirely inside the local
container. It sends that audio to the requesting member's current Discord voice channel
through the host's `AudioOutput` port and its shared `speech` lane. It does not call a speech API, download a voice
model, or require an API key.

The model can call the `speak` tool. Playback succeeds only for a guild message whose
author is currently connected to a voice channel. The Adapter connects or moves the Bot
to that channel, mixes playback with the other [audio routes](audio.md), and waits for
the speech lane to finish before the temporary WAV file is deleted. DMs and users outside a VC receive a
tool rejection.

Configure the Plugin in `config/plugins/voice.json`:

Declared environment variables override this file. Dashboard settings override both
with namespaced keys such as `voice.speed`; saved operational settings require restart.
When the final human leaves the VC, the host stops all audio lanes and disconnects.

- `enabled`: expose or hide the `speak` tool.
- `voice`: an eSpeak NG voice name; the sample uses `ja`.
- `speed`: words per minute, from 80 to 450.
- `volume`: eSpeak amplitude, from 0 to 200.
- `maximum_characters`: per-invocation input limit, up to 2,000.
- `timeout_seconds`: synthesis timeout, from 1 to 120 seconds.

The Dockerfile installs eSpeak NG, and the bot extra installs discord.py's voice
dependencies. A small AudioSource converts the generated WAV into Discord's 48 kHz
stereo PCM format, so FFmpeg is not required. eSpeak NG prioritizes size and offline
operation over naturalness. A fork can replace the synthesizer without changing Core.

eSpeak NG is not relicensed under Anima's MIT license. This repository
publishes source and a local-build Docker recipe rather than a prebuilt image; review
their licenses if you redistribute a built image.
