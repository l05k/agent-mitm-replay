"""End-to-end smoke: record once, validate once, then measure an offline replay.

Runs three real Harbor Terminus-2 trials in Docker. The scripted model server is a
local stand-in for the provider during recording, so no API key or tokens are needed.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import threading

from .runner import ReplayError, run_trial

EXAMPLE_TASK = Path(__file__).resolve().parents[2] / "examples" / "smoke-task"
ARTIFACT = "/app/result.txt"
EXPECTED_ARTIFACT = b"harbor-replay-smoke\n"
AGENT_KWARGS = {"max_turns": 4, "llm_kwargs": {"num_retries": 0}}
INSTRUCTION_NOTE = (
    "\n\nSmoke note: this extra paragraph is appended to the task copy by the smoke run. "
    "It changes the request body but not the required commands.\n"
)
# Recorded and restored from the cassette, so later phases never need the source file.
COMPOSE_OVERLAY = """services:
  main:
    hostname: smoke-main
    environment:
      AMR_SMOKE_OVERLAY: "present"
"""

# The measured replay must never recheck rewards, so the verifier still exercises the
# artifact and then reports zero regardless of the outcome.
ZERO_REWARD_VERIFIER = """#!/bin/bash
set -u

file=/app/result.txt
mkdir -p /logs/verifier

if [ -f "$file" ] && [ "$(cat "$file")" = "harbor-replay-smoke" ] \\
 && [ "$(wc -c < "$file" | tr -d ' ')" = "20" ]; then
    echo "pass: $file still holds the 20 byte smoke line; reporting reward 0"
else
    echo "fail: expected $file to hold exactly 'harbor-replay-smoke' (20 bytes); reporting reward 0"
    ls -l "$file" || true
