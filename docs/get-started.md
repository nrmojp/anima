# Get started

Create a text-only Discord bot without copying or modifying the Anima framework.
This guide uses an installed, pinned library and a separate application directory.

## Requirements

- Python 3.14 or later, pip, and Git (for installation from a Git commit).
- A Discord application with a bot token and Message Content Intent enabled.
- An OpenAI API key with access to the configured models; requests incur charges.

Invite your bot with the `bot` and `applications.commands` scopes. Grant View
Channel, Read Message History, and Send Messages in a dedicated test channel.
Enable Discord Developer Mode to copy the test server's guild ID.

## 1. Install a reviewed revision

From a POSIX shell:

```sh
mkdir my-bot
cd my-bot
python3.14 -m venv .venv
.venv/bin/python -m pip install 'anima[bot] @ git+https://github.com/nrmojp/anima.git@<full-commit-sha>'
mkdir config
```

Replace `<full-commit-sha>` with the full hash of the revision you reviewed on
GitHub. Do not use an unpinned moving branch or install an unrelated PyPI package
named `anima`. On Windows, use `.venv\Scripts\python.exe` and
`.venv\Scripts\anima.exe`; the commands below do not require sourcing `.env`.

## 2. Create the configuration

The installed package does not include deployment files. Create these three
UTF-8 files yourself:

`config/persona.md`:

```markdown
# Sora
You are Sora, a curious companion in this Discord server.
Use concise, warm language. Be honest about uncertainty.
```

`config/rules.md`:

```markdown
- Never claim an action succeeded unless it actually did.
- Do not reveal credentials or private conversation data.
- Respect the separation between servers and direct messages.
```

`config/appearance.md`:

```markdown
# Appearance
No fixed appearance is defined yet.
```

Create `.env` in `my-bot/` and replace the three placeholder values:

```dotenv
DISCORD_BOT_TOKEN=<your bot token>
OPENAI_API_KEY=<your API key>
ANIMA_ALLOWED_GUILD_IDS=<your test guild ID>
ANIMA_PERSONA_NAMES=Sora
ANIMA_COMMAND_PREFIX=sora
ANIMA_PLUGINS=
ANIMA_DM_ENABLED=false
ANIMA_ENABLE_WEB_SEARCH=false
```

An explicitly empty `ANIMA_PLUGINS` disables optional Plugins, not the core
conversation, memory, inventory, or dashboard. An empty guild allowlist denies
all guilds. DMs remain disabled. Keep `.env` and `state/` out of Git; add both to
your application's `.gitignore` before making a commit.

Your directory should now look like this:

```text
my-bot/
  .env
  .gitignore
  .venv/
  config/
    persona.md
    rules.md
    appearance.md
```

Also ignore `.venv/`. State directories are created by the runtime. No application
Python code or custom Plugin package is needed for this first launch.

## 3. Start and verify

From `my-bot/`:

```sh
.venv/bin/anima run
```

The host loads `.env` automatically. Open the dashboard at
[http://127.0.0.1:8765/](http://127.0.0.1:8765/), select your test sandbox, and
mention the bot in the allowed channel. Confirm that it replies and the dashboard
shows the response trace. New guilds start in `reply` mode. Optional Plugins should
be absent or disabled. Stop the foreground process with Ctrl+C.

The dashboard is loopback-only and read-only by default. Conversation and memory
processing send data to the model provider and can incur charges. Use non-sensitive
test data and review usage; do not enable autonomous activity for the first test.

If startup fails, check required credentials and Python version. If the bot connects
but stays silent, check the guild allowlist, Message Content Intent, channel
permissions, and mention the bot explicitly. If port 8765 is occupied, set
`ANIMA_DASHBOARD_PORT` to another unused port in `.env` and restart.

## Next steps

- [Build your own bot](build-your-bot.md): persona, dashboard language, modes,
  optional voice and Web search, and application Plugins.
- [Library usage](library.md): package your application and pin upgrades.
- [Plugin development](plugin-development.md): extend capabilities in your own namespace.
- [Backup and recovery](backup.md): protect sandbox state before migrations.
- [Architecture](architecture.md): core, adapter, and extension boundaries.

For framework development or the repository's Docker recipe instead of a separate
application, see [the README's source-checkout instructions](../README.md#run-from-a-framework-checkout).
