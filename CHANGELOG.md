# Changelog

## Unreleased

- Expose `--agent-timeout-multiplier` on record and replay. Save the resolved
  multiplier with the cassette; replay defaults to it and permits an explicit
  override. Reject non-finite/non-positive values before starting a trial.
- Restore `tool_s` as the sum of native per-tool execution durations, counting
  parallel calls separately. Keep Harbor's `agent_execution_s` distinct and
  fail when native timing evidence is missing or incomplete; no turn-time
  subtraction or inferred tool latency.
- Require schema-5 cassettes with timeout and native timing configuration.
  Timed-out recordings remain invalid samples with partial flows/logs only.
- Remote targeted regressions: 43 passed. Real Harbor timeout proof (2-second
  task budget multiplied by 2) returns `AgentTimeoutError` after about 4.4
  seconds, without a complete cassette. Built-in Codex 0.169 native-tool timing
  and provider-stopped replay pass, including replay timeout override.
- Historical native Codex 0.80 Chat mode also passes remote capture/replay
  with native OTLP durations and the recorded timeout multiplier inherited.
- Terminal-Bench 2.1 `circuit-fibsqrt` Pi/OpenClaw end-to-end verification was
  stopped at the user's request. Attempts failed during Harbor's NVM bootstrap
  (network error / agent setup timeout), before model execution; retain them as
  invalid infrastructure attempts. Do not claim those task-harness pairs pass.

- Treat client cancellation after a complete SSE terminal event as a successful
  model response, including the mitmproxy `error` hook path. Capture streamed
  bytes before HTTP EOF, persist a clean replayable response once, and retain
  strict errors for incomplete streams, upstream failures and non-2xx replies.
- Verify 18 behavior tests and an actual TCP RST after `[DONE]` while the
  provider keeps the HTTP response open; audit reports one response, zero errors.
- Review hardening: reject ambiguous HTTP/2 CANCEL errors and validate gzip/
  deflate integrity before recovery, so compressed truncation cannot be saved
  as a successful response merely because its decoded text contains `[DONE]`.
- Final reviewed behavior suite: 21 tests pass, including ambiguous reset and
  truncated gzip/deflate rejection.

- Launch Harbor's built-in `codex`, `openclaw` and `pi` agents instead of
  project-owned agent adapters and tool-timing plugins. Harbor owns in-container
  agent installation, configuration, execution, logs and the verifier.
- Pin Codex 0.169.0, OpenClaw 2026.7.1 and Pi 0.85.1 through Harbor agent
  kwargs. Pi is configured with `model_api=openai-completions`; users keep the
  same `--agent pi` CLI. Harbor installs Node 22 and the pinned CLI packages.
- Keep only the HTTP model-response proxy: record successful exchanges and
  replay them by method, route, model, streaming mode and conversation shape.
  Add no provider fallback, protocol translation, redirect following or custom
  retry; built-in Harbor and agent retry behavior is left unchanged.
- Report `agent_execution_s` from Harbor's agent phase as the timing metric,
  with `harbor_s` and `phase_seconds` diagnostics. Remove native tool timing
  and the `tool_s` field.
- Validate replay with the standard `agent/trajectory.json`: compare tool names
  and arguments only; observations, outputs and IDs are not compared. Report
  verifier rewards without gating on them.
- Require schema-4 cassettes containing `manifest.json`, `flows.mitm` and
  `commands.json`; older cassettes must be recaptured.
- Reject WebSocket upgrades and report upstream HTTP status and structured JSON
  error details in failed trial summaries and CLI errors.
- Update the scripted Docker smoke and example image for Harbor-installed
  agents; the image no longer preinstalls the harnesses.
- Keep TLS verification enabled by default and document Responses versus Chat
  Completions compatibility.
- Add opt-in `--install-mirror cn` for Node/npm/NVM repository mirrors and
  per-source overrides. Save resolved URLs in the cassette and restore them
  through Harbor Compose overlays; official sources remain the default.
  Harbor's first NVM bootstrap URL remains GitHub and is documented explicitly.
- Verification: 10 behavior tests pass. On lyh228, unchanged Harbor 0.24.0's
  built-in Pi 0.85.1 records two real `deepseek-v4.1-flash` calls and replays
  them after the model relay is stopped and key removed. Both rewards are 1,
  artifacts match at 20 bytes, and installation mirrors restore automatically.
  The test image uses bounded curl timeouts and reachable official GitHub IPv4
  addresses; no Harbor patch, CLI wrapper, or installer TLS bypass is used.
- Configure OpenClaw's native Chat Completions API, primary model and required
  model metadata explicitly. Pin 2026.7.1 for Harbor's JSONL session/trajectory
  contract and silence non-JSON console logs; do not add custom parsers.
