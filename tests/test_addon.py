import json
import gzip

import pytest

from mitmproxy import http, io
from mitmproxy.flow import Error
from mitmproxy.test import taddons, tflow

from agent_mitm_replay.addon import ReplayAddon


def model_flow(role='user', text='original', path='/v1/chat/completions'):
    return tflow.tflow(req=http.Request.make('POST', f'http://127.0.0.1{path}',
        json.dumps({'model': 'test', 'stream': True, 'messages': [{'role': role, 'content': text}]}).encode()))


def test_responses_match_route_and_shape_but_not_volatile_text(tmp_path):
    addon = ReplayAddon()
    cassette, audit = tmp_path / 'flows.mitm', tmp_path / 'audit.jsonl'
    with cassette.open('wb') as file:
        writer = io.FlowWriter(file)
        for role in ('user', 'tool'):
            flow = model_flow(role)
            flow.response = http.Response.make(200, role.encode())
            writer.add(flow)
    with taddons.context(addon) as settings:
        settings.configure(addon, amr_mode='replay', amr_flows=str(cassette),
                           amr_audit=str(audit), amr_model_path='/v1/chat/completions')
        wrong = model_flow(path='/v1/responses')
        addon.request(wrong)
        assert wrong.response.status_code == 404
        for role in ('tool', 'user'):
            flow = model_flow(role, text='generated observation')
            addon.request(flow)
            assert flow.response.get_text() == role
        exhausted = model_flow()
        addon.request(exhausted)
        assert exhausted.response.status_code == 400
        addon.done()
    summary = json.loads(audit.read_text().splitlines()[-1])
    assert (summary['matched'], summary['misses'], summary['remaining']) == (2, 1, 0)


def test_capture_injects_key_but_does_not_persist_it(tmp_path, monkeypatch):
    monkeypatch.setenv('AMR_TRIAL_TOKEN', 'trial-token')
    monkeypatch.setenv('AMR_UPSTREAM_KEY', 'not-a-real-key')
    addon, cassette = ReplayAddon(), tmp_path / 'flows.mitm'
    with taddons.context(addon) as settings:
        settings.configure(addon, amr_mode='record', amr_flows=str(cassette),
                           amr_model_path='/v1/chat/completions')
        unauthorized = model_flow()
        addon.request(unauthorized)
        assert unauthorized.response.status_code == 400
        flow = model_flow()
        flow.request.headers['authorization'] = 'Bearer trial-token'
        addon.request(flow)
        assert flow.request.headers['authorization'] == 'Bearer not-a-real-key'
        flow.response = http.Response.make(200, b'ok')
        addon.response(flow)
        addon.done()
    recorded, = io.read_flows_from_paths([str(cassette)])
    assert 'authorization' not in recorded.request.headers


def test_responses_provider_failure_retains_diagnostic_without_a_cassette_turn(tmp_path):
    addon = ReplayAddon()
    cassette, audit = tmp_path / 'flows.mitm', tmp_path / 'audit.jsonl'
    with taddons.context(addon) as settings:
        settings.configure(addon, amr_mode='record', amr_flows=str(cassette),
                           amr_audit=str(audit), amr_model_path='/v1/responses')
        flow = model_flow(path='/v1/responses')
        addon.request(flow)
        flow.response = http.Response.make(500, json.dumps({'error': {
            'message': 'not implemented', 'type': 'new_api_error', 'code': 'convert_request_failed'},
            'unrelated': 'not part of the diagnostic'}).encode(), {'content-type': 'application/json'})
        addon.response(flow)
        addon.done()
    events = [json.loads(line) for line in audit.read_text().splitlines()]
    assert events[0]['status'] == 500 and events[0]['path'] == '/v1/responses'
    assert events[0]['upstream_error'] == {
        'message': 'not implemented', 'type': 'new_api_error', 'code': 'convert_request_failed'}
    assert events[-1]['errors'] == 1
    assert list(io.read_flows_from_paths([str(cassette)])) == []


