# Runtime observations

Plugins may provide bounded, read-only current state through `BoundPlugin.state_providers`.
This is separate from persistent memory, prior tool results, and active modes. State is
not authorization to act. The contract lives in `anima.core.observations` and has no
dependency on a model vendor, transport SDK, or concrete plugin.

```python
from anima.core.observations import RuntimeStateProvider

provider = RuntimeStateProvider(
    plugin="example",
    fields=("available", "active"),
    observe=lambda source: {"available": True, "active": service.is_active()},
    instructions="Normal replies are delivered automatically; no action tool is needed.",
)
# Pass state_providers=(provider,) when constructing BoundPlugin.
```

Use one provider per plugin, with the manifest's owner name. Callbacks must be fast,
synchronous local reads, without network access or side effects. Reuse the same
underlying state as active modes rather than maintaining another state machine.
Only declared scalar fields are accepted (up to 16 fields, 256 characters per string,
1,800 characters of state JSON). Automatic-behavior instructions are trusted static
plugin declarations, limited to 600 characters. Dynamic strings are quoted JSON data,
not instructions. Do not include credentials, private memory, or arbitrary log content.

The sandbox-local registry rejects cross-sandbox source events. Snapshots include the
provider, sandbox, observation timestamp, version and local-runtime basis. Exceptions,
undeclared fields and oversized/invalid values become `status=unknown`; they are never
reported as inactive. Only the exception type is exposed. Static behavior instructions
remain available on failure. A `runtime.observed` trace records each bounded snapshot.

`Context.runtime_observations` is an optional provider-neutral callable returning owned
`(plugin, text)` parts. A conversation backend must invoke it before **each** model
request, append the fresh parts outside memory compaction, and never reuse old snapshots.
The default OpenAI responder does this on all tool rounds and attributes the text to
its plugin in context usage measurements. Other backends can consume the same callable;
decision classifiers and self-time do not automatically consume conversation observations.
This adds no model calls and no tools. Complete model prompts are not logged.

An event's fields describe receipt-time facts, not guaranteed current remote state.
Name such fields accordingly. A successful tool result and the next snapshot refer to
different times. Natural completion, external disconnection and other changes must be
read from the owning service on subsequent calls. Observations do not replace tool
validation, sandbox checks, idempotency, or explicit user intent.