- Harbor 0.24.0's built-in Codex 0.169.0 passes a two-turn scripted Responses
  capture and provider-stopped replay with matching tool calls and 5-byte
  artifacts. The image preinstalls the real pinned CLI; removing stale Codex
  cache prevents a version-check warning from triggering unnecessary reinstall.
- Diagnose Harbor 0.27.0's missing OpenClaw upload file: `auto_mounting`
  defaults false, so host-generated config is absent inside `/logs/agent`.
  Explicitly enable Harbor's `auto_mounting=true`; A/B mount inspection and
  the exact config-copy command pass without modifying Harbor. Newer OpenClaw
  session storage is not compatible with Harbor's JSONL collector; use the
  tested 2026.7.1 release. Project dependency pins remain unchanged.
- Verify official Terminal-Bench `fix-git` at commit `2fd12b88aafdd04a52c298e3940bcb189f9766d6`
  using Harbor's built-in Pi and real DeepSeek: seven calls captured and seven
  hits offline, matching tool-call trajectories, reward 1 and both official
  verifier tests passing in record and replay. Preserve initial installer and
  verifier-download failures as invalid attempts. Use only dependency/network
  configuration changes, not task/test changes.
- Add explicit `--agent-version` and `--model-api` choices, persisted for replay.
  Native Codex 0.80.0 `wire_api=chat` passes real DeepSeek fix-git capture and
  offline replay through Harbor 0.24.0: 13 model calls/hits, 15 tool calls,
  reward 1, two official verifier tests passing in both phases. Default Codex
  0.169.0 remains Responses-only; there is no automatic downgrade/converter.
- Harbor 0.24.0's built-in OpenClaw 2026.7.1 passes the same real benchmark:
  14 model calls/hits, 15 matching tool calls, reward 1 and both verifier tests
  passing in record/replay after stopping the model relay and removing the key.
  All 10 project behavior tests pass.

## Previous snapshot

- Replace exact HTTP request matching with a single FIFO response sequence. Prompt and terminal-output differences no longer cause misses; exhaustion and recorded redirects still fail without provider fallback.
- Add `validate`: replay once offline, require task success, complete response consumption, and identical ordered command batches/arguments, then write `validation.json`.
- Require the acceptance marker for measured `replay`. Keep running the verifier, but do not repeat command, reward, or remaining-response validation. Basic process/trial exceptions and proxy errors still fail.
- Report `elapsed_sec` around Harbor startup through exit (including verifier and teardown) plus Harbor stage durations. Only replay summaries are `measured: true`; they omit reward fields.
- Save environment kwargs, copied Compose overlays, the reward threshold, task metadata, and `commands.json` in schema-2 cassettes. Host mounts and data restoration stay caller-managed. Schema-1 cassettes require a fresh recording.
- Make environment options, extra instructions, and artifacts record-only so later phases reuse the saved experiment settings. Preserve local proxy routing, `--yes`, closed child stdin, and private cassette/run directories.
- Update the token-free Docker smoke to exercise recording, pre-validation gating, offline validation with a changed prompt, restored Compose settings, and a measured replay whose verifier returns zero. Remove strict-divergence checks and per-request hashing.
- Document NAS data initialization, timing/cache assumptions, one-time acceptance limitations, and the distinction between fixed model responses and exact filesystem-operation replay.
- Verified in colima with Harbor 0.24.0: 34 behavior tests pass; the three-trial Docker smoke passes offline validation and measured replay with a zero-reward verifier and identical 20-byte artifacts. Separate real trials reject changed commands and a failed validation reward without writing an acceptance marker.

## 0.1.0

- Add managed `record`, `replay`, and Docker `smoke` commands for Harbor Terminus-2.
- Preserve model configuration and a stable session ID in each cassette manifest.
- Replay raw HTTP request matches once per recorded response; reject misses and exhaustion with HTTP 400 without provider fallback.
- Fail the CLI on proxy errors, misses, unconsumed responses, Harbor trial exceptions, or verifier rewards below the chosen threshold.
- Support filesystem bind mounts and Compose overlays while keeping the container path fixed.
- Pin Python 3.13.x, Harbor 0.24.0, and mitmproxy 12.2.3.
- Verify 19 behavior tests and a local Docker smoke with two recorded requests, two offline hits, identical 20-byte artifacts, and a changed-instruction `BadRequestError` scenario.
- Keep replay requests buffered before matching. Request streaming can open an upstream connection before the request hook and is therefore enabled only during recording.

The local Docker smoke used a prepared image with bash, tmux, and C.UTF-8. The default Ubuntu image build requires registry and package-mirror access and was not exercised in the local restricted network.
