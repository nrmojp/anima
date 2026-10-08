# Architecture

Provider-neutral conversation and decision boundaries and their host factory injection
are specified in [model-backends.md](model-backends.md). Default classification uses
Responses; Decisions and diagnostic shadow comparison require explicit configuration.

[Framework composition](framework-composition.md) is the authoritative reference
for shared-code ownership, deployment settings, and extension hook contracts.

## Recent runtime refinements

- `Event.response_to` persists the internal response source independently of external replies.
  Context selection pins the source and up to three reference ancestors; later messages
  remain background. Target metadata survives adapter tool-context compaction.
- Contextual addressing uses a bounded OpenAI classifier behind the shared call limiter,
  with a 15-second timeout and fail-closed telemetry. It is disabled in silent mode.
- Self-time invalid final decisions receive one record-only repair with tools disabled,
  retaining observations without replaying side effects. Invalid/repaired events log
  validation reasons and field lengths, not private response bodies.
- Context occupancy v2 excludes image references from text characters, separates image
  counts and payload bytes, and retains legacy wire size. Neither measure is API tokens.
- Dashboard memory metadata is extracted by its adapter; semantic consolidation belongs
  to sleep v7, while StateStore safety limits remain unchanged.
- Finder metadata is ignored during safe sandbox enumeration; sleep deadline reads run
  off the event loop without loading the complete event history.

Detailed contracts: [conversation-addressing.md](conversation-addressing.md),
[person-memory.md](person-memory.md), [dashboard-layout.md](dashboard-layout.md).
The dashboard retains the established blue/white design-system layout, accessible
focus and skip navigation, and responsive sidebar rather than a persona-specific theme.

## Layer boundaries

| Layer | Responsibility | Dependencies |
| --- | --- | --- |
| `core` | Actor, state, memory, context, resources, jobs, modes, self time | Standard library and neutral capability contracts |
| `capabilities` | Tools, commands, response contributions, configuration, plugin discovery | Core domain records and ports |
| `plugins` | Self-contained optional abilities | Core, contracts, own directory |
| `adapters` | Discord, OpenAI, audio, storage, dashboard, test interface | Core and contracts, external SDKs |
| `bootstrap` | Settings, service assembly, process lifecycle, operational CLI | All layers |

Core and capability contracts form the SDK-independent domain boundary. They must
never import concrete plugins, adapters, or bootstrap code. Plugins may not import
shared adapters or another plugin. Architecture tests enforce these restrictions.

## Runtime ownership

`SandboxRouter` owns one `PersonaActor` and one durable state tree per sandbox.
Each actor serializes incoming events and maintenance work through a bounded queue.
The router starts shared sandbox services, job management, self time, plugins, and
the actor. Startup failures roll back acquired resources. Shutdown drains or stops
the actor, stops plugins and background work, and releases services in reverse order.
`RuntimeHost` remains available as a smaller plugin-only host for custom compositions.

`SandboxKey(kind, id)` accepts safe adapter-defined namespaces. Discord uses canonical
`guild:<id>` and `dm:<id>` keys. Legacy `discord_guild`/`discord_dm` aliases normalize
to those keys. Guild state is under `state/sandboxes/guilds/<id>/`; DM state is under
`state/sandboxes/dms/<id>/`; other namespaces use their own directory. Unassigned
legacy state prevents startup rather than leaking into a different sandbox.

## Model and tool flow

### Runtime diagnostics and context refinements

Self time chooses `deepen` (advance an existing interest) or `broaden` (explore a
connected new interest). Each decision records its direction and discovery, including
no-action decisions, in durable history passed to the next session.

Conversation history includes attachment counts and unread markers, not image data.
The model lists `interface.current_attachments` and reads only needed current-turn
attachments through `resource_read`. Historical attachments are not current resources.
Reading an image always makes it available for model observation; `attach_to_reply`
defaults to false and must be explicitly true to send that image to the user.

System completion events can carry `response_target` and its bot flag. Speaker memory,
local retrieval and context identity use that target while retaining the system
origin of the event. Plugins should preserve the original requester for job notices.

Sleep scheduling checks only the cursor deadline in a worker thread. Maintenance
execution remains serialized by the actor. Batch event/job validation checks the
state tree once and validates each event's sandbox ownership without repeated tree
walks. Sandbox discovery ignores only Finder `.DS_Store` metadata; unsafe entries
and symlinks otherwise remain rejected.

