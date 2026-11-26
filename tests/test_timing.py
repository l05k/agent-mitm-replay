import json

import pytest

from agent_mitm_replay.timing import TimingError, prepare_timing, tool_seconds


def trial(tmp_path, ids):
    trial_dir = tmp_path / 'jobs' / 'trial' / 'run'
    agent = trial_dir / 'agent'
    agent.mkdir(parents=True)
    (agent / 'trajectory.json').write_text(json.dumps({'steps': [{
        'tool_calls': [{'tool_call_id': call_id, 'function_name': f'tool-{call_id}'}
                       for call_id in ids],
    }]}))
    return trial_dir


def records(path, *events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(event) + '\n' for event in events))


def test_pi_parallel_tool_durations_are_summed_not_unioned(tmp_path):
    trial_dir = trial(tmp_path, ['a', 'b', 'c'])
    records(trial_dir / 'agent' / 'amr-timing.jsonl',
            {'event': 'ready'},
            {'event': 'tool', 'id': 'a', 'start_ns': '0', 'duration_ns': '100000000'},
            {'event': 'tool', 'id': 'b', 'start_ns': '0', 'duration_ns': '200000000'},
            {'event': 'tool', 'id': 'c', 'start_ns': '100000000', 'duration_ns': '50000000'})
    assert tool_seconds(trial_dir, 'pi', '0.85.1') == {'tool_s': 0.35, 'tool_calls': 3}


def test_openclaw_duration_is_native_and_failed_tools_still_count(tmp_path):
    trial_dir = trial(tmp_path, ['one', 'two'])
    records(trial_dir / 'agent' / 'amr-timing.jsonl',
            {'event': 'ready'},
            {'event': 'ready'},
            {'event': 'tool', 'id': 'one', 'duration_ns': 1_500_000_000, 'error': None},
            {'event': 'tool', 'id': 'two', 'duration_ns': 250_000_000, 'error': 'exit 1'})
    assert tool_seconds(trial_dir, 'openclaw', '2026.7.1') == {
        'tool_s': 1.75, 'tool_calls': 2}


@pytest.mark.parametrize('ids, events, message', [
    (['a', 'b'], [{'event': 'ready'}, {'event': 'tool', 'id': 'a', 'duration_ns': '1'}],
     'does not cover'),
    (['a'], [{'event': 'ready'},
             {'event': 'tool', 'id': 'a', 'duration_ns': '1'},
             {'event': 'tool', 'id': 'z', 'duration_ns': '1'}], 'does not cover'),
    (['a'], [{'event': 'ready'},
             {'event': 'tool', 'id': 'a', 'duration_ns': '1'},
             {'event': 'tool', 'id': 'a', 'duration_ns': '2'}], 'Duplicate'),
    (['a'], [{'event': 'ready'}, {'event': 'error', 'detail': 'end without start'}],
     'hook error'),
    (['a'], [{'event': 'ready'}, {'event': 'tool', 'id': 'a'}], 'does not carry'),
    (['a'], [{'event': 'ready'}, {'event': 'tool', 'id': 'a', 'duration_ns': '-1'}],
     'negative'),
    (['a'], [{'event': 'tool', 'id': 'a', 'duration_ns': '1'}], 'did not initialise'),
])
def test_incomplete_or_inconsistent_pi_evidence_fails(tmp_path, ids, events, message):
    trial_dir = trial(tmp_path, ids)
    records(trial_dir / 'agent' / 'amr-timing.jsonl', *events)
    with pytest.raises(TimingError, match=message):
        tool_seconds(trial_dir, 'pi', '0.85.1')


def test_missing_native_log_is_not_zero(tmp_path):
    trial_dir = trial(tmp_path, ['a'])
    with pytest.raises(TimingError, match='Missing native Pi timing log'):
        tool_seconds(trial_dir, 'pi', '0.85.1')


def test_native_log_extra_missing_or_bad_records_fail(tmp_path):
    trial_dir = trial(tmp_path, ['a'])
    path = trial_dir / 'agent' / 'amr-timing.jsonl'
    records(path, {'event': 'ready'}, {'event': 'tool', 'id': 'a', 'duration_ns': '1'})
    path.write_text(path.read_text() + '{"event": "tool", "id": "a",')
    with pytest.raises(TimingError, match='Invalid JSON'):
        tool_seconds(trial_dir, 'pi', '0.85.1')
    path.write_text('{"event": "ready"}\n{"event": "mystery"}\n')
    with pytest.raises(TimingError, match='Unknown Pi timing event'):
        tool_seconds(trial_dir, 'pi', '0.85.1')


def test_prepare_rejects_an_unpinned_agent_version(tmp_path):
    with pytest.raises(TimingError, match='not validated'):
        prepare_timing(tmp_path, 'pi', '0.73.1')


def test_codex_sequence_sums_native_durations_and_rejects_gaps(tmp_path):
    trial_dir = trial(tmp_path, ['a', 'b'])
    output = tmp_path
    path = output / 'codex-tools.jsonl'
    records(path, {'event': 'otel'},
            {'seq': 1, 'id': 'a', 'start_ns': 0, 'end_ns': 40_000_000},
            {'seq': 2, 'id': 'b', 'start_ns': 0, 'end_ns': 60_000_000})
    assert tool_seconds(trial_dir, 'codex', '0.169.0') == {
        'tool_s': 0.1, 'tool_calls': 2}
    records(path, {'event': 'otel'},
            {'seq': 2, 'id': 'a', 'start_ns': 0, 'end_ns': 1})
    with pytest.raises(TimingError, match='sequence is incomplete'):
        tool_seconds(trial_dir, 'codex', '0.169.0')


def test_codex_080_uses_native_duration_without_a_sequence(tmp_path):
    trial_dir = trial(tmp_path, ['a', 'b'])
    records(tmp_path / 'codex-tools.jsonl', {'event': 'otel'},
            {'seq': None, 'id': 'a', 'duration_ns': 70_000_000},
            {'seq': None, 'id': 'b', 'duration_ns': 30_000_000})
    assert tool_seconds(trial_dir, 'codex', '0.80.0') == {
        'tool_s': 0.1, 'tool_calls': 2}


def test_codex_missing_telemetry_for_tool_calls_fails(tmp_path):
    trial_dir = trial(tmp_path, ['a'])
    with pytest.raises(TimingError, match='did not export'):
        tool_seconds(trial_dir, 'codex', '0.169.0')


def test_no_tool_calls_returns_zero_without_tool_events(tmp_path):
    trial_dir = trial(tmp_path, [])
    path = trial_dir / 'agent' / 'amr-timing.jsonl'
    records(path, {'event': 'ready'})
    assert tool_seconds(trial_dir, 'pi', '0.85.1') == {'tool_s': 0.0, 'tool_calls': 0}
