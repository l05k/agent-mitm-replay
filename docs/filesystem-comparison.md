# Comparing filesystems

The main use case: record one trial on filesystem A, then replay the same model responses on filesystem B, and compare how the task behaves.

## Bind mounts

Create a separate, freshly initialized host directory for each filesystem and bind each one to the same container path. The example assumes the task's workspace is `/app`. Copy all task inputs into each host directory before running.

```bash
uv run agent-mitm-replay record \
  --task /path/to/task --cassette cassettes/task-a \
  --model openai/gpt-4o-mini --upstream https://api.openai.com \
  --environment-kwarg 'mounts=[{"type":"bind","source":"/mnt/fs-a/task-a","target":"/app"}]'

uv run agent-mitm-replay replay \
  --task /path/to/task --cassette cassettes/task-a \
  --environment-kwarg 'mounts=[{"type":"bind","source":"/mnt/fs-b/task-a","target":"/app"}]'
```

The tool does not format disks or reset existing files. Preparing each directory is up to you.

## Compose overlays

When mounts are not enough, pass Compose overlay files through to Harbor:

```bash
--environment-kwarg 'extra_docker_compose=["/absolute/overlay.yaml"]'
```

## Keep everything else fixed

Replay matches requests exactly, so anything that changes what the agent sees changes the next model request and causes a miss. Keep these the same between runs:

- container image
- task inputs
- working directory
- agent options
- locale
- shell prompt

This is strict request replay, not playback of recorded actions. If filesystem B produces different terminal output, the agent's next request differs from the recording and replay fails. That failure is a useful result: it tells you the filesystem change affected the agent's trajectory.

## Timing

Record and replay have very different model latency: replayed responses return almost instantly. So:

- compare filesystem variants against each other in replay mode, not a record run against a replay run;
- measure tool and filesystem time separately from model wait time.

Instant replay can also change when background shell commands finish relative to the agent's next step, which can change terminal output.
