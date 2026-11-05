from __future__ import annotations

import importlib.metadata
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import tomllib
from urllib.parse import urlsplit
import uuid


class ReplayError(RuntimeError):
    """A recording or replay did not satisfy the experiment contract."""


# Keys that would move model traffic off the mitmdump listener this runner owns,
# so a recording could not observe (or a replay serve) those calls:
# * ``api_base`` is the listener URL the runner passes with ``--agent-kwarg``;
#   harbor/llms/lite_llm.py splats ``llm_call_kwargs`` over it.
# * ``llm_backend`` selects the backend (terminus_2._init_llm): only "litellm"
#   talks to ``api_base``, "tinker" builds a backend that ignores it entirely.
# * inside ``llm_kwargs``/``llm_call_kwargs``: litellm main.py reads the endpoint
#   from ``api_base`` and lets ``base_url`` overwrite it, reuses a caller
#   ``client`` verbatim, re-routes on ``custom_llm_provider``, and dispatches
#   ``model_list``/``fallbacks`` deployments with their own api_base/keys.
#   A call-level ``model`` can also select a provider that ignores api_base.
_REROUTING_AGENT_KEYS = ("api_base",)
_REROUTING_LLM_KEYS = (
    "api_base", "base_url", "client", "custom_llm_provider", "model_list",
    "fallbacks", "model",
)
_NO_PROXY_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _reject_padded_option_names(options: dict, *, source: str) -> None:
    """Refuse names that Harbor would normalize into a different option.

    Harbor strips ``--agent-kwarg`` keys, so ``"api_base "`` would pass an exact
    name check here and still reach Harbor as ``api_base``.
    """
    bad = sorted(
        repr(key)
        for key in options
        if not isinstance(key, str) or not key or key != key.strip()
    )
    if bad:
        raise ReplayError(
            f"{source} has empty or padded option names: {', '.join(bad)}"
        )


def _reject_rerouting_options(options: dict, *, source: str) -> None:
    """Refuse agent or LLM kwargs that would bypass the recording proxy."""
    bad = [key for key in _REROUTING_AGENT_KEYS if key in options]
    if options.get("llm_backend") not in (None, "litellm"):
        bad.append("llm_backend")
    for key in ("llm_kwargs", "llm_call_kwargs"):
        value = options.get(key)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ReplayError(f"{source}: {key} must be a mapping")
        _reject_padded_option_names(value, source=f"{source}: {key}")
        bad += [f"{key}.{name}" for name in _REROUTING_LLM_KEYS if name in value]
    if bad:
        raise ReplayError(f"{source} cannot reroute model traffic: {', '.join(sorted(bad))}")


def _merge_no_proxy(*values: str | None) -> str:
    """Merge NO_PROXY entries, keeping configured exclusions and unblocking loopback."""
    entries: list[str] = []
    seen: set[str] = set()
    for value in (*values, *_NO_PROXY_LOOPBACK):
        for entry in (value or "").split(","):
            entry = entry.strip()
            if entry and entry.lower() not in seen:
                seen.add(entry.lower())
                entries.append(entry)
    return ",".join(entries)


def _child_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Keep loopback client traffic local while preserving other proxy settings."""
    overlay = {key: str(value) for key, value in (env or {}).items()}
    no_proxy = _merge_no_proxy(
        os.environ.get("NO_PROXY"),
        os.environ.get("no_proxy"),
        overlay.get("NO_PROXY"),
        overlay.get("no_proxy"),
    )
    child = {**os.environ, **overlay, "NO_PROXY": no_proxy, "no_proxy": no_proxy}
    child["HARBOR_TELEMETRY"] = "0"
    child["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    return child


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ReplayError(f"Cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise ReplayError(f"Expected a JSON object in {path}")
    return value


def _binary(name: str) -> str:
    candidate = Path(sys.executable).parent / name
    if not candidate.is_file():
        raise ReplayError(f"{name} is missing from this Python environment; run uv sync")
    return str(candidate)


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def _ready(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ReplayError("mitmdump exited before startup; inspect proxy.log")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise ReplayError("mitmdump did not start within 15 seconds; inspect proxy.log")


def _task_step_names(task: Path) -> list[str] | None:
    """Configured multi-step names, or ``None`` for a single-step task."""
    try:
        config = tomllib.loads((task / "task.toml").read_text())
    except (OSError, ValueError) as error:
        raise ReplayError(f"Cannot read task config {task / 'task.toml'}: {error}") from error
    steps = config.get("steps")
    if steps is None:
        return None
    if not isinstance(steps, list) or not steps:
        raise ReplayError("task.toml [[steps]] must be a non-empty list")
    names = []
    for step in steps:
        name = step.get("name") if isinstance(step, dict) else None
        if not isinstance(name, str) or not name:
            raise ReplayError("Every task.toml [[steps]] entry needs a name")
        names.append(name)
    return names


def _check_rewards(rewards: object, minimum_reward: float, *, where: str) -> dict:
    if not isinstance(rewards, dict) or not rewards:
        raise ReplayError(f"Harbor verifier produced no rewards: {where}")
    for name, value in rewards.items():
        if (
            not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < minimum_reward
        ):
            raise ReplayError(f"Reward {name}={value!r} is below {minimum_reward}: {where}")
    return rewards


def check_harbor_result(
    job: Path, minimum_reward: float, *, expected_steps: list[str] | None = None
) -> dict:
    """Check persisted results: Harbor may return exit code zero on trial errors."""
    result = _read_json(job / "result.json")
    stats = result.get("stats", {})
    if (
        result.get("finished_at") is None
        or result.get("n_total_trials") != 1
        or stats.get("n_completed_trials") != 1
        or stats.get("n_errored_trials") != 0
        or any(stats.get(key, 0) for key in ("n_pending_trials", "n_running_trials", "n_cancelled_trials"))
    ):
        raise ReplayError(f"Harbor did not complete one error-free trial: {job / 'result.json'}")
    trials = list(job.glob("*/result.json"))
    if len(trials) != 1:
        raise ReplayError(f"Expected one trial result in {job}, found {len(trials)}")
    trial = _read_json(trials[0])
    if trial.get("exception_info") is not None:
        raise ReplayError(f"Harbor trial exception: {trials[0]}")
    step_results = trial.get("step_results")
    if expected_steps is None:
        if step_results is not None:
            raise ReplayError(
                f"Single-step task produced step results; task.toml and trial disagree: {trials[0]}"
            )
    else:
        if not isinstance(step_results, list):
            raise ReplayError(f"Multi-step task produced no step results: {trials[0]}")
        finished = []
        for step in step_results:
            if not isinstance(step, dict) or not isinstance(step.get("step_name"), str):
                raise ReplayError(f"Malformed step result: {trials[0]}")
            name = step["step_name"]
            if step.get("exception_info") is not None:
                raise ReplayError(f"Harbor step {name!r} failed: {trials[0]}")
            if step.get("verifier_result") is None:
                raise ReplayError(f"Harbor step {name!r} has no verifier result: {trials[0]}")
            _check_rewards(
                step["verifier_result"].get("rewards"), minimum_reward,
                where=f"step {name!r} in {trials[0]}",
            )
            finished.append(name)
        if sorted(finished) != sorted(expected_steps):
            raise ReplayError(
                f"Harbor steps {finished!r} do not match task.toml steps {expected_steps!r}: {trials[0]}"
            )
    rewards = _check_rewards(
        (trial.get("verifier_result") or {}).get("rewards"), minimum_reward, where=str(trials[0])
    )
    return {"rewards": rewards, "trial_dir": str(trials[0].parent)}


def check_proxy_audit(path: Path, mode: str) -> dict:
    try:
        events = [json.loads(line) for line in path.read_text().splitlines() if line]
    except (OSError, ValueError) as error:
        raise ReplayError(f"Cannot read proxy audit {path}: {error}") from error
    summaries = [event for event in events if event.get("event") == "summary"]
    if len(summaries) != 1:
        raise ReplayError(f"Proxy audit has no unique shutdown summary: {path}")
    summary = summaries[0]
    if summary.get("errors", 0) or summary.get("misses", 0):
        raise ReplayError(f"Proxy rejected requests or encountered transport errors: {path}")
    total = summary.get("total", 0)
    if total < 1:
        raise ReplayError(f"No model responses were recorded or loaded: {path}")
    if mode == "replay" and (summary.get("remaining") != 0 or summary.get("matched") != total):
        raise ReplayError(f"Replay did not consume every recorded response exactly once: {path}")
    return summary


def run_trial(
    mode: str,
    *,
    task: Path,
    cassette: Path,
    model: str | None = None,
    upstream: str | None = None,
    output: Path | None = None,
    agent_kwargs: dict[str, object] | None = None,
    environment_kwargs: dict[str, object] | None = None,
    extra_instructions: list[str] | None = None,
    artifacts: list[str] | None = None,
    minimum_reward: float = 1.0,
    env: dict[str, str] | None = None,
) -> dict:
    if mode not in {"record", "replay"}:
        raise ReplayError(f"Unknown mode: {mode}")
    task, cassette = task.resolve(), cassette.resolve()
    if not (task / "task.toml").is_file() or not (task / "instruction.md").is_file():
        raise ReplayError(f"Not a Harbor task directory: {task}")
    expected_steps = _task_step_names(task)
    if not math.isfinite(minimum_reward):
        raise ReplayError("minimum_reward must be finite")
    overrides = dict(agent_kwargs or {})
    if "session_id" in overrides:
        raise ReplayError("session_id is managed by the recording")
    _reject_padded_option_names(overrides, source="agent arguments")
    _reject_rerouting_options(overrides, source="agent arguments")
    harbor, mitmdump = _binary("harbor"), _binary("mitmdump")
    if mode == "record":
        if not model or not upstream:
            raise ReplayError("record requires --model and --upstream")
        options = {
            "enable_summarize": False,
            "record_terminal_session": False,
            **overrides,
            "session_id": f"amr-{uuid.uuid4().hex}",
        }
        artifact_paths = list(artifacts or [])
        instructions = list(extra_instructions or [])
    else:
        manifest = _read_json(cassette / "manifest.json")
        if manifest.get("schema_version") != 1:
            raise ReplayError("Unsupported cassette manifest version")
        recorded = {key: manifest.get(key) for key in ("model", "upstream", "agent_kwargs", "artifacts", "extra_instructions")}
        if (
            not isinstance(recorded["model"], str)
            or not isinstance(recorded["upstream"], str)
            or not isinstance(recorded["agent_kwargs"], dict)
            or not isinstance(recorded["artifacts"], list)
            or not isinstance(recorded["extra_instructions"], list)
        ):
            raise ReplayError(f"Cassette manifest is incomplete: {cassette / 'manifest.json'}")
        if model is not None and model != recorded["model"]:
            raise ReplayError("Replay model must match the recording")
        model, upstream = recorded["model"], recorded["upstream"]
        options = dict(recorded["agent_kwargs"])
        _reject_padded_option_names(options, source=f"Recorded agent options ({cassette / 'manifest.json'})")
        _reject_rerouting_options(options, source=f"Recorded agent options ({cassette / 'manifest.json'})")
        if any(options.get(key) != value for key, value in overrides.items()):
            raise ReplayError("Replay agent options must match the recording")
        artifact_paths = list(artifacts if artifacts is not None else recorded["artifacts"])
        instructions = list(extra_instructions if extra_instructions is not None else recorded["extra_instructions"])
    parsed = urlsplit(upstream)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ReplayError("--upstream must be an HTTP(S) origin without path or credentials")
    if not model.startswith("openai/"):
        raise ReplayError("This runner uses the OpenAI Chat API; select an openai/ model")
    if mode == "record":
        cassette.mkdir(parents=True, mode=0o700, exist_ok=False)
    upstream = upstream.rstrip("/")
    flows = cassette / "flows.mitm"
    if mode == "replay" and not flows.is_file():
        raise ReplayError(f"Recording missing: {flows}")
    output = (output or (cassette / "record" if mode == "record" else cassette / "replays" / uuid.uuid4().hex)).resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    port = _available_port()
    audit = output / "proxy-audit.jsonl"
    addon = Path(__file__).with_name("addon.py")
    proxy_command = [
        mitmdump, "-q", "--mode", f"reverse:{upstream}",
        "--listen-host", "127.0.0.1", "--listen-port", str(port),
        "--set", "connection_strategy=lazy", "--set", f"confdir={output / 'mitm-config'}",
        "--set", "server_replay_reuse=false", "--set", "server_replay_ignore_content=false",
        "-s", str(addon), "--set", f"amr_mode={mode}", "--set", f"amr_flows={flows}",
        "--set", f"amr_audit={audit}",
    ]
    if mode == "record":
        proxy_command += ["-w", str(flows), "--set", "stream_large_bodies=1", "--set", "store_streamed_bodies=true"]
    harbor_command = [
        harbor, "run", "--yes", "--path", str(task), "--agent", "terminus-2", "--model", model,
        "--n-concurrent", "1", "--n-attempts", "1", "--max-retries", "0",
        "--jobs-dir", str(output / "jobs"), "--job-name", "trial",
        "--agent-kwarg", f"api_base=http://127.0.0.1:{port}/v1",
    ]
    for key, value in options.items():
        harbor_command += ["--agent-kwarg", f"{key}={json.dumps(value)}"]
    for key, value in (environment_kwargs or {}).items():
        if key == "mounts":
            harbor_command += ["--mounts", json.dumps(value)]
        elif key == "extra_docker_compose":
            for path in value:
                harbor_command += ["--extra-docker-compose", str(path)]
        else:
            harbor_command += ["--environment-kwarg", f"{key}={json.dumps(value)}"]
    for instruction in instructions:
        harbor_command += ["--extra-instruction", instruction]
    for artifact in artifact_paths:
        harbor_command += ["--artifact", artifact]
    child_env = _child_env(env)
    if mode == "replay":
        child_env["OPENAI_API_KEY"] = "offline-replay-placeholder"
    _write_json(output / "command.json", {"argv": harbor_command})
    print(f"{mode}: {output}", flush=True)
    with (output / "proxy.log").open("w") as proxy_log, (output / "harbor.log").open("w") as harbor_log:
        proxy = subprocess.Popen(proxy_command, env=child_env, stdin=subprocess.DEVNULL, stdout=proxy_log, stderr=subprocess.STDOUT, start_new_session=True)
        harbor_process = None
        try:
            _ready(proxy, port)
            harbor_process = subprocess.Popen(harbor_command, env=child_env, stdin=subprocess.DEVNULL, stdout=harbor_log, stderr=subprocess.STDOUT, start_new_session=True)
            harbor_exit = harbor_process.wait()
        finally:
            if harbor_process is not None:
                _stop(harbor_process)
            _stop(proxy)
    if proxy.returncode != 0:
        raise ReplayError(f"Proxy failed with exit {proxy.returncode}; inspect {output / 'proxy.log'}")
    proxy_summary = check_proxy_audit(audit, mode)
    if harbor_exit != 0:
        raise ReplayError(f"Harbor failed with exit {harbor_exit}; inspect {output / 'harbor.log'}")
    harbor_summary = check_harbor_result(
        output / "jobs/trial", minimum_reward, expected_steps=expected_steps
    )
    summary = {"mode": mode, "model_requests": proxy_summary["total"], "reward": min(harbor_summary["rewards"].values()), "rewards": harbor_summary["rewards"], "output": str(output), "trial_dir": harbor_summary["trial_dir"]}
    _write_json(output / "summary.json", summary)
    if mode == "record":
        _write_json(cassette / "manifest.json", {
            "schema_version": 1, "model": model, "upstream": upstream,
            "agent_kwargs": options, "artifacts": artifact_paths,
            "extra_instructions": instructions,
            "harbor_version": importlib.metadata.version("harbor"),
            "mitmproxy_version": importlib.metadata.version("mitmproxy"),
            "model_requests": proxy_summary["total"],
        })
    return summary
