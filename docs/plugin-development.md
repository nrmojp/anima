# Plugin development

For plugins in another application package, configure `ANIMA_PLUGIN_NAMESPACE`
as described in [using Anima as a library](library.md). Do not copy or shadow the
framework's `anima` package.

If you are starting a new bot, first follow [Build your own bot](build-your-bot.md).
Persona-only customization needs configuration, not a Plugin. This guide covers
capabilities that add behavior beyond the base conversation and memory system.

[Framework composition](framework-composition.md) is the authoritative reference
for shared-code ownership, deployment settings, and extension hook contracts.

A Capability Plugin is trusted, in-tree code added after forking or copying this
repository. `PluginLoader` automatically discovers immediate child packages of
`anima.plugins`; it does not discover installed distributions or hot-load code.

Use `src/anima/plugins/<plugin_id>/` for every plugin. The built-in reference lives at
`src/anima/plugins/echo/`. Use a stable lowercase plugin ID and keep plugin-specific
SDK integrations and helper code inside this directory. Keep `__init__.py` as a small public-API facade; put behavior in
focused modules such as `plugin.py`, `config.py`, or `ports.py`.

`plugin.py` is the entry point and must expose one `PLUGIN` definition. A directory
without `plugin.py` is treated as supporting code rather than an enabled Plugin. The
directory name and `PLUGIN.manifest.name` must match.

## Build a plugin

1. Declare a `PluginManifest` with a semantic version, provided capability IDs, required
   capability IDs, and any permissions.
2. Declare non-secret `ConfigField` values when configuration is required.
3. Implement `create(services, configuration)` on the definition. `services` is a
   `SandboxServices` bundle bound to exactly one `SandboxKey`.
4. Implement `start`, `stop`, and `snapshot` on the instance.
5. Expose zero or more `ToolProvider` and `CommandProvider` objects. Implement
   `ResponseContributionProvider` on the instance only when final-answer selection is
   more appropriate than an additional tool round.
6. Assign the definition to `PLUGIN` in `plugin.py`. `PluginLoader` registers it without
   a bootstrap edit.
7. Add non-secret host settings to typed configuration and documented defaults. Add
   required credential names to `.env.example` with empty values; never put credentials
   in plugin configuration or source code.
8. Add unit tests for manifest validation, disabled behavior, lifecycle rollback, and
   every command or tool branch.

The built-in `anima.plugins.echo` module and `tests/test_generic_runtime_routes.py` are
the smallest
SDK-free examples.

## Contracts and boundaries

- A Manifest describes identity, dependency, permission, and configuration metadata.
- A `ToolProvider` exposes model-callable functions with closed JSON object schemas.
- A `CommandProvider` exposes explicit interface commands and their permissions.
- A `ResponseContributionProvider` adds narrowly named fields to the model's final
  structured response and consumes those values as ordinary `ToolResult` metadata.
- An `AvailabilityProvider` evaluates request-local support without changing whether
  the Plugin is installed or enabled for the Sandbox.
- Lifecycle methods acquire and release background tasks, files, and connections.
- A Port is a narrow host-owned protocol for a capability that needs infrastructure.
- An Adapter implements a Port using Discord, a local process, or another SDK/runtime.

Audio Plugins receive the host-owned `AudioOutput` service. Submit WAV artifacts or a
48 kHz stereo signed-16-bit `PCMSource` with an `AudioLane` rather than calling Discord
directly. `speech`, `music`, and `effect`
routes already share one mixer; see [Shared audio routes](audio.md). A new audio Plugin
normally supplies synthesis, selection, or media decoding while the host retains VC
connection and mixing responsibility.

Stateful Plugins request `plugin_storage` as a `PluginStorage`; they must not request or
derive the host state root. Use JSON state for small durable records, artifact paths for
user-facing output, and temporary paths for disposable intermediate files. A Plugin may
also declare `DashboardPanelSpec` values in its manifest. Panels are data-only requests
for trusted renderers, not arbitrary HTML. See [Base capability routes](base-routes.md).
Set each panel's `group` to the relevant operations area (`status`, `memory`,
`configuration`, or `diagnostics`) and use `eyebrow` for a short label. Both are
declarative metadata; the dashboard escapes the text before rendering it.

Plugins may depend on `core`, `capabilities`, the Python standard library, and code below
their own directory. Plugin-specific Ports, Adapters, configuration, assets, and helper
code all stay in that directory. They must not import another Plugin or a shared
`anima.adapters` implementation. Third-party source copied into the Plugin must also stay
inside its directory and include its required license and notices. Do not add
plugin-specific branches to Core, Responder/model adapters, or interface adapters.

## Sandbox and dependencies

The composition root creates one plugin runtime per sandbox and supplies only services
for that `SandboxKey`. Do not pass an entire Discord client, global state store, or
unrestricted service locator to a plugin. Store state beneath a sandbox-specific root.
This is contractual isolation for trusted code, not an operating-system security
sandbox.

