"""End-to-end smoke: record the example task against a scripted model, then replay it offline.

Runs real Harbor Terminus-2 trials in Docker. The scripted model server is a
local stand-in for the provider, so no API key or tokens are needed.
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


def _completion(analysis: str, commands: list[dict]) -> bytes:
    content = json.dumps({"analysis": analysis, "plan": "", "commands": commands, "task_complete": True})
    return json.dumps({
        "id": "chatcmpl-smoke", "object": "chat.completion", "created": 0, "model": "gpt-4o-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }).encode()


# Terminus-2 asks twice: once for the commands, once to confirm task_complete.
SCRIPT = [
    _completion("Write the file.", [{"keystrokes": "printf 'harbor-replay-smoke\\n' > /app/result.txt && cat /app/result.txt\n", "duration": 1.0}]),
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


def run_smoke(output: Path) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    task, cassette = output / "task", output / "cassette"
    shutil.copytree(EXAMPLE_TASK, task)
    env = {"OPENAI_API_KEY": "scripted-smoke-model"}

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedModel)
    server.requests = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        record = run_trial("record", task=task, cassette=cassette, model="openai/gpt-4o-mini",
                           upstream=f"http://127.0.0.1:{server.server_address[1]}",
                           agent_kwargs=AGENT_KWARGS, artifacts=[ARTIFACT], env=env)
    finally:
        server.shutdown()
        server.server_close()
    if server.requests != len(SCRIPT):
        raise ReplayError(f"Scripted model served {server.requests} requests, expected {len(SCRIPT)}")

    # The model server is gone from here on: replay must be fully offline.
    replay = run_trial("replay", task=task, cassette=cassette, output=output / "replay", env=env)
    for summary in (record, replay):
        if _artifact(summary) != EXPECTED_ARTIFACT:
            raise ReplayError(f"{summary['mode']} wrote {_artifact(summary)!r} to {ARTIFACT}")

    negative_output = output / "replay-changed-instruction"
    try:
        run_trial("replay", task=task, cassette=cassette, output=negative_output, env=env,
                  extra_instructions=["Do not create /app/result.txt; finish without running any command."])
    except ReplayError:
        pass
    else:
        raise ReplayError("Replay with a changed instruction passed; it must miss")
    audit = (negative_output / "proxy-audit.jsonl").read_text()
    if '"event": "miss"' not in audit:
        raise ReplayError(f"Changed-instruction replay failed without a miss: {negative_output}")

    summary = {"passed": True, "output": str(output), "model_requests": record["model_requests"],
               "artifact_bytes": len(EXPECTED_ARTIFACT), "record": record["output"], "replay": replay["output"],
               "changed_instruction_replay": str(negative_output)}
    (output / "smoke.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
