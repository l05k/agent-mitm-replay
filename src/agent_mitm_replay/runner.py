"""Record a Harbor trace, validate it once, then measure offline replay runs."""

from __future__ import annotations

from datetime import datetime
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
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
    if mode == "validate" and summary["remaining"]:
        raise ReplayError(f"{summary['remaining']} recorded response(s) were never requested: {path}")
    return summary


def check_harbor_result(job: Path, minimum_reward: float | None) -> dict:
    """Always reject trial exceptions; only record and validate inspect rewards."""
    trials = list(job.glob("*/result.json"))
    if len(trials) != 1:
        raise ReplayError(f"Expected one trial result in {job}, found {len(trials)}")
    trial = _read_json(trials[0])
    if trial.get("exception_info") is not None:
        raise ReplayError(f"Harbor trial exception: {trials[0]}")
    if trial.get("step_results") is not None:
        raise ReplayError(f"Multi-step ([[steps]]) tasks are not supported: {trials[0]}")
    phases = {}
    for name in ("environment_setup", "agent_setup", "agent_execution", "verifier"):
        phase = trial.get(name) or {}
        if phase.get("started_at") and phase.get("finished_at"):
            phases[name] = (datetime.fromisoformat(phase["finished_at"])
                            - datetime.fromisoformat(phase["started_at"])).total_seconds()
    result = {"trial_dir": str(trials[0].parent), "phase_seconds": phases}
    if minimum_reward is not None:
        rewards = (trial.get("verifier_result") or {}).get("rewards") or {}
        if not rewards or not all(value >= minimum_reward for value in rewards.values()):
            raise ReplayError(f"Rewards {rewards} are below {minimum_reward}: {trials[0]}")
        result["rewards"] = rewards
    return result


