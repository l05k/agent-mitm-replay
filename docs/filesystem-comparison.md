# Comparing filesystems

Preselect tasks and harnesses. Capture each pair once on a reference ext4
workspace from a pristine snapshot. Keep a single task definition, model,
container layout and pinned image per pair. Bind every measured work/data
path; Docker root, agent binaries and logs stay off the measured mount. A bind
mount hides image files below its destination. Include hidden files, symlinks
and repository state in the snapshot.

Before each replay, restore the same initial data in a fresh directory on the
tested filesystem. Restoration reads/writes data and changes cache state.
Keep the same restore procedure across backends; do not describe the result as
cold-cache. The runner verifies the observed tool sequence and outcomes on
every trial; reward is not an inclusion criterion. Compare `tool_s` across
accepted replays only, not capture runs or `harbor_s`. Keep and report
uncaptured tasks, divergence, runtime errors and timeouts.

See [Usage](usage.md) for the commands and [Timing and validation](design.md) for
exact boundaries. In a paper use per-task paired ratios on a common valid set,
not ratios of aggregate medians or unpaired successes.
