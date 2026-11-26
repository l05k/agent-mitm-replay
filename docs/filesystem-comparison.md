# Comparing filesystems

Preselect tasks, harnesses and a concrete model. Capture each task-harness pair
once on a reference ext4 workspace restored from a pristine snapshot. Keep a
single task definition, container layout and pinned image per pair. Bind every
measured work/data path; Docker root, Harbor's agent installation directories
and logs stay off the measured mount. A bind mount hides image files below its
destination. Include hidden files, symlinks and repository state in the
snapshot.

Before each replay, restore the same initial data in a fresh directory on the
tested filesystem. The runner mounts directories but does not snapshot or
restore them; restoration and task-environment setup remain caller-managed.
Restoration reads/writes data and changes cache state. Keep the same restore
procedure across backends; do not describe the result as cold-cache.

The runner compares the standard Harbor trajectory's tool names and arguments
only; it does not compare tool outputs, observations or filesystem effects, and
reward is not an inclusion criterion. Compare `tool_s` (sum of native tool
durations) and `agent_execution_s` (whole agent phase) separately across
accepted replays only, not capture runs or `harbor_s`. Keep and report
uncaptured tasks, divergence, runtime errors and timeouts. Do not describe the
result as an exact workload.

See [Usage](usage.md) for the commands and [Timing and validation](design.md)
for the exact boundaries. In a paper use per-task paired ratios on a common
valid set, not ratios of aggregate medians or unpaired successes.
