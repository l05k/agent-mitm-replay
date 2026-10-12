import json
import os
import urllib.request

import pytest

from harbor_mitm_replay.runner import (
    ReplayError,
    _child_env,
    check_harbor_result,
    check_proxy_audit,
    run_trial,
)


def make_job(tmp_path, *, exception=None, reward=1.0, errors=0, finished=True):
    job = tmp_path / "job"
    trial = job / "trial"
    trial.mkdir(parents=True)
    (job / "result.json").write_text(json.dumps({
        "finished_at": "2026-10-12T00:00:00" if finished else None,
        "n_total_trials": 1,
        "stats": {"n_completed_trials": 1, "n_errored_trials": errors},
    }))
    (trial / "result.json").write_text(json.dumps({
        "exception_info": exception,
        "verifier_result": {"rewards": {"reward": reward}},
    }))
    return job


@pytest.mark.parametrize("changes", [
    {"errors": 1},
    {"exception": {"exception_type": "BadRequestError"}},
    {"finished": False},
    {"reward": 0.0},
    {"reward": float("nan")},
])
def test_zero_exit_is_not_enough_for_success(tmp_path, changes):
    job = make_job(tmp_path, **changes)
    with pytest.raises(ReplayError):
        check_harbor_result(job, 1.0)


def test_reward_threshold_is_inclusive(tmp_path):
    job = make_job(tmp_path, reward=0.5)
    assert check_harbor_result(job, 0.5)["rewards"] == {"reward": 0.5}
    with pytest.raises(ReplayError):
        check_harbor_result(job, 1.0)


@pytest.mark.parametrize("changes", [
    {"remaining": 1, "matched": 1},
    {"misses": 1},
    {"errors": 1},
    {"total": 0, "matched": 0},
])
def test_replay_requires_complete_error_free_consumption(tmp_path, changes):
    audit = tmp_path / "audit.jsonl"
    summary = {"event": "summary", "total": 2, "matched": 2, "misses": 0, "errors": 0, "remaining": 0, **changes}
    audit.write_text(json.dumps(summary) + "\n")
    with pytest.raises(ReplayError):
        check_proxy_audit(audit, "replay")


def make_task(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text("")
    (task / "instruction.md").write_text("Do nothing.\n")
    return task


def make_cassette(tmp_path, **manifest):
    cassette = tmp_path / "cassette"
    cassette.mkdir()
    (cassette / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "model": "openai/gpt-4o-mini",
        "upstream": "http://127.0.0.1:8080",
        "agent_kwargs": {"session_id": "hrr-recorded"},
        "artifacts": [],
        "extra_instructions": [],
        **manifest,
    }))
    return cassette


@pytest.mark.parametrize("agent_kwargs", [
    {"api_base": "http://attacker.example/v1"},
    {"llm_backend": "tinker"},
    {"llm_call_kwargs": {"api_base": "http://attacker.example/v1"}},
    {"llm_call_kwargs": {"base_url": "http://attacker.example/v1"}},
    {"llm_call_kwargs": {"custom_llm_provider": "bedrock"}},
    {"llm_kwargs": {"client": "sentinel"}},
    {"llm_kwargs": {"model_list": [{"model_name": "openai/gpt-4o-mini"}]}},
    {"llm_call_kwargs": {"model": "bedrock/anthropic.claude"}},
    {"llm_call_kwargs": {"fallbacks": [{"api_base": "http://attacker.example/v1"}]}},
])
def test_record_refuses_rerouting_options_before_side_effects(tmp_path, agent_kwargs):
    task, cassette = make_task(tmp_path), tmp_path / "cassette"
    with pytest.raises(ReplayError, match="cannot reroute model traffic"):
        run_trial(
            "record", task=task, cassette=cassette,
            model="openai/gpt-4o-mini", upstream="http://127.0.0.1:8080",
            agent_kwargs=agent_kwargs,
        )
    assert not cassette.exists()


def test_record_refuses_caller_session_id(tmp_path):
    task, cassette = make_task(tmp_path), tmp_path / "cassette"
    with pytest.raises(ReplayError, match="session_id is managed by the recording"):
        run_trial(
            "record", task=task, cassette=cassette,
            model="openai/gpt-4o-mini", upstream="http://127.0.0.1:8080",
            agent_kwargs={"session_id": "attacker"},
        )
    assert not cassette.exists()


@pytest.mark.parametrize("manifest", [
    {"agent_kwargs": {"api_base": "http://attacker.example/v1"}},
    {"agent_kwargs": {"llm_kwargs": {"base_url": "http://attacker.example/v1"}}},
    {"agent_kwargs": {"llm_call_kwargs": {"api_base": "http://attacker.example/v1"}}},
    {"agent_kwargs": {"llm_call_kwargs": {"model": "bedrock/anthropic.claude"}}},
    {"agent_kwargs": {"llm_kwargs": {"fallbacks": [{"base_url": "http://attacker.example/v1"}]}}},
    {"model": None},
    {"agent_kwargs": "not-a-mapping"},
])
def test_replay_refuses_tampered_manifest_before_side_effects(tmp_path, manifest):
    task, cassette = make_task(tmp_path), make_cassette(tmp_path, **manifest)
    with pytest.raises(ReplayError):
        run_trial("replay", task=task, cassette=cassette)
    assert not (cassette / "replays").exists()


def test_replay_accepts_recorded_session_id_from_manifest(tmp_path):
    task, cassette = make_task(tmp_path), make_cassette(tmp_path)
    with pytest.raises(ReplayError, match="Recording missing"):
        run_trial("replay", task=task, cassette=cassette)
    assert not (cassette / "replays").exists()


@pytest.mark.parametrize("parent_no_proxy", [
    {"NO_PROXY": "internal.example"},
    {"no_proxy": ".corp.example"},
    {"NO_PROXY": "internal.example,127.0.0.1", "no_proxy": ".corp.example"},
])
def test_child_env_keeps_proxy_routing_and_unblocks_loopback(monkeypatch, parent_no_proxy):
    parent = {"HTTP_PROXY": "http://proxy.example:3128", **parent_no_proxy}
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", dict(parent))
        child = _child_env({"HTTPS_PROXY": "http://overlay.example:8443", "no_proxy": "overlay.example"})

    assert child["NO_PROXY"] == child["no_proxy"]
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", dict(child))
        assert urllib.request.proxy_bypass("127.0.0.1")
        assert urllib.request.proxy_bypass("localhost")
        assert urllib.request.proxy_bypass("::1")
        assert urllib.request.proxy_bypass("overlay.example")
        assert not urllib.request.proxy_bypass("api.openai.com")
        assert urllib.request.getproxies() == {
            "http": "http://proxy.example:3128",
            "https": "http://overlay.example:8443",
            "no": child["NO_PROXY"],
        }
        for value in parent_no_proxy.values():
            for entry in value.split(","):
                assert urllib.request.proxy_bypass(entry)
                assert urllib.request.proxy_bypass(f"sub.{entry.strip().lstrip('.')}")


