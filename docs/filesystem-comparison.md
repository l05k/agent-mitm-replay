# Comparing filesystems

Measure the effect of a task-data filesystem on Harbor end-to-end completion time while replaying a fixed sequence of model responses. The Agent still runs through Harbor; this is not a separate shell-script or syscall replay engine.

## One-time acceptance, then measurements

1. Prepare a fixed task/image and record one successful trial.
2. Restore initial task data and run `validate` once. It checks task success, response consumption, and command-trajectory equality, then writes the acceptance marker. Exclude this warm-up/acceptance run from performance samples.
3. Before each measured `replay`, restore the same initial data to the selected NAS. The tool only mounts it; it never copies, resets, or formats data.
4. Keep the container mount paths and runtime configuration fixed. Change only the host directories that select the tested filesystem.

```bash
# Prepare /mnt/reference/task-a with the task's initial inputs before recording.
uv run agent-mitm-replay record \
  --task /path/to/task --cassette cassettes/task-a \
  --model openai/gpt-4o-mini --upstream https://api.openai.com \
  --mount /mnt/reference/task-a:/app

# Restore /mnt/reference/task-a before acceptance.
uv run agent-mitm-replay validate \
  --task /path/to/task --cassette cassettes/task-a \
  --mount /mnt/reference/task-a:/app

# Restore initial inputs on NAS A before every measured invocation.
uv run agent-mitm-replay replay \
  --task /path/to/task --cassette cassettes/task-a \
  --mount /mnt/nas-a/task-a:/app

# Restore the same inputs on NAS B before every measured invocation.
uv run agent-mitm-replay replay \
  --task /path/to/task --cassette cassettes/task-a \
  --mount /mnt/nas-b/task-a:/app
```

An empty bind mount hides the image's existing task files. Populate it from the same initial task state, including hidden files such as `.git`, permissions, and links.

## What belongs on the NAS

Mount all task data/work directories, for example `/app` and `/data`. Keep the system image, Harbor installation, cassette, proxy/Harbor logs, and unrelated caches on local storage. Container files outside the selected mounts are not part of the tested filesystem. On another machine, host mount sources must exist on the Docker daemon host.

Bake tools and dependencies into the image and prepare it before measurements; avoid timed image builds and downloads. Pin the same image and Harbor/Agent versions. Store run outputs on local storage. Environment kwargs and copied Compose overlays come from the cassette; host mounts remain per-run inputs. Self-contained overlays avoid machine-specific external paths.

## What the result means

`elapsed_sec` measures Harbor startup through exit, including Agent work, verifier, and cleanup. Stage durations are also reported. Only replay runs are performance samples; record and validation involve different work.

The verifier still executes in measured runs and contributes I/O/time, but its reward is not rechecked. There is no per-run command comparison, syscall tracing, or workload certification. A successful one-time validation is assumed to transfer to the tested NAS configurations. Changing task/image or execution conditions can invalidate that assumption.

The proxy serves response N for request N, so different hostnames, timestamps, commit hashes, or terminal output no longer cause request-matching failures. This does **not** guarantee the same filesystem operations: shell branches, interactive programs, background work, or timing races can still change execution.

Recorded command durations remain fixed waits, not completion signals. A slow NAS can therefore change whether output or a foreground command has finished before the next keystrokes arrive. This tool deliberately does not patch Harbor to wait for command completion; do not describe the experiment as exact syscall replay or a closed-loop adaptive Agent benchmark.

Validation is not a cache-control mechanism. State whether measurements use warm/cold client and NAS caches, keep that policy consistent across filesystems, and repeat measured runs. Input restoration itself may warm caches; it is outside the reported timer but still part of the experimental setup.
