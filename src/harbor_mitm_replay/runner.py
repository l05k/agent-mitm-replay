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
from urllib.parse import urlsplit
import uuid


class ReplayError(RuntimeError):
    """A recording or replay did not satisfy the experiment contract."""


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


def check_harbor_result(job: Path, minimum_reward: float) -> dict:
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
    rewards = (trial.get("verifier_result") or {}).get("rewards")
    if not isinstance(rewards, dict) or not rewards:
        raise ReplayError(f"Harbor verifier produced no rewards: {trials[0]}")
    for name, value in rewards.items():
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum_reward:
            raise ReplayError(f"Reward {name}={value!r} is below {minimum_reward}: {trials[0]}")
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
    if not math.isfinite(minimum_reward):
        raise ReplayError("minimum_reward must be finite")
    harbor, mitmdump = _binary("harbor"), _binary("mitmdump")
    overrides = dict(agent_kwargs or {})
    if "api_base" in overrides or "session_id" in overrides:
        raise ReplayError("api_base and session_id are managed by the recording")
    if mode == "record":
        if not model or not upstream:
            raise ReplayError("record requires --model and --upstream")
        options = {
            "enable_summarize": False,
            "record_terminal_session": False,
            **overrides,
            "session_id": f"hrr-{uuid.uuid4().hex}",
        }
        artifact_paths = list(artifacts or [])
        instructions = list(extra_instructions or [])
    else:
        manifest = _read_json(cassette / "manifest.json")
        if manifest.get("schema_version") != 1:
            raise ReplayError("Unsupported cassette manifest version")
        if model is not None and model != manifest["model"]:
            raise ReplayError("Replay model must match the recording")
        model, upstream = manifest["model"], manifest["upstream"]
        options = dict(manifest["agent_kwargs"])
        if any(options.get(key) != value for key, value in overrides.items()):
            raise ReplayError("Replay agent options must match the recording")
        artifact_paths = list(artifacts if artifacts is not None else manifest["artifacts"])
        instructions = list(extra_instructions if extra_instructions is not None else manifest["extra_instructions"])
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
        "-s", str(addon), "--set", f"hrr_mode={mode}", "--set", f"hrr_flows={flows}",
        "--set", f"hrr_audit={audit}",
    ]
    if mode == "record":
        proxy_command += ["-w", str(flows), "--set", "stream_large_bodies=1", "--set", "store_streamed_bodies=true"]
    harbor_command = [
        harbor, "run", "--path", str(task), "--agent", "terminus-2", "--model", model,
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
    child_env = {**os.environ, **(env or {}), "HARBOR_TELEMETRY": "0", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    if mode == "replay":
        child_env["OPENAI_API_KEY"] = "offline-replay-placeholder"
    _write_json(output / "command.json", {"argv": harbor_command})
    print(f"{mode}: {output}", flush=True)
    with (output / "proxy.log").open("w") as proxy_log, (output / "harbor.log").open("w") as harbor_log:
        proxy = subprocess.Popen(proxy_command, env=child_env, stdout=proxy_log, stderr=subprocess.STDOUT, start_new_session=True)
        harbor_process = None
        try:
            _ready(proxy, port)
            harbor_process = subprocess.Popen(harbor_command, env=child_env, stdout=harbor_log, stderr=subprocess.STDOUT, start_new_session=True)
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
    harbor_summary = check_harbor_result(output / "jobs/trial", minimum_reward)
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