`provides` and `requires` contain capability IDs rather than Python module names. The
catalog validates unique providers, missing dependencies, and cycles before any instance
starts. Enabling a dependent plugin without its provider is rejected.

## Tool and command safety

Function tools use closed JSON object schemas. The registry validates arguments, binds
execution to a `SandboxKey`, and creates a deterministic invocation ID. Providers should
use that ID to make side effects idempotent. Commands are validated for permissions,
namespace restrictions, and arguments before provider code runs.

When a configured capability depends on request-local state such as a voice channel,
quota, or transient service health, return `AvailabilityStatus`. Unavailable tools are
not sent to the model; unavailable commands return a private, safe reason. Do not put
credentials, upstream response bodies, or diagnostic details in that reason.

Discord publishes `CommandSpec` paths automatically at startup. A one-segment path is a
top-level slash command; a two-segment path becomes a Discord command group and
subcommand. The same root cannot be both forms. Keep names stable because changing a
path replaces the command users see after the next sync.

Return durable side-effect summaries as `ActionRecord` values and retrieved entities as
`ContextReference` values in `ToolResult`. The model adapter carries these records across
tool rounds into final response metadata; do not encode them into reply text or add
Plugin-specific result parsing to the responder.

Use a response contribution for intent that belongs to the final answer itself—for
example, selecting a reaction or one referenced catalog item while composing the reply.
Use a function tool for explicit multi-step work, model-visible intermediate results,
or operations that may require another reasoning round. Contributions support scalar
JSON types and arrays, reserve `reply` for Core, and must have unique property names.

To deliver generated files, create them beneath `PluginStorage.artifact_path` and return
their absolute paths in `ToolResult.attachments`. The shared result route carries them to
the active interface adapter. Do not construct Discord files or upload objects inside a
Plugin.

For `resource_read`, observation does not imply delivery: use `attach_to_reply=true`
only when the user should receive the image. Background system Events should preserve
the requester's `MentionedPerson` as `response_target` and its bot flag so memory and
forms of address remain bound to the intended recipient.

Use `anima.core.plugin_logs.plugin_log` for scoped diagnostic details:

```python
from anima.core.plugin_logs import plugin_log

plugin_log("my_plugin", "request", sandbox_key=str(context.sandbox_key),
           event_id=context.source.id, model="local", parameters={"size": 256})
```

Common tool execution records inputs/results/failures automatically. Never pass
credentials or image binaries. Redaction is defense in depth, not permission to log
secrets. Logs are sensitive runtime data under the owning sandbox's
`runtime/plugins/<plugin>/<UTC-date>.jsonl` and use operational retention. Trusted
operators can inspect the latest entries in Dashboard diagnostics. Tool providers can
optionally return `(owner, text)` fragments from `context_parts(context)` for accurate
per-plugin instruction accounting; registry attribution covers declared tools.

Background capabilities request `event_ingress` to run a stored or scheduled Event
through the Persona Agent, then use `message_output` for proactive delivery. Both
services are already bound to the current Sandbox. Destination IDs must originate from
trusted interface context; never accept a Sandbox or destination override from the
model.

`EventIngress.publish` returns the actor's `ProcessOutcome` (or `ResponseDraft` when
bound to a draft-only handler). Internal events use `author_id="self"`, the trusted
sandbox key, and the trusted channel ID. Their normal actor response is delivered
without a Discord source message; do not send it again through `message_output`.

Configuration precedence is manifest defaults, `config/plugins/<id>.json`, declared
environment variables, then Dashboard values in `config/config.json`. Dashboard keys
are namespaced (`voice.speed`); the plugin receives its original key (`speed`). Declare
fields in the manifest rather than editing bootstrap. Secrets remain environment-only.

## Review checklist

- The manifest ID, provided capabilities, tools, and command paths are unique.
- Inputs use closed schemas; side effects are idempotent by invocation ID.
- State is sandbox-scoped and never shared through implicit globals.
- Permissions are declared and checked before privileged operations.
- External SDK types remain in a Plugin-local Adapter behind a narrow Port.
- `start` acquires resources once; `stop` cancels tasks and closes resources.
- Snapshot data contains operational status but no secrets or conversation text.
- Logs and metrics identify the plugin and sandbox without exposing private content.
- Normal, boundary, invalid, disabled, rollback, and shutdown paths have unit tests.
- SDK-free classes reach 100% line coverage.
- No plugin-specific branch is added to Core, a responder, or an interface adapter.

Run the full test suite, coverage report, public-tree scan, and `git diff --check` before
submitting the plugin. The architecture test enforces the import direction.

## Bundled skills

Plugins may ship reviewed task instructions at `skills/<name>/SKILL.md` beside
`plugin.py`. PluginLoader discovers them automatically, and the host exposes only
enabled, available providers. Include these documents in wheel package data.
Skills use common resource reads and existing tools; they are not executable entry
points. See [skills.md](skills.md) for metadata, requirements, safety and examples.
