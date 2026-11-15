# agent-mitm-replay

Record model responses from Codex or OpenClaw in a real Harbor task, then run
and validate that task offline on a restored workspace. This measures **native
harness tool wall time**, not model or whole-agent latency.

- `record` captures the model exchanges and the actual tools and outcomes. Task
  reward is recorded but not used to select traces.
- `replay` reexecutes the tools and accepts a timing only when the request
  sequence and tool evidence match. Divergence, errors and timeouts are not
  timing samples.

Requires a local Linux Docker daemon, Compose v2, Python 3.13, Harbor 0.24.0,
and a task image containing the pinned native harness. The image and workspace
are prepared by the experimenter; the runner never installs an agent in a
measured trial.

```sh
uv sync --locked
uv run agent-mitm-replay --help
uv run --locked pytest
```

[Usage](docs/usage.md) · [Timing and validation](docs/design.md) ·
[Filesystem comparison](docs/filesystem-comparison.md) · [Security](SECURITY.md)
