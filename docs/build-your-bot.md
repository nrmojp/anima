# Build your own bot with Anima

Anima is a foundation for your own persona agent. It supplies conversation, memory,
inventory, bounded autonomous activity, extension contracts, and an operations
dashboard. It does not supply a finished character, your private memories, or a
complete set of entertainment or business features.

The simplest customization is a persona-only, text-only Discord bot. You can add
capabilities later without editing the conversation engine.

## 1. Make your own project

Fork the repository or copy/clone it into your own working directory:

```sh
git clone https://github.com/nrmojp/anima.git my-bot
cd my-bot
cp .env.example .env
```

Use a private repository or local checkout if your persona or configuration should
not be public. Anima's MIT license covers its original code, not third-party content
or assets you add. Preserve required license notices when redistributing a derivative.

## 2. Give the bot an identity

Edit the sample files in `config/`. No Python changes are required.

| File | What belongs here |
| --- | --- |
| `persona.md` | Name, personality, interests, relationships, and speaking style |
| `rules.md` | Behavioral boundaries and rules the character should follow |
| `appearance.md` | Visual identity for capabilities that need it; optional in meaning, keep the file |
| `dashboard.json` | Optional dashboard title and introductory text |
| `plugins/<id>.json` | Non-secret settings declared by an installed Plugin |

For example, replace `config/persona.md` with:

```markdown
# Sora

You are Sora, a curious companion in this Discord server.
You enjoy astronomy and small everyday discoveries.
Use concise, warm language. Be honest about uncertainty.
```

Keep credentials out of these files. Persona text influences behavior; it does not
grant permissions or override sandbox isolation.

To brand the dashboard, optionally create `config/dashboard.json`:

```json
{
  "locale": "en",
  "browser_title": "Sora Operations",
  "heading": "Sora Observatory",
  "eyebrow": "SORA / STATUS",
  "memory_guide": "Sora's memories and learned habits in the selected sandbox."
}
```

The dashboard defaults to English. Use `"locale": "ja"` for Japanese. This changes
the interface and date/number formatting, not the persona or memory contents.

## 3. Configure a first text-only bot

Create your own Discord application and bot. Enable Message Content Intent, invite
it with the `bot` and `applications.commands` scopes, and grant the channel permissions
it needs to view messages, read history, and send replies. Optional audio capabilities
also need voice-channel Connect and Speak permissions.

Fill in `.env` locally:

```dotenv
DISCORD_BOT_TOKEN=
OPENAI_API_KEY=
ANIMA_ALLOWED_GUILD_IDS=<your test guild ID>
ANIMA_PERSONA_NAMES=Sora
ANIMA_COMMAND_PREFIX=sora
ANIMA_PLUGINS=
ANIMA_DM_ENABLED=false
ANIMA_ENABLE_WEB_SEARCH=false
```

Do not paste real values into source code or commit `.env`. Use your own test guild
first. An empty guild allowlist disables all guilds; DMs require a separate opt-in.
An explicit empty `ANIMA_PLUGINS` disables optional Plugins, not conversation, memory,
inventory, management commands, or the dashboard.

`ANIMA_PERSONA_NAMES` contains comma-separated calling names. The command prefix is
a lowercase ASCII identifier of up to 20 characters. Discord's bot name and avatar
are managed separately in your Discord application; editing persona text does not
change them.

The framework sends conversation data to the configured model provider. Conversation
and memory processing can incur API charges. Review what your bot may send and monitor
usage in the dashboard; do not use private test data without appropriate permission.

## 4. Run and check it

