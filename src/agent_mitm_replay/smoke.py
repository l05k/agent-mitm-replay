"""End-to-end smoke run: real Docker, scripted upstream, offline replay.

``run_smoke`` writes a self-contained Harbor task, records one Terminus-2 trial
against a scripted OpenAI Chat Completions server that runs as a thread in this
process, shuts that server down, replays the same trial from the cassette
without any upstream, and finally replays it once more with a changed
instruction to prove that an unmatched request fails instead of reaching an
upstream.

The scripted upstream is a smoke fixture, not a production model: it stands in
for the provider so the workflow can be exercised without an API key. The model
calls are made by the Harbor process on the host, so the task container runs
with ``network_mode: none``. Everything the run produces (the generated task,
the cassette, the record and replay outputs, the proxy audits and the Harbor
logs) stays under *output* for inspection.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from .runner import ReplayError, run_trial

MODEL = "openai/gpt-4o-mini"
ARTIFACT_PATH = "/app/result.txt"
ARTIFACT_CONTENT = b"harbor-replay-smoke\n"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
TASK_NAME = "agent-mitm-replay-smoke"

#: The scripted upstream ignores auth, but LiteLLM still needs a key set.
SCRIPTED_API_KEY = "scripted-smoke-upstream"

#: Requests the scripted upstream must serve: the recording turn plus the
#: completion confirmation turn.
SCRIPTED_REQUESTS = 2

_CHANGED_INSTRUCTION = (
    "Additional requirement: /app/result.txt must not be created at all; finish "
    "immediately without running any command."
)

_WRITE_COMMAND = (
    "printf 'harbor-replay-smoke\\n' > /app/result.txt && cat /app/result.txt\n"
)

_INSTRUCTION = """# Write the smoke artifact

Create the file `/app/result.txt` whose entire contents are this single line:

```
harbor-replay-smoke
```

That is the 19 characters above followed by exactly one newline: 20 bytes in
total. Finish by running `cat /app/result.txt` so the contents appear in the
terminal.
"""

_DOCKERFILE = """FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

