# Shared audio routes

Anima exposes one sandbox-scoped audio route with three independent lanes:

| Lane | Intended input | Mix behavior |
| --- | --- | --- |
| `speech` | TTS and spoken responses | Ducks music while a speech frame is present |
| `music` | Songs and ambient loops | Uses the configured normal or ducked volume |
| `effect` | Short one-shot sounds | Overlays the mix without ducking music |

```text
Voice Plugin ─ AudioOutput(speech) ─┐
Future Music ─ AudioOutput(music) ──┼─ MixingAudioSource ─ Discord VC
Future Effect ─ AudioOutput(effect) ┘
```

The host creates one `DiscordVoiceOutput` and one mixer per sandbox. A Plugin submits a
WAV file or a streaming `PCMSource` with an `AudioLane`; it does not control Discord's
player or the mixer's slots.
Submitting a second source to the same lane stops, cleans up, and replaces the first.
Lanes can also be stopped independently. This means speech can finish without stopping
music, and an effect can play over both.

`AudioOutput` is the asynchronous Port intended for Plugins. `play_wav` is the convenient
artifact route used by Voice; `play_pcm` is the streaming route for a future decoder,
Music, or Effect Plugin. Both converge on the same mixer. The lower-level
`AudioMixerPort` and `AudioInput` contracts let an Adapter connect a continuous transport.

The complete base control surface is deliberately capability-neutral:

- play a PCM or WAV source on any lane;
- replace or stop one lane without affecting the others;
- set `music`, `speech`, and `effect` volume independently;
- set the volume used for Music while Speech is active;
- stop all lanes and disconnect the transport.

The Discord Adapter keeps one continuous PCM source attached to the voice connection.
Idle reads produce silence so that a later lane can begin without replacing Discord's
player. Every volume is clamped to `0.0..1.0`. PCM addition saturates rather than
wrapping on overflow.
