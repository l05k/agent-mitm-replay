import json

import pytest

from agent_mitm_replay.runner import (
    ReplayError, TrialFailure, check_harbor_result, check_proxy_audit, command_trace, run_trial,
)


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
    result.write_text(json.dumps({
        'verifier_result': {'rewards': {'reward': 0.0}},
        'agent_execution': {'started_at': '2026-11-25T00:00:00', 'finished_at': '2026-11-25T00:00:02'},
    }))
    assert check_harbor_result(trial.parent)['rewards'] == {'reward': 0.0}
    result.write_text(json.dumps({'exception_info': {'exception_type': 'AgentTimeoutError'}}))
    with pytest.raises(TrialFailure) as failure:
        check_harbor_result(trial.parent)
    assert failure.value.status == 'timeout'


def test_harbor_phase_time_remains_separate(tmp_path):
    trial = tmp_path / 'trial'
    trial.mkdir()
    (trial / 'result.json').write_text(json.dumps({
        'agent_execution': {'started_at': '2026-11-25T00:00:00', 'finished_at': '2026-11-25T00:00:02.5'},
    }))
    assert check_harbor_result(tmp_path)['phase_seconds']['agent_execution'] == 2.5


def test_command_trace_compares_harbor_calls_not_generated_ids_or_output(tmp_path):
    agent = tmp_path / 'agent'
    agent.mkdir()
    trajectory = {'steps': [{'source': 'agent', 'tool_calls': [
        {'tool_call_id': 'first-id', 'function_name': 'bash', 'arguments': {'command': 'cat input'}}],
        'observation': {'results': [{'content': 'first output'}]}}]}
    path = agent / 'trajectory.json'
    path.write_text(json.dumps(trajectory))
    recorded = command_trace(tmp_path)
    trajectory['steps'][0]['tool_calls'][0]['tool_call_id'] = 'new-id'
    trajectory['steps'][0]['observation']['results'][0]['content'] = 'other output'
    path.write_text(json.dumps(trajectory))
    assert command_trace(tmp_path) == recorded
    trajectory['steps'][0]['tool_calls'][0]['arguments']['command'] = 'cat other-input'
    path.write_text(json.dumps(trajectory))
    assert command_trace(tmp_path) != recorded


@pytest.mark.parametrize('multiplier', [0, -1, float('nan'), float('inf')])
def test_invalid_agent_timeout_fails_before_creating_cassette(tmp_path, multiplier):
    task = tmp_path / 'task'
    task.mkdir()
    cassette = tmp_path / 'cassette'
    with pytest.raises(ReplayError, match='finite and positive'):
        run_trial('record', task=task, cassette=cassette, agent='pi',
                  model='openai/example', upstream='http://127.0.0.1/v1',
                  agent_timeout_multiplier=multiplier)
    assert not cassette.exists()
