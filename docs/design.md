# Timing and validation

Harbor launches Codex or OpenClaw without modifying their tool implementations.
The bench adapter supplies a local model endpoint, the pinned agent version and
native instrumentation: Codex OTLP tool-result events and OpenClaw tool hooks.
Logs stay on the control disk. Workspace writes use native tool semantics; the
runner does not inject an agent-step durability barrier.

The proxy stores successful HTTP model exchanges. In replay it matches method,
route, model, streaming mode and conversation shape; it uses recorded order
within each match key. Exact prompt bytes and generated transport IDs are not
request keys. Unmatched or unused responses fail the trial. No offline request
can fall back to the model provider. This is model-response replay, not an
identical-syscall or adaptive-agent claim. Task networking remains independent;
keep it fixed across runs.

Tool intervals come from measured durations on the actual harness. We union
intervals and remove proxy exchange time from delegation waits, without
subtracting simultaneously executing concrete tools. Do not substitute the
Harbor agent phase for `tool_s`: it also includes harness work outside tool
execution. This metric ends with the harness call; detached jobs not awaited
by it are not measured to background completion.

Validation after every trial compares observed tool calls, arguments, explicit
outcomes and model-visible output, removing known harness-generated framing
such as Codex command timing. Different work or output yields divergence; no
retry or permissive fallback. Missing native telemetry is an error. Verifier
reward is reported and can be zero; an identical trajectory can fail to solve
the task. Harbor infrastructure exceptions still invalidate the trial.

Media, background tools and nondeterministic commands can yield unstable
results. Report their observed divergence against the preselected denominator;
do not select a replacement task after seeing timings. The adapters are pinned
rather than claiming support for all versions or tool schemas.