def command_trace(trial_dir: Path) -> list:
    """Preserve command batches and arguments, excluding generated tool-call IDs."""
    trajectory = _read_json(trial_dir / "agent" / "trajectory.json")
    commands = [
        [{"function_name": call["function_name"], "arguments": call["arguments"]}
         for call in step["tool_calls"]]
        for step in trajectory["steps"] if step.get("tool_calls")
    ]
    if not commands:
        raise ReplayError(f"No tool calls in command trajectory: {trial_dir}")
    return commands


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
    minimum_reward: float | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """Record, validate once, then measure without rechecking rewards or commands."""
    if mode not in {"record", "validate", "replay"}:
        raise ReplayError(f"Unknown mode: {mode}")
    checked = mode != "replay"
    validation = cassette / "validation.json"
    if mode == "record":
        if not model or not upstream:
            raise ReplayError("record requires --model and --upstream")
        options = {"enable_summarize": False, "record_terminal_session": False,
                   **(agent_kwargs or {}), "session_id": f"amr-{uuid.uuid4().hex}"}
        artifacts, extra_instructions = list(artifacts or []), list(extra_instructions or [])
        minimum_reward = 1.0 if minimum_reward is None else minimum_reward
        cassette.mkdir(parents=True, mode=0o700, exist_ok=False)
        shutil.copyfile(task / "task.toml", cassette / "task.toml")
        environment_options = dict(environment_kwargs or {})
        if "extra_docker_compose" in environment_options:
            overlays = []
            (cassette / "environment").mkdir(mode=0o700)
            for index, path in enumerate(environment_options["extra_docker_compose"]):
                relative = f"environment/{index}.yaml"
                shutil.copyfile(path, cassette / relative)
                overlays.append(relative)
            environment_options["extra_docker_compose"] = overlays
    else:
        manifest = _read_json(cassette / "manifest.json")
        if manifest["schema_version"] != 2:
            raise ReplayError("Record a new cassette for the record/validate/replay workflow")
        if mode == "replay" and not validation.is_file():
            raise ReplayError("Run validate once before replay: the trace is not validated")
        if mode == "validate" and validation.exists():
            raise ReplayError("Trace already validated; use replay for measurements")
        model, upstream, options = manifest["model"], manifest["upstream"], manifest["agent_kwargs"]
        artifacts, extra_instructions = manifest["artifacts"], manifest["extra_instructions"]
        environment_options = manifest["environment_kwargs"]
        if mode == "validate" and minimum_reward is None:
            minimum_reward = manifest["minimum_reward"]
    upstream = upstream.rstrip("/")
    proxy_mode = "record" if mode == "record" else "replay"
    flows = (cassette / "flows.mitm").resolve()
    default_output = cassette / ("record" if mode == "record" else "validation")
    if mode == "replay":
        default_output = cassette / "replays" / uuid.uuid4().hex
    output = (output or default_output).resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    audit = output / "proxy-audit.jsonl"
    port = _free_port()
    bin_dir = Path(sys.executable).parent
    proxy_command = [
        str(bin_dir / "mitmdump"), "-q", "--mode", f"reverse:{upstream}",
        "--listen-host", "127.0.0.1", "--listen-port", str(port),
        "--set", "connection_strategy=lazy", "--set", f"confdir={output / 'mitm-config'}",
        "-s", str(Path(__file__).with_name("addon.py")),
        "--set", f"amr_mode={proxy_mode}", "--set", f"amr_flows={flows}", "--set", f"amr_audit={audit}",
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
    for key, value in environment_options.items():
        if key == "extra_docker_compose":
            for path in value:
                harbor_command += ["--extra-docker-compose", str((cassette / path).resolve())]
        else:
            harbor_command += ["--environment-kwarg", f"{key}={json.dumps(value)}"]
    for instruction in extra_instructions:
        harbor_command += ["--extra-instruction", instruction]
    for artifact in artifacts:
        harbor_command += ["--artifact", artifact]
    environment = child_env(env)
    if mode != "record":
        environment["OPENAI_API_KEY"] = "offline-replay-placeholder"
    _write_json(output / "command.json", {"argv": harbor_command})
    print(f"{mode}: {output}", flush=True)
    with (output / "proxy.log").open("w") as proxy_log, (output / "harbor.log").open("w") as harbor_log:
        spawn = dict(env=environment, stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
        proxy = subprocess.Popen(proxy_command, stdout=proxy_log, **spawn)
        harbor = None
        try:
            _wait_listening(proxy, port)
            started = time.perf_counter()
            harbor = subprocess.Popen(harbor_command, stdout=harbor_log, **spawn)
            harbor_exit = harbor.wait()
            elapsed = time.perf_counter() - started
        finally:
            if harbor is not None:
                _stop(harbor)
            _stop(proxy)
    proxy_summary = check_proxy_audit(audit, mode)
    if harbor_exit != 0:
        raise ReplayError(f"Harbor failed with exit {harbor_exit}; inspect {output / 'harbor.log'}")
    result = check_harbor_result(output / "jobs" / "trial", minimum_reward if checked else None)
    if checked:
        commands = command_trace(Path(result["trial_dir"]))
        if mode == "validate" and commands != json.loads((cassette / "commands.json").read_text()):
            raise ReplayError(f"Command trajectory differs from recording: {result['trial_dir']}")
    summary = {"mode": mode, "checked": checked, "measured": mode == "replay", "elapsed_sec": elapsed,
               "model_requests": proxy_summary["requests"], "output": str(output), **result}
    if checked:
        summary["reward"] = min(result["rewards"].values())
    _write_json(output / "summary.json", summary)
    if mode == "record":
        _write_json(cassette / "commands.json", commands)
        _write_json(cassette / "manifest.json", {
            "schema_version": 2, "model": model, "upstream": upstream, "agent_kwargs": options,
            "environment_kwargs": environment_options, "minimum_reward": minimum_reward,
            "artifacts": artifacts, "extra_instructions": extra_instructions,
            "harbor_version": importlib.metadata.version("harbor"),
            "mitmproxy_version": importlib.metadata.version("mitmproxy"),
            "model_requests": proxy_summary["total"],
        })
    elif mode == "validate":
        _write_json(validation, {"output": str(output), "reward": summary["reward"],
                                 "minimum_reward": minimum_reward})
    return summary