def test_websocket_upgrade_cannot_be_recorded_or_replayed(tmp_path):
    addon = ReplayAddon()
    cassette, audit = tmp_path / 'flows.mitm', tmp_path / 'audit.jsonl'
    upgrade = model_flow(path='/v1/responses')
    upgrade.response = http.Response.make(101, b'', {'connection': 'Upgrade', 'upgrade': 'websocket'})
    with taddons.context(addon) as settings:
        settings.configure(addon, amr_mode='record', amr_flows=str(cassette),
                           amr_audit=str(audit), amr_model_path='/v1/responses')
        addon.request(upgrade)
        addon.response(upgrade)
        addon.done()
    assert json.loads(audit.read_text().splitlines()[-1])['errors'] == 1
    assert list(io.read_flows_from_paths([str(cassette)])) == []
    with cassette.open('wb') as file:
        io.FlowWriter(file).add(upgrade)
    addon = ReplayAddon()
    with taddons.context(addon) as settings:
        settings.configure(addon, amr_mode='replay', amr_flows=str(cassette),
                           amr_audit=str(audit), amr_model_path='/v1/responses')
        request = model_flow(path='/v1/responses')
        addon.request(request)
        assert request.response.status_code == 400
        assert json.loads(request.response.get_text())['error']['type'] == 'unsupported_websocket'
        assert 'upgrade' not in request.response.headers
        addon.done()


@pytest.mark.parametrize('path,terminal', [
    ('/v1/chat/completions', b'data: [DONE]\r\n\r\n'),
    ('/v1/responses', b'event: response.completed\ndata: {"type":"response.completed"}\n\n'),
])
@pytest.mark.parametrize('compressed', [False, True])
def test_completed_sse_client_disconnect_is_recorded_and_replayable(tmp_path, path, terminal, compressed):
    addon = ReplayAddon()
    cassette, audit = tmp_path / 'flows.mitm', tmp_path / 'audit.jsonl'
    content = b'data: {"delta":"hello"}\n\n' + terminal
    wire = gzip.compress(content) if compressed else content
    with taddons.context(addon) as settings:
        settings.configure(addon, amr_mode='record', amr_flows=str(cassette),
                           amr_audit=str(audit), amr_model_path=path)
        flow = model_flow(path=path)
        flow.request.headers['authorization'] = 'Bearer secret'
        addon.request(flow)
        flow.response = http.Response.make(200, b'', {'content-type': 'text/event-stream',
                                                      'transfer-encoding': 'chunked'})
        if compressed:
            flow.response.headers['content-encoding'] = 'gzip'
        addon.responseheaders(flow)
        # mitmproxy has not assigned the streamed body when error() runs.
        flow.response.raw_content = None
        for offset in range(0, len(wire), 3):
            chunk = wire[offset:offset + 3]
            assert flow.response.stream(chunk) == chunk
        flow.error = Error('Client disconnected.')
        addon.error(flow)
        addon.response(flow)  # Defensive: do not record the turn twice.
        addon.done()
    saved, = io.read_flows_from_paths([str(cassette)])
    assert saved.response.raw_content == wire
    assert saved.response.content == content
    assert saved.error is None
    assert 'authorization' not in saved.request.headers
    assert 'transfer-encoding' not in saved.response.headers
    assert saved.response.headers['content-length'] == str(len(wire))
    summary = json.loads(audit.read_text().splitlines()[-1])
    assert summary['errors'] == 0 and summary['total'] == summary['requests'] == 1
    replay = ReplayAddon()
    with taddons.context(replay) as settings:
        settings.configure(replay, amr_mode='replay', amr_flows=str(cassette), amr_model_path=path)
        request = model_flow(text='different prompt', path=path)
        replay.request(request)
        assert request.response.content == content
        request.error = Error('Client disconnected.')
        replay.error(request)
        assert replay.errors == 0 and replay.matched == 1
        replay.done()


