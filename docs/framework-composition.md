# Shared framework and deployment boundaries

Anima is the shared framework. Individual agents are assembled from plugins,
fixed resources, and non-secret deployment settings. Do not embed a specific
persona, music catalog, voice model, or drawing style in shared code.

## Ownership

| Owner | Scope |
| --- | --- |
| Shared Anima framework | `core/`, `capabilities/`, `adapters/`, `bootstrap/`, package markers |
| Plugin | Definitions, providers, concrete API integrations, factories, CLI, and startup helpers under `plugins/<id>/` |
| Deployment | `deployment.json`, persona, rules, appearance, drawing, dashboard, face images, songs, and voice models |
| Sandbox | Person memory, mood, habitus, inventory, jobs, modes, and runtime logs |

Shared code does not import concrete plugins. `PluginLoader` discovers each
`plugin.py` entry point and aggregates declared configuration and capability
facets. Conversation, local memory, long-term memory search, inventory, Self
Time, administrative commands, and the dashboard remain core features even
when optional plugins are disabled.

## Assembly

Bootstrap creates API adapters for conversation and memory, sandboxes, message
delivery, shared audio output, inventory, and `JobManager`. It passes
sandbox-bound `SandboxServices` to each plugin's `create()` method.
Advanced assembly uses `PluginAssembly` from `services.values["assembly"]`.
This is a startup dependency for trusted plugins, not a model-facing interface.
It contains no secret values; API clients are obtained through an explicit
factory. Actor and store access is also bound to the current sandbox.
Plugins are not security-isolated, so arbitrary third-party code must not be
installed automatically.

Dependencies registered with `publish()` must be unique. Obtain collaborating
services through `get()` rather than importing another plugin directly.
`after=("voice",)` specifies assembly order only if that plugin exists; use
`requires` for mandatory capability dependencies. Declare audio usage with
`uses_audio`, and activation defaults with `default_enabled` and `enabled_env`.
The plugin lifecycle owns service startup and shutdown. The host does not
instantiate concrete plugin implementation classes.

Shared Discord PCM output and the mixer provide speech, music, and effect
lanes. TTS, song selection, and playback implementations belong to plugins,
which feed audio into the shared transport. Feature-specific SDK integrations
also belong inside plugins. Plugins may reuse shared adapters only through
their public integration paths (currently the PCM mixer and command execution
helpers).

## Declarative extensions

- `PluginRuntime` aggregates `tool_providers`, `command_providers`, and
  `resource_registrations`.
- Add structured output through `response_providers`. The optional
  `observe_tool_result(result, context)` hook receives tool observations;
  `consume_response()` validates output, and `enrich_draft()` attaches response
  metadata. The shared OpenAI adapter does not special-case song IDs or drawing
  quotas.
- Declare primitives available during Self Time through
  `internal_tool_providers` and `internal_action_tools`. Change execution policy
  rather than adding separate content-editing tools for conversation and
  internal activity.
- `mode_providers`, `reload_configuration()`, `register_cli(subparsers)`,
  `doctor_checks(root, values, online, checks, online_check)`, and
  `run_application(app_main, **options)` are optional hooks implemented only by
  plugins that need them.
- Manifests provide dashboard panel names, descriptions, renderers, and groups.
  Renderers are safe shared components, not arbitrary JavaScript injection.

## Configuration and persona

Precedence is: non-secret defaults in `deployment.json`, then `.env` or process
environment, then settings saved by the dashboard. Never put tokens, API keys,
or secrets in `deployment.json`; supply them only through `.env` or the process
environment.
Generic defaults are resource root `config`, state root `../state`, dashboard
host `127.0.0.1`, and administrative command prefix `anima`. Configure names used
to address the persona with `ANIMA_PERSONA_NAMES`, and the command prefix with
`ANIMA_COMMAND_PREFIX`.

Plugin setting types, environment variables, and defaults come from the
manifest's `ConfigField` declarations. `<resource_root>/plugins/<id>.json`
provides per-plugin initial settings. Dashboard `config.json` uses namespaced
keys such as `voice.voice_queue_size`. Legacy flat keys are normalized only when
the setting name is unique; ambiguous keys and duplicate assignments are
rejected. Explicit `ANIMA_PLUGINS` values, including an empty string, override
declaration defaults. Web search permission and plugin selection are independent;
the tool is not exposed unless the plugin is selected.

Fixed persona/rules resources define conversational identity, appearance defines
visual traits, drawing defines style and signature, and dashboard defines
display branding. Set age or behavior such as "express anger cutely" in the
persona, not in shared prompts.

## Dependency updates and compatibility

Install Anima from a reviewed Git commit or a wheel, and keep application
plugins in a separate package. See [library usage](library.md) for dependency
integration. Develop shared changes in Anima, then update the consuming
application's pinned dependency after validation. Do not edit installed package
files or copy framework implementation into an application namespace.

The dependency's Git commit or published package version identifies the reviewed
framework revision. Validate upgrades with unit tests, coverage, package checks,
and deployment-specific integration tests. A second source-tree fingerprint or
vendored-copy synchronization step is not required.

For compatibility with existing logs, shared code retains music/research data
types in `Event` and `ResponseDraft`, legacy status fields, and readers for
historical events such as `drawing.saved`. These are compatibility data formats;
they do not instantiate or activate concrete plugins. Legacy modules directly
under the package root are compatibility aliases, not the implementation.
The legacy Aivis voice API belongs to the voice plugin; the public
`adapters.discord.voice` module handles generic WAV/PCM output.

Completion notifications are identified by events with internal author `self`
and a response target, not by plugin or tool names. Close open promises only
after committing the response. Ordinary bot posts are not treated as internal
completion notifications.
