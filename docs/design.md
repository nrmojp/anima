# Framework design

Skills provide reviewed task procedures over existing capabilities, not new permissions
or a second execution engine. See [skills.md](skills.md) for the implementation contract.

[Conversation and decision backends](model-backends.md) defines the two
model boundaries. Both remain independently replaceable across hosted and local
providers; addressing and engagement are decision uses, not provider-specific ports.

[Framework composition](framework-composition.md) is the authoritative reference
for shared-code ownership, deployment settings, and extension hook contracts.

Anima is a persona-neutral foundation. The deployed persona is configuration, not a
framework identity. Optional feature plugins add behavior without changing the core.

## Requirements

- Serialize reasoning and durable changes independently for every sandbox.
- Preserve persona, rules, learned habits, mood, unfinished intentions, and memory.
- Treat incoming history and retrieved references as untrusted data.
- Use one bounded agentic mechanism for conversations and internal self time.
- Keep external SDK types outside the core and capability contracts.
- Support synchronous and asynchronous plugins through common contracts.
- Offer primitive resource operations whose targets grow through plugin declarations.
- Keep storage, audio, events, output, and lifecycle as shared host-owned routes.
- Make behavior observable, diagnosable, and recoverable without live external services.

## Conversation

Contextual requests are classified conservatively, independently of proactive speech.
Name mentions alone do not trigger replies. A continuation needs the same participant,
an agent response within two minutes, and no intervening other participant; explicit
DMs, mentions and replies bypass classification. Ambiguous requests fail closed.
Internal response links and trusted target metadata preserve the intended participant
through queue delays and tool rounds. See [conversation-addressing.md](conversation-addressing.md).

Person memories identify people by sandbox-scoped IDs, not display names. The dashboard
shows document headings, entry counts and IDs. Sleep consolidates repeated experiences
without discarding important facts or mixing provenance. See [person-memory.md](person-memory.md).

Images are read only when needed via primitive resources, not injected into every
request. Observing an image and sending it to the user are independent decisions.

Explicit mentions, DMs, and replies addressed to the agent trigger responses.
Referenced messages are included with author identity and sandbox scope. Deleted
messages are tombstoned and excluded from future context. Tool actions and retrieved
references attach to assistant events so follow-up questions do not require repeated
search or speculative claims of success. Discord typing status covers response work;
ordinary messages do not automatically mention the original author.

## Internal state

Each sandbox has cursor, mood, habitus, daily digest, unfinished intentions, memory,
event logs, deletion markers, inventory, temporary artifacts, jobs, and runtime status.
Nap, sleep, reflection, and self time update only the owning sandbox. Sleep synchronizes
memory retrieval after durable memory processing. Automatic sleep does not require a
new conversation message. Maintenance commands allow controlled manual experiments.

Self time has finite iterations, interval and daily limits, recent-session context,
and evidence-based completion. It may reconcile unfinished intentions with verified
work. Summaries and committed state are observable; raw hidden reasoning is not required.
Exploration can deepen an existing interest or broaden into a connected new one;
direction and discoveries remain available to subsequent sessions.

## Capabilities

Trusted in-tree plugins declare tools, commands, resources, response fields, availability,
configuration, lifecycle, and dashboard panels. Empty declarations are valid. The sample
echo plugin demonstrates contracts; local voice demonstrates shared audio and storage.
Music selection, drawing, reminders, media catalogues, and custom reactions are not
part of the base distribution.

## Operations

The bot and dashboard start and stop manually. The dashboard defaults to loopback and
read-only behavior; a long admin token enables configuration writes. Secrets stay in
environment variables. Content reload affects active sandboxes; operational settings
requiring new connections or services take effect after restart.

Offline backup and restore require the same process lock as the bot. Restore must
validate the complete archive before publishing a new sandbox and never overwrite an
existing one. Integrity faults are visible and preserved, not silently reset.

## Verification

Every behavior change needs normal, boundary, and invalid-input tests. Full tests,
coverage, architecture checks, public-content scanning, and whitespace validation are
required before commit. External-service tests are separate and require cost/data review.
See [architecture](architecture.md), [base routes](base-routes.md), and
[backup and recovery](backup.md) for concrete contracts and operational details.