@pytest.mark.parametrize('body,error,status', [
    (b'data: {"delta":"partial"}\n\n', 'Client disconnected.', 200),
    (b'data: [DONE]\n', 'Client disconnected.', 200),
    (b'data: [DONE]\n\n', 'server connection reset', 200),
    (b'data: [DONE]\n\n', 'stream reset by client (CANCEL)', 200),
    (b'data: [DONE]\n\n', 'Client disconnected.', 500),
])
def test_incomplete_or_upstream_failed_sse_still_fails(tmp_path, body, error, status):
    addon = ReplayAddon()
    cassette, audit = tmp_path / 'flows.mitm', tmp_path / 'audit.jsonl'
    with taddons.context(addon) as settings:
        settings.configure(addon, amr_mode='record', amr_flows=str(cassette),
                           amr_audit=str(audit), amr_model_path='/v1/chat/completions')
        flow = model_flow()
        addon.request(flow)
        flow.response = http.Response.make(status, b'', {'content-type': 'text/event-stream'})
        addon.responseheaders(flow)
        flow.response.stream(body)
        flow.error = Error(error)
        addon.error(flow)
        addon.done()
    assert list(io.read_flows_from_paths([str(cassette)])) == []
    summary = json.loads(audit.read_text().splitlines()[-1])
    assert summary['errors'] == 1 and summary['total'] == 0


@pytest.mark.parametrize('encoding', ['gzip', 'deflate'])
def test_truncated_compression_with_terminal_sse_is_not_recorded(tmp_path, encoding):
    import zlib

    addon = ReplayAddon()
    cassette, audit = tmp_path / 'flows.mitm', tmp_path / 'audit.jsonl'
    with taddons.context(addon) as settings:
        settings.configure(addon, amr_mode='record', amr_flows=str(cassette),
                           amr_audit=str(audit), amr_model_path='/v1/chat/completions')
        flow = model_flow()
        addon.request(flow)
        flow.response = http.Response.make(200, b'', {
            'content-type': 'text/event-stream', 'content-encoding': encoding})
        addon.responseheaders(flow)
        content = b'data: [DONE]\n\n'
        wire = gzip.compress(content)[:-8] if encoding == 'gzip' else zlib.compress(content)[:-4]
        flow.response.stream(wire)
        flow.error = Error('Client disconnected.')
        addon.error(flow)
        addon.done()
    assert list(io.read_flows_from_paths([str(cassette)])) == []
    summary = json.loads(audit.read_text().splitlines()[-1])
    assert summary['errors'] == 1 and summary['total'] == 0


@pytest.mark.parametrize('native_seq', ['1', None])
def test_codex_telemetry_is_authenticated_and_not_recorded_as_model_exchange(tmp_path, monkeypatch, native_seq):
    monkeypatch.setenv('AMR_TRIAL_TOKEN', 'trial-token')
    addon = ReplayAddon()
    path = tmp_path / 'codex-tools.jsonl'
    attributes = {'event.name': 'codex.tool_result', 'event.timestamp': '2026-11-26T00:00:00Z',
                  'tool_result_seq': '1', 'call_id': 'call-1', 'duration_ms': '25',
                  'tool_name': 'exec_command', 'arguments': '{}', 'output': 'ok',
                  'success': 'true', 'output_truncated': 'false'}
    if native_seq is None:
        attributes.pop('tool_result_seq')  # Native Codex 0.80.0 has no sequence field.
    payload = {'resourceLogs': [{'scopeLogs': [{'logRecords': [{'attributes': [
        {'key': key, 'value': {'stringValue': value}} for key, value in attributes.items()]}]}]}]}
    with taddons.context(addon) as settings:
        settings.configure(addon, amr_codex_tools=str(path))
        flow = model_flow(path='/amr-logs')
        flow.request.content = json.dumps(payload).encode()
        addon.request(flow)
        assert flow.response.status_code == 400
        flow.response = None
        flow.request.headers['authorization'] = 'Bearer trial-token'
        addon.request(flow)
        assert flow.response.status_code == 200
        assert addon.requests == addon.total == addon.errors == 0
        addon.done()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0] == {'event': 'otel'}
    assert records[1]['id'] == 'call-1'
    assert records[1]['duration_ns'] == 25_000_000
    assert records[1]['seq'] == (1 if native_seq is not None else None)
