# Usage

Use a single-step Harbor task and pin the task release, Docker image digest and
initial workspace. Run the tool on the Docker host (inside Colima on macOS),
not from a controller with a remote Docker socket.

Harbor manages the selected built-in agent in the task container. The image
must be glibc-based and provide bash, curl, git and CA certificates; installers
need nvm, Node.js and npm access. Codex's built-in adapter can reuse a matching
preinstalled CLI, which was used for its successful scripted capture/replay
test. Keep agent caches out of the image: stale Codex caches can emit a warning
that breaks Harbor's version detection and triggers reinstall. Pi still installs
per trial. "Offline replay" means the model provider is offline; installer and
task networking remain independent.

| `--agent` | CLI pin installed by Harbor | Model protocol |
| --- | --- | --- |
| `codex` | `@openai/codex` 0.169.0 | Responses |
| `openclaw` | `openclaw` 2026.7.1 | Chat Completions |
| `pi` | `@earendil-works/pi-coding-agent` 0.85.1 | Chat Completions |

Harbor 0.24.0 supplies Node 22 for its installers; Harbor 0.27.0 supplies
Node 24 for OpenClaw. OpenClaw 2026.7.1 supports both current Node lines.
`--agent-version VERSION` explicitly selects a native CLI version. The chosen
version and `--model-api` are saved in the manifest and restored on replay.

### Optional installation mirrors

`record` and `smoke` accept `--install-mirror cn`. Official sources remain the
default. The CN preset sets Node.js to `https://npmmirror.com/mirrors/node`,
npm to `https://registry.npmmirror.com`, and the NVM git repository to
`https://gitee.com/mirrors/nvm.git`. Override individual sources with
`--node-mirror URL`, `--npm-registry URL`, or `--nvm-source URL`.

```sh
uv run agent-mitm-replay record --agent pi --install-mirror cn \
  --model openai/deepseek-v4.1-flash --upstream "$UPSTREAM_BASE_URL" \
  --task "$TASK" --cassette "$CASSETTE"
```

The runner supplies these environment variables through a Harbor Compose
overlay: `NVM_NODEJS_ORG_MIRROR`, `npm_config_registry`, and `NVM_SOURCE`
with NVM's `METHOD=git`. Resolved URLs are saved in the manifest and replay
restores them; there is no mirror retry, automatic fallback or Harbor patch.
Mirrors are trusted software supply-chain inputs.

**NVM bootstrap limitation:** Harbor hardcodes its initial `install.sh` URL
to GitHub. The preset mirrors the NVM repository, not that first script.
The environment must still reach `raw.githubusercontent.com`. Fix its network
or DNS outside this project; do not disable certificate checks for installers.
This option does not change apt sources or the model API endpoint.

**Codex cold-install limitation:** Harbor 0.24.0 explicitly overrides
`NVM_NODEJS_ORG_MIRROR` with `https://nodejs.org/dist` in its Codex installer.
The configured Node mirror therefore does not apply to that cold-install
command; the npm registry and NVM repository settings still apply. The
validated Codex runs use a matching preinstalled native binary. This project
does not patch Harbor to override its installer behavior.

Validated on lyh228 with Harbor 0.24.0's built-in Pi 0.85.1 and real
`deepseek-v4.1-flash`: two recorded calls, model relay stopped and key removed
before replay, rewards 1 in both phases, identical 20-byte artifacts. The test
environment uses `.curlrc` connection/total timeouts and reachable official
GitHub IPv4 addresses for the hardcoded bootstrap. Those address choices are
host-specific network configuration, not part of the CN preset or an installer
TLS bypass. Upstream model TLS was bypassed only in the separately authorized
temporary relay for the provider's expired certificate.


