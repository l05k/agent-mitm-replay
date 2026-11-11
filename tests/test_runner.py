import json
import os
import urllib.request

import pytest

from agent_mitm_replay.runner import (
    ReplayError, check_harbor_result, check_proxy_audit, child_env, command_trace, run_trial,
)


def make_job(tmp_path, *, exception=None, rewards=None, steps=None):
    trial = tmp_path / "job" / "trial"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({
        "exception_info": exception,
        "verifier_result": {"rewards": {"reward": 1.0} if rewards is None else rewards},
        "step_results": steps,
    }))
    return trial.parent


@pytest.mark.parametrize("changes", [
    {"exception": {"exception_type": "BadRequestError"}},
    {"rewards": {"reward": 0.0}},
    {"rewards": {"a": 1.0, "b": 0.5}},
    {"rewards": {"reward": float("nan")}},
    {"rewards": {}},
    {"steps": [{"step_name": "solve"}]},
])
def test_zero_exit_is_not_enough_for_success(tmp_path, changes):
    with pytest.raises(ReplayError):
        check_harbor_result(make_job(tmp_path, **changes), 1.0)


def test_reward_threshold_is_inclusive(tmp_path):
    job = make_job(tmp_path, rewards={"reward": 0.5})
    assert check_harbor_result(job, 0.5)["rewards"] == {"reward": 0.5}
    with pytest.raises(ReplayError):
        check_harbor_result(job, 1.0)


@pytest.mark.parametrize("changes", [{"remaining": 1}, {"misses": 1}, {"errors": 1}, {"total": 0}])
def test_validation_requires_every_response_used_without_misses(tmp_path, changes):
    audit = tmp_path / "audit.jsonl"
    summary = {"event": "summary", "total": 2, "matched": 2, "misses": 0, "errors": 0, "remaining": 0, **changes}
    audit.write_text(json.dumps(summary) + "\n")
    with pytest.raises(ReplayError):
        check_proxy_audit(audit, "validate")


def test_measurement_does_not_repeat_response_completeness_validation(tmp_path):
    audit = tmp_path / "audit.jsonl"
    summary = {"event": "summary", "total": 2, "matched": 1, "misses": 0, "errors": 0, "remaining": 1}
    audit.write_text(json.dumps(summary) + "\n")
    assert check_proxy_audit(audit, "replay")["remaining"] == 1
    summary["misses"] = 1
    audit.write_text(json.dumps(summary) + "\n")
    with pytest.raises(ReplayError):
        check_proxy_audit(audit, "replay")


@pytest.mark.parametrize("rewards", [{"reward": 0.0}, {}, {"reward": float("nan")}])
def test_measurement_ignores_verifier_reward(tmp_path, rewards):
    result = check_harbor_result(make_job(tmp_path, rewards=rewards), None)
    assert "rewards" not in result


def test_measurement_still_rejects_trial_exceptions(tmp_path):
    with pytest.raises(ReplayError):
        check_harbor_result(make_job(tmp_path, exception={"exception_type": "TimeoutError"}), None)


@pytest.mark.parametrize("validated,mode", [(False, "replay"), (True, "validate")])
def test_trace_lifecycle_gate_precedes_execution(tmp_path, validated, mode):
    cassette = tmp_path / "cassette"
    cassette.mkdir()
    (cassette / "manifest.json").write_text(json.dumps({"schema_version": 3}))
    if validated:
        (cassette / "validation.json").write_text("{}")
    output = tmp_path / "run"
    with pytest.raises(ReplayError):
        run_trial(mode, task=tmp_path / "task", cassette=cassette, output=output)
    assert not output.exists()


@pytest.mark.parametrize("steps", [
    [],
    [{"source": "agent", "message": "done"}],
    [{"source": "agent", "tool_calls": []}],
])
def test_command_trace_rejects_missing_tool_calls(tmp_path, steps):
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "trajectory.json").write_text(json.dumps({"steps": steps}))
    with pytest.raises(ReplayError):
        command_trace(tmp_path)


def test_command_trace_preserves_batches_order_and_arguments(tmp_path):
    agent = tmp_path / "agent"
    agent.mkdir()
    first = {"function_name": "bash_command", "arguments": {"keystrokes": "cat input\n", "duration": 0.2}}
    second = {"function_name": "bash_command", "arguments": {"keystrokes": "cp input output\n", "duration": 1}}
    trajectory = {"steps": [
        {"source": "user", "message": "task"},
        {"tool_calls": [{"tool_call_id": "call-a", **first}, {"tool_call_id": "call-b", **second}]},
        {"tool_calls": [{"tool_call_id": "call-c", **first}]},
        {"source": "agent", "message": "finished", "tool_calls": []},
    ]}
    path = agent / "trajectory.json"
    path.write_text(json.dumps(trajectory))
    expected = [[first, second], [first]]
    assert command_trace(tmp_path) == expected
    trajectory["steps"][1]["tool_calls"][0]["tool_call_id"] = "different-generated-id"
    path.write_text(json.dumps(trajectory))
    assert command_trace(tmp_path) == expected


@pytest.mark.parametrize("change", ["command", "duration", "order", "batch"])
def test_command_trace_detects_workload_changes(tmp_path, change):
    agent = tmp_path / "agent"
    agent.mkdir()
    calls = [
        {"function_name": "bash_command", "arguments": {"keystrokes": "cat input\n", "duration": 0.2}},
        {"function_name": "bash_command", "arguments": {"keystrokes": "cp input output\n", "duration": 1}},
    ]
    path = agent / "trajectory.json"
    path.write_text(json.dumps({"steps": [{"tool_calls": calls}]}))
    expected = command_trace(tmp_path)
    steps = [{"tool_calls": calls}]
    if change == "command":
        calls[0]["arguments"]["keystrokes"] = "cat other\n"
    elif change == "duration":
        calls[0]["arguments"]["duration"] = 2
    elif change == "order":
        calls.reverse()
    else:
        steps = [{"tool_calls": [call]} for call in calls]
    path.write_text(json.dumps({"steps": steps}))
    assert command_trace(tmp_path) != expected


@pytest.mark.parametrize("parent_no_proxy", [{}, {"NO_PROXY": "internal.example"}, {"no_proxy": "internal.example"}])
def test_child_env_sends_loopback_past_an_ambient_proxy(monkeypatch, parent_no_proxy):
    monkeypatch.setattr(os, "environ", {"HTTP_PROXY": "http://proxy.example:3128", **parent_no_proxy})
    child = child_env()
    monkeypatch.setattr(os, "environ", child)
    assert urllib.request.proxy_bypass("127.0.0.1")
    assert urllib.request.proxy_bypass("localhost")
    assert not urllib.request.proxy_bypass("api.openai.com")
    if parent_no_proxy:
        assert urllib.request.proxy_bypass("internal.example")


def test_child_env_keeps_both_no_proxy_spellings_and_the_overlay(monkeypatch):
    monkeypatch.setattr(os, "environ", {"HTTP_PROXY": "http://proxy.example:3128",
                                        "NO_PROXY": "internal.example", "no_proxy": ".corp.example,internal.example"})
    child = child_env({"no_proxy": "overlay.example"})
    assert child["NO_PROXY"] == child["no_proxy"] == "internal.example,.corp.example,overlay.example,127.0.0.1,localhost"
    monkeypatch.setattr(os, "environ", child)
    for host in ("internal.example", "sub.corp.example", "overlay.example", "127.0.0.1", "localhost"):
        assert urllib.request.proxy_bypass(host), host
    assert not urllib.request.proxy_bypass("api.openai.com")
