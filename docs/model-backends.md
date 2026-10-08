# Conversation and decision backends

Status: implemented locally, awaiting publication and consumer pin update (2026-10-08).

## Configuration and extension entry points

The default remains Responses classification. To explicitly use Decisions:

```sh
ANIMA_DECISION_BACKEND=decisions
ANIMA_DECISION_MODEL=gpt-6-luna
ANIMA_DECISION_ADDRESS_THRESHOLD=0.9
ANIMA_DECISION_SPEECH_THRESHOLD=0.95
ANIMA_DECISION_REACTION_THRESHOLD=0.9
```

Thresholds are conservative initial settings, not calibrated accuracy guarantees.
Responses returns labels without fabricated probabilities. Its default label policy
does not use the Decisions thresholds. A custom backend chosen in Decisions mode
must supply probabilities or composition fails. Model options remain adapter-owned.

Optional comparison uses `ANIMA_DECISION_SHADOW_BACKEND=responses|decisions` and
`ANIMA_DECISION_SHADOW_DAILY_LIMIT` (default 10 per sandbox, UTC day). It is off by
default. Durable attempts are reserved before comparison; at most one comparison
per sandbox is outstanding. Shutdown cancels it; failures are diagnostic only.

Hosts can call `build_client(settings, conversation_factory=..., decision_factory=...)`
or supply the same keyword arguments to `build_sandbox`. The conversation factory
implements `ConversationServiceFactory`: responder, memory maintainer and self-time
services. Its responder accepts the host's tool/response registries and persona reload.
The decision factory takes `(settings, client)` and returns `DecisionBackend`; it can
ignore the default SDK client and use its own configured hosted/local model. These
factories are independent and never discovered from untrusted conversation content.
Auxiliary embedding/vector-store and plugin API services are separate integrations,
not implicitly changed by replacing the conversation or decision implementation.

`ConversationRequest` / `ConversationResult` provide neutral sessions for new
adapters. Existing OpenAI domain services use an adapter-owned request wrapper to
preserve provider-specific observations/structured response contributions. Both
paths use the existing `ThoughtBackend` / `AgenticLoop`, never a new loop. The
neutral Responses session validates the framework's supported schema subset locally;
unsupported schema constructs fail before calling the API. Third-party adapters
implement their own serialization and must preserve the same domain contracts.
The Decisions implementation uses the SDK's typed low-level POST, so an existing
2.x SDK can call it without a production dependency upgrade.

## Boundaries

Anima exposes two provider-neutral model contracts: conversation and decision.
Providers can be selected independently; OpenAI Responses, OpenAI Decisions,
other hosted APIs and local models are adapters, not core concepts.
Addressing and engagement are uses of the decision contract, not additional model
abstraction layers. Existing domain ports for response and maintenance remain.

```text
Actor / self time / maintenance -> ConversationBackend -> provider adapter
Address / engagement policy    -> DecisionBackend     -> provider adapter
ConversationBackend turn      -> ThoughtBackend      -> AgenticLoop
Decision evidence             -> typed result        -> local policy -> action
```

The core owns context, sandbox isolation, authorization, budgets, scheduling,
tool execution and result adoption. Adapters own wire serialization, credentials,
SDK interaction and validated conversion to neutral records. Bootstrap selects
implementations. Plugins receive contracts, never SDK clients for host reasoning.

## Conversation contract

`ConversationBackend.open(request)` creates a request-local `ConversationSession`
implementing the existing `ThoughtBackend.think` contract. `AgenticLoop` continues
to own iteration limits and tool execution; no second agent loop is introduced.
The session owns provider-specific continuation state and closes on cancellation
or completion. Sessions never share conversational state across sandboxes.

`ConversationRequest` carries instructions, ordered neutral messages, text/image
parts, tool declarations and an optional output schema. Messages preserve speaker,
response-source and reference identity. Tool calls and observations retain existing
neutral invocation IDs. A final result uses validated neutral content/structured
output plus usage, refusal and optional reasoning summaries. This does not expose
hidden reasoning. Existing responder, self-time and memory-maintainer services map
their domain requests/results to this shared conversation contract.

Conversation capabilities declare images, local tool calls, structured output,
streaming and optional reasoning summaries. Hosted search/retrieval is not assumed
portable: replace it with a neutral local tool where equivalent, or reject an
unsupported required capability. Never silently drop images, schemas or tools.
Schema validation occurs locally even when a provider offers constrained output.

## Decision contract

`DecisionBackend.evaluate(request) -> DecisionResult` is a single evaluation with
no tools, conversation continuation, state mutation or generated response text.
`DecisionRequest` contains shared neutral evidence and uniquely named questions:

- Predicate: a condition returning a boolean judgement and optional probability.
- Choice: a closed list of unique opaque candidate IDs with descriptions.
- Score: explicitly ordered labelled levels, using zero-based indices.

Results match questions by name, not array position. Each answer has
`answered`, `refused` or `unavailable` status and type-specific value. Probability
distributions and provider confidence are optional and distinct; their semantics
are not assumed interchangeable or empirically calibrated. Do not invent 0/1
probabilities for an adapter that only returns labels. Preserve optional token
counts, duration, actual model and provider correlation IDs as diagnostics.
Transport failures use neutral error categories (timeout, rate limit,
authentication, unsupported capability, invalid response, provider failure).
Cancellation propagates. Refusal is a normal answer state, not a false judgement.

Validate names, answer types, candidate membership, finite numeric ranges,
distribution coverage and sums within a documented rounding tolerance. Reject
missing/duplicate answers and malformed results. Independent questions may share
a request; dependent decisions must use another request or joint candidates.

Capabilities declare supported question types, probability signals, image support
and candidate limits. Predicate boolean-only adapters support label adoption but
not probability-threshold adoption. Score adapters returning only an ordinal label
must not claim to provide an expected score distribution.

## Domain mapping and adoption

Addressing builds evidence from the bounded current channel context, preserving
author and reply identities. A predicate tests whether a response is clearly
expected. Explicit addressed events retain their classification bypass.

Engagement creates a candidate map: none, speak(target), react(target, face).
Only allowed actions, current message IDs and available faces are included.
Opaque IDs map back to domain values without splitting user-controlled strings.
Joint candidates avoid inconsistent independently selected action/target/face.
All-disabled requests resolve locally to none. Reject oversized candidate sets
before sending; do not silently truncate or approximate a global choice by chunks.
An explicitly selected compatible alternate backend may be configured, but there
is no automatic provider fallback in the first implementation.

`DecisionPolicy` is a pure core component. It adopts boolean/label results or
requires configured probability thresholds, optionally a top-two margin.
Speech and reaction thresholds are separate. Missing required signals fail closed;
backend/policy incompatibility is rejected at startup. Low-confidence, refused,
unavailable or invalid results never initiate contextual or autonomous speech.
Keep no-action, rejection and API failure distinguishable in telemetry.
Recheck activity permissions and target eligibility immediately before execution.

## Configuration and observability

Bootstrap uses separate provider/model configurations for conversation and decision.
An optional per-purpose decision override (address/engagement) selects the same
contract, not a new interface. Provider options and secrets stay in adapter-owned
configuration; unknown options and unavailable capabilities fail startup.
Existing response-model configuration remains compatible during migration and
retains the existing Responses path by default. Selecting Decisions must be explicit.

Use the shared call limiter and durable attempt budgets for every actual request.
Retry only bounded transient read-only evaluations within the total deadline;
do not retry refusals, invalid configuration or authentication failures. Preserve
the conservative addressing timeout. No silent cross-provider retries.
Log purpose, backend, model, selection, signals, adoption reason, duration and
usage without evidence bodies or secrets. API price calculation belongs to a
provider-aware usage service, not a universal token rate. Missing usage is unknown.

An opt-in shadow mode calls a secondary backend on the same bounded evidence and
records disagreement. Only the primary result can cause actions. Secondary timeouts
cannot delay primary actions, and secondary calls consume explicit rate/cost budgets.

## Proposed modules and rollout

- `core/model_contracts.py`: neutral conversation and decision records/Protocols.
- `core/decision_policy.py`: validation-independent adoption policy.
- `core/decision_requests.py`: domain evidence and engagement candidate mapping.
- `adapters/openai/conversation.py`: existing Responses backend extraction.
- `adapters/openai/decisions.py`: Decisions request/result conversion.
- `adapters/openai/response_decisions.py`: JSON classification without probability claims.
- `core/decision_shadow.py`: bounded nonblocking comparison lifecycle.
- `bootstrap/model_backends.py`: independent host factories and scoped composition.
- Bootstrap: injected factories, settings and capability checks.

First add contracts and migrate existing Responses classification retaining domain
results and fail-closed behavior. Engagement now uses joint candidate IDs rather than
the old independently generated fields; wording/model outputs need rollout evaluation.
Then extract the conversation implementation behind the shared contract,
preserving `AgenticLoop`, domain results, tool metadata and plugin contributions.
Add Decisions and opt-in configuration next; address adoption precedes engagement.
No consumer source copies or persona-specific core defaults are introduced.

Acceptance requires pure-policy/candidate classes at 100% line coverage, adapter
fixtures for refusal and malformed answers, and both providers through the same
contract tests. Cover missing probability, capability mismatch, candidate limits,
cancellation, retries, timeouts, sandbox identity and denied actions. Run mock
interface regressions for conversation tools, contextual replies and reactions.
Live evaluation is separate, synthetic first, with disclosed payload and cost;
small synthetic samples do not establish production accuracy or calibrated thresholds.
