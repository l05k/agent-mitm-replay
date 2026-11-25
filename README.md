# agent-mitm-replay

Record and replay the model HTTP exchanges of Harbor's built-in Codex,
OpenClaw and Pi agents on a real task. Recording sends model requests to an
OpenAI-compatible endpoint; replay serves the recorded responses locally so the
same Harbor task can run again without a provider.

The reported timing metric is Harbor's `agent_execution` phase
(`agent_execution_s`). It includes the agent's model calls, tool work and
harness overhead; it is not native tool wall time or pure model latency.
`harbor_s` and the individual `phase_seconds` are diagnostic context.

Replay requires every recorded model response to be consumed and compares the
standard Harbor trajectory's tool names and arguments. It does not compare tool
outputs, observations or call IDs, so the same tool calls with different
filesystem effects can pass. Verifier rewards are reported but do not gate a
trial. This is model-response replay, not an exact workload or syscall claim.

The only custom component in a measured trial is the HTTP record/replay proxy.
Harbor launches its built-in agent adapter, installs and configures the pinned
CLI in the container, writes the agent logs and `agent/trajectory.json`, and
runs the verifier. The task image must provide bash, curl, git and CA
certificates on a glibc-based distribution, and the environment must allow
Harbor's installer to reach nvm, Node.js and npm. "Offline replay" means no
model provider, not an air-gapped trial.

Requires a local Linux Docker daemon, Compose v2, Python 3.13 and Harbor 0.24.0.

```sh
uv sync --locked
uv run agent-mitm-replay --help
uv run --locked pytest
```

[Usage](docs/usage.md) · [Timing and validation](docs/design.md) ·
[Filesystem comparison](docs/filesystem-comparison.md) · [Security](SECURITY.md)
