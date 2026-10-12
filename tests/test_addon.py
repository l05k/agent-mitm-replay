"""Tests for the record/replay mitmproxy addon.

The tests cover what a consumer of the addon can observe: which recorded
response is served for which request, what ends up in the audit file, and that
a replay never lets a request fall through towards the network.

The addon is exercised together with mitmproxy's own ``ServerPlayback`` addon,
because that addon registers the ``server_replay_*`` options the matching
borrows -- exactly as in a stock mitmdump run.

Note that an addon answers a request by setting ``flow.response`` in the
``request`` hook; mitmproxy only dials the upstream when that response is still
missing afterwards (``proxy/layers/http``: ``elif self.flow.response:`` sends
the response, the ``make_server_connection()`` branch is the else branch).
"""

import hashlib
import json
from pathlib import Path

import pytest

from mitmproxy import exceptions
from mitmproxy import http
from mitmproxy import io as mitmproxy_io
from mitmproxy.addons.script import load_script
from mitmproxy.addons.serverplayback import ServerPlayback
from mitmproxy.test import taddons
from mitmproxy.test import tflow

from harbor_mitm_replay.addon import ReplayAddon

ADDON_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "harbor_mitm_replay" / "addon.py"
)
CHAT_URL = "https://api.openai.com/v1/chat/completions"
BODY_A = '{"model": "m", "stream": true}'
BODY_B = '{"model": "m", "stream": false}'


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request(body: str) -> http.Request:
    return http.Request.make(
        "POST",
        CHAT_URL,
        content=body.encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def flow(body: str, response: str | None = None, status: int = 200) -> http.HTTPFlow:
    resp: http.Response | bool = False
    if response is not None:
        resp = http.Response.make(
            status, response.encode("utf-8"), {"content-type": "application/json"}
        )
    return tflow.tflow(req=request(body), resp=resp)


def error_flow(body: str) -> http.HTTPFlow:
    return tflow.tflow(req=request(body), err=True)


def cassette(path: Path, flows: list[http.HTTPFlow]) -> Path:
    with open(path, "wb") as fp:
        writer = mitmproxy_io.FlowWriter(fp)
        for f in flows:
            writer.add(f)
    return path


def audit_lines(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]


def send(addon, flow: http.HTTPFlow) -> http.HTTPFlow:
    """Run the request hook (the proxy answer happens right here)."""
    addon.request(flow)
    return flow


def test_mitmdump_entry_point_loads_and_replays(tmp_path):
    """addon.py must import standalone (mitmdump -s addon.py) and replay."""
    module = load_script(str(ADDON_PATH))
    assert module is not None, "addon.py must load without relative imports"
    addon = module.addons[0]

    recorded = cassette(tmp_path / "flows.mitm", [flow(BODY_A, '{"ok": true}')])
    audit = tmp_path / "audit.jsonl"
    with taddons.context(ServerPlayback(), module) as tctx:
        tctx.configure(
            addon, hrr_mode="replay", hrr_flows=str(recorded), hrr_audit=str(audit)
        )

        hit = send(addon, flow(BODY_A))
        assert hit.response is not None and hit.response.status_code == 200
        assert hit.response.text == '{"ok": true}'
        assert hit.is_replay == "response"

        miss = send(addon, flow(BODY_B))
        assert miss.response is not None and miss.response.status_code == 400
        assert not miss.is_replay

        addon.done()

    assert [line["event"] for line in audit_lines(audit)] == ["hit", "miss", "summary"]


def test_replay_serves_recorded_responses_in_order(tmp_path):
    """Two responses for the same request are consumed one by one, then 400."""
    recorded = cassette(
        tmp_path / "flows.mitm",
        [
            flow(BODY_A, '{"answer": 1}'),
            flow(BODY_A, '{"answer": 2}'),
        ],
    )
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(
            addon, hrr_mode="replay", hrr_flows=str(recorded), hrr_audit=str(audit)
        )

        first = send(addon, flow(BODY_A))
        assert first.response.status_code == 200
        assert first.response.text == '{"answer": 1}'
        assert first.response.headers["content-type"] == "application/json"
        assert first.is_replay == "response"

        second = send(addon, flow(BODY_A))
        assert second.response is not None
        assert second.response.text == '{"answer": 2}'
        assert second.is_replay == "response"

        # The bucket is empty: answered locally, never forwarded upstream.
        third = send(addon, flow(BODY_A))
        assert third.response is not None
        assert third.response.status_code == 400
        assert json.loads(third.response.text)["error"]["type"] == "replay_miss"
        assert not third.is_replay

        # The proxy also fires the response hook for the ingested response; a
        # replay must not audit it a second time.
        addon.response(first)

        addon.done()

    lines = audit_lines(audit)
    assert [line["event"] for line in lines] == ["hit", "hit", "miss", "summary"]
    assert [line["response_index"] for line in lines[:2]] == [0, 1]
    assert [line["response_sha256"] for line in lines[:2]] == [
        sha256('{"answer": 1}'),
        sha256('{"answer": 2}'),
    ]
    assert lines[0]["request_sha256"] == sha256(BODY_A)
    assert lines[0]["status_code"] == 200
    assert lines[0]["is_replay"] is True
    assert lines[2] == {
        "event": "miss",
        "response_index": None,
        "request_sha256": sha256(BODY_A),
        "response_sha256": None,
        "status_code": 400,
        "is_replay": False,
    }
    assert lines[3] == {
        "event": "summary",
        "matched": 2,
        "misses": 1,
        "remaining": 0,
        "total": 2,
        "requests": 3,
        "errors": 0,
    }


def test_replay_consumes_each_bucket_in_recording_order(tmp_path):
    """Records are consumed per request hash, not in cassette-global order."""
    recorded = cassette(
        tmp_path / "flows.mitm",
        [
            flow(BODY_A, '{"which": "a1"}'),
            flow(BODY_B, '{"which": "b1"}'),
            flow(BODY_B, '{"which": "b2"}'),
            flow(BODY_A, '{"which": "a2"}'),
        ],
    )
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(
            addon, hrr_mode="replay", hrr_flows=str(recorded), hrr_audit=str(audit)
        )

        assert send(addon, flow(BODY_B)).response.text == '{"which": "b1"}'
        assert send(addon, flow(BODY_A)).response.text == '{"which": "a1"}'
        assert send(addon, flow(BODY_A)).response.text == '{"which": "a2"}'
        assert send(addon, flow(BODY_B)).response.text == '{"which": "b2"}'

        addon.done()

    lines = audit_lines(audit)
    assert [line["response_index"] for line in lines[:-1]] == [1, 0, 3, 2]
    assert lines[-1]["matched"] == 4
    assert lines[-1]["remaining"] == 0
    assert lines[-1]["total"] == 4
    assert lines[-1]["misses"] == 0
    assert lines[-1]["errors"] == 0


def test_replay_matches_the_raw_request_bytes(tmp_path):
    """JSON that differs only in formatting is a different request."""
    recorded = cassette(
        tmp_path / "flows.mitm", [flow('{"model": "m", "n": 1}', '{"ok": true}')]
    )
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(
            addon, hrr_mode="replay", hrr_flows=str(recorded), hrr_audit=str(audit)
        )

        for variant in ('{"n": 1, "model": "m"}', '{"model": "m",  "n": 1}'):
            miss = send(addon, flow(variant))
            assert miss.response is not None
            assert miss.response.status_code == 400
            assert not miss.is_replay

        hit = send(addon, flow('{"model": "m", "n": 1}'))
        assert hit.response.text == '{"ok": true}'

        addon.done()

    lines = audit_lines(audit)
    assert [line["event"] for line in lines] == ["miss", "miss", "hit", "summary"]
    assert lines[0]["request_sha256"] == sha256('{"n": 1, "model": "m"}')
    assert lines[2]["response_index"] == 0
    assert lines[3]["misses"] == 2
    assert lines[3]["matched"] == 1
    assert lines[3]["remaining"] == 0


def test_replay_reports_unconsumed_records(tmp_path):
    recorded = cassette(
        tmp_path / "flows.mitm",
        [flow(BODY_A, f'{{"answer": {index}}}') for index in range(3)],
    )
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(
            addon, hrr_mode="replay", hrr_flows=str(recorded), hrr_audit=str(audit)
        )

        assert send(addon, flow(BODY_A)).response.text == '{"answer": 0}'
        addon.done()

    summary = audit_lines(audit)[-1]
    assert summary["total"] == 3
    assert summary["matched"] == 1
    assert summary["remaining"] == 2
    assert summary["requests"] == 1
    assert summary["misses"] == 0


def test_replay_fails_closed_when_the_cassette_is_not_configured(tmp_path):
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        with pytest.raises(exceptions.OptionsError):
            tctx.configure(addon, hrr_mode="replay", hrr_audit=str(audit))

        refused = send(addon, flow(BODY_A))
        assert refused.response is not None, "a broken replay must not forward"
        assert refused.response.status_code == 503
        assert json.loads(refused.response.text)["error"]["type"] == (
            "replay_not_configured"
        )
        assert not refused.is_replay

    assert not audit.exists(), "no audit summary when replay never configured"


def test_record_audits_completed_responses_and_errors(tmp_path):
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(addon, hrr_mode="record", hrr_audit=str(audit))

        for body, response in ((BODY_A, '{"answer": 1}'), (BODY_B, '{"answer": 2}')):
            completed = flow(body, response)
            ingested = completed.response
            send(addon, completed)
            assert completed.response is ingested, "record must not touch the flow"
            addon.response(completed)

        broken = error_flow(BODY_A)
        send(addon, broken)
        addon.error(broken)

        # Lines are flushed per record, so they are readable before shutdown.
        assert [line["event"] for line in audit_lines(audit)] == [
            "response",
            "response",
            "error",
        ]

        addon.done()

    lines = audit_lines(audit)
    assert [line["response_index"] for line in lines[:2]] == [0, 1]
    assert lines[0]["request_sha256"] == sha256(BODY_A)
    assert lines[0]["response_sha256"] == sha256('{"answer": 1}')
    assert lines[0]["status_code"] == 200
    assert lines[0]["is_replay"] is False
    assert lines[1]["response_index"] == 1
    assert lines[1]["response_sha256"] == sha256('{"answer": 2}')
    assert lines[2]["event"] == "error"
    assert lines[2]["error"] == "error"
    assert lines[2]["response_sha256"] is None
    assert lines[3] == {
        "event": "summary",
        "matched": 0,
        "misses": 0,
        "remaining": 0,
        "total": 2,
        "requests": 3,
        "errors": 1,
    }


def test_configure_rejects_unusable_replay_configuration(tmp_path):
    paths = {
        "valid": str(cassette(tmp_path / "valid.mitm", [flow(BODY_A, '{"ok": true}')])),
        "empty": str(cassette(tmp_path / "empty.mitm", [error_flow(BODY_A)])),
        "missing": str(tmp_path / "missing.mitm"),
        "audit": str(tmp_path / "audit.jsonl"),
    }
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:

        def reject(**overrides) -> None:
            kwargs = {
                "hrr_mode": "replay",
                "hrr_flows": paths["valid"],
                "hrr_audit": paths["audit"],
            }
            kwargs.update(overrides)
            with pytest.raises(exceptions.OptionsError):
                tctx.configure(addon, **kwargs)

        reject(hrr_mode="nonsense")
        reject(hrr_flows=None)  # replay needs a cassette
        reject(hrr_audit=None)  # ... and an audit file
        reject(hrr_flows="flows.mitm")  # paths must be absolute
        reject(hrr_audit="audit.jsonl")
        reject(hrr_flows=paths["missing"])  # not readable
        reject(hrr_flows=paths["empty"])  # nothing to replay
        reject(server_replay_ignore_content=True)  # would match loosely

        # Nothing above installed a cassette, so requests stay refused ...
        refused = send(addon, flow(BODY_A))
        assert refused.response is not None
        assert refused.response.status_code == 503

        # ... while a valid configuration afterwards replays normally.
        tctx.configure(
            addon,
            hrr_mode="replay",
            hrr_flows=paths["valid"],
            hrr_audit=paths["audit"],
        )
        assert send(addon, flow(BODY_A)).response.text == '{"ok": true}'


def test_replays_alongside_the_builtin_server_replay_addon(tmp_path):
    recorded = cassette(tmp_path / "flows.mitm", [flow(BODY_A, '{"ok": true}')])
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(
            addon, hrr_mode="replay", hrr_flows=str(recorded), hrr_audit=str(audit)
        )
        assert send(addon, flow(BODY_A)).response.text == '{"ok": true}'

        # Turning on mitmproxy's own replay machinery is refused: it would
        # serve requests that this addon never audits.
        with pytest.raises(exceptions.OptionsError):
            tctx.configure(addon, server_replay=[str(recorded)])
        with pytest.raises(exceptions.OptionsError):
            tctx.configure(addon, server_replay_reuse=True)
