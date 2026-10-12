# Changelog

## Unreleased

- Refuse redirecting API responses before streaming them to the client, and reject redirects in existing cassettes with HTTP 400 (`unsafe_redirect`).
- Reject agent and nested LLM routing overrides before starting processes, including options restored from the cassette manifest.
- Merge both `NO_PROXY` spellings and exempt loopback model traffic while preserving existing exclusions and other proxy settings.
- Run Harbor with `--yes` and closed child stdin so task environment templates cannot block on a hidden confirmation prompt.
- Keep HTTP replay safety in the framework-independent addon and Harbor launch/result handling in the runner; no Harbor source changes.
- Verify 62 behavior tests, stopped-upstream HTTP replay, legacy and streamed redirect refusal, and real Docker Harbor trials with host environment templates and an ambient proxy (zero proxy requests, matching artifacts, reward 1.0).

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
