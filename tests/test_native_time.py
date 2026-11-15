import json

from agent_mitm_replay.trace import codex_event, read_openclaw


def test_codex_uses_event_time_when_otlp_timestamp_is_zero():
    fields = {'event.name': 'codex.tool_result', 'event.timestamp': '2026-11-15T00:00:00Z',
              'tool_result_seq': '1', 'call_id': 'call', 'duration_ms': '50',
              'tool_name': 'exec_command', 'tool_namespace': 'functions',
              'arguments': '{}', 'output': '', 'success': 'true', 'output_truncated': 'false'}
    record = {'timeUnixNano': '0', 'attributes': [
        {'key': key, 'value': {'stringValue': value}} for key, value in fields.items()]}
    tool = codex_event(record)
    assert tool['end_ns'] > 0 and tool['end_ns'] - tool['start_ns'] == 50_000_000


def test_openclaw_repeated_registry_loads_do_not_drop_a_tool(tmp_path):
    log = tmp_path / 'tools.jsonl'
    events = [{'event': 'ready'}, {'event': 'ready'},
              {'event': 'start', 'run': 'r', 'id': 'c', 'name': 'exec'},
              {'event': 'end', 'run': 'r', 'id': 'c', 'name': 'exec', 'duration_ms': 50,
               'end_ms': 100, 'arguments': {'command': 'false'}, 'error': None,
               'result': {'content': [{'type': 'text', 'text': 'failed'}],
                          'details': {'exitCode': 1, 'status': 'completed'}}}]
    log.write_text('\n'.join(map(json.dumps, events)))
    tool, = read_openclaw(log)
    assert tool.end_ns - tool.start_ns == 50_000_000
    assert tool.outcome['result']['exit_code'] == 1