OpenClaw uses an explicit primary model, complete model metadata and
`api=openai-completions` at provider/model level. Otherwise recent releases
can select Responses despite the custom endpoint. Console/file logging is
set to `silent` to keep its JSON result parseable by Harbor's built-in adapter.
Harbor 0.27.0 defaults `auto_mounting` to false. Its OpenClaw adapter writes
the upload config on the host, then immediately copies it inside the container;
without the log bind, `/logs/agent/openclaw.upload.json` is absent. The runner
now explicitly passes `--environment-kwarg auto_mounting=true`. An A/B container
inspection confirms this creates the log mounts and makes the exact copy work.
The same option is accepted by Harbor 0.24.0, which already mounts logs.
OpenClaw 2026.11.9 changed `sessionFile` into a session key and emits logs after
the JSON result; this breaks Harbor's JSONL trajectory collection. The runner
pins 2026.7.1 rather than adding a custom trajectory parser or skipping checks.

### Real Terminal-Bench fix-git verification

Official task source: `harbor-framework/terminal-bench-2`, commit
`2fd12b88aafdd04a52c298e3940bcb189f9766d6`, directory `fix-git`.
Original image config digest:
`sha256:b041e51b3fd55da33dc43648fcfb28f4887374a8676c77b0836923422e7fde29`.
The test image only adds installer tools and network configuration; task
instructions, Git state and verifier tests are unchanged. Each phase starts
a fresh Harbor container. Verifier dependencies use the official uv release
mirror via `UV_DOWNLOAD_URL` and a reachable Python package index.

Harbor 0.24.0's built-in Pi 0.85.1 with real `deepseek-v4.1-flash` passes:
seven recorded calls, seven offline hits, zero misses/remaining responses,
matching Harbor tool-call trajectories, and reward 1 in both phases. The
official verifier runs both tests successfully in both phases. Model relay
is stopped and API key removed before replay. Earlier installation/verifier
download failures remain invalid attempts, not successful timing samples.

Built-in Codex 0.169.0 still fails against this provider's Responses endpoint.
Historical native Codex 0.80.0 with `wire_api=chat` passes the same real task:
13 recorded calls and 13 offline hits, matching Harbor tool trajectories and
reward 1 in both phases. No protocol converter is involved. This is a pinned
legacy CLI experiment, not a claim about current Codex support.

Harbor 0.24.0's built-in OpenClaw 2026.7.1 also passes real fix-git capture
and provider-stopped replay: 14 model calls/hits, 15 matching tool calls,
reward 1 and both official verifier tests passing in each phase. No Harbor
upgrade, source patch, custom agent or protocol conversion is required.

```sh
uv run agent-mitm-replay record --agent openclaw --install-mirror cn \
  --model openai/deepseek-v4.1-flash --upstream "$UPSTREAM_BASE_URL" \
  --task "$TASK" --cassette "$CASSETTE"
```




```sh
# Set OPENAI_API_KEY in the environment for record only.
# UPSTREAM_BASE_URL is an OpenAI-compatible base URL with its API prefix.
uv run agent-mitm-replay record --agent codex --model openai/qwen3.8-max \
  --upstream "$UPSTREAM_BASE_URL" --task "$TASK" --cassette "$CASSETTE" \
  --mount "$REFERENCE_COPY:/app"
uv run agent-mitm-replay replay --task "$TASK" --cassette "$CASSETTE" \
  --mount "$FRESH_TARGET_COPY:/app"
```

Record on ext4 with a disposable copy, not the original template. For OpenClaw
or Pi, record independently with `--agent openclaw` or `--agent pi` and a fresh
copy. Codex needs the provider's Responses endpoint; OpenClaw and Pi use Chat
Completions. Use the same concrete model for cross-harness experiments.

The proxy forwards upstream authentication itself; containers receive only a
per-trial token. Repeat `--mount` for each disjoint data directory. Container
paths, task directory contents and the image must match capture. Prefer a
prebuilt image by digest and keep unrelated caches and logs off the tested
filesystem. The runner does not snapshot, initialize or restore task data; the
mount sources and task environment remain caller-managed. A bind mount hides
image files below its destination.

### Pi

