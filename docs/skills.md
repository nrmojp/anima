# Skills

Skills teach an agent how to combine existing tools. They do not implement new
capabilities, alter persona, or grant permissions. The implementation is neutral
to the conversation and decision API providers and reuses the same AgenticLoop.

## Packaging and discovery

Place reviewed skills in `<ANIMA_ROOT>/skills/<name>/SKILL.md`, or alongside a
plugin entry point at `<plugin-package>/skills/<name>/SKILL.md`. PluginLoader
discovers the latter without extra imports. Only enabled, available plugins
contribute skills. Package these files as package data when distributing a wheel.
Application skills use IDs `core:<name>`; plugin skills use `<plugin>:<name>`.
Duplicate IDs are diagnosed, not silently overridden. The directory must match
the frontmatter name. No downloads or user-home scanning occur.

```yaml
---
name: research-notes
description: Research a question, compare sources and prepare a short saved note.
metadata:
  anima.requires: "web_search"
  anima.contexts: "conversation self_time"
---
Read the available evidence, search when needed, distinguish facts from inference,
and write a note to core.inventory only when requested or permitted by the run.
Read references/checklist.md for the reporting checklist when needed.
```

`name` and `description` follow the Agent Skills format. Standard optional fields
can be included. Anima's extension metadata values are strings: `anima.requires`
lists required provided capability names; `anima.contexts` lists `conversation`
and/or `self_time` (default: conversation). `allowed-tools` is not an authority
grant: the host's existing tool allowlist and permission checks remain effective.
Unavailable requirements remove a skill from the model catalog and reject reads.

## Progressive disclosure and execution

The model sees a small catalog of IDs and descriptions, not all skill bodies.
Read a selected skill with existing `resource_read`, collection `core.skills`,
resource ID `core:research-notes`, `attach_to_reply: false`. Read referenced text
using `core:research-notes/references/checklist.md`. `resource_list` enumerates the
visible catalog. There is no skill-specific action tool. The resulting text is a
normal observation in the current tool loop, so subsequent calls can use existing
search, inventory and plugin tools. The loop's round/time limits still apply.

Skill bodies are not copied into durable conversation context, persona or memory.
The catalog requests avoiding rereads within a run; this is a model instruction,
not a cache that returns an empty body on repeated calls. Every read returns the
full text, so retries and context rebuilds remain correct. Each new run can select
and read the skill again. Cross-tool workflows stay agentic, not fixed scripts.
Offloading is automatic at the end of each response run or self-time decision loop:
request-local tool transcripts are discarded, while only operation metadata remains.
There is no explicit mid-run unload operation; instructions read in an active loop
remain in its transcript until completion. Tests verify that a skill body reaches
the next tool round, but does not enter a subsequent response run on the same responder.

## Safety, limits and administration

This first version supports administrator-reviewed text instructions and text
references only. It does not execute bundled scripts, expose binary assets,
install remote bundles, or let the model edit skills. Only `list/read` operations
are registered. Relative traversal, backslashes and symlinks are rejected. Each
UTF-8 file is bounded to 32 KiB; YAML aliases are rejected. References must be
under `references/`, at most four path segments, with .md/.txt/.json/.yaml/.yml
extensions. Discovery checks at most 256 immediate entries per root and accepts
at most 64 skills per sandbox composition; invalid entries are diagnosed. Roots
are trusted deployment inputs, not Discord-controlled paths. This is not OS
process isolation. Skill instructions cannot override the user's request or
existing host permission, quota and sandbox checks.

Use the existing configuration reload operation to rescan skills. Changes to
identity or filtering metadata require reload before reading; instruction body
and reference text are read fresh. Plugin activation changes retain the existing
restart requirements. Diagnostics, catalog and the last 50 successful reads are
written to sandbox-local `runtime/skills.json`. History is per process and reset
on composition. It records IDs, timestamps, context, event ID and character/byte
counts, never instruction text. `skill.read` telemetry follows existing logging
and retention. Dashboard shows this information under Model resources. Counts
are text sizes, not tokenizer-exact token estimates; existing context telemetry
accounts for catalog contributions under each providing owner.

Tests cover validation, pagination, dependency/context filtering, unsafe paths,
read-only enforcement, reload, Dashboard sandbox separation, and skill read →
inventory write → dummy Discord response through a provider-neutral AgenticLoop.
No external API or live Discord is required.

References: [Agent Skills specification](https://agentskills.io/specification),
[client integration](https://agentskills.io/client-implementation/adding-skills-support).
