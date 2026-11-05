# Usage

## Commands

```text
agent-mitm-replay record   --task DIR --cassette DIR --model MODEL --upstream ORIGIN [options]
agent-mitm-replay validate --task DIR --cassette DIR [options]
agent-mitm-replay replay   --task DIR --cassette DIR [options]
agent-mitm-replay smoke    [--output DIR]
```

The lifecycle is **record → validate once → replay for measurements**. Record and validation times are not performance samples. Neither offline command needs an API key.

### Shared options

| Option | Meaning |
| --- | --- |
| `--task DIR` | Harbor task directory. Single-step tasks only. Use the same task and image throughout an experiment. |
| `--cassette DIR` | `record` creates a new cassette. `validate` and `replay` read it. |
| `--output DIR` | New output directory. Defaults to `CASSETTE/record`, `CASSETTE/validation`, or `CASSETTE/replays/<id>`. Must not exist. |
| `--mount HOST_DIR:CONTAINER_DIR` | Bind-mount task data into the container. Repeatable. Host paths are intentionally not saved: change them to select another NAS/filesystem, keeping container paths fixed. |

`--minimum-reward N` is available for `record` and `validate` only. Record defaults to `1.0`; validate defaults to the threshold saved during recording.

### Record-only options

| Option | Meaning |
| --- | --- |
| `--model MODEL` | LiteLLM OpenAI-compatible model name, e.g. `openai/gpt-4o-mini`. |
| `--upstream ORIGIN` | Provider origin, e.g. `https://api.openai.com`, without `/v1` or another path. |
| `--agent-kwarg KEY=VALUE` | Terminus-2 option, e.g. `reasoning_effort=max`. Repeatable. |
| `--environment-kwarg KEY=VALUE` | Harbor environment option. `extra_docker_compose=[...]` supplies Compose overlays. Repeatable. |
| `--extra-instruction TEXT` | Additional task instruction. Repeatable. |
| `--artifact PATH` | Container path collected by Harbor after the trial. Repeatable. |

These settings are restored automatically by validate/replay. Compose overlay files are copied into the cassette; use self-contained overlays, since copying does not relocate their external dependencies or rewrite relative paths. `VALUE` is parsed as JSON when possible.

## Record

```bash
export OPENAI_API_KEY=...
uv run agent-mitm-replay record \
  --task /path/to/task --cassette cassettes/task-a \
  --model openai/gpt-4o-mini --upstream https://api.openai.com
```

Record requires a successful Harbor trial with every reward at or above the threshold. It saves model responses, environment settings, task metadata, and the command trajectory. The command trajectory preserves tool names, arguments, ordering, and batches, including command durations; generated tool-call IDs and terminal output are excluded.

The runner sets a local proxy `api_base`, a stable `session_id`, and disables summarization and terminal-session recording by default. Do not override `api_base` or `llm_kwargs.base_url`: that would bypass recording. The runner uses Harbor `--yes` and closes child stdin, so host environment templates are confirmed automatically.

### Cassette layout

```text
cassettes/task-a/
├── flows.mitm          # model requests and responses; contains credentials
├── manifest.json       # schema 2: model, agent/environment settings, threshold
├── task.toml           # task metadata snapshot, including image/workdir if configured
├── commands.json       # recorded command batches used for one-time validation
├── environment/        # copied Compose overlays, when supplied
├── record/             # logs, Harbor jobs, summary.json
├── validation/         # logs, Harbor jobs, summary.json
├── validation.json     # written only after successful validate
└── replays/<id>/       # measured-run logs, Harbor jobs, summary.json
```

The task directory and image still come from `--task`; the metadata snapshot is not an image or task-data backup. Old schema-1 cassettes must be recorded again; there is no migration shim.

## Validate once

Restore the initial task data before validation, outside this tool's timer.

```bash
uv run agent-mitm-replay validate \
  --task /path/to/task --cassette cassettes/task-a
```

The proxy returns response N for model call N, regardless of the request body. Validation succeeds only when:

- every recorded response is consumed, without exhaustion or proxy errors;
- Harbor writes one single-step trial result without an exception;
- every verifier reward meets the threshold;
- the command trajectory equals the recording.

Success writes `validation.json`. Validation of an already accepted cassette is refused: subsequent runs use `replay`. A failed attempt writes no acceptance marker; use a new `--output` for a manual retry after correcting the environment. There are no automatic retries or provider fallbacks.

Acceptance is a one-time experimental assumption, not a proof that every future NAS run generates identical filesystem operations. Do not edit an accepted cassette or silently change its task, image, or agent setup; record a new trace instead.

## Measured replay

Restore the initial task data again before **each** measurement.

```bash
uv run agent-mitm-replay replay \
  --task /path/to/task --cassette cassettes/task-a \
  --mount /mnt/nas-a/task-a:/app
```

Replay requires `validation.json` before starting Harbor. It still executes the Agent, terminal commands, verifier, and artifact collection. It does **not** compare commands, check verifier rewards, or reject unused recorded responses. A reward of zero is retained in Harbor's raw result but is not an acceptance decision. Basic process/trial exceptions, proxy transport errors, and response exhaustion still fail the command rather than producing a normal performance result.

A measured summary includes:

- `checked: false`, `measured: true`;
- `elapsed_sec`: monotonic wall time from starting the Harbor child until it exits, including verifier, environment setup/cleanup, and Harbor's own output work;
- `phase_seconds`: Harbor's environment setup, agent setup/execution, and verifier durations, when present;
- `model_requests`, `output`, and `trial_dir`.

Measured summaries omit `reward` and `rewards`. Record/validate summaries have `checked: true`, `measured: false` and include rewards. Proxy startup/shutdown, runner post-processing, and caller-managed input restoration are outside `elapsed_sec`.

Compare **replay against replay**, never record against replay. See [Comparing filesystems](filesystem-comparison.md) for NAS preparation and timing limitations.

## Proxy audit

Every run writes events and final counters to `proxy-audit.jsonl`:

```json
{"event": "hit", "turn": 1}
{"event": "summary", "matched": 2, "misses": 0, "remaining": 0, "total": 2, "requests": 2, "errors": 0}
```

`response` means a provider response was recorded; `hit` means the next recorded response was served, **not** that prompts matched. `miss` means the response sequence was exhausted (local HTTP 400). `error` means a transport failure or refused redirect. Counters remain useful for diagnostics, but only validation requires complete consumption.

## Smoke test

```bash
uv run agent-mitm-replay smoke --output smoke-output
```

The smoke uses a scripted local provider and three real Docker trials, with no provider tokens. It checks:

- record serves two responses and writes the expected 20-byte artifact;
- measured replay is refused before validation;
- after the provider is stopped and the prompt is changed, validation still passes with the same commands and artifact;
- copied Compose configuration works after its source file is removed;
- measured replay skips command/reward acceptance, while the verifier actually executes and reports reward zero;
- the measured artifact remains correct and verifier time is included.

The first image build needs registry and apt access for Ubuntu 24.04 and tmux.