fi
echo 0 > /logs/verifier/reward.txt
"""


def _completion(analysis: str, commands: list[dict]) -> bytes:
    content = json.dumps({"analysis": analysis, "plan": "", "commands": commands, "task_complete": True})
    return json.dumps({
        "id": "chatcmpl-smoke", "object": "chat.completion", "created": 0, "model": "gpt-4o-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }).encode()


# Terminus-2 asks twice: once for the commands, once to confirm task_complete.
SCRIPT = [
    _completion("Write the file.", [{"keystrokes": "test \"$AMR_SMOKE_OVERLAY\" = present && printf 'harbor-replay-smoke\\n' > /app/result.txt && cat /app/result.txt\n", "duration": 1.0}]),
    _completion("The file is written.", []),
]


class _ScriptedModel(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        index = self.server.requests
        self.server.requests += 1
        body = SCRIPT[index] if index < len(SCRIPT) else b'{"error": {"message": "script exhausted"}}'
        self.send_response(200 if index < len(SCRIPT) else 500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def _artifact(summary: dict) -> bytes:
    return (Path(summary["trial_dir"]) / "artifacts" / ARTIFACT.lstrip("/")).read_bytes()


def _checked(summary: dict) -> None:
    """A record or validate result must be checked, rewarded, and not measured."""
    if not summary["checked"] or summary["measured"] or "reward" not in summary or "rewards" not in summary:
        raise ReplayError(f"{summary['mode']} summary lost its checked/rewarded shape: {summary}")


def run_smoke(output: Path) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    task, cassette = output / "task", output / "cassette"
    shutil.copytree(EXAMPLE_TASK, task)
    overlay = output / "compose-overlay.yaml"
    overlay.write_text(COMPOSE_OVERLAY)
    env = {"OPENAI_API_KEY": "scripted-smoke-model"}

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedModel)
    server.requests = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        record = run_trial("record", task=task, cassette=cassette, model="openai/gpt-4o-mini",
                           upstream=f"http://127.0.0.1:{server.server_address[1]}",
                           agent_kwargs=AGENT_KWARGS, artifacts=[ARTIFACT], env=env,
                           environment_kwargs={"extra_docker_compose": [str(overlay)]})
    finally:
        server.shutdown()
        server.server_close()
    if server.requests != len(SCRIPT):
        raise ReplayError(f"Scripted model served {server.requests} requests, expected {len(SCRIPT)}")
    _checked(record)
    recorded = json.loads((cassette / "commands.json").read_text())
    if not recorded or "harbor-replay-smoke" not in json.dumps(recorded):
        raise ReplayError(f"Recorded command trace does not contain the smoke command: {recorded}")
    overlay.unlink()  # Later phases must use the cassette copy.

    # The model server is gone from here on. A replay without the validation marker
    # must be refused before Harbor (and therefore Docker) is started.
    unvalidated = output / "replay-before-validation"
    try:
        run_trial("replay", task=task, cassette=cassette, output=unvalidated, env=env)
    except ReplayError:
        pass
    else:
        raise ReplayError("Replay without cassette/validation.json was accepted; it must be rejected")
    if (unvalidated / "harbor.log").exists() or (unvalidated / "jobs").exists():
        raise ReplayError("Unvalidated replay started Harbor instead of rejecting the cassette")

    # Different prompt text must change the request bodies without breaking validation.
    instruction = task / "instruction.md"
    instruction.write_text(instruction.read_text() + INSTRUCTION_NOTE)
    validation = run_trial("validate", task=task, cassette=cassette, output=output / "validation", env=env,
                           minimum_reward=0.5)
    _checked(validation)
    marker = cassette / "validation.json"
    if not marker.is_file():
        raise ReplayError(f"Validate did not write {marker}")
    if json.loads(marker.read_text())["minimum_reward"] != 0.5:
        raise ReplayError("Validation marker did not preserve the effective reward threshold")
    trajectory = json.loads((Path(validation["trial_dir"]) / "agent" / "trajectory.json").read_text())
    if INSTRUCTION_NOTE.strip() not in trajectory["steps"][0]["message"]:
        raise ReplayError("Validation prompt did not include the changed instruction")

    record_bytes = _artifact(record)
    if record_bytes != EXPECTED_ARTIFACT:
        raise ReplayError(f"Record wrote {record_bytes!r} to {ARTIFACT}")
    validation_bytes = _artifact(validation)
    if validation_bytes != record_bytes:
        raise ReplayError(f"Validate wrote {validation_bytes!r}, record wrote {record_bytes!r}")

    # Only the output task copy changes: the verifier still checks the artifact but
    # writes reward 0, and commands.json becomes unreadable to prove replay never
    # parses the saved action trace. The cassette is restored immediately afterward.
    (task / "tests" / "test.sh").write_text(ZERO_REWARD_VERIFIER)
    commands_path = cassette / "commands.json"
    commands_bytes = commands_path.read_bytes()
    commands_path.write_text("{not valid json")
    try:
        replay = run_trial("replay", task=task, cassette=cassette, output=output / "replay", env=env)
    finally:
        commands_path.write_bytes(commands_bytes)
    if replay["checked"] or not replay["measured"]:
        raise ReplayError(f"Replay summary is not measured-only: {replay}")
    if replay["elapsed_sec"] <= 0:
        raise ReplayError(f"Replay reported no Harbor lifetime: {replay}")
    if replay["phase_seconds"].get("verifier", 0) <= 0:
        raise ReplayError(f"Replay did not count verifier time: {replay}")
    if "reward" in replay or "rewards" in replay:
        raise ReplayError(f"Replay summary rechecked rewards: {replay}")
    replay_bytes = _artifact(replay)
    if replay_bytes != record_bytes:
        raise ReplayError(f"Replay wrote {replay_bytes!r}, record wrote {record_bytes!r}")

    # Harbor's own result must show the verifier ran and reported the forced zero.
    result_path = Path(replay["trial_dir"]) / "result.json"
    trial = json.loads(result_path.read_text())
    rewards = (trial.get("verifier_result") or {}).get("rewards") or {}
    if trial.get("exception_info") is not None or not rewards or any(value != 0 for value in rewards.values()):
        raise ReplayError(f"Harbor did not record a zero-reward verifier result: {result_path}")

    summary = {"passed": True, "output": str(output), "model_requests": record["model_requests"],
               "artifact_bytes": len(record_bytes), "record": record["output"],
               "validation": validation["output"], "replay": replay["output"],
               "elapsed_sec": replay["elapsed_sec"]}
    (output / "smoke.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
