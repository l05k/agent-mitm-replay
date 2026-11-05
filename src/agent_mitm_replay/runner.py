"""Run one Harbor Terminus-2 trial behind the record/replay proxy."""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid


class ReplayError(RuntimeError):
    """A recording or replay did not meet the experiment's pass criteria."""


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ReplayError(f"Cannot read {path}: {error}") from error


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def child_env(env: dict[str, str] | None = None) -> dict[str, str]:
    overlay = env or {}
    child = {**os.environ, **overlay}
    # Keep every existing exclusion from both spellings, then let the model hop to the
    # local proxy bypass any ambient HTTP proxy.
    entries = []
    for source in (os.environ, overlay):
        for key in ("NO_PROXY", "no_proxy"):
            entries += [entry.strip() for entry in source.get(key, "").split(",") if entry.strip()]
    no_proxy = ",".join(dict.fromkeys([*entries, "127.0.0.1", "localhost"]))
    child.update(NO_PROXY=no_proxy, no_proxy=no_proxy, HARBOR_TELEMETRY="0", LITELLM_LOCAL_MODEL_COST_MAP="True")
    return child


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _wait_listening(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and process.poll() is None:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            return
        except OSError:
            time.sleep(0.05)
    raise ReplayError("mitmdump did not start; inspect proxy.log")


def _stop(process: subprocess.Popen) -> None:
    """SIGINT the process group so mitmdump flushes flows.mitm and the audit."""
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def check_proxy_audit(path: Path, mode: str) -> dict:
    lines = [json.loads(line) for line in path.read_text().splitlines() if line]
    summary = next((line for line in lines if line["event"] == "summary"), None)
    if summary is None:
        raise ReplayError(f"Proxy wrote no summary: {path}")
    if summary["errors"] or summary["misses"]:
        raise ReplayError(f"Proxy saw {summary['misses']} miss(es) and {summary['errors']} error(s): {path}")
    if summary["total"] < 1:
        raise ReplayError(f"No model responses recorded or loaded: {path}")
    if mode == "replay" and summary["remaining"]:
        raise ReplayError(f"{summary['remaining']} recorded response(s) were never requested: {path}")
    return summary


def check_harbor_result(job: Path, minimum_reward: float) -> dict:
    """Harbor can exit 0 after a failed trial, so read its result files instead."""
    trials = list(job.glob("*/result.json"))
    if len(trials) != 1:
        raise ReplayError(f"Expected one trial result in {job}, found {len(trials)}")
    trial = _read_json(trials[0])
    if trial.get("exception_info") is not None:
        raise ReplayError(f"Harbor trial exception: {trials[0]}")
    if trial.get("step_results") is not None:
        raise ReplayError(f"Multi-step ([[steps]]) tasks are not supported: {trials[0]}")
    rewards = (trial.get("verifier_result") or {}).get("rewards") or {}
    if not rewards or not all(value >= minimum_reward for value in rewards.values()):
        raise ReplayError(f"Rewards {rewards} are below {minimum_reward}: {trials[0]}")
    return {"rewards": rewards, "trial_dir": str(trials[0].parent)}


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
    mounts: list[tuple[str, str]] | None = None,
    extra_instructions: list[str] | None = None,
    artifacts: list[str] | None = None,
    minimum_reward: float = 1.0,
    env: dict[str, str] | None = None,
) -> dict:
    """Record or replay one trial. Replay reuses model and agent options from the manifest."""
    if mode == "record":
        if not model or not upstream:
            raise ReplayError("record requires --model and --upstream")
        # Summarization adds model calls that depend on timing; a fixed session_id
        # keeps the request bodies identical between record and replay.
        options = {"enable_summarize": False, "record_terminal_session": False,
                   **(agent_kwargs or {}), "session_id": f"amr-{uuid.uuid4().hex}"}
        artifacts, extra_instructions = list(artifacts or []), list(extra_instructions or [])
        cassette.mkdir(parents=True, mode=0o700, exist_ok=False)
    else:
        manifest = _read_json(cassette / "manifest.json")
        model, upstream, options = manifest["model"], manifest["upstream"], manifest["agent_kwargs"]
        if artifacts is None:
            artifacts = manifest["artifacts"]
        if extra_instructions is None:
            extra_instructions = manifest["extra_instructions"]
    upstream = upstream.rstrip("/")
    flows = (cassette / "flows.mitm").resolve()
    output = (output or (cassette / "record" if mode == "record" else cassette / "replays" / uuid.uuid4().hex)).resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    audit = output / "proxy-audit.jsonl"
    port = _free_port()
    bin_dir = Path(sys.executable).parent
    proxy_command = [
        str(bin_dir / "mitmdump"), "-q", "--mode", f"reverse:{upstream}",
        "--listen-host", "127.0.0.1", "--listen-port", str(port),
        # lazy: never dial the provider unless a request is actually forwarded.
        "--set", "connection_strategy=lazy", "--set", f"confdir={output / 'mitm-config'}",
        "-s", str(Path(__file__).with_name("addon.py")),
        "--set", f"amr_mode={mode}", "--set", f"amr_flows={flows}", "--set", f"amr_audit={audit}",
    ]
    if mode == "record":
        proxy_command += ["-w", str(flows), "--set", "stream_large_bodies=1", "--set", "store_streamed_bodies=true"]
    harbor_command = [
        str(bin_dir / "harbor"), "run", "--yes", "--path", str(task), "--agent", "terminus-2", "--model", model,
        "--n-concurrent", "1", "--n-attempts", "1", "--max-retries", "0",
        "--jobs-dir", str(output / "jobs"), "--job-name", "trial",
        "--agent-kwarg", f"api_base=http://127.0.0.1:{port}/v1",
    ]
    for key, value in options.items():
        harbor_command += ["--agent-kwarg", f"{key}={json.dumps(value)}"]
    if mounts:
        binds = [{"type": "bind", "source": str(Path(source).resolve()), "target": target} for source, target in mounts]
        harbor_command += ["--mounts", json.dumps(binds)]
    for key, value in (environment_kwargs or {}).items():
        if key == "extra_docker_compose":
            for path in value:
                harbor_command += ["--extra-docker-compose", str(path)]
        else:
            harbor_command += ["--environment-kwarg", f"{key}={json.dumps(value)}"]
    for instruction in extra_instructions:
        harbor_command += ["--extra-instruction", instruction]
    for artifact in artifacts:
        harbor_command += ["--artifact", artifact]
    environment = child_env(env)
    if mode == "replay":
        environment["OPENAI_API_KEY"] = "offline-replay-placeholder"
    _write_json(output / "command.json", {"argv": harbor_command})
    print(f"{mode}: {output}", flush=True)
    with (output / "proxy.log").open("w") as proxy_log, (output / "harbor.log").open("w") as harbor_log:
        spawn = dict(env=environment, stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
        proxy = subprocess.Popen(proxy_command, stdout=proxy_log, **spawn)
        harbor = None
        try:
            _wait_listening(proxy, port)
            harbor = subprocess.Popen(harbor_command, stdout=harbor_log, **spawn)
            harbor_exit = harbor.wait()
        finally:
            if harbor is not None:
                _stop(harbor)
            _stop(proxy)
    proxy_summary = check_proxy_audit(audit, mode)
    if harbor_exit != 0:
        raise ReplayError(f"Harbor failed with exit {harbor_exit}; inspect {output / 'harbor.log'}")
    result = check_harbor_result(output / "jobs" / "trial", minimum_reward)
    summary = {"mode": mode, "model_requests": proxy_summary["total"], "reward": min(result["rewards"].values()),
               "rewards": result["rewards"], "output": str(output), "trial_dir": result["trial_dir"]}
    _write_json(output / "summary.json", summary)
    if mode == "record":
        _write_json(cassette / "manifest.json", {
            "schema_version": 1, "model": model, "upstream": upstream, "agent_kwargs": options,
            "artifacts": artifacts, "extra_instructions": extra_instructions,
            "harbor_version": importlib.metadata.version("harbor"),
            "mitmproxy_version": importlib.metadata.version("mitmproxy"),
            "model_requests": proxy_summary["total"],
        })
    return summary
