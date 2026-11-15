"""Read actual harness tool events, not timings inferred from model turns."""

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re


class TraceError(ValueError):
    """The native tool evidence is incomplete or inconsistent."""


@dataclass(frozen=True)
class Tool:
    start_ns: int
    end_ns: int
    name: str
    arguments: object
    outcome: object


def _without_codex_timing(output: str) -> str:
    # unified_exec adds generated framing before the command's actual output.
    return re.sub(r"\AChunk ID: [0-9a-f]+\nWall time: [\d.]+ seconds\n", "", output)


def codex_log_records(payload: dict):
    for resource in payload.get("resourceLogs", []):
        for scope in resource.get("scopeLogs", []):
            yield from scope.get("logRecords", [])


def codex_event(record: dict) -> dict | None:
    attrs = {a["key"]: next(iter(a["value"].values())) for a in record.get("attributes", [])}
    if attrs.get("event.name") != "codex.tool_result":
        return None
    end = round(datetime.fromisoformat(attrs["event.timestamp"]).timestamp() * 1e9)
    return {
        "seq": int(attrs["tool_result_seq"]), "id": attrs["call_id"],
        "end_ns": end, "start_ns": end - int(attrs["duration_ms"]) * 1_000_000,
        "name": attrs["tool_name"], "namespace": attrs["tool_namespace"],
        "arguments": attrs["arguments"], "output": attrs["output"],
        "success": attrs["success"], "truncated": attrs["output_truncated"],
    }


def _records(path: Path) -> list[dict]:
    if not path.is_file():
        raise TraceError(f"Missing native tool log: {path}")
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def read_codex(path: Path) -> list[Tool]:
    log = _records(path)
    if not log or log[0] != {"event": "otel"}:
        raise TraceError("Codex did not export its native tool log")
    tools = sorted(log[1:], key=lambda event: event["seq"])
    result = []
    for i, event in enumerate(tools, 1):
        if event["seq"] != i or not event["id"]:
            raise TraceError("Codex tool result sequence is incomplete")
        if str(event["truncated"]).lower() != "false":
            raise TraceError(f"Codex tool output was truncated: {event['name']}")
        success = str(event["success"]).lower()
        if success not in {"true", "false"}:
            raise TraceError("Missing Codex tool status")
        arguments = event["arguments"]
        if arguments.startswith(("[", "{")):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                pass  # custom tools can accept raw, non-JSON input
        name = ".".join(filter(None, (event["namespace"], event["name"])))
        result.append(Tool(event["start_ns"], event["end_ns"], name, arguments,
                           {"ok": success == "true", "output": _without_codex_timing(event["output"])}))
    return result


def read_openclaw(path: Path) -> list[Tool]:
    log = _records(path)
    if not log or log[0] != {"event": "ready"}:
        raise TraceError("OpenClaw did not load the tool trace plugin")
    pending = {}
    tools = []
    for event in log[1:]:
        kind = event["event"]
        if kind == "ready":
            continue  # OpenClaw loads a registry for discovery and again for the agent.
        key = (event["run"], event["id"])
        if not key[1]:
            raise TraceError("OpenClaw tool event is missing its call identifier")
        if kind == "start":
            if key in pending:
                raise TraceError(f"Duplicate OpenClaw tool start: {key}")
            pending[key] = event["name"]
        elif kind == "end" and pending.pop(key, None) == event["name"]:
            duration, end = event["duration_ms"], event["end_ms"]
            if type(duration) is not int or type(end) is not int or duration < 0:
                raise TraceError("OpenClaw tool event is missing its native duration")
            if event["error"] is None and event["result"] is None:
                raise TraceError(f"OpenClaw tool outcome missing: {event['name']}")
            result = event["result"]
            if isinstance(result, dict) and "content" in result:
                details = result.get("details") or {}
                # Only model-facing content and explicit outcome fields are stable;
                # details also carry wall time, process IDs and session handles.
                result = {"content": result["content"], "exit_code": details.get("exitCode"),
                          "status": details.get("status"), "is_error": result.get("isError")}
            tools.append(Tool((end - duration) * 1_000_000, end * 1_000_000,
                              event["name"], event["arguments"],
                              {"error": event["error"], "result": result}))
        else:
            raise TraceError(f"Unmatched OpenClaw tool event: {kind} {key}")
    if pending:
        raise TraceError("OpenClaw returned without all tool outcomes")
    return tools


def signature(tools: list[Tool]) -> list[dict]:
    return [{"name": tool.name, "arguments": tool.arguments, "outcome": tool.outcome} for tool in tools]


def _union(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged = []
    for start, end in sorted(spans):
        if start < 0 or end < start:
            raise TraceError("Invalid native tool interval")
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(merged[-1][1], end)
        else:
            merged.append((start, end))
    return merged


# Delegation waits can enclose the model exchange of a child. Do not subtract
# that exchange from simultaneous concrete tools in the parent or another child.
_WAIT_TOOLS = {"wait", "wait_agent", "close_agent", "sessions_spawn", "sessions_yield", "subagents"}


def tool_seconds(tools: list[Tool], model_spans: list[tuple[int, int]]) -> float:
    waits = _union(model_spans)
    spans = []
    for tool in tools:
        start, end = tool.start_ns, tool.end_ns
        if start < 0 or end < start:
            raise TraceError("Invalid native tool interval")
        if tool.name.rsplit(".", 1)[-1] in _WAIT_TOOLS:
            for left, right in waits:
                if right <= start:
                    continue
                if left >= end:
                    break
                if left > start:
                    spans.append((start, left))
                start = min(end, right)
        spans.append((start, end))
    return sum(end - start for start, end in _union(spans)) / 1e9
