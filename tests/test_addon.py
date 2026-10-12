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

from agent_mitm_replay.addon import ReplayAddon

ADDON_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "agent_mitm_replay" / "addon.py"
)
CHAT_URL = "https://api.openai.com/v1/chat/completions"
BODY_A = '{"model": "m", "stream": true}'
BODY_B = '{"model": "m", "stream": false}'
RECORDED_DATE = "Wed, 01 Jan 2025 00:00:00 GMT"
UPSTREAM_REDIRECT_BODY = "<html>moved to another host</html>"


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


def redirect_flow(body: str, status: int, location: str) -> http.HTTPFlow:
    """A completed flow whose upstream answered with a redirect."""
    redirected = flow(body, UPSTREAM_REDIRECT_BODY, status=status)
    redirected.response.headers["location"] = location
    return redirected


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
            addon, amr_mode="replay", amr_flows=str(recorded), amr_audit=str(audit)
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
            addon, amr_mode="replay", amr_flows=str(recorded), amr_audit=str(audit)
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
            addon, amr_mode="replay", amr_flows=str(recorded), amr_audit=str(audit)
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
            addon, amr_mode="replay", amr_flows=str(recorded), amr_audit=str(audit)
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
            addon, amr_mode="replay", amr_flows=str(recorded), amr_audit=str(audit)
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
            tctx.configure(addon, amr_mode="replay", amr_audit=str(audit))

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
        tctx.configure(addon, amr_mode="record", amr_audit=str(audit))

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
                "amr_mode": "replay",
                "amr_flows": paths["valid"],
                "amr_audit": paths["audit"],
            }
            kwargs.update(overrides)
            with pytest.raises(exceptions.OptionsError):
                tctx.configure(addon, **kwargs)

        reject(amr_mode="nonsense")
        reject(amr_flows=None)  # replay needs a cassette
        reject(amr_audit=None)  # ... and an audit file
        reject(amr_flows="flows.mitm")  # paths must be absolute
        reject(amr_audit="audit.jsonl")
        reject(amr_flows=paths["missing"])  # not readable
        reject(amr_flows=paths["empty"])  # nothing to replay
        reject(server_replay_ignore_content=True)  # would match loosely

        # Nothing above installed a cassette, so requests stay refused ...
        refused = send(addon, flow(BODY_A))
        assert refused.response is not None
        assert refused.response.status_code == 503

        # ... while a valid configuration afterwards replays normally.
        tctx.configure(
            addon,
            amr_mode="replay",
            amr_flows=paths["valid"],
            amr_audit=paths["audit"],
        )
        assert send(addon, flow(BODY_A)).response.text == '{"ok": true}'


def test_replays_alongside_the_builtin_server_replay_addon(tmp_path):
    recorded = cassette(tmp_path / "flows.mitm", [flow(BODY_A, '{"ok": true}')])
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(
            addon, amr_mode="replay", amr_flows=str(recorded), amr_audit=str(audit)
        )
        assert send(addon, flow(BODY_A)).response.text == '{"ok": true}'

        # Turning on mitmproxy's own replay machinery is refused: it would
        # serve requests that this addon never audits.
        with pytest.raises(exceptions.OptionsError):
            tctx.configure(addon, server_replay=[str(recorded)])
        with pytest.raises(exceptions.OptionsError):
            tctx.configure(addon, server_replay_reuse=True)


