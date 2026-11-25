"""Exercise Harbor's built-in agents with a local scripted model."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

from .runner import AGENTS, ReplayError, run_trial

COMMAND = "printf smoke > /app/result.txt; printf 'input: '; cat /app/input.txt"


def codex_response(request: dict, turn: int) -> list[dict]:
    response = {"id": f"resp_{turn}", "object": "response", "created_at": 0, "model": request["model"]}
    if turn == 0:
        item = {"type": "function_call", "id": "fc_smoke", "call_id": "call_smoke", "name": "exec_command",
                "arguments": json.dumps({"cmd": COMMAND, "yield_time_ms": 1000}), "status": "completed"}
    else:
        item = {"type": "message", "id": "msg_smoke", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Done.", "annotations": []}]}
    return [
        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {**response, "status": "completed", "output": [item],
         "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}}},
    ]


def chat_response(request: dict, turn: int) -> list[dict]:
    base = {"id": "chatcmpl_smoke", "object": "chat.completion.chunk", "created": 0, "model": request["model"]}
    delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_smoke", "type": "function",
             "function": {"name": next(tool["function"]["name"] for tool in request["tools"]
                                        if tool["function"]["name"] in {"exec", "bash"}),
                          "arguments": json.dumps({"command": COMMAND})}}]} if turn == 0 else {
             "role": "assistant", "content": "Done."}
    return [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if turn == 0 else "stop"}]}]


class ScriptedModel(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not self.path.startswith('/v1/models'):
            self.send_error(404, self.path)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"object":"list","data":[],"models":[]}')

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        turn = self.server.turns
        self.server.turns += 1
        if turn >= 2 or self.path not in {'/v1/responses', '/v1/chat/completions'}:
            self.send_error(400, f'unexpected smoke turn {turn}: {self.path}')
            return
        codex = self.path.endswith('/responses')
        events = (codex_response if codex else chat_response)(body, turn)
        data = ''.join((f'event: {event["type"]}\n' if codex else '') + f'data: {json.dumps(event)}\n\n'
                       for event in events)
        payload = (data + ('' if codex else 'data: [DONE]\n\n')).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


def run_smoke(output: Path, image: str, *, agents: list[str] | None = None,
              install_mirrors: dict[str, str] | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    task = output / 'task'
    (task / 'tests').mkdir(parents=True)
    (task / 'environment').mkdir()
    (task / 'instruction.md').write_text('Use the shell in /app to create result.txt containing smoke.')
    (task / 'task.toml').write_text(f'''schema_version = "1.4"
[environment]
docker_image = {json.dumps(image)}
workdir = "/app"
[agent]
timeout_sec = 120
[verifier]
timeout_sec = 30
''')
    (task / 'tests' / 'test.sh').write_text(
        '#!/bin/bash\nset -e\ntest "$(cat /app/result.txt)" = smoke\necho 0 > /logs/verifier/reward.txt\n')
    results = {}
    for agent in agents or AGENTS:
        root = output / agent
        for name in ('capture', 'replay'):
            work = root / name
            work.mkdir(parents=True)
            (work / 'input.txt').write_text('same\n')
        cassette = root / 'cassette'
        with ThreadingHTTPServer(('127.0.0.1', 0), ScriptedModel) as server:
            server.turns = 0
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                capture = run_trial('record', agent=agent, model='openai/gpt-5.4', task=task, cassette=cassette,
                                    upstream=f'http://127.0.0.1:{server.server_port}/v1',
                                    mounts=[(str(root / 'capture'), '/app')], env={'OPENAI_API_KEY': ''},
                                    install_mirrors=install_mirrors)
            finally:
                server.shutdown()
                thread.join()
            if server.turns != 2:
                raise ReplayError(f'{agent} smoke expected two model calls, got {server.turns}')
        replay = run_trial('replay', task=task, cassette=cassette,
                           mounts=[(str(root / 'replay'), '/app')], env={'OPENAI_API_KEY': ''})
        if (capture['rewards'] != {'reward': 0.0} or replay['rewards'] != capture['rewards']
                or (root / 'replay' / 'result.txt').read_bytes() != b'smoke'
                or not 0 < replay['agent_execution_s'] < replay['harbor_s']):
            raise ReplayError(f'{agent} failed replay metric or artifact check: {replay}')
        results[agent] = {'agent_execution_s': replay['agent_execution_s'],
                          'model_requests': replay['model_requests']}
    return results
