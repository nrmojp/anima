# Base system verification

## Scope

The base runtime includes conversation and agentic loops, sandbox isolation,
memory and Vector Store synchronization, sleep and self time, autonomous speech,
Inventory and primitive resource tools, plugin loading and asynchronous services,
Discord transport, shared audio routing and mixing, and the operations Dashboard.

The bundled examples remain `echo` and local eSpeak NG `voice`. No persona media,
music catalog, image generation, reminders, custom emoji, credentials, or runtime
state are distributed. Dashboard plugin status is rendered from plugin metadata
and a generic snapshot rather than fixed music/reminder panels.

## Checks (2026-09-27)

- Python 3.14: 417 unit tests passed.
- Linux Python 3.14 container: 417 unit tests passed, with no external network and
  a read-only repository mount.
- Total line coverage: 98%; the CI minimum remains 95%.
- New proactivity, mailbox, service lifecycle, message output, resource providers,
  runtime configuration, state storage, audio mixer and sample plugins: 100%.
- Docker build and isolated `anima --help`: passed. No Bot was started.
- Public-tree scan includes tracked and untracked files, not only committed files.
- JavaScript syntax and `git diff --check`: passed.

No paid API or live Discord test was executed. Production credentials and persona
data were not imported. Live conversation and VC checks require a configured
deployment and are separate from these offline checks.

## Remaining coverage boundaries

100% is a target, not a claim for every transferred class. The coverage report
still identifies the following SDK-independent boundaries:

- `core/actor.py`: 318–319 (fallback forwarding of an unusual maintenance
  exception), 607 and 656 (unreachable assertions after bounded retry loops).
- `core/sandbox.py`: 55–56 and 66 (defensive checks already rejected by Event
  or SandboxKey validation before those statements).
- `capabilities/commands.py`: 91, 169, 175–181 (invalid namespace and optional
  availability-provider rejection paths).
- `capabilities/contracts.py`: 192, 265, 278–287 (native-kind accessor, invalid
  context instructions and optional availability-provider rejection paths).
- `bootstrap/settings.py`: 30, 33, 46, 48 (non-file/non-object plugin configuration
  and float/list environment conversion; bundled examples use bool/int/text).
- `bootstrap/native_tools.py`: 17 and 31 (attempt to execute a native tool as a
  function and ignoring a malformed citation source).

Startup, operational CLI and SDK adapters retain additional unexecuted defensive
and external failure paths. These are visible in `coverage report --show-missing`;
they must not be confused with live API or gateway validation.

## Operational compatibility

Read `compatibility.md` before migrating an existing installation. The default
configuration denies all guilds and DMs. Dashboard exposure is loopback-only;
configuration mutations require an explicitly configured admin token. Restart
for operational settings; persona documents and appearance support reload.