Use the same CLI with `--agent pi`; no agent-specific flags are required:

```sh
export OPENAI_API_KEY=...
uv run agent-mitm-replay record --agent pi \
  --model openai/deepseek-v4.1-flash --upstream https://provider.example/v1 \
  --task "$TASK" --cassette "$CASSETTE" --mount "$REFERENCE_COPY:/app"
uv run agent-mitm-replay replay --task "$TASK" --cassette "$CASSETTE" \
  --mount "$FRESH_TARGET_COPY:/app"
```

Internally the runner passes Harbor's `model_api=openai-completions` and
`thinking=off` kwargs. Harbor installs the pinned Pi package and its Node
runtime; only the per-trial token enters the container, and the upstream key
stays in the host proxy. The task and image remain trusted inputs, and native
commands keep their normal filesystem access.

### Diagnosing Codex provider failures

The default Codex 0.169.0 requires a working Responses API with SSE streaming.
For a Chat-Completions-only provider, explicitly select the historical native
version and protocol:

```sh
uv run agent-mitm-replay record --agent codex --agent-version 0.80.0 \
  --model-api chat-completions --install-mirror cn \
  --model openai/deepseek-v4.1-flash --upstream "$UPSTREAM_BASE_URL" \
  --task "$TASK" --cassette "$CASSETTE"
```

The runner changes Codex's native `wire_api` and the recorded route together.
Unsupported version/protocol combinations fail; there is no protocol conversion,
version fallback, or automatic downgrade. Old Codex is pinned intentionally;
its behavior and security fixes are not those of the latest release.

Failed runs include the model endpoint, HTTP status, and the upstream JSON
error's `message`, `type` and `code` in `proxy-audit.jsonl` and `summary.json`.
The CLI also includes that diagnostic instead of showing only Harbor's generic
agent-exit error. Failed exchanges are not saved as replayable cassette turns.
Provider messages can contain request details; treat run diagnostics as
private. Use a Responses-capable upstream for current Codex, or explicitly run
the pinned legacy Chat version; OpenClaw and Pi use Chat Completions natively.

TLS verification stays enabled by default. Any temporary insecure relay used
for diagnosing an expired certificate is external to the runner and must not
be mistaken for a project-wide certificate bypass.

### Outputs and cassette schema

`--output DIRECTORY` must be new. Defaults are `CASSETTE/record` and
`CASSETTE/replays/<id>`. A complete capture has `manifest.json`, `flows.mitm`
and `commands.json`. The manifest records the agent, pinned CLI version,
model, upstream, task SHA-256, installation mirrors and mount destinations; replay rejects a
different agent version, task content or destination set. Require schema 4:
older cassettes must be recaptured; there is no migration.

Every run writes Harbor logs under `jobs/`, plus `proxy.log`, `harbor.log`,
`proxy-audit.jsonl` and `command.json`. A classified failure writes
`summary.json` with `status=divergence|error|timeout` and no timing fields.
Setup errors such as a mismatched task or unavailable Docker abort the
experiment.

`summary.json` fields on success: `agent_execution_s` (the comparison metric,
from Harbor's `agent_execution` phase), `harbor_s` (whole subprocess,
diagnostic only), `phase_seconds`, `model_requests`, and `rewards` (reported,
not a gate). Never put a failed attempt into a timing distribution.

## Local smoke

```sh
docker build -t amr-native-smoke examples/smoke-task/environment
uv run agent-mitm-replay smoke --image amr-native-smoke --output /tmp/amr-native-smoke
```

The smoke task records two scripted model responses, stops the scripted model,
then checks an offline replay of the same task. It uses no provider tokens and
produces no performance evidence. The image supplies the base tools; Harbor
installs the selected agents, so the task environment needs installer network
access. `--agent` is repeatable and defaults to all registered harnesses:

```sh
uv run agent-mitm-replay smoke --agent pi --image YOUR_IMAGE --output /tmp/amr-pi-smoke
```
