import json

import pytest

from agent_mitm_replay.runner import TrialFailure, check_harbor_result, check_proxy_audit
from agent_mitm_replay.trace import Tool, TraceError, read_codex, read_openclaw, signature, tool_seconds


@pytest.mark.parametrize('changes', [{'remaining': 1}, {'misses': 1}, {'requests': 1}])
def test_replay_rejects_response_count_or_route_divergence(tmp_path, changes):
    audit = tmp_path / 'audit.jsonl'
    audit.write_text(json.dumps({'event': 'summary', 'errors': 0, 'misses': 0,
                                 'remaining': 0, 'total': 2, 'requests': 2, **changes}))
    with pytest.raises(TrialFailure) as failure:
        check_proxy_audit(audit, recording=False)
    assert failure.value.status == 'divergence'


def test_unsolved_trial_is_distinct_from_timeout(tmp_path):
    trial = tmp_path / 'job' / 'trial'
    trial.mkdir(parents=True)
    result = trial / 'result.json'
    result.write_text(json.dumps({'verifier_result': {'rewards': {'reward': 0.0}}}))
    assert check_harbor_result(trial.parent)['rewards'] == {'reward': 0.0}
    result.write_text(json.dumps({'exception_info': {'exception_type': 'AgentTimeoutError'}}))
    with pytest.raises(TrialFailure) as failure:
        check_harbor_result(trial.parent)
    assert failure.value.status == 'timeout'


def test_tool_time_unions_parallel_work_and_does_not_charge_model_wait():
    ns = 10**9
    spans = [Tool(0, 9 * ns, 'wait_agent', {}, None),
             Tool(ns, 3 * ns, 'shell', {}, None),
             Tool(7 * ns, 10 * ns, 'search', {}, None)]
    assert tool_seconds(spans, [(2 * ns, 6 * ns), (5 * ns, 7 * ns)]) == 6


def test_codex_signature_ignores_latency_but_retains_output_and_exit_status(tmp_path):
    log = tmp_path / 'tools.jsonl'
    def trial(chunk, wall, code):
        event = {'seq': 1, 'id': chunk, 'name': 'exec_command', 'namespace': '',
                 'arguments': '{"cmd": "cat file"}', 'success': True, 'truncated': False,
                 'start_ns': 0, 'end_ns': 9,
                 'output': f'Chunk ID: {chunk}\nWall time: {wall} seconds\nProcess exited with code {code}\nOutput:\npayload'}
        log.write_text('{"event": "otel"}\n' + json.dumps(event))
        return signature(read_codex(log))
    assert trial('a1', '0.02', 0) == trial('b2', '0.09', 0)
    assert trial('a1', '0.02', 0) != trial('b2', '0.09', 1)


def test_openclaw_requires_outcome_for_every_started_tool(tmp_path):
    log = tmp_path / 'tools.jsonl'
    log.write_text('\n'.join(map(json.dumps, [{'event': 'ready'},
        {'event': 'start', 'id': 'call', 'run': 'run', 'name': 'read'}])))
    with pytest.raises(TraceError, match='without all tool outcomes'):
        read_openclaw(log)
