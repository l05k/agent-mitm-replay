import json

import pytest

from harbor_mitm_replay.runner import ReplayError, check_harbor_result, check_proxy_audit


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