With Python 3.14, from the project root (POSIX shell):

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -e '.[bot]'
.venv/bin/anima run
```

The application loads `.env` automatically. On Windows, use the corresponding
`.venv\Scripts\` executables. Stop a local foreground process with Ctrl+C.

Alternatively, build and run the local container:

```sh
mkdir -p state
# On Linux, grant container uid 10001 write access to state/ and config/.
docker compose up --build
# To stop it from another shell:
docker compose stop
```

The dashboard starts with the bot at [http://127.0.0.1:8765/](http://127.0.0.1:8765/).
The sample Compose configuration exposes it only on loopback. It is read-only unless
you configure a long private `ANIMA_DASHBOARD_ADMIN_TOKEN`.

Mention your bot in the allowed guild and check its reply and dashboard trace.
New guilds start in `reply` mode. With the example prefix, use `/sora-mode` to change
activity and `/sora-maintenance` for controlled maintenance. These commands require
Manage Server permission. Keep `reply` mode for initial tests; `proactive` also permits
autonomous activity and can produce additional model requests.

Runtime data is stored in `state/`, separated by sandbox. Keep it out of Git and back
it up before migrations. [Backup and recovery](backup.md) describes the offline commands.

## 5. Add only the capabilities you want

Installed does not mean enabled. `PluginLoader` discovers `src/anima/plugins/<id>/plugin.py`,
while `ANIMA_PLUGINS` selects which discovered Plugins run.

| Bundled Plugin | Purpose |
| --- | --- |
| `echo` | Small reference tool and explicit `/echo` command, not the normal conversation engine |
| `voice` | Local eSpeak NG synthesis and playback through the shared Discord audio output |
| `web_search` | Optional native Web search; also requires `ANIMA_ENABLE_WEB_SEARCH=true` |

For example, use `ANIMA_PLUGINS=voice` for local speech capability, or
`ANIMA_PLUGINS=voice,web_search` with Web permission enabled. The Voice Plugin supplies
a model-callable `speak` tool; enabling it does not make every text reply automatically
spoken. See [Voice setup](voice.md), including the local eSpeak NG requirement.

Configure a Plugin through `config/plugins/<id>.json`, its declared environment
variables, or the dashboard. Dashboard keys are namespaced, for example `voice.speed`.
Restart after changing environment variables, Plugin selection, or source code.
Rebuild the container after source or dependency changes; `config/` and `state/` are
bind-mounted, but Plugin source is packaged into the image.

Music playback, drawing, reminders, custom emoji reactions, and character-specific
assets are not bundled. A Plugin or your own implementation is needed for those features.

## 6. Create a Plugin when configuration is not enough

Use `src/anima/plugins/echo/` as the smallest working reference. Add your own directory
under `src/anima/plugins/` and export `PLUGIN` from its `plugin.py`.

When copying the example, change its manifest name, capability IDs, tool names,
command paths, configuration namespace, and environment variable names so it does
not collide with the installed example. Then implement your behavior. Keep its
helpers and SDK integrations inside that Plugin directory.
Declare required external packages in your project's dependency setup; PluginLoader
discovers code but does not install packages. Preserve licenses and notices for any
third-party source you copy into a Plugin.

- Use a Tool for an action the model can choose.
- Use a Command for an explicit user operation.
- Declare Resource Collections for targets accessed through primitive resource tools.
- Declare configuration and dashboard panels rather than adding special cases to the host.
- Use sandbox-bound services for storage, messages, audio, jobs, and event ingress.

Add the Plugin ID to `ANIMA_PLUGINS`. You do not need to edit a central registry or
the bootstrap to register it. Plugins are trusted Python code, not security-isolated
downloads: review them before enabling them.

See [Plugin development](plugin-development.md), [base extension routes](base-routes.md),
and [framework composition](framework-composition.md) for the actual contracts.

## 7. Keep your bot maintainable

Put personality and deployment settings in configuration, behavior additions in
Plugins, and runtime state in `state/`. This keeps framework upgrades separate from
your character and capabilities.

Before committing code changes, run all unit tests, inspect coverage, and run
`git diff --check`. `python scripts/check_framework_sync.py` verifies the common
framework snapshot; adding a persona or Plugin does not change that snapshot.
If you deliberately modify the framework itself, review the change, test it, and
update its lock as described in [framework composition](framework-composition.md).
