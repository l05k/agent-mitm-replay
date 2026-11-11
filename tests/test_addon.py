"""Replay behavior of the mitmproxy addon, as seen by the model client.

An addon answers a request by setting ``flow.response`` in the ``request``
hook; mitmproxy only contacts the upstream when the response is still missing.
Replay serves the cassette as a fixed sequence, so every test asserts what the
client receives, in order.
"""

import asyncio
import json
from pathlib import Path

from mitmproxy import http
from mitmproxy import io as mitmproxy_io
from mitmproxy.test import taddons, tflow

from agent_mitm_replay.addon import ReplayAddon

BODY_A = '{"model": "m", "stream": true}'
BODY_B = '{"model": "m", "stream": false}'
URL = "https://api.openai.com/v1/chat/completions"


def request(body: str = BODY_A, method: str = "POST", url: str = URL) -> http.HTTPFlow:
    return tflow.tflow(req=http.Request.make(method, url, content=body.encode()))


def flow(body: str, response: str) -> http.HTTPFlow:
    resp = http.Response.make(200, response.encode(), {"content-type": "application/json"})
    return tflow.tflow(req=http.Request.make("POST", URL, content=body.encode()), resp=resp)


def redirect(body: str, status: int = 307) -> http.HTTPFlow:
    moved = flow(body, "moved")
    moved.response.status_code = status
    moved.response.headers["location"] = "https://elsewhere.example/v1/chat/completions"
    return moved


def cassette(path: Path, flows: list[http.HTTPFlow]) -> Path:
    with open(path, "wb") as fp:
        writer = mitmproxy_io.FlowWriter(fp)
        for f in flows:
            writer.add(f)
    return path


def audit_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def replay(addon, tmp_path, recorded: list[http.HTTPFlow], requests: list[http.HTTPFlow]) -> tuple[list[http.HTTPFlow], list[dict]]:
    flows_path, audit = cassette(tmp_path / "flows.mitm", recorded), tmp_path / "audit.jsonl"
    with taddons.context(addon) as tctx:
        tctx.configure(addon, amr_mode="replay", amr_flows=str(flows_path), amr_audit=str(audit))
        answered = []
        for f in requests:
            asyncio.run(addon.request(f))
            addon.response(f)  # mitmproxy fires it for locally set responses too
            answered.append(f)
        addon.done()
    return answered, audit_lines(audit)


def test_recorded_responses_are_served_once_in_cassette_order(tmp_path):
    recorded = [flow(BODY_A, '{"n": 1}'), flow(BODY_B, '{"n": 2}')]
    answered, lines = replay(ReplayAddon(), tmp_path, recorded, [request(), request(), request()])
    assert [f.response.text for f in answered[:2]] == ['{"n": 1}', '{"n": 2}']
    assert answered[0].is_replay == "response" and answered[1].is_replay == "response"
    assert answered[2].response.status_code == 400
    assert json.loads(answered[2].response.text)["error"]["type"] == "replay_miss"
    assert [line["event"] for line in lines] == ["hit", "hit", "miss", "summary"]
    assert lines[-1] == {"event": "summary", "matched": 2, "misses": 1, "remaining": 0,
                         "total": 2, "requests": 3, "errors": 0}


def test_replay_ignores_method_url_and_body(tmp_path):
    recorded = [flow(BODY_A, "a1"), flow(BODY_B, "b1"), flow(BODY_A, "a2")]
    answered, lines = replay(ReplayAddon(), tmp_path, recorded, [
        request("", "GET", "https://other.example/v1/models"),
        request('{"model": "m", "messages": []}'),
        request(BODY_A, "PUT", "https://api.openai.com/v1/responses"),
    ])
    assert [f.response.text for f in answered] == ["a1", "b1", "a2"]
    assert [line["event"] for line in lines] == ["hit", "hit", "hit", "summary"]


