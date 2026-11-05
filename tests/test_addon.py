"""Replay behavior of the mitmproxy addon, as seen by the model client.

An addon answers a request by setting ``flow.response`` in the ``request``
hook; mitmproxy only contacts the upstream when the response is still missing.
"""

import hashlib
import json
from pathlib import Path

from mitmproxy import http
from mitmproxy import io as mitmproxy_io
from mitmproxy.addons.script import load_script
from mitmproxy.addons.serverplayback import ServerPlayback
from mitmproxy.test import taddons, tflow

from agent_mitm_replay.addon import ReplayAddon

ADDON_PATH = Path(__file__).resolve().parents[1] / "src" / "agent_mitm_replay" / "addon.py"
BODY_A = '{"model": "m", "stream": true}'
BODY_B = '{"model": "m", "stream": false}'


def flow(body: str, response: str | None = None) -> http.HTTPFlow:
    request = http.Request.make("POST", "https://api.openai.com/v1/chat/completions", content=body.encode())
    resp = http.Response.make(200, response.encode(), {"content-type": "application/json"}) if response else False
    return tflow.tflow(req=request, resp=resp)


def cassette(path: Path, flows: list[http.HTTPFlow]) -> Path:
    with open(path, "wb") as fp:
        writer = mitmproxy_io.FlowWriter(fp)
        for f in flows:
            writer.add(f)
    return path


def audit_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def replay(addon, tmp_path, recorded: list[http.HTTPFlow], requests: list[str]) -> tuple[list[http.HTTPFlow], list[dict]]:
    flows_path, audit = cassette(tmp_path / "flows.mitm", recorded), tmp_path / "audit.jsonl"
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(addon, amr_mode="replay", amr_flows=str(flows_path), amr_audit=str(audit))
        answered = []
        for body in requests:
            f = flow(body)
            addon.request(f)
            addon.response(f)  # mitmproxy fires it for locally set responses too
            answered.append(f)
        addon.done()
    return answered, audit_lines(audit)


def test_mitmdump_script_loads_and_replays(tmp_path):
    module = load_script(str(ADDON_PATH))
    answered, lines = replay(module.addons[0], tmp_path, [flow(BODY_A, '{"ok": 1}')], [BODY_A, BODY_B])
    assert answered[0].response.text == '{"ok": 1}' and answered[0].is_replay == "response"
    assert answered[1].response.status_code == 400
    assert [line["event"] for line in lines] == ["hit", "miss", "summary"]


def test_each_recording_is_used_once_then_misses(tmp_path):
    recorded = [flow(BODY_A, '{"n": 1}'), flow(BODY_A, '{"n": 2}')]
    answered, lines = replay(ReplayAddon(), tmp_path, recorded, [BODY_A, BODY_A, BODY_A])
    assert [f.response.text for f in answered[:2]] == ['{"n": 1}', '{"n": 2}']
    assert answered[2].response.status_code == 400
    assert json.loads(answered[2].response.text)["error"]["type"] == "replay_miss"
    assert lines[-1] == {"event": "summary", "matched": 2, "misses": 1, "remaining": 0,
                         "total": 2, "requests": 3, "errors": 0}


def test_matching_is_fifo_per_request_not_global(tmp_path):
    recorded = [flow(BODY_A, "a1"), flow(BODY_B, "b1"), flow(BODY_B, "b2"), flow(BODY_A, "a2")]
    answered, _ = replay(ReplayAddon(), tmp_path, recorded, [BODY_B, BODY_A, BODY_A, BODY_B])
    assert [f.response.text for f in answered] == ["b1", "a1", "a2", "b2"]


def test_one_changed_byte_is_a_miss(tmp_path):
    answered, lines = replay(ReplayAddon(), tmp_path, [flow(BODY_A, "a")], [BODY_A.replace(" ", "")])
    assert answered[0].response.status_code == 400
    assert lines[0] == {"event": "miss", "turn": 1,
                        "request_sha256": hashlib.sha256(BODY_A.replace(" ", "").encode()).hexdigest()}
    assert lines[-1]["remaining"] == 1


def test_record_audits_one_line_per_model_call(tmp_path):
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(addon, amr_mode="record", amr_audit=str(audit))
        for body in (BODY_A, BODY_B):
            f = flow(body, "ok")
            addon.request(f)
            assert f.response.text == "ok"  # untouched: mitmproxy forwards it upstream
            addon.response(f)
        failed = flow(BODY_A)
        addon.request(failed)
        addon.error(failed)
        addon.done()
    lines = audit_lines(audit)
    assert [(line["event"], line.get("turn")) for line in lines] == [
        ("response", 1), ("response", 2), ("error", 3), ("summary", None)]
    assert lines[-1]["total"] == 2 and lines[-1]["errors"] == 1
