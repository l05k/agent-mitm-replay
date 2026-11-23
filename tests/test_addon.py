import json

from mitmproxy import http, io
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