def test_record_refuses_a_redirect_before_the_client_sees_it(tmp_path):
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(addon, amr_mode="record", amr_audit=str(audit))

        upstream = redirect_flow(BODY_A, 307, "https://redirected.example/v1/chat")
        send(addon, upstream)

        # The headers hook runs before mitmproxy decides how to deliver the
        # response, which is what keeps the redirect away from the client.
        addon.responseheaders(upstream)

        refusal = upstream.response
        assert refusal.status_code == 400
        assert "location" not in refusal.headers
        assert json.loads(refusal.text)["error"]["type"] == "unsafe_redirect"
        # Whatever upstream body mitmproxy feeds to the stream, the local
        # answer is what leaves; nothing of the redirect body is forwarded.
        assert refusal.stream(UPSTREAM_REDIRECT_BODY.encode()) == [refusal.raw_content]
        assert refusal.stream(b"more upstream body") == []

        # When the upstream body is buffered instead of streamed, mitmproxy
        # stores it in the response the client would get; the response hook
        # puts the refusal back without auditing it a second time.
        upstream.response.data.content = UPSTREAM_REDIRECT_BODY.encode()
        addon.response(upstream)
        assert upstream.response.status_code == 400
        assert "location" not in upstream.response.headers
        assert json.loads(upstream.response.text)["error"]["type"] == "unsafe_redirect"

        addon.done()

    lines = audit_lines(audit)
    assert [line["event"] for line in lines] == ["error", "summary"]
    assert lines[0]["error"] == "unsafe_redirect"
    assert lines[0]["request_sha256"] == sha256(BODY_A)
    assert lines[0]["response_sha256"] is None
    assert lines[0]["status_code"] == 400
    assert lines[0]["is_replay"] is False
    assert lines[0]["response_index"] is None
    assert lines[1] == {
        "event": "summary",
        "matched": 0,
        "misses": 0,
        "remaining": 0,
        "total": 1,
        "requests": 1,
        "errors": 1,
    }


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "location",
    [
        "https://redirected.example/v1/chat/completions",
        "/v1/chat/completions",
        "chat/completions",
    ],
)
def test_record_refuses_every_redirect_status(tmp_path, status, location):
    """Absolute and relative Locations are refused alike."""
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(addon, amr_mode="record", amr_audit=str(audit))

        upstream = redirect_flow(BODY_A, status, location)
        send(addon, upstream)
        addon.responseheaders(upstream)
        addon.response(upstream)

        assert upstream.response.status_code == 400
        assert "location" not in upstream.response.headers
        assert json.loads(upstream.response.text)["error"]["type"] == "unsafe_redirect"

        addon.done()

    lines = audit_lines(audit)
    assert [line["event"] for line in lines] == ["error", "summary"]
    assert lines[1]["errors"] == 1


def test_record_leaves_responses_a_client_cannot_follow_alone(tmp_path):
    """Only a redirect status with a Location is refused."""
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(addon, amr_mode="record", amr_audit=str(audit))

        # A 307 without a Location gives the client no address to follow.
        bare = flow(BODY_A, '{"answer": 1}', status=307)
        bare.response.headers["date"] = RECORDED_DATE
        send(addon, bare)
        addon.responseheaders(bare)
        assert bare.response.status_code == 307
        assert bare.response.stream is False
        addon.response(bare)
        assert bare.response.text == '{"answer": 1}'
        assert bare.response.headers["date"] == RECORDED_DATE

        # A 200 may carry a Location (the resource it created) and stays as is.
        created = flow(BODY_B, '{"answer": 2}')
        created.response.headers["location"] = "/v1/models/created"
        send(addon, created)
        addon.responseheaders(created)
        addon.response(created)
        assert created.response.status_code == 200
        assert created.response.headers["location"] == "/v1/models/created"
        assert created.response.text == '{"answer": 2}'

        addon.done()

    lines = audit_lines(audit)
    assert [line["event"] for line in lines] == ["response", "response", "summary"]
    assert [line["response_index"] for line in lines[:2]] == [0, 1]
    assert [line["status_code"] for line in lines[:2]] == [307, 200]
    assert lines[2]["total"] == 2
    assert lines[2]["errors"] == 0