def test_empty_cassette_misses_without_forwarding(tmp_path):
    answered, lines = replay(ReplayAddon(), tmp_path, [], [request()])
    assert answered[0].response.status_code == 400
    assert json.loads(answered[0].response.text)["error"]["type"] == "replay_miss"
    assert lines == [{"event": "miss", "turn": 1},
                     {"event": "summary", "matched": 0, "misses": 1, "remaining": 0,
                      "total": 0, "requests": 1, "errors": 0}]


def test_unused_recordings_are_reported_as_remaining(tmp_path):
    recorded = [flow(BODY_A, "a"), flow(BODY_B, "b")]
    answered, lines = replay(ReplayAddon(), tmp_path, recorded, [request()])
    assert answered[0].response.text == "a"
    assert lines[-1]["remaining"] == 1 and lines[-1]["total"] == 2 and lines[-1]["matched"] == 1


def test_replay_refuses_a_recorded_redirect_and_consumes_it(tmp_path):
    recorded = [redirect(BODY_A), flow(BODY_B, "b")]
    answered, lines = replay(ReplayAddon(), tmp_path, recorded, [request(BODY_A), request(BODY_B)])
    refused = answered[0].response
    assert refused.status_code == 400 and "location" not in refused.headers
    assert json.loads(refused.text)["error"]["type"] == "unsafe_redirect"
    assert answered[1].response.text == "b"
    assert [line["event"] for line in lines] == ["error", "hit", "summary"]
    assert lines[-1]["errors"] == 1 and lines[-1]["remaining"] == 0


def test_record_audits_one_line_per_model_call(tmp_path):
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(addon) as tctx:
        tctx.configure(addon, amr_mode="record", amr_audit=str(audit))
        for body, latency in ((BODY_A, 1.0), (BODY_B, 3.0)):
            f = flow(body, "ok")
            f.request.timestamp_start = 90.0
            f.request.timestamp_end = 100.0
            f.response.timestamp_start = 100.5
            f.response.timestamp_end = 100.0 + latency
            asyncio.run(addon.request(f))
            assert f.response.text == "ok"  # untouched: mitmproxy forwards it upstream
            addon.response(f)
        failed = request(BODY_A)
        asyncio.run(addon.request(failed))
        assert failed.response is None  # record never answers locally
        addon.error(failed)
        addon.done()
    lines = audit_lines(audit)
    assert [(line["event"], line.get("turn")) for line in lines] == [
        ("response", 1), ("response", 2), ("error", 3), ("summary", None)]
    assert lines[-1] == {"event": "summary", "matched": 0, "misses": 0, "remaining": 0,
                         "total": 2, "requests": 3, "errors": 1, "mean_response_latency_sec": 2.0}


def test_record_fails_on_a_redirect(tmp_path):
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(addon) as tctx:
        tctx.configure(addon, amr_mode="record", amr_audit=str(audit))
        for f in (redirect(BODY_A, 301), flow(BODY_B, "ok")):
            asyncio.run(addon.request(f))
            addon.response(f)
        addon.done()
    lines = audit_lines(audit)
    assert [line["event"] for line in lines] == ["error", "response", "summary"]
    assert lines[-1]["errors"] == 1


def test_replay_withholds_response_during_nonblocking_delay(tmp_path, monkeypatch):
    async def scenario():
        sleeping, release = asyncio.Event(), asyncio.Event()
        original_sleep = asyncio.sleep

        async def controlled_sleep(seconds):
            assert seconds == 2.5
            sleeping.set()
            await release.wait()

        monkeypatch.setattr(asyncio, "sleep", controlled_sleep)
        addon = ReplayAddon()
        path = cassette(tmp_path / "flows.mitm", [flow(BODY_A, "answer")])
        with taddons.context(addon) as tctx:
            tctx.configure(addon, amr_mode="replay", amr_flows=str(path), amr_response_delay_sec="2.5")
            pending = request()
            task = asyncio.create_task(addon.request(pending))
            await sleeping.wait()
            try:
                assert pending.response is None
                assert addon.matched == 0
                # Other event-loop work can proceed while the response is withheld.
                await original_sleep(0)
            finally:
                release.set()
                await task
            assert pending.response.text == "answer"
            assert addon.matched == 1

    asyncio.run(scenario())
