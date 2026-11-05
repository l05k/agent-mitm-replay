# Changelog

## Unreleased

- Rewrite the README as a short overview and quick start; move the CLI reference, filesystem comparison guide, and design notes to `docs/`.
- Rename the repository, distribution, and CLI to `agent-mitm-replay`, the Python package to `agent_mitm_replay`, and addon options to `amr_*`.
- Simplify to research-code scope: the addon matches with mitmproxy's `ServerPlayback` and writes one audit line per model call (`response`, `hit`, `miss`, `error` with `turn` and `request_sha256`) plus a summary. Routing-option and padded-name checks, multi-step result validation, and the smoke `--image` option are removed; multi-step trials now fail the run.
- Redirects (3xx with `Location`) fail the run: replay answers a recorded redirect with HTTP 400 `unsafe_redirect` so the client cannot follow it to the provider, and recording counts one as an error.
- Add `--mount HOST_DIR:CONTAINER_DIR`, replacing `--environment-kwarg mounts=[...]`. `--agent-kwarg` is now record-only; replay always uses the recorded agent options.
- The smoke command uses `examples/smoke-task` directly.
- Exempt loopback model traffic from an ambient HTTP proxy, merging `NO_PROXY`, `no_proxy`, and the child overlay without dropping existing entries.
- Create cassette and run directories with mode `0700` and document that cassettes contain the provider API key.
- Run Harbor with `--yes` and closed child stdin so task environment templates cannot block on a hidden confirmation prompt.
- Keep HTTP replay safety in the framework-independent addon and Harbor launch/result handling in the runner; no Harbor source changes.
- Verified in the colima VM with Harbor 0.24.0 and mitmproxy 12.2.3: old code 77 tests, new code 22 tests; old and new Docker smokes each pass (2 recorded requests, reward 1.0, 20-byte artifact, changed instruction fails with 9 HTTP 400 misses); the new code replays a cassette recorded by the old code and the old code replays a cassette recorded by the new code, both with reward 1.0 and byte-identical artifacts.

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
