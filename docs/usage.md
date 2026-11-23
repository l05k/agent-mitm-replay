# Usage

Use a single-step Harbor task and pin the task release, Docker image digest and
initial workspace. Preinstall Codex `0.169.0` and/or OpenClaw `2026.11.3` on the
image's `PATH` (OpenClaw requires Node 24.16+ on the Node 24 line). Startup fails
if the selected binary does not match. Run the tool on the Docker host (inside
Colima on macOS), not from a controller with a remote Docker socket.

```sh
# Set OPENAI_API_KEY in the environment for record only.
# UPSTREAM_BASE_URL is an OpenAI-compatible base URL with its API prefix.
uv run agent-mitm-replay record --agent codex --model openai/qwen3.8-max \
  --upstream "$UPSTREAM_BASE_URL" --task "$TASK" --cassette "$CASSETTE" \
  --mount "$REFERENCE_COPY:/app"
uv run agent-mitm-replay replay --task "$TASK" --cassette "$CASSETTE" \
  --mount "$FRESH_TARGET_COPY:/app"
```

Record on ext4 with a disposable copy, not the original template. For OpenClaw,
record independently with `--agent openclaw` and a fresh copy. Codex needs the
provider's Responses endpoint; OpenClaw uses Chat Completions. Use the same
concrete model on both. The proxy forwards upstream authentication itself;
containers receive only a per-trial token. Repeat `--mount` for each disjoint
data directory. The container paths, task directory contents, and image must
match capture. Prefer a prebuilt image by digest and keep unrelated caches and
logs off the tested filesystem.

### Diagnosing Codex provider failures

Codex requires a working **Responses API** (`POST /v1/responses`, or the same
route under the configured API prefix) with SSE streaming. A successful
`/chat/completions` request does not prove Codex compatibility. This project
does not translate between the two protocols or fall back to another provider.

Failed runs include the model endpoint, HTTP status, and the upstream JSON
error's `message`, `type`, and `code` in `proxy-audit.jsonl` and `summary.json`.
The CLI also includes that diagnostic instead of showing only Harbor's generic
agent-exit error. Failed exchanges are not saved as replayable cassette turns.
Provider messages can contain request details; treat run diagnostics as private.

For example, on lyh228 the supplied gateway's `deepseek-v4.1-flash` returned
HTTP 200 for Chat Completions but HTTP 500 with `convert_request_failed` /
`not implemented` for Responses. This is a recording/provider compatibility
failure, not an offline replay miss. Use a Responses-capable upstream for Codex;
Chat-Completions-only models can be used with OpenClaw instead.

TLS verification stays enabled by default. Any temporary insecure relay used
for diagnosing an expired certificate is external to the runner and must not
be mistaken for a project-wide certificate bypass.


`--output DIRECTORY` must be new. Defaults are `CASSETTE/record` and
`CASSETTE/replays/<id>`. A complete capture has `manifest.json`, `flows.mitm`,
and `tools.json`. Migrate older cassettes by recapturing. Every run writes
Harbor logs under `jobs/` and proxy audit data. A classified failure writes
`summary.json` with `status=divergence|error|timeout` and no tool time. Setup
errors such as a mismatched task or unavailable Docker abort the experiment.

`summary.json` fields on success: `tool_s` (the comparison metric), `harbor_s`
(whole subprocess, diagnostic only), `phase_seconds`, `model_requests`, and
`rewards` (not a gate). Never put a failed attempt into a timing distribution.

## Local verification

```sh
docker build -t amr-native-smoke examples/smoke-task/environment
uv run agent-mitm-replay smoke --image amr-native-smoke --output /tmp/amr-native-smoke
```

This runs both native harnesses, records a local scripted model, shuts it down,
then checks a valid offline replay and rejection of changed tool output. It
uses no provider tokens and produces no performance evidence.
