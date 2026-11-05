# Usage

- [Commands and options](#commands-and-options)
- [Record](#record)
- [Replay](#replay)
- [When a run passes](#when-a-run-passes)
- [Reading the audit file](#reading-the-audit-file)
- [Testing divergence](#testing-divergence)
- [Smoke test](#smoke-test)

## Commands and options

```text
agent-mitm-replay record --task DIR --cassette DIR --model MODEL --upstream ORIGIN [options]
agent-mitm-replay replay --task DIR --cassette DIR [options]
agent-mitm-replay smoke  [--output DIR]
```

Options shared by `record` and `replay`:

| Option | Meaning |
| --- | --- |
| `--task DIR` | Harbor task directory. Single-step tasks only; a trial with `[[steps]]` results fails. |
| `--cassette DIR` | Cassette directory. `record` creates it and fails if it already exists. |
| `--output DIR` | Where to write logs and Harbor jobs. Defaults to `CASSETTE/record` for record and `CASSETTE/replays/<random id>` for replay. Must not exist. |
| `--minimum-reward N` | Lowest verifier reward that counts as a pass. Default `1.0`. |
| `--mount HOST_DIR:CONTAINER_DIR` | Bind-mount a host directory into the task container. Not stored in the cassette, so replay can use a different one. Repeatable. |
| `--environment-kwarg KEY=VALUE` | Passed to Harbor's environment. `extra_docker_compose=[...]` maps to Harbor's `--extra-docker-compose`. `VALUE` is parsed as JSON when possible. Repeatable. |
| `--extra-instruction TEXT` | Extra instruction appended to the task. Repeatable. |
| `--artifact PATH` | Container path that Harbor collects after the trial. Repeatable. |

Options for `record` only:

| Option | Meaning |
| --- | --- |
| `--model MODEL` | LiteLLM model name for an OpenAI-compatible API, e.g. `openai/gpt-4o-mini`. |
| `--upstream ORIGIN` | Provider origin, e.g. `https://api.openai.com`. No `/v1` or other path. |
| `--agent-kwarg KEY=VALUE` | Passed to Terminus-2, e.g. `temperature=0`. Stored in the cassette and reused by replay. Repeatable. |

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

The runner sets `api_base` to the local proxy and a fixed `session_id`, and turns off Terminus-2 summarization so the request bodies stay identical between record and replay. Do not pass your own `api_base` or `base_url`: model traffic would bypass the proxy and never be recorded.

The runner passes `--yes` to Harbor and closes child stdin, so a task's host environment templates are confirmed automatically.

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

## Replay

```bash
uv run agent-mitm-replay replay \
  --task /path/to/task \
  --cassette cassettes/task-a
```

Replay reads `manifest.json` and reuses the recorded model, upstream, and agent options. Artifacts and extra instructions also come from the manifest unless you pass `--artifact` or `--extra-instruction`. Mounts do not: pass `--mount` for the filesystem you want to test.

Replay needs no API key. The proxy answers every request from the cassette or with HTTP 400, and refuses recorded redirects, so it does not forward model calls to the provider. This assumes the agent sends its model calls to the proxy: do not set `api_base` or `llm_kwargs.base_url` yourself (see [Design and limitations](design.md#limitations)). The runner sets a placeholder `OPENAI_API_KEY`, sets `LITELLM_LOCAL_MODEL_COST_MAP=True` so LiteLLM does not download its price map, and adds `127.0.0.1,localhost` to `NO_PROXY` and `no_proxy`, keeping existing entries from both spellings, so an ambient HTTP proxy cannot intercept the local model hop.

## When a run passes

The runner reads Harbor's trial `result.json` instead of trusting Harbor's exit code, because Harbor can exit 0 when a trial errored. A run passes only if:

- the proxy saw no misses and no transport errors;
- in replay, every recorded response was used;
- Harbor wrote exactly one trial result, with no exception;
- every verifier reward is at least `--minimum-reward`.

## Reading the audit file

Every run writes `proxy-audit.jsonl`: one line per model call, then a summary.

```json
{"event": "response", "turn": 1, "request_sha256": "9f2c..."}
{"event": "summary", "matched": 0, "misses": 0, "remaining": 0, "total": 2, "requests": 2, "errors": 0}
```

| `event` | Meaning |
| --- | --- |
| `response` | Record: the provider answered this call and it was saved. |
| `hit` | Replay: an unused recording had exactly this request, and its response was returned. |
| `miss` | Replay: no unused recording matched; the agent got HTTP 400 and the trial fails. |
| `error` | The call failed in transit. |

`turn` counts model calls from 1. Compare the record and replay audits line by line: the first turn whose `request_sha256` differs, or whose event is `miss`, is where the agent's behavior diverged.

## Testing divergence

To confirm that replay catches a changed trajectory, change the instruction:

```bash
uv run agent-mitm-replay replay \
  --task /path/to/task \
  --cassette cassettes/task-a \
  --extra-instruction 'Change the task so the recorded request must miss.'
```

This must fail. The audit shows a `miss` at turn 1, because the first request already contains the changed instruction.

## Smoke test

```bash
uv run agent-mitm-replay smoke --output smoke-output
```

The smoke test copies `examples/smoke-task`, records it against a scripted local model server (no provider tokens), shuts the server down, and replays offline. It checks that:

- the record run serves exactly the two scripted model calls and gets reward `1.0`;
- replay passes with the model server gone, using every recorded response;
- record and replay both write the same 20 bytes to `/app/result.txt`;
- replay with a changed instruction fails with a `miss`.

It builds the task image from `ubuntu:24.04` and installs `tmux` with apt, so Docker needs network access for the first build.
