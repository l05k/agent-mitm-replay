"""Exact native per-tool timing for the pinned harnesses.

The harness owns the only trustworthy tool clock: Pi's extension events,
OpenClaw's ``after_tool_call`` duration, and Codex's ``codex.tool_result``
telemetry.  Nothing here infers a tool interval from model turns or wraps a
CLI/tool.  ``tool_seconds`` rejects evidence that does not cover the standard
Harbor trajectory one-to-one by tool-call id.
"""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any

PI_LOG_FILENAME = "amr-timing.jsonl"
OPENCLAW_LOG_FILENAME = "amr-timing.jsonl"
CODEX_LOG_FILENAME = "codex-tools.jsonl"
PI_EXTENSION_FILENAME = "amr-timing-pi.ts"
OPENCLAW_PLUGIN_FILENAME = "amr-timing-openclaw.mjs"


class TimingError(ValueError):
    """Native timing evidence is missing, incomplete, or inconsistent."""


_PINNED = {
    "pi": {"0.85.1"},
    "openclaw": {"2026.7.1"},
    "codex": {"0.169.0", "0.80.0"},
}

_PI_MOUNT = "/tmp/harbor-pi-agent/extensions/amr-timing.ts"
_OPENCLAW_MOUNT = "/opt/amr-timing/amr-timing.mjs"
_CODEX_COLLECTOR_PATH = "/amr-logs"


def _check_pair(agent: str, agent_version: str) -> None:
    if agent not in _PINNED:
        raise TimingError(f"Native tool timing is not defined for agent {agent!r}")
    if agent_version not in _PINNED[agent]:
        raise TimingError(
            f"Native tool timing is not validated for {agent} {agent_version!r}"
        )


def _stage_asset(output: Path, name: str, destination_name: str) -> Path:
    destination = output / "timing" / destination_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).with_name(name)
    if not source.is_file():
        raise TimingError(f"Bundled timing asset is missing: {source}")
    shutil.copyfile(source, destination)
    destination.chmod(0o444)
    return destination


