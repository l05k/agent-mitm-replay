import json
import os
import urllib.request

import pytest

from agent_mitm_replay.runner import ReplayError, check_harbor_result, check_proxy_audit, child_env


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
def test_replay_requires_every_response_used_without_misses(tmp_path, changes):
    audit = tmp_path / "audit.jsonl"
    summary = {"event": "summary", "total": 2, "matched": 2, "misses": 0, "errors": 0, "remaining": 0, **changes}
    audit.write_text(json.dumps(summary) + "\n")
    with pytest.raises(ReplayError):
        check_proxy_audit(audit, "replay")


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