def test_record_keeps_the_indices_of_the_responses_around_a_refusal(tmp_path):
    """A refusal is recorded like any response, so it takes the next index."""
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(addon, amr_mode="record", amr_audit=str(audit))

        first = flow(BODY_A, '{"answer": 1}')
        first.response.headers["date"] = RECORDED_DATE
        send(addon, first)
        addon.responseheaders(first)
        addon.response(first)
        assert first.response.text == '{"answer": 1}'
        assert first.response.headers["date"] == RECORDED_DATE

        refused = redirect_flow(BODY_B, 302, "https://redirected.example/v1/chat")
        send(addon, refused)
        addon.responseheaders(refused)
        refused.response.data.content = UPSTREAM_REDIRECT_BODY.encode()
        addon.response(refused)
        assert json.loads(refused.response.text)["error"]["type"] == "unsafe_redirect"

        third = flow(BODY_A, '{"answer": 2}')
        send(addon, third)
        addon.responseheaders(third)
        addon.response(third)

        addon.done()

    lines = audit_lines(audit)
    assert [line["event"] for line in lines] == [
        "response",
        "error",
        "response",
        "summary",
    ]
    assert [lines[0]["response_index"], lines[2]["response_index"]] == [0, 2]
    assert lines[0]["response_sha256"] == sha256('{"answer": 1}')
    assert lines[2]["response_sha256"] == sha256('{"answer": 2}')
    assert lines[3] == {
        "event": "summary",
        "matched": 0,
        "misses": 0,
        "remaining": 0,
        "total": 3,
        "requests": 3,
        "errors": 1,
    }


def test_replay_serves_recorded_headers_and_bytes_verbatim(tmp_path):
    """A hit is copied as recorded: no refresh(), no rewritten headers."""
    recorded_flow = flow(BODY_A, '{"ok": true}')
    recorded_flow.response.headers["date"] = RECORDED_DATE
    recorded = cassette(tmp_path / "flows.mitm", [recorded_flow])
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(
            addon, amr_mode="replay", amr_flows=str(recorded), amr_audit=str(audit)
        )

        hit = send(addon, flow(BODY_A))
        assert hit.response.status_code == 200
        assert hit.response.raw_content == b'{"ok": true}'
        assert hit.response.headers["date"] == RECORDED_DATE
        assert hit.is_replay == "response"

        addon.done()

    summary = audit_lines(audit)[-1]
    assert summary["errors"] == 0
    assert summary["matched"] == 1


@pytest.mark.parametrize("status", [301, 308])
def test_replay_refuses_a_cassette_that_still_holds_a_redirect(tmp_path, status):
    """A cassette from before the refusal must not serve the redirect."""
    recorded = cassette(
        tmp_path / "flows.mitm",
        [
            redirect_flow(
                BODY_A, status, "https://redirected.example/v1/chat/completions"
            )
        ],
    )
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(
            addon, amr_mode="replay", amr_flows=str(recorded), amr_audit=str(audit)
        )

        refused = send(addon, flow(BODY_A))
        assert refused.response.status_code == 400
        assert "location" not in refused.response.headers
        assert json.loads(refused.response.text)["error"]["type"] == "unsafe_redirect"
        assert not refused.is_replay

        addon.done()

    lines = audit_lines(audit)
    assert [line["event"] for line in lines] == ["error", "summary"]
    assert lines[0]["error"] == "unsafe_redirect"
    assert lines[0]["request_sha256"] == sha256(BODY_A)
    assert lines[0]["status_code"] == 400
    assert lines[1] == {
        "event": "summary",
        "matched": 0,
        "misses": 0,
        "remaining": 0,
        "total": 1,
        "requests": 1,
        "errors": 1,
    }


def test_replay_still_consumes_a_bucket_after_a_recorded_redirect(tmp_path):
    recorded = cassette(
        tmp_path / "flows.mitm",
        [
            redirect_flow(BODY_A, 302, "/v1/moved"),
            flow(BODY_A, '{"answer": "next"}'),
        ],
    )
    audit = tmp_path / "audit.jsonl"
    addon = ReplayAddon()
    with taddons.context(ServerPlayback(), addon) as tctx:
        tctx.configure(
            addon, amr_mode="replay", amr_flows=str(recorded), amr_audit=str(audit)
        )

        refused = send(addon, flow(BODY_A))
        assert refused.response.status_code == 400

        served = send(addon, flow(BODY_A))
        assert served.response.status_code == 200
        assert served.response.text == '{"answer": "next"}'
        assert served.is_replay == "response"

        addon.done()

    summary = audit_lines(audit)[-1]
    assert summary["errors"] == 1
    assert summary["matched"] == 1
    assert summary["misses"] == 0
    assert summary["remaining"] == 0