RUN apt-get update \\
 && apt-get install -y --no-install-recommends tmux \\
 && rm -rf /var/lib/apt/lists/*

# Deterministic prompt for the login shell Terminus-2 drives inside tmux.
RUN echo "if [ -f /root/.bashrc ]; then . /root/.bashrc; fi" > /root/.bash_profile \\
 && echo "export PS1='smoke:/app# '" >> /root/.bash_profile

WORKDIR /app
"""

#: Profile mounted over /root/.bash_profile when a prepared image is used; the
#: same two lines the example Dockerfile bakes into its image.
_PROFILE = """if [ -f /root/.bashrc ]; then . /root/.bashrc; fi
export PS1='smoke:/app# '
"""

_TEST_SH = """#!/bin/bash
# Verifier for the smoke task: /app/result.txt must hold the scripted artifact.
set -u

file=/app/result.txt
mkdir -p /logs/verifier

if [ -f "$file" ] && [ "$(cat "$file")" = "harbor-replay-smoke" ] \\
 && [ "$(wc -c < "$file" | tr -d ' ')" = "20" ]; then
    echo "pass: $file holds the 20 byte smoke line"
    echo 1 > /logs/verifier/reward.txt
else
    echo "fail: expected $file to hold exactly 'harbor-replay-smoke' (20 bytes)"
    ls -l "$file" || true
    echo 0 > /logs/verifier/reward.txt
fi
"""


def _completion(*, content: str, index: int) -> dict[str, Any]:
    """One OpenAI Chat Completions response carrying *content*."""
    return {
        "id": f"chatcmpl-agent-mitm-smoke-{index}",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL.split("/", 1)[1],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 512, "completion_tokens": 64, "total_tokens": 576},
    }


_SCRIPTED_RESPONSES: tuple[dict[str, Any], ...] = (
    _completion(
        content=json.dumps(
            {
                "analysis": (
                    "The task wants /app/result.txt to hold the single line "
                    "harbor-replay-smoke; nothing has been written yet."
                ),
                "plan": (
                    "Write the line with printf, then print the file with cat so "
                    "the result is visible."
                ),
                "commands": [{"keystrokes": _WRITE_COMMAND, "duration": 1.0}],
                "task_complete": True,
            }
        ),
        index=1,
    ),
    _completion(
        content=json.dumps(
            {
                "analysis": (
                    "cat printed harbor-replay-smoke, so /app/result.txt holds the "
                    "required line."
                ),
                "plan": "No further commands are needed; confirm completion.",
                "commands": [],
                "task_complete": True,
            }
        ),
        index=2,
    ),
)


class _ScriptedUpstream(ThreadingHTTPServer):
    """Loopback HTTP server that answers with a fixed sequence of completions."""

    daemon_threads = True

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(("127.0.0.1", 0), _CompletionHandler)
        self.responses = responses
        self.lock = threading.Lock()
        self.requests = 0
        self.paths: list[str] = []


class _CompletionHandler(BaseHTTPRequestHandler):
    """Serve scripted completions and count the requests that reached us."""

    protocol_version = "HTTP/1.1"

    @property
    def _upstream(self) -> _ScriptedUpstream:
        return cast(_ScriptedUpstream, self.server)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            # Drain the body: the scripted upstream ignores model input.
            self.rfile.read(length)
        upstream = self._upstream
        with upstream.lock:
            index = upstream.requests
            upstream.requests += 1
            upstream.paths.append(self.path)
        if self.path.rstrip("/") != CHAT_COMPLETIONS_PATH:
            self._send_json(
                404,
                {
                    "error": {
                        "type": "not_found",
                        "message": f"scripted upstream serves {CHAT_COMPLETIONS_PATH}",
                    }
                },
            )
            return
        if index >= len(upstream.responses):
            self._send_json(
                500,
                {
                    "error": {
                        "type": "upstream_exhausted",
                        "message": (
                            f"the scripted upstream has no response left for "
                            f"request {index + 1}"
                        ),
                    }
                },
            )
            return
        self._send_json(200, upstream.responses[index])

    def do_GET(self) -> None:
        self._send_json(
            405,
            {
                "error": {
                    "type": "method_not_allowed",
                    "message": f"scripted upstream serves {CHAT_COMPLETIONS_PATH}",
                }
            },
        )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the scripted upstream quiet; the audit lives in the proxy log."""


def _start_upstream() -> _ScriptedUpstream:
    upstream = _ScriptedUpstream(list(_SCRIPTED_RESPONSES))
    threading.Thread(
        target=upstream.serve_forever, name="smoke-upstream", daemon=True
    ).start()
    return upstream


def _stop_upstream(upstream: _ScriptedUpstream) -> None:
    upstream.shutdown()
    upstream.server_close()


def _upstream_origin(upstream: _ScriptedUpstream) -> str:
    host, port = upstream.server_address[:2]
    return f"http://{host}:{port}"


def _upstream_requests(upstream: _ScriptedUpstream) -> int:
    with upstream.lock:
        return upstream.requests


def _require_upstream_requests(
    upstream: _ScriptedUpstream, expected: int, stage: str
) -> None:
    served = _upstream_requests(upstream)
    if served != expected:
        raise ReplayError(
            f"the scripted upstream served {served} request(s) after {stage}, "
            f"expected {expected}"
        )


def _agent_kwargs() -> dict[str, object]:
    """Terminus-2 options shared by the recording and both replays."""
    return {
        "max_turns": 4,
        "enable_summarize": False,
        "record_terminal_session": False,
        "llm_kwargs": {"num_retries": 0},
    }


def _child_env() -> dict[str, str]:
    """Environment overrides for the Harbor processes of the smoke run."""
    return {
        "OPENAI_API_KEY": SCRIPTED_API_KEY,
        # Model traffic goes to the loopback proxy: never reroute it.
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }


def _task_toml(image: str | None) -> str:
    lines = [
        'schema_version = "1.4"',
        "",
        "[metadata]",
        f'name = "{TASK_NAME}"',
        'description = "Smoke task: write /app/result.txt for the record/replay check."',
        "",
        "[agent]",
        "timeout_sec = 600.0",
        "",
        "[verifier]",
        "timeout_sec = 120.0",
        "",
        "[environment]",
        'workdir = "/app"',
    ]
    if image is None:
        lines.append("build_timeout_sec = 900.0")
    else:
        lines.append(f"docker_image = {json.dumps(image)}")
    return "\n".join(lines) + "\n"


#: Compose overlay for the Dockerfile variant. The build itself keeps whatever
#: network the Docker daemon needs; the running container needs none, because
#: the Terminus-2 model calls are made by the Harbor process on the host.
_COMPOSE_ISOLATED = """services:
  main:
    network_mode: none
"""


def _compose_prebuilt(*, image: str, profile: Path) -> str:
    """Task compose for a prepared local image.

    The image is never pulled, the container has no network (the model calls
    happen on the host) and only the dedicated shell profile is bind-mounted;
    ``/app`` stays container-local and is created by the start command.
    """
    return (
        "services:\n"
        "  main:\n"
        f"    image: {json.dumps(image)}\n"
        "    pull_policy: never\n"
        "    network_mode: none\n"
        '    command: ["sh", "-c", "mkdir -p /app && exec sleep infinity"]\n'
        "    healthcheck:\n"
        '      test: ["CMD-SHELL", "test -d /app"]\n'
        "      interval: 1s\n"
        "      timeout: 5s\n"
        "      retries: 30\n"
        "      start_period: 1s\n"
        "    volumes:\n"
        "      - type: bind\n"
        f"        source: {json.dumps(str(profile))}\n"
        "        target: /root/.bash_profile\n"
        "        read_only: true\n"
    )


def _write_task(task: Path, *, image: str | None) -> None:
    """Materialize the smoke task, building from the example Dockerfile or
    starting from a prepared local image."""
    environment = task / "environment"
    tests = task / "tests"
    environment.mkdir(parents=True)
    tests.mkdir()
    (task / "instruction.md").write_text(_INSTRUCTION)
    (task / "task.toml").write_text(_task_toml(image))
    (tests / "test.sh").write_text(_TEST_SH)
    if image is None:
        (environment / "Dockerfile").write_text(_DOCKERFILE)
        (environment / "docker-compose.yaml").write_text(_COMPOSE_ISOLATED)
        return
    profile = environment / "bash_profile"
    profile.write_text(_PROFILE)
    profile.chmod(0o644)
    (environment / "docker-compose.yaml").write_text(
        _compose_prebuilt(image=image, profile=profile.resolve())
    )


def _read_audit_events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text().splitlines()
    except OSError as error:
        raise ReplayError(f"cannot read proxy audit {path}: {error}") from error
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as error:
            raise ReplayError(f"invalid audit line in {path}: {error}") from error
        if isinstance(event, dict):
            events.append(event)
    if not events:
        raise ReplayError(f"proxy audit {path} is empty")
    return events


def _audit_summary(events: list[dict[str, Any]], path: Path) -> dict[str, Any]:
    summaries = [event for event in events if event.get("event") == "summary"]
    if len(summaries) != 1:
        raise ReplayError(
            f"proxy audit {path} has {len(summaries)} shutdown summaries, expected 1"
        )
    return summaries[0]


def _recorded_request_hashes(record_audit: Path) -> set[str]:
    return {
        str(event["request_sha256"])
        for event in _read_audit_events(record_audit)
        if event.get("event") == "response" and "request_sha256" in event
    }


def _trial_exception(job: Path) -> str | None:
    """``"Type: message"`` of the recorded trial exception, when there is one."""
    results = sorted(job.glob("*/result.json"))
    if not results:
        return None
    try:
        result = json.loads(results[0].read_text())
    except (OSError, ValueError):
        return None
    info = result.get("exception_info")
    if not isinstance(info, dict):
        return None
    exception_type = info.get("exception_type")
    if not isinstance(exception_type, str):
        return None
    message = str(info.get("exception_message") or "")
    if len(message) > 200:
        message = message[:200] + "..."
    return f"{exception_type}: {message}" if message else exception_type