Tool ownership attributes request character counts to plugins or core, including
schemas, instruction fragments and response contributions. `model.context.measured`
records counts only, not input bodies; these are not token counts. Dashboard shows
these shares and up to 100 recent input-token measurements by operation.

`plugin_log` writes scoped diagnostics to `runtime/plugins/<plugin>/<UTC-date>.jsonl`.
Common tool execution records inputs, outputs and failures. Bootstrap routes enabled
plugin telemetry to the same handler. Logs redact credentials and binary payloads,
bound strings, reject unsafe paths, use private permissions and apply operational
retention. Detailed inputs remain sensitive runtime data. Dashboard exposes only the
selected sandbox's latest 50 entries to trusted operators, with compact scrollable
panels and text-only rendering.

`ContextBuilder` assembles persona, rules, habitus, mood, unfinished intentions,
digest, speaker/channel memories, recent messages, referenced messages, and completed
actions. External text is reference data, never an instruction override.

`AgenticLoop` is provider-neutral. A thought backend translates provider responses
into tool calls; an executor validates and dispatches primitive tool operations.
`OpenAIThoughtBackend` and `OpenAIToolExecutor` implement the Responses API boundary.
Conversation and self-time calls share this loop and reserve a tool-free final round.

`ToolRegistry` prepares request-local schemas, checks source requirements and permissions,
validates arguments, and assigns deterministic invocation IDs. Native tools return
through their owning consumer. Actions, references, and attachments are accumulated
and persisted with the assistant event. `ResponseContributionRegistry` composes
plugin fields into the final strict schema without allowing replacement of core fields.

## Resource route

Plugins declare `ResourceRegistration` values, not additional CRUD function names.
`ResourceRegistry` enforces collection scope, operation permissions, and transfer policy.
Core supplies inventory, temporary artifacts, current attachments, memory, and open items.
The model uses common list/search/read/write/delete/transfer operations.

## Memory and maintenance

Nap compresses conversation into a daily digest. Sleep writes durable sandbox memory
and unfinished intentions, then synchronizes that sandbox's OpenAI Vector Store.
Reflection updates habitus without rewriting persona identity. Automatic sleep checks
run independently of incoming messages and back off after failure.

File search recalls older memories when a synchronized index exists. A sandbox-bound
local retriever provides fallback. Index failures do not prevent normal replies.
Memory audit is read-only and does not silently repair or discard memories.

## Self time

Self time is a bounded internal session controlled by activity mode, interval, daily
quota, busy state, and maintenance commands. It receives the current state plus recent
completed sessions and current-session decisions. Primitive resources are available;
extra action tools require explicit host permission. Web search is opt-in.
Committed notes and mood updates are persisted. The dashboard shows session summaries,
actions, tools, and provider-supplied reasoning summaries—not hidden reasoning traces.
Only self time may replace `open.md`, preserving active job promises.

## Shared extension routes

The host exposes sandbox-bound event ingress, message output, audio output, retrieval,
plugin storage, inventory, jobs, and background notification. Plugins do not receive a
Discord client or an unrestricted state root. Audio keeps the existing shared speech,
music, and effect mixer, volume controls, ducking, and local eSpeak NG sample.

`PluginLoader` discovers trusted `plugins/*/plugin.py` definitions deterministically.
Typed configuration and dashboard metadata come from manifests. The dashboard renders
only trusted renderer IDs and escapes contributed strings; it does not run plugin HTML.

## Operations and safety

Process locking prevents duplicate bot instances and protects offline snapshot work.
State transactions have integrity checksums. Damaged cursor/mood files are copied to
quarantine and startup refuses silent reset. Only invalid unterminated JSONL tails may
be repaired after preserving the original. Gzip archives are verified before source
removal; writes check free space. Backup/restore validates paths and hashes and refuses
overwrite of an existing sandbox.

The dashboard has per-sandbox status, readable memory, inventory, resources, jobs,
self time, token usage, error history, and live trace. Persona, rules, appearance,
branding, and non-secret settings are editable with an explicit admin token. Content
reload is separate from settings that require restart. The default listener is loopback.

No deployment persona, credentials, logs, media, or custom feature plugins are bundled.

## Library distribution

Anima is independently installable as a wheel. Applications may provide trusted
plugins in their own package via `ANIMA_PLUGIN_NAMESPACE`, without copying or
shadowing `anima`. See [library usage](library.md) for distribution and API boundaries.
