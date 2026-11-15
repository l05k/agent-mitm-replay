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
