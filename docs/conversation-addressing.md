# Response targets and provenance in multi-person conversations

Status: implemented and unit-tested (2026-10-06). Model quality in multi-person
conversations requires separate live validation.

## Purpose and problem

Preserve the flow of the whole channel while distinguishing who spoke to whom
and which message the current response addresses. Do not split the conversation
into independent per-person sessions or keyword-based topics. This change does
not add model calls.

The regression scenario reproduces a bot replying "Drawing them is fun for me,
too" to one person thanking another for their icons. Besides deciding whether
to respond, the bot confused who created the work or made the statement.
Before this change, human history entries had display names but no IDs, and
ordinary bot posts did not retain their response source. Messages from other
people arriving while an event waited in the queue could also appear as newer
history entries than the event being processed. These are attribution risks,
not a complete determination of every cause of the observed behavior.

## 1. Separate external replies from internal response relationships

`Event` includes an optional `response_to: str | None` field.

| Field | Meaning |
| --- | --- |
| `reply_to` | Reply reference in an external interface such as Discord; existing behavior is preserved |
| `response_to` | Source event ID for the bot's generated response, retained even for ordinary posts |
| `response_target` | Person to address for a system notification or similar event; not a message reference |

For ordinary responses, `commit_response` saves `response_to=source.id`; the
model does not choose the ID. Proactive posts use `response_to=None` and do not
pretend to reply to the triggering message. For asynchronous completion
notifications, the response source is the completion event and the person comes
from the existing `response_target`. Preserve the original request relationship
through existing job provenance; do not confuse the completion event ID with
the original request ID. Reactions still target the existing `reply_to`; this
change does not add a new response relationship to them.

Missing fields load as `None`. New values use the same safety validation as
event IDs. Do not rewrite existing logs. For older bot posts with replies, use
`reply_to` to identify the relationship. Older posts without replies have an
unknown response source; do not infer or backfill it from adjacent messages.
Relationships to deleted events may retain IDs, but must not quote or restore
deleted content.

## 2. Pin the current response target

`ContextBuilder` builds trusted metadata from `source_event`, separately from
message content:

- Response kind: ordinary response, proactive post, or system notification.
- Target event ID, conversation ID, and target person ID and display name.
- For system notifications, distinguish the notification source from the person
  to address.
- Shared rule: respond to the specified event rather than the latest post. Do
  not treat words addressed to someone else or quoted text as the author's own
  request.

Use the target event's content already present in history; do not duplicate long
content in instructions. Treat user content and display names as escaped data,
not text concatenated into trusted instructions. Preserve target metadata during
context compaction across tool round trips in the Responses adapter.
This is a core responsibility, independent of OpenAI-specific model decisions
or Discord mention presentation.

## 3. History metadata and bounded selection

Attach event ID, author ID, display name, timestamp, external reply reference,
and internal response source to every human and bot history entry. Preserve
`author_id=self` as the bot's existing internal identifier. IDs distinguish
people with the same display name or changed names. Identify whose statement is
being quoted; do not attribute it to the quoting person's opinions or creations.

Select history for an ordinary response in this order:

1. Always include the source, even if it would fall outside the existing
   count-based window.
2. Prioritize the source's reply/response references within the same sandbox and
   conversation. Follow at most three ancestor levels and stop on cycles.
3. Fill remaining slots with recent surrounding messages, then restore
   chronological presentation. Preserve the existing `AttentionPolicy` window
   limit.
4. Mark entries received after the source as background messages received while
   the response was pending. Do not replace the target or redirect the response
   to their authors.

If the source and its ancestors exceed the window, prefer the source and nearer
ancestors. Use journal order to break timestamp ties. If the source is absent
from the log, insert the received source as the target and treat entries with
an undetermined relative order as background.
Resolve references outside the window by ID from the same storage area's
journal, without fetching externally. Explicitly identify unknown, deleted, or
cross-boundary references rather than guessing content.
Proactive posts read the latest channel-wide context without creating an
obligation to reply to a particular person. Self Time is outside this
conversation-response selection policy and retains its internal activity
context.

## 4. Agency and response eligibility

Shared rules distinguish "someone else created or saved this" from "I performed
this action." Claims of the bot's own creation or storage must be supported by
its execution records or provenance. Being able to join a discussion does not
make the discussed work the bot's own.

Explicit mentions and replies to the bot retain their direct-response path.
Other eligible human messages are classified without exact-name, previous-speaker
or reply-to-human prefilters. The bounded decision service evaluates independent
`mentions_self` and `expects_reply` questions together. Only the latter controls
response adoption. Name variants are recognized by the model; mere name mentions
do not trigger replies. Conservative multi-person criteria live in the decision
instructions, using the same history metadata and internal response sources.
When older logs have an unknown response source, an inference may inform the
decision but must not be saved as a confirmed relationship. For explicit bot
requests attached to replies to humans, distinguish the requester from the
person whose message is quoted.

## 5. Observability and validation

Structured context-build events record source ID, target person ID, response
kind, selected entry count, later background entry count, and unresolved
reference count. Do not duplicate message bodies or person memory in ordinary
traces. Response-save events record the sent ID and `response_to`, making the
relationship traceable independently of external reply presentation.
Event names are `conversation.context.built` and `conversation.response.saved`.

Regression tests verify the following without external APIs:

- B and C posting while a response to A is pending does not change A's target
  message or identity.
- Alternating messages from A and B retain response sources for ordinary bot
  posts across persistence and reload.
- Same-name users, quotes, and requests mentioning the bot in replies to humans
  remain distinguishable.
- Sources outside the window, missing or deleted references, cycles, and
  cross-sandbox or cross-conversation references are handled safely.
- Proactive posts are not responses to specific messages, and asynchronous
  notifications retain the requester.
- Targets survive tool round trips and instruction compaction, and old logs
  remain readable.
- The observed thanks-for-someone-else's-icons scenario is reproduced as
  eligibility input. Mocks do not guarantee generated-response quality.

Implementation order: backward-compatible event extension, history selection
and metadata, compaction and eligibility integration, then observability and
regression tests. Aim for 100% coverage of changed classes without external
dependencies, and check the complete unit suite and overall coverage.
Validate model attribution separately in explicitly authorized multi-person
Discord tests.
