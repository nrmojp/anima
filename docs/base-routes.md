# Base capability routes

Anima implements shared routes before adding feature Plugins. A route owns validation,
sandbox binding, dispatch, and lifecycle; a Plugin supplies only feature behavior.

| Route | Producer | Consumer | Boundary |
| --- | --- | --- | --- |
| Events | interface adapter | persona actor | `Event` and `SandboxKey` |
| Background events | scheduler/Plugin | persona actor | `EventIngress` |
| Responses and artifacts | persona actor | interface adapter | `ResponseDraft` / `MessageSender` |
| Proactive output | scheduler/Plugin | interface adapter | `MessageOutput` |
| Function tools | model adapter | Plugin provider | `ToolRegistry` |
| Native tools | model provider | Plugin consumer | `NativeToolEvent` |
| Response contributions | model final response | Plugin consumer | `ResponseContributionRegistry` |
| Commands | interface adapter | Plugin provider | `CommandRegistry` |
| Memory recall | persona core | retrieval adapter | `MemoryRetriever` |
| Audio | Plugin | voice transport | `AudioOutput` and `AudioMixerPort` |
| Plugin state | Plugin | local storage adapter | `PluginStorage` |
| Dashboard | Plugin manifest | operations UI | `DashboardPanelSpec` |
| Availability | Plugin provider | tool/command registry | `AvailabilityStatus` |
| Lifecycle | composition root | Plugin instance | `RuntimeHost` |

Feature identifiers are data in a manifest or registry. Core registries and adapters do
not branch on `music`, `drawing`, or another Plugin name.

An enabled Plugin may implement `AvailabilityProvider` to evaluate a named tool or
command for the current request. `available` publishes or runs it; `disabled`,
`unsupported`, and `unavailable` suppress tools and retain a short safe reason.
Commands remain visible in the interface but reject privately with that reason. This is
separate from process-level Plugin enablement and never treats model selection as user
authorization.

`SandboxKey` contains an adapter-defined `namespace` and an opaque safe `identifier`.
Discord persists `guild` and `dm`, with `discord_guild` / `discord_dm` namespace aliases;
another interface may use `matrix_room`,
`tenant`, or its own namespace without changing Core. Commands restrict themselves with
`required_namespace` rather than a Discord-only boolean.

`DiscordCommandHandler` derives Sandbox, actor, and permissions from a live Interaction,
dispatches through that Sandbox's `CommandRegistry`, and maps private results to
ephemeral replies. `DiscordCommandRegistrar` converts each enabled Plugin's one- or
two-segment path and typed parameters into discord.py application commands, then syncs
the command tree once after connection. Plugin code receives no Discord Interaction
object and never imports discord.py.

## Memory and response metadata routes

The reference composition supplies sandbox-bound `LocalMemoryRetriever` and
`MemoryVectorStore` adapters. The model may search longer-term memory through the
primitive resource route and hosted file search. Neither a Plugin nor the model can
select another Sandbox or filesystem path. A fork can replace the local fallback behind
the same `MemoryRetriever` Port.

Function and model-native Tool results may produce `ActionRecord` and
`ContextReference` values. The OpenAI Adapter accumulates them across tool rounds in a
transport-neutral `ResponseDraft`; the Persona Actor commits that draft's
metadata. Hosts can therefore persist what happened without parsing reply prose.

`ToolResult` may also carry up to 16 absolute artifact paths created through
`PluginStorage`. Function and native Tool attachments follow the same accumulation path
into `ResponseDraft`; the Discord Adapter alone converts them to SDK
file objects. Core and Plugin code never import Discord attachment types.

A Plugin can also declare named `ResponseContribution` fields when an action must be
selected as part of the model's final answer rather than in an extra tool round. The
OpenAI adapter combines all fields into one strict structured-output schema, keeps the
human reply in the reserved `reply` field, and routes each contributed value back only
to its owner. The consumer returns an ordinary `ToolResult`, so actions, references,
and artifacts follow the same transport-neutral metadata route as tools. Property names
are globally unique within a Sandbox; Plugins do not replace the whole response schema.

## Background event and proactive output routes

Each Sandbox receives a `SandboxEventMailbox` exposed as `EventIngress`. It rejects
events for every other Sandbox and serializes publications. `PersonaActor` owns a
single-writer queue, so live interface events and background events cannot mutate context
concurrently. This gives Reminder and Scheduler Plugins the same response path without
manufacturing Discord messages.

For delivery without an incoming message, Plugins use the sandbox-bound `MessageOutput`
with an opaque destination ID previously obtained from a trusted Event. The Discord
Adapter resolves that destination and converts generic attachments. A Plugin cannot
select a different Sandbox or access the Discord client.

## Storage route

The composition root supplies a `PluginStorageFactory` bound to a transport-neutral
`SandboxKey(namespace, identifier)`.
`PluginCatalog` narrows it again for each Plugin and exposes only `plugin_storage` to the
instance. Plugins cannot choose another sandbox or plugin root.

- JSON state is stored beneath
  `<sandbox-root>/runtime/plugins/<plugin>/`.
- durable user-facing artifacts are stored beneath
  `<sandbox-root>/world/<plugin>/`.
- temporary products are stored under that Plugin's runtime directory and can be
  cleared without touching durable state.
- names are single safe path components; absolute paths, traversal, and symbolic-link
  targets are rejected.
- JSON writes use a same-directory temporary file and atomic replacement.

The bundled Voice Plugin exercises this route for generated WAV files. It no longer
receives the host's unrestricted state root.

## Dashboard contribution route

A Plugin may declare `DashboardPanelSpec` values in its manifest. The spec contains
only an ID, title, renderer ID, order, description, group, and eyebrow—never HTML or
executable code. `group` is one of `status`, `memory`, `configuration`, or `diagnostics`;
it defaults to `status`. The operations UI groups panels by this metadata, then sorts
by order and ID within a group.
The catalog rejects duplicate panel IDs across Plugins. Process and sandbox snapshots
carry the declarations to the Dashboard, which escapes all Plugin text before display.

## Service lifecycle route

Host services may implement `SandboxServiceLifecycle`. `SandboxRouter` / `RuntimeHost` start each unique
service before Plugin instances, stops Plugins before services in reverse order, and
rolls back already-started services when startup fails. The Discord audio connection and
background Event mailbox use this route, so disabling the last Plugin or stopping a
Sandbox cannot leave transport resources behind.