def prepare_timing(
    output: Path,
    agent: str,
    agent_version: str,
    *,
    proxy_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Stage native timing hooks and return the agent integration needed.

    The result has exactly three keys: ``options`` (deep-merge into the same
    agent-kwarg values the caller already builds), ``env`` (extra agent
    environment), and ``mounts`` (``(host_source, container_target)`` read-only
    source/destination pairs).
    """
    agent_version = str(agent_version)
    _check_pair(agent, agent_version)
    output = Path(output)
    if not output.is_dir():
        raise TimingError(f"Timing output directory does not exist: {output}")

    options: dict[str, Any] = {}
    env: dict[str, str] = {}
    mounts: list[tuple[str, str]] = []

    if agent == "pi":
        extension = _stage_asset(output, PI_EXTENSION_FILENAME, "amr-timing.ts")
        mounts.append((str(extension), _PI_MOUNT))
    elif agent == "openclaw":
        plugin = _stage_asset(output, OPENCLAW_PLUGIN_FILENAME, "amr-timing.mjs")
        mounts.append((str(plugin), _OPENCLAW_MOUNT))
        manifest = _stage_asset(output, "openclaw.plugin.json", "openclaw.plugin.json")
        mounts.append((str(manifest), "/opt/amr-timing/openclaw.plugin.json"))
        options["openclaw_config"] = {
            "plugins": {
                "allow": ["amr-timing", "openai"],
                "load": {"paths": [_OPENCLAW_MOUNT]},
                "entries": {"amr-timing": {"enabled": True}},
            }
        }
    else:
        if not proxy_url or not token:
            raise TimingError(
                "Codex timing requires the proxy URL and trial token for its OTLP collector"
            )
        exporter = {
            "otlp-http": {
                "endpoint": proxy_url.rstrip("/") + _CODEX_COLLECTOR_PATH,
                "protocol": "json",
                "headers": {"authorization": f"Bearer {token}"},
            }
        }
        if agent_version == "0.169.0":
            options["config"] = {
                "otel": {
                    "exporter": exporter,
                    "log_sse_deltas": False,
                    "metrics_exporter": "none",
                    "tool_result": {"max_bytes": 1048576},
                }
            }
        else:
            # Codex 0.80 rejects the newer log/metrics keys and has no
            # [otel.tool_result] table; its native tool result still carries
            # duration_ms and call_id.
            options["config"] = {"otel": {"exporter": exporter}}

    return {"options": options, "env": env, "mounts": mounts}


def _trajectory_ids(trial_dir: Path) -> list[str]:
    path = trial_dir / "agent" / "trajectory.json"
    try:
        trajectory = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise TimingError(f"Missing Harbor trajectory: {path}") from error
    except (OSError, ValueError) as error:
        raise TimingError(f"Cannot read Harbor trajectory: {path}: {error}") from error
    if not isinstance(trajectory, dict):
        raise TimingError(f"Invalid Harbor trajectory: {path}")
    steps = trajectory.get("steps")
    if not isinstance(steps, list):
        raise TimingError(f"Harbor trajectory has no steps: {path}")
    ids: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            raise TimingError(f"Invalid Harbor trajectory step: {path}")
        calls = step.get("tool_calls")
        if calls is None:
            continue
        if not isinstance(calls, list):
            raise TimingError(f"Invalid Harbor trajectory tool_calls: {path}")
        for call in calls:
            if not isinstance(call, dict):
                raise TimingError(f"Invalid Harbor trajectory tool call: {path}")
            call_id = call.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise TimingError(f"Harbor tool call has no id: {path}")
            ids.append(call_id)
    return ids


def _records(path: Path, *, description: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError as error:
        raise TimingError(f"Missing native {description} log: {path}") from error
    except OSError as error:
        raise TimingError(f"Cannot read native {description} log: {path}: {error}") from error
    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise TimingError(
                f"Invalid JSON in native {description} log {path}:{number}: {error}"
            ) from error
        if not isinstance(record, dict):
            raise TimingError(f"Invalid native {description} record: {path}:{number}")
        records.append(record)
    if not records:
        raise TimingError(f"Empty native {description} log: {path}")
    return records


def _add_duration(
    durations: dict[str, int], call_id: object, duration: object, *, source: str
) -> None:
    if not isinstance(call_id, str) or not call_id:
        raise TimingError(f"{source} tool event is missing its call id")
    if call_id in durations:
        raise TimingError(f"Duplicate native tool result for {call_id}")
    if isinstance(duration, bool) or not isinstance(duration, (int, str)):
        raise TimingError(f"Native tool duration is missing for {call_id}")
    try:
        value = int(duration)
    except (TypeError, ValueError) as error:
        raise TimingError(f"Native tool duration is invalid for {call_id}") from error
    if value < 0:
        raise TimingError(f"Native tool duration is negative for {call_id}")
    durations[call_id] = value


def _read_pi(trial_dir: Path) -> dict[str, int]:
    path = trial_dir / "agent" / PI_LOG_FILENAME
    records = _records(path, description="Pi timing")
    if records[0].get("event") != "ready":
        raise TimingError(f"Pi timing extension did not initialise: {path}")
    durations: dict[str, int] = {}
    for record in records[1:]:
        event = record.get("event")
        if event == "ready":
            raise TimingError(f"Pi timing extension loaded more than once: {path}")
        if event == "error":
            raise TimingError(f"Pi timing hook error: {record.get('detail', path)}")
        if event != "tool":
            raise TimingError(f"Unknown Pi timing event: {event!r}")
        call_id = record.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise TimingError("Pi timing record is missing its tool call id")
        if call_id in durations:
            raise TimingError(f"Duplicate Pi timing record for {call_id}")
        duration = record.get("duration_ns")
        if duration is None:
            raise TimingError(
                "Pi timing record does not carry a completed native duration: "
                f"{call_id}"
            )
        _add_duration(durations, call_id, duration, source="Pi")
    return durations


def _read_openclaw(trial_dir: Path) -> dict[str, int]:
    path = trial_dir / "agent" / OPENCLAW_LOG_FILENAME
    records = _records(path, description="OpenClaw timing")
    ready = False
    durations: dict[str, int] = {}
    for record in records:
        event = record.get("event")
        if event == "ready":
            ready = True
            continue
        if event == "error":
            raise TimingError(f"OpenClaw timing hook error: {record.get('detail', path)}")
        if event != "tool":
            raise TimingError(f"Unknown OpenClaw timing event: {event!r}")
        _add_duration(durations, record.get("id"), record.get("duration_ns"),
                      source="OpenClaw")
    if not ready:
        raise TimingError(f"OpenClaw timing plugin did not initialise: {path}")
    return durations


def _codex_log(trial_dir: Path, native_log: Path | None) -> Path:
    if native_log is not None:
        return native_log
    local = trial_dir / "agent" / CODEX_LOG_FILENAME
    if local.is_file():
        return local
    # The existing proxy collector writes beside the Harbor output:
    # <output>/jobs/trial/<trial>/agent -> <output>/codex-tools.jsonl.
    if len(trial_dir.parents) >= 3:
        return trial_dir.parents[2] / "codex-tools.jsonl"
    return local


def _read_codex(path: Path, *, require_sequence: bool) -> tuple[bool, dict[str, int]]:
    """Return (evidence_seen, durations) from the OTLP collector log."""
    if not path.is_file():
        return False, {}
    records = _records(path, description="Codex timing")
    if records[0].get("event") != "otel":
        raise TimingError(f"Codex did not export its native tool telemetry: {path}")
    durations: dict[str, int] = {}
    expected_seq = 1
    for record in records[1:]:
        if record.get("event") not in (None, "tool"):
            raise TimingError(f"Unknown Codex timing event: {record.get('event')!r}")
        seq = record.get("seq")
        if require_sequence:
            if isinstance(seq, bool) or not isinstance(seq, int):
                raise TimingError("Codex tool telemetry is missing its sequence")
            if seq != expected_seq:
                raise TimingError("Codex tool telemetry sequence is incomplete")
            expected_seq += 1
        duration = record.get("duration_ns")
        if duration is None and "start_ns" in record and "end_ns" in record:
            start, end = record["start_ns"], record["end_ns"]
            if (isinstance(start, bool) or isinstance(end, bool)
                    or not isinstance(start, int) or not isinstance(end, int)):
                raise TimingError("Codex tool telemetry has invalid timestamps")
            duration = end - start
        _add_duration(durations, record.get("id"), duration, source="Codex")
    return True, durations


def _validate(durations: dict[str, int], trajectory_ids: list[str]) -> dict[str, Any]:
    if len(set(trajectory_ids)) != len(trajectory_ids):
        raise TimingError("Harbor trajectory repeats a tool-call id")
    expected, actual = set(trajectory_ids), set(durations)
    missing, extra = expected - actual, actual - expected
    if missing or extra:
        raise TimingError(
            "Native tool timing does not cover the Harbor trajectory: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )
    for duration in durations.values():
        if duration < 0:
            raise TimingError("Native tool duration is negative")
    total_ns = sum(durations.values())
    tool_s = total_ns / 1_000_000_000
    if not math.isfinite(tool_s):
        raise TimingError("Native tool duration is not finite")
    return {"tool_s": tool_s, "tool_calls": len(durations)}


def tool_seconds(
    trial_dir: Path,
    agent: str,
    agent_version: str,
    *,
    native_log: Path | None = None,
) -> dict[str, Any]:
    """Sum native per-tool durations and validate them against the trajectory.

    Parallel tool spans are summed per tool; overlapping time is counted once per
    tool (never unioned).  Model-turn time is not part of this value.
    """
    agent_version = str(agent_version)
    _check_pair(agent, agent_version)
    trial_dir = Path(trial_dir)
    trajectory_ids = _trajectory_ids(trial_dir)
    if agent == "pi":
        durations = _read_pi(trial_dir)
    elif agent == "openclaw":
        durations = _read_openclaw(trial_dir)
    else:
        log = _codex_log(trial_dir, native_log)
        seen, durations = _read_codex(
            log, require_sequence=agent_version == "0.169.0"
        )
        if not seen and trajectory_ids:
            raise TimingError(
                "Codex did not export native tool telemetry for a trial with tool calls"
            )
    return _validate(durations, trajectory_ids)
