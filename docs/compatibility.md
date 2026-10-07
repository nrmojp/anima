# Compatibility policy

## Supported environment

- Python 3.14
- Docker Engine with Compose v2
- discord.py 2.x
- OpenAI Python SDK 2.x and the Responses API

- eSpeak NG from the Python 3.14 slim image's Debian repositories
- Discord voice support provided by the `discord.py[voice]` extra

## Public API

### Base-system migration (pre-alpha)

- `ANIMA_CONFIG_ROOT` is replaced by `ANIMA_ROOT` (default `config/`).
- `ANIMA_ALLOWED_GUILDS` is replaced by `ANIMA_ALLOWED_GUILD_IDS`; empty now denies all.
- Plugin enablement uses `ANIMA_PLUGINS`; web search is independently opt-in.
- Plugin UI keys use `<plugin>.<key>`; plugin JSON files keep unprefixed keys.
- The minimal `PersonaAgent` / `ModelReply` / `FileMemoryRetriever` implementation is
  replaced by `PersonaActor` / `ResponseDraft` / `LocalMemoryRetriever`.
- Top-level compatibility facades re-export the layered implementation. New integrations
  should import `core`, `capabilities`, `adapters`, or `bootstrap` explicitly.
- Discord state uses `sandboxes/guilds/<id>` and `sandboxes/dms/<id>`. Older template
  directories under `discord_guild` / `discord_dm` must be backed up and deliberately
  migrated while stopped; no automatic move or deletion is performed.
- Persona, rules, appearance, and branding are deployment configuration. No existing
  private configuration, runtime data, or feature media is copied into this template.

Before `1.0`, modules re-exported by `anima.core`, `anima.capabilities`, and
`anima.bootstrap` are the intended public surface, but incompatible changes may occur in
minor releases. Adapter internals and sample Plugins are examples, not stable APIs.

After `1.0`, incompatible public-contract or persisted-format changes require a major
version. Deprecations should remain for at least one minor release and include migration
instructions. Security fixes may remove unsafe behavior without a deprecation window.
