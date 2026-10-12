# agent-mitm-replay

Scripted record/replay for agent model calls with [mitmproxy](https://mitmproxy.org/), currently integrated with [Harbor](https://github.com/harbor-framework/harbor) Terminus-2.

The first run sends model requests to the real provider and stores a mitmproxy flow cassette. Later runs return those recorded responses from a local proxy. A replay miss is a hard HTTP 400 and never falls through to the provider.

This is useful when the task environment changes but model sampling must stay fixed. The task, container, terminal commands, verifier, and filesystem still execute normally on every run.

## Requirements

- Linux or macOS
- Python 3.13.x
- Docker with Compose v2 for local Harbor environments
- The CLI uses Harbor `terminus-2` with an OpenAI-compatible Chat Completions API
- A provider API key for `record` only

Harbor 0.24.0 and mitmproxy 12.2.3 are pinned because their Python 3.12 dependency ranges conflict. The project is intentionally constrained to Python `>=3.13,<3.14`.

## Install

```bash
git clone git@github.com:l05k/agent-mitm-replay.git
cd agent-mitm-replay
uv sync --locked
```

## Record a trial

The upstream is an origin, without `/v1` or another API path. The proxy owns the path appended by LiteLLM.

Use the provider's final endpoint origin. Responses with HTTP 301, 302, 303, 307, or 308 and a `Location` header are rejected with HTTP 400 (`unsafe_redirect`), including relative redirects. This prevents the model client from following an unrecorded request outside the proxy. Old cassettes containing redirects are rejected during replay too.

```bash
export OPENAI_API_KEY=...
uv run agent-mitm-replay record \
  --task /path/to/task \
  --cassette cassettes/task-a \
  --model openai/gpt-4o-mini \
  --upstream https://api.openai.com \
  --artifact /app/result.txt
```

The command fails unless Harbor reports one completed, error-free trial with reward at least `1.0`. It writes:

```text
cassettes/task-a/
├── flows.mitm
├── manifest.json
└── record/
    ├── command.json
    ├── proxy-audit.jsonl
    ├── proxy.log
    ├── harbor.log
    ├── summary.json
    └── jobs/
```

Treat a cassette as sensitive: it contains prompts, tool observations, and model responses. Do not commit it unless your project explicitly permits that.

`api_base` and `session_id` are managed by the runner. Routing overrides in `llm_kwargs` or `llm_call_kwargs` (`api_base`, `base_url`, `client`, `custom_llm_provider`, `model_list`, `fallbacks`, and `model`) are rejected before starting processes; restored manifest options are checked too. The current Harbor adapter supports the `litellm` backend only. Normal generation parameters remain configurable.

Runs are noninteractive: the runner passes Harbor's `--yes` and closes child stdin. Host environment templates in the task are confirmed automatically; missing required variables still fail Harbor's validation.

## Replay offline

Stop or disconnect the provider before starting replay. The runner starts mitmproxy with no upstream fallback and sets `LITELLM_LOCAL_MODEL_COST_MAP=True` to prevent LiteLLM from downloading its model-price map.

The runner merges `NO_PROXY` and `no_proxy` and adds `127.0.0.1`, `localhost`, and `::1`, so ambient HTTP proxy settings cannot reroute the local model hop. Other proxy settings and existing exclusions are retained.

```bash
uv run agent-mitm-replay replay \
  --task /path/to/task \
  --cassette cassettes/task-a \
  --artifact /app/result.txt
```

A replay succeeds only when every recorded response is consumed exactly once, Harbor has no trial exception, and every verifier reward meets the threshold. The command checks persisted Harbor results instead of trusting Harbor's process exit code.

To test divergence deliberately:

```bash
uv run agent-mitm-replay replay \
  --task /path/to/task \
  --cassette cassettes/task-a \
  --extra-instruction 'Change the task so the recorded request must miss.'
```

This command should fail with a `ReplayError` after Harbor records a `BadRequestError` trial. A changed request is a useful signal that the new filesystem or task environment changed the agent trajectory.

## Run the local smoke test

The smoke command uses a scripted local OpenAI-compatible server, so it does not consume provider tokens. It still runs a real Harbor Terminus-2 trial in Docker, records the model requests through mitmproxy, stops the fake upstream, and runs a second trial offline.

```bash
uv run agent-mitm-replay smoke --output smoke-output
```

`--image` is intended for a local image containing `bash`, `tmux`, and `C.UTF-8`. Without it, the smoke helper builds the example task image from Docker Hub's `ubuntu:24.04`; the build needs Docker network access.

The smoke verifies:

- first run reward is `1.0`;
- replay runs with the model upstream stopped;
- replay creates the same `/app/result.txt` bytes;
- the upstream receives no replay requests;
- changing the instruction produces a replay miss.

## Compare filesystem mounts

Use separate, freshly initialized host directories and bind each to the same container path. The example assumes the task's workspace is `/app`; copy all task inputs into each host directory before running.

```bash
uv run agent-mitm-replay record \
  --task /path/to/task --cassette cassettes/task-a \
  --model openai/gpt-4o-mini --upstream https://api.openai.com \
  --environment-kwarg 'mounts=[{"type":"bind","source":"/mnt/fs-a/task-a","target":"/app"}]'

uv run agent-mitm-replay replay \
  --task /path/to/task --cassette cassettes/task-a \
  --environment-kwarg 'mounts=[{"type":"bind","source":"/mnt/fs-b/task-a","target":"/app"}]'
```

The tool does not format disks or reset existing files. Keep image, inputs, working directory, agent options, locale, and shell prompt fixed. A changed terminal observation changes the next model request and therefore produces a miss; this is strict request replay, not unconditional action playback. Matching is FIFO within each request key, not a global sequence check.

Record and replay have different model latency. Compare filesystem variants using the same replay mode, and measure tool/filesystem work separately from model waits. Instant replay can change the completion timing of background shell commands.

`--environment-kwarg 'extra_docker_compose=["/absolute/overlay.yaml"]'` passes Compose overlays through to Harbor when mounts alone are insufficient.

## Experiment boundaries

- Use one cassette and one proxy process per Harbor trial. Do not share a cassette across concurrent trials.
- The replay key includes method, URL, and raw request body. Headers are not part of the key.
- The runner disables Harbor telemetry and remote LiteLLM price-map fetches; it does not globally firewall arbitrary network access by task commands or verifiers. Use your task's network policy when complete network isolation is required.
- Streaming response bytes can be stored, but original inter-event timing is not reproduced.
- This project intentionally treats a request miss as an error. It does not silently record a new response or call the provider.
- Harbor may exit zero even when a trial errored; inspect `result.json`, as the runner does.
- Verifier LLM calls are a separate traffic source. Point them at a matching replay service or disable them for a fully offline run.

The HTTP replay addon has no Harbor dependency. Harbor-specific launch options and result validation live in `runner.py`; the current CLI supports Harbor Terminus-2. Other evaluation frameworks must route all model traffic through the proxy and validate their own trial results. No Harbor source patch is required.

## Development

```bash
uv run pytest
uv run agent-mitm-replay --help
```

The repository's permanent tests cover the replay boundary and result validation. The end-to-end smoke requires a working Docker daemon and is opt-in.
