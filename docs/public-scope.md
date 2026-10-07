# Public scope and content licenses

## Included

- Python source for the persona-agent Core, Capability contracts, adapters, and bootstrap
- tests, CI, Docker configuration, and development documentation
- neutral sample persona and rules, plus local eSpeak NG voice configuration
- an empty runtime state directory containing only `.gitkeep`

The included source and original documentation are licensed under MIT as stated in
`LICENSE`.

## Excluded

- real persona identity, private prompts, memories, mood, habitus, logs, and telemetry
- Discord guild, channel, message, or user IDs
- API credentials and local `.env` files
- character art, emoji, stickers, generated images, music, lyrics, and other media
- voice models, training data, checkpoints, and generated speech
- runtime databases, indexes, caches, and state

User-supplied personas and media are not automatically covered by Anima's MIT license.
Users are responsible for the rights and terms of every asset, model, dataset, SDK, and
service they add.

## Distribution model

The upstream project publishes source code, not a prebuilt Docker image. The included
Dockerfile and Compose configuration are recipes for users to build locally. Python,
discord.py, the OpenAI SDK, eSpeak NG, transitive dependencies, and base-image
packages remain under their respective licenses; Discord and OpenAI usage remains
subject to their service terms.

If the project later distributes container images, executables, model files, or media,
maintainers must perform a separate dependency and notice review for that artifact.

## Publication check

Run `python scripts/check_public_tree.py` before publication. CI runs the same scanner.
It rejects tracked secret files, non-empty credential assignments, probable Discord IDs,
private keys, media/model formats, runtime state, and Git LFS pointers. Maintainers may
set `PUBLIC_SCAN_DENY_TERMS` to a comma-separated private-name denylist during release.
