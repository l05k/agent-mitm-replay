# Timing and validation

## Architecture

`record` and `replay` run one single-step Harbor task through `harbor run`. The
runner passes `--agent codex|openclaw|pi`, a pinned `version` agent kwarg and
the model endpoint through Harbor's `--agent-env`. Harbor installs and
configures the CLI in the container, runs the agent, writes the agent logs and
standard `agent/trajectory.json`, and runs the verifier. The only custom
component in a measured trial is the local mitmproxy addon in front of the
model endpoint.

The proxy accepts only the selected model route: `/v1/responses` for the
default Codex, `/v1/chat/completions` for OpenClaw/Pi and explicitly selected
historical Codex Chat mode, or the corresponding route under a custom prefix.
The CLI version and wire API are recorded together and reused offline. There
is no translation or automatic version fallback. Other requests, such as
Codex billing metadata probes, receive a local 404 and do not reach the provider.

## Model recording and replay

Recording forwards the request upstream with the host's provider bearer key
and stores only successful HTTP model exchanges. The key never enters the
container; the agent receives an ephemeral per-trial token. Saved flows have
authorization removed. Replay has no provider credential.

SSE clients may close the connection after the protocol terminal event without
waiting for HTTP EOF. Recording observes streamed bytes and accepts that client
disconnect only after a fully framed `[DONE]` (Chat Completions) or
`response.completed` data event (Responses), with a 2xx SSE response. It stores
the complete body and counts the exchange once even when mitmproxy calls
`error` instead of `response`. Incomplete streams, upstream transport failures
and non-success status codes still fail the audit gate.

Replay matches method, route, model, streaming mode and conversation shape;
recorded order is used within each match key. Exact prompt bytes and generated
transport IDs are not request keys. An unmatched request or an unused recorded
response fails the trial. No offline request can fall back to the provider:
redirects and WebSocket upgrades are rejected. The project adds no protocol
translation, provider fallback or retry; retry behavior built into Harbor, its
installers and the agent CLIs is left in place.

## Timing

`agent_execution_s` is the duration of Harbor's `agent_execution` phase in the
trial result. It includes the agent's model exchanges, tool execution and
harness overhead. Do not describe it as native tool wall time, tool latency or
model latency.

`harbor_s` is wall-clock time around the Harbor subprocess. It includes
environment setup, agent installation and configuration, agent execution, the
verifier and teardown; treat it as diagnostic only. `phase_seconds` reports
each Harbor phase that has start and finish timestamps. Accepted replays are
compared on `agent_execution_s`, never on capture runs or `harbor_s`.

Agent installation happens in Harbor's `agent_setup` phase, outside
`agent_execution_s`. Configuration is not uniformly outside it: for example,
Pi uploads its runtime configuration during agent execution. `agent_execution_s`
therefore includes runtime setup and configuration performed by the agent, and
`agent_setup` still needs the installer's network access.

## Validation and failure

Every trial must contain exactly one Harbor result with no infrastructure
exception, a single-step task and a positive `agent_execution` phase. The proxy
audit must show at least one model exchange, no errors and, on replay, full
consumption of the recording.

The standard `agent/trajectory.json` is compared in ordered batches by tool
function name and arguments only. Observations, tool outputs and call IDs are
not compared. Replay can therefore accept the same tool calls even when their
outputs or filesystem effects differ; this is not an exact workload claim.
Reward is reported but never gates a trial, and the same tool calls can still
leave the task unsolved.

A divergence is an unmatched or unused model response or a different
tool-call trajectory. An error is a proxy or upstream failure, missing
artifact or timing, or a Harbor exception; a Harbor timeout is reported as a
timeout. Failures are fail-fast: the runner makes one Harbor trial attempt and
adds no retry or permissive fallback. Retry behavior built into Harbor's
installers or the agent CLIs is left unchanged. Setup errors such as a
mismatched task or an unavailable Docker daemon abort the experiment before a
timing sample exists.

Media, background processes, network-dependent tools and nondeterministic
commands can produce unstable trajectories or timings. Report observed
divergence against the preselected denominator; do not select a replacement
task after seeing timings. The pinned versions are specific, and Harbor's
installer owns how they are installed. This project does not claim support for
other agent versions, other protocols or identical task workloads.