def _artifact_host_path(trial_dir: str) -> Path:
    """Collected artifact path: ``/app/result.txt`` -> ``artifacts/app/result.txt``."""
    return Path(trial_dir) / "artifacts" / ARTIFACT_PATH.lstrip("/")


def _read_artifact(trial_dir: str) -> bytes:
    path = _artifact_host_path(trial_dir)
    try:
        return path.read_bytes()
    except OSError as error:
        raise ReplayError(f"cannot read collected artifact {path}: {error}") from error


def _run_negative_replay(
    *,
    task: Path,
    cassette: Path,
    output: Path,
    record_audit: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    """Replay with a changed instruction and require a genuine cassette miss."""
    try:
        run_trial(
            "replay",
            task=task,
            cassette=cassette,
            output=output,
            agent_kwargs=_agent_kwargs(),
            extra_instructions=[_CHANGED_INSTRUCTION],
            env=env,
        )
    except ReplayError as error:
        message = str(error)
    else:
        raise ReplayError(
            "replay with a changed instruction succeeded; an unmatched request "
            "must fail instead of being served"
        )

    audit_path = output / "proxy-audit.jsonl"
    events = _read_audit_events(audit_path)
    misses = [event for event in events if event.get("event") == "miss"]
    hits = [event for event in events if event.get("event") == "hit"]
    if not misses or hits:
        raise ReplayError(
            f"changed-instruction replay must miss without hits: {audit_path} "
            f"({len(hits)} hit(s), {len(misses)} miss(es))"
        )
    status_codes = sorted({event.get("status_code") for event in misses})
    if status_codes != [400]:
        raise ReplayError(
            f"changed-instruction replay must answer every miss with HTTP 400, "
            f"got {status_codes} in {audit_path}"
        )
    if any(
        str(event.get("request_sha256")) in _recorded_request_hashes(record_audit)
        for event in misses
    ):
        raise ReplayError(
            "the changed instruction produced a request byte-identical to a "
            f"recorded one; the miss in {audit_path} is not a content mismatch"
        )
    trial_exception = _trial_exception(output / "jobs" / "trial")
    if trial_exception is None or not trial_exception.startswith("BadRequestError"):
        raise ReplayError(
            "the changed-instruction trial must fail with the provider "
            f"BadRequestError raised by the 400 miss, got {trial_exception!r} "
            f"(evidence: {audit_path})"
        )
    summary = _audit_summary(events, audit_path)
    return {
        "output": str(output),
        "requests": summary.get("requests"),
        "hits": len(hits),
        "misses": len(misses),
        "status_codes": status_codes,
        "error": message,
        "trial_exception": trial_exception,
    }


def run_smoke(output: Path, *, image: str | None = None) -> dict[str, Any]:
    """Run the scripted record/replay smoke test against real Docker.

    Records one Terminus-2 trial with a scripted upstream, replays it offline,
    verifies the collected artifact and both rewards, and replays once more with
    a changed instruction to confirm that a cassette miss fails the trial. The
    generated task, cassette and all outputs are left under *output*, which must
    not exist yet.
    """
    if image is not None:
        image = image.strip()
        if not image:
            raise ReplayError("--image must name a prepared local image")
    resolved = Path(output).expanduser()
    if resolved.exists():
        raise ReplayError(
            f"output directory already exists: {resolved}; pass an empty --output path"
        )
    resolved = resolved.resolve()
    resolved.mkdir(parents=True)

    task = resolved / "task"
    _write_task(task, image=image)
    cassette = resolved / "cassette"
    record_output = resolved / "record"
    replay_output = resolved / "replays" / "positive"
    negative_output = resolved / "replays" / "negative-changed-instruction"

    upstream = _start_upstream()
    try:
        record = run_trial(
            "record",
            task=task,
            cassette=cassette,
            model=MODEL,
            upstream=_upstream_origin(upstream),
            output=record_output,
            agent_kwargs=_agent_kwargs(),
            artifacts=[ARTIFACT_PATH],
            env=_child_env(),
        )
    finally:
        _stop_upstream(upstream)

    unexpected = sorted(
        {
            path
            for path in upstream.paths
            if path.rstrip("/") != CHAT_COMPLETIONS_PATH
        }
    )
    if unexpected:
        raise ReplayError(f"the scripted upstream served unexpected paths: {unexpected}")
    _require_upstream_requests(upstream, SCRIPTED_REQUESTS, "the recording")

    replay = run_trial(
        "replay",
        task=task,
        cassette=cassette,
        output=replay_output,
        agent_kwargs=_agent_kwargs(),
        env=_child_env(),
    )
    artifacts: dict[str, bytes] = {}
    for label, summary in (("record", record), ("replay", replay)):
        artifacts[label] = _read_artifact(summary["trial_dir"])
        if artifacts[label] != ARTIFACT_CONTENT:
            raise ReplayError(
                f"the {label} container produced {artifacts[label]!r} for "
                f"{ARTIFACT_PATH}, expected {ARTIFACT_CONTENT!r}"
            )
        if summary["model_requests"] != SCRIPTED_REQUESTS:
            raise ReplayError(
                f"{label} used {summary['model_requests']} model request(s), "
                f"expected {SCRIPTED_REQUESTS}"
            )
        if summary["reward"] != 1.0:
            raise ReplayError(
                f"{label} reward is {summary['reward']!r}, expected 1.0"
            )
    _require_upstream_requests(upstream, SCRIPTED_REQUESTS, "the offline replay")

    negative = _run_negative_replay(
        task=task,
        cassette=cassette,
        output=negative_output,
        record_audit=record_output / "proxy-audit.jsonl",
        env=_child_env(),
    )
    _require_upstream_requests(
        upstream, SCRIPTED_REQUESTS, "the offline and changed-instruction replays"
    )

    summary = {
        "passed": True,
        "output": str(resolved),
        "task": str(task),
        "image": image,
        "model": MODEL,
        "cassette": str(cassette),
        "upstream_requests": _upstream_requests(upstream),
        "record": {
            "output": record["output"],
            "model_requests": record["model_requests"],
            "reward": record["reward"],
        },
        "replay": {
            "output": replay["output"],
            "model_requests": replay["model_requests"],
            "reward": replay["reward"],
        },
        "artifact": {
            "source_path": ARTIFACT_PATH,
            "bytes": len(artifacts["replay"]),
            "sha256": hashlib.sha256(artifacts["replay"]).hexdigest(),
            "record_host_path": str(_artifact_host_path(record["trial_dir"])),
            "replay_host_path": str(_artifact_host_path(replay["trial_dir"])),
        },
        "negative": negative,
    }
    (resolved / "smoke.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary