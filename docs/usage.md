# Usage

- [Commands and options](#commands-and-options)
- [Record](#record)
- [Replay](#replay)
- [When a run passes](#when-a-run-passes)
- [Testing divergence](#testing-divergence)
- [Smoke test](#smoke-test)

## Commands and options

```text
agent-mitm-replay record --task DIR --cassette DIR --model MODEL --upstream ORIGIN [options]
agent-mitm-replay replay --task DIR --cassette DIR [options]
agent-mitm-replay smoke  [--output DIR] [--image IMAGE]
```

Options shared by `record` and `replay`:

| Option | Meaning |
| --- | --- |
| `--task DIR` | Harbor task directory. |
| `--cassette DIR` | Cassette directory. `record` creates it and fails if it already exists. |
| `--output DIR` | Where to write logs and Harbor jobs. Defaults to `CASSETTE/record` for record and `CASSETTE/replays/<random id>` for replay. Must not exist. |
| `--minimum-reward N` | Lowest verifier reward that counts as a pass. Default `1.0`. |
| `--agent-kwarg KEY=VALUE` | Passed to Harbor's agent. `VALUE` is parsed as JSON when possible, otherwise used as a string. Repeatable. |
| `--environment-kwarg KEY=VALUE` | Passed to Harbor's environment. `mounts` and `extra_docker_compose` map to Harbor's `--mounts` and `--extra-docker-compose`. Repeatable. |
| `--extra-instruction TEXT` | Extra instruction appended to the task. Repeatable. |
| `--artifact PATH` | Container path that Harbor collects after the trial. Repeatable. |

Options for `record` only:

| Option | Meaning |
| --- | --- |
| `--model MODEL` | LiteLLM model name, which must start with `openai/`, e.g. `openai/gpt-4o-mini`. |
| `--upstream ORIGIN` | Provider origin, e.g. `https://api.openai.com`. |

On success the command prints a JSON summary (mode, number of model requests, rewards, output paths) and exits 0. On failure it prints `error: ...` and exits 1.

## Record

```bash
export OPENAI_API_KEY=...
uv run agent-mitm-replay record \
  --task /path/to/task \
  --cassette cassettes/task-a \
  --model openai/gpt-4o-mini \
  --upstream https://api.openai.com \
  --artifact /app/result.txt
```

### Choosing `--upstream`

Give the origin only (scheme, host, optional port), without `/v1` or credentials. LiteLLM appends the API path itself.

Use the provider's final endpoint. If the provider answers with a redirect (HTTP 301, 302, 303, 307, or 308 with a `Location` header, including relative ones), the proxy replaces it with HTTP 400 `unsafe_redirect`. Otherwise the model client could follow the redirect to an address outside the proxy, and that request would never be recorded.

### Agent options

The runner sets `api_base` and `session_id` itself. Options that would send model traffic around the proxy are rejected before any process starts, in `--agent-kwarg` as well as inside `llm_kwargs` and `llm_call_kwargs`:

`api_base`, `base_url`, `client`, `custom_llm_provider`, `model_list`, `fallbacks`, `model`

Only the `litellm` backend is supported. Normal generation parameters such as temperature stay configurable.

Option names must not be empty or have leading or trailing whitespace. Harbor strips whitespace from names, so `api_base ` would otherwise slip past the check and still reroute traffic.

### Cassette layout

```text
cassettes/task-a/
├── flows.mitm          # recorded requests and responses
├── manifest.json       # model, upstream, agent options, artifacts, instructions
└── record/             # output of the record run
    ├── command.json    # Harbor command line
    ├── proxy-audit.jsonl
    ├── proxy.log
    ├── harbor.log
    ├── summary.json
    └── jobs/           # Harbor job and trial results
```

Each replay run writes the same kind of output under `cassettes/task-a/replays/<id>/` unless you pass `--output`.

### Non-interactive runs

The runner passes `--yes` to Harbor and closes child stdin, so nothing can wait on a hidden prompt. Host environment templates in the task are confirmed automatically. Missing required variables still fail Harbor's validation.

## Replay

```bash
uv run agent-mitm-replay replay \
  --task /path/to/task \
  --cassette cassettes/task-a
```

Replay reads `manifest.json` and reuses the recorded model, upstream, and agent options. Artifacts and extra instructions are also taken from the manifest unless you pass `--artifact` or `--extra-instruction`. Any `--agent-kwarg` you pass must equal the recorded value. Recorded options go through the same routing checks as new ones.

Stop or disconnect the provider before replaying if you want to prove the run is offline. The runner:

- starts mitmproxy with no upstream fallback;
- sets a placeholder `OPENAI_API_KEY`;
- sets `LITELLM_LOCAL_MODEL_COST_MAP=True`, so LiteLLM does not download its model price map;
- adds `127.0.0.1`, `localhost`, and `::1` to both `NO_PROXY` and `no_proxy`, so an ambient HTTP proxy cannot reroute the local model hop. Existing exclusions and other proxy settings are kept.

Cassettes that contain redirect responses are rejected during replay too.

## When a run passes

The runner reads Harbor's persisted `result.json` files instead of trusting Harbor's exit code, because Harbor can exit 0 when a trial errored. A run passes only if:

- the proxy reported no errors;
- in replay, every recorded response was used exactly once, with no misses;
- Harbor reports exactly one trial, with no exception;
- every verifier reward is at least `--minimum-reward`.

For a task with `[[steps]]`, every configured step must report a verifier result that meets the threshold. A step exception, a missing step, or an extra step fails the run.

## Testing divergence

To confirm that replay catches a changed trajectory, change the instruction:

```bash
uv run agent-mitm-replay replay \
  --task /path/to/task \
  --cassette cassettes/task-a \
  --extra-instruction 'Change the task so the recorded request must miss.'
```

This should fail with a `ReplayError`, after Harbor records a trial that ended in `BadRequestError`. In real experiments, a miss like this means the new environment changed what the agent saw and therefore what it asked the model.

## Smoke test

```bash
uv run agent-mitm-replay smoke --output smoke-output
```

The smoke test runs a real Harbor Terminus-2 trial in Docker against a scripted local OpenAI-compatible server, so it uses no provider tokens. It records through mitmproxy, stops the fake server, and replays offline. It checks that:

- the recorded run gets reward `1.0`;
- replay runs with the model server stopped;
- replay produces the same `/app/result.txt` bytes;
- the model server receives no requests during replay;
- changing the instruction causes a replay miss.

By default it builds the example task image from Docker Hub's `ubuntu:24.04`, which needs network access. To skip the build, pass `--image` with a local image that has `bash`, `tmux`, and the `C.UTF-8` locale.
