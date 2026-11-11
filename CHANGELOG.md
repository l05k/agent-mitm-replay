# Changelog

## Unreleased

- Save complete-request-to-complete-response mean model latency in schema-3 cassettes. Validate/replay asynchronously wait that mean before each response; `--response-delay-sec` overrides it per run, including zero to disable waiting. Report the effective delay in run summaries. Schema-1/2 cassettes require a fresh recording; token timing is not reproduced.
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
