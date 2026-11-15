"""Capture and measure the same native harness on a fresh Harbor task."""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

from .native import AGENTS, write_config
from .trace import TraceError, read_codex, read_openclaw, signature, tool_seconds


class ReplayError(RuntimeError):
    """Invalid setup or unsuccessful trial."""


class TrialFailure(ReplayError):
    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def task_digest(task: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(task.rglob("*")):
        if path.is_file():
            digest.update(os.fsencode(path.relative_to(task)) + b"\0")
            digest.update(path.stat().st_mode.to_bytes(4, "big"))
            with path.open("rb") as file:
                digest.update(hashlib.file_digest(file, "sha256").digest())
    return digest.hexdigest()


def child_env(gateway: str, env: dict[str, str] | None = None) -> dict[str, str]:
    child = {**os.environ, **(env or {})}
    exclusions = [part.strip() for source in (os.environ, env or {})
                  for key in ("NO_PROXY", "no_proxy") for part in source.get(key, "").split(",") if part.strip()]
    no_proxy = ",".join(dict.fromkeys([*exclusions, "127.0.0.1", "localhost", gateway]))
    child.update(NO_PROXY=no_proxy, no_proxy=no_proxy, HARBOR_TELEMETRY="0",
                 LITELLM_LOCAL_MODEL_COST_MAP="True")
    return child


def docker_gateway() -> str:
    output = subprocess.check_output(["docker", "network", "inspect", "bridge"], text=True)
    for network in json.loads(output)[0]["IPAM"]["Config"]:
        if (gateway := network.get("Gateway")) and ":" not in gateway:
            return gateway
    raise ReplayError("The local Docker bridge must have a reachable IPv4 gateway")


def _port(host: str) -> int:
    with socket.socket() as listener:
        listener.bind((host, 0))
        return listener.getsockname()[1]


def _wait_listening(process: subprocess.Popen, host: str, port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and process.poll() is None:
        try:
            socket.create_connection((host, port), timeout=0.1).close()
            return
        except OSError:
            time.sleep(0.05)
    raise ReplayError("mitmdump did not start; inspect proxy.log")


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def check_proxy_audit(path: Path, *, recording: bool) -> dict:
    if not path.is_file() or not (lines := path.read_text().splitlines()):
        raise TrialFailure("error", f"Missing proxy audit: {path}")
    summary = json.loads(lines[-1])
    if summary["event"] != "summary" or summary["errors"] or summary["total"] < 1:
        raise TrialFailure("error", f"Incomplete or failed model exchange: {path}")
    if summary["misses"] or summary["remaining"] or summary["total"] != summary["requests"]:
        raise TrialFailure("divergence" if not recording else "error", f"Model turns differ: {path}")
    return summary


def check_harbor_result(job: Path) -> dict:
    trials = list(job.glob("*/result.json"))
    if len(trials) != 1:
        raise TrialFailure("error", f"Expected one Harbor trial result in {job}, found {len(trials)}")
    path, = trials
    trial = _read_json(path)
    if info := trial.get("exception_info"):
        kind = info["exception_type"]
        status = "timeout" if "Timeout" in kind else "error"
        raise TrialFailure(status, f"Harbor {kind}: {path}")
    if trial.get("step_results") is not None:
        raise TrialFailure("error", f"Multi-step Harbor tasks are not supported: {path}")
    phases = {}
    for name in ("environment_setup", "agent_setup", "agent_execution", "verifier"):
        phase = trial.get(name) or {}
        if phase.get("started_at") and phase.get("finished_at"):
            phases[name] = (datetime.fromisoformat(phase["finished_at"])
                            - datetime.fromisoformat(phase["started_at"])).total_seconds()
    return {"trial_dir": str(path.parent), "phase_seconds": phases,
            "rewards": (trial.get("verifier_result") or {}).get("rewards") or {}}


def run_trial(mode: str, *, task: Path, cassette: Path, agent: str | None = None,
              model: str | None = None, upstream: str | None = None,
              output: Path | None = None, mounts: list[tuple[str, str]] | None = None,
              env: dict[str, str] | None = None) -> dict:
    if mode not in {"record", "replay"}:
        raise ReplayError(f"Unknown mode: {mode}")
    recording = mode == "record"
    digest = task_digest(task)
    if recording:
        if agent not in AGENTS or not model or not upstream:
            raise ReplayError("record requires --agent, --model and --upstream")
        manifest = {"schema_version": 3, "agent": agent, "agent_version": AGENTS[agent].VERSION,
                    "model": model, "upstream": upstream, "task_sha256": digest,
                    "mounts": [target for _, target in mounts or []]}
    else:
        manifest = _read_json(cassette / "manifest.json")
        if manifest["schema_version"] != 3:
            raise ReplayError("Record a new cassette with native tool events")
        agent, model, upstream = manifest["agent"], manifest["model"], manifest["upstream"]
        if manifest["agent_version"] != AGENTS[agent].VERSION or manifest["task_sha256"] != digest:
            raise ReplayError("Harness version or task inputs differ from the recording")
        if manifest["mounts"] != [target for _, target in mounts or []]:
            raise ReplayError("Task mount destinations differ from the recording")
        reference = _read_json(cassette / "tools.json")
    if not model.startswith("openai/") or not model.removeprefix("openai/"):
        raise ReplayError("Use an openai/<model> identifier for the compatible gateway")
    url = urlsplit(upstream)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.query or url.fragment:
        raise ReplayError("--upstream must be an HTTP(S) model base URL without credentials or query")
    if recording:
        cassette.mkdir(parents=True, mode=0o700, exist_ok=False)
    output = (output or (cassette / ("record" if recording else f"replays/{uuid.uuid4().hex}"))).resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)

    gateway = docker_gateway()
    port = _port(gateway)
    proxy_url = f"http://{gateway}:{port}"
    api_prefix = url.path.rstrip("/") or "/v1"
    base_url = proxy_url + api_prefix
    model_path = api_prefix + ("/responses" if agent == "codex" else "/chat/completions")
    config_dir = output / "agent-config"
    audit = output / "proxy-audit.jsonl"
    token = uuid.uuid4().hex
    telemetry = output / "codex-tools.jsonl"
    write_config(config_dir, agent, model.removeprefix("openai/"), base_url, proxy_url, token)
    bin_dir = Path(sys.executable).parent
    proxy_command = [
        str(bin_dir / "mitmdump"), "-q", "--mode", f"reverse:{url.scheme}://{url.netloc}",
        "--listen-host", gateway, "--listen-port", str(port),
        "--set", "connection_strategy=lazy", "--set", "store_streamed_bodies=true",
        "--set", f"confdir={output / 'mitm-config'}", "-s", str(Path(__file__).with_name("addon.py")),
        "--set", f"amr_mode={mode}", "--set", f"amr_flows={cassette.resolve() / 'flows.mitm'}",
        "--set", f"amr_audit={audit}", "--set", f"amr_model_path={model_path}",
    ]
    if agent == "codex":
        proxy_command += ["--set", f"amr_telemetry={telemetry}"]
    sources = [{"type": "bind", "source": str(Path(source).resolve()), "target": target,
                "bind": {"create_host_path": False}} for source, target in mounts or []]
    sources.append({"type": "bind", "source": str(config_dir), "target": "/opt/amr", "read_only": True})
    command = [str(bin_dir / "harbor"), "run", "--yes", "--path", str(task),
               "--agent", AGENTS[agent].import_path(), "--model", model,
               "--n-concurrent", "1", "--n-attempts", "1", "--max-retries", "0",
               "--jobs-dir", str(output / "jobs"), "--job-name", "trial",
               "--allow-agent-host", gateway, "--mounts", json.dumps(sources)]
    environment = child_env(gateway, env)
    key = environment.pop("OPENAI_API_KEY", "")
    environment["AMR_TRIAL_TOKEN"] = token
    proxy_env = {**environment, "AMR_UPSTREAM_KEY": key if recording else ""}
    _write_json(output / "command.json", {"argv": command})
    print(f"{mode}: {output}", flush=True)
    with (output / "proxy.log").open("w") as proxy_log, (output / "harbor.log").open("w") as harbor_log:
        spawn = dict(stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
        proxy = subprocess.Popen(proxy_command, env=proxy_env, stdout=proxy_log, **spawn)
        try:
            _wait_listening(proxy, gateway, port)
            started = time.perf_counter()
            harbor = subprocess.Popen(command, env=environment, stdout=harbor_log, **spawn)
            try:
                code = harbor.wait()
                elapsed = time.perf_counter() - started
            finally:
                _stop(harbor)
        finally:
            _stop(proxy)

    try:
        result = check_harbor_result(output / "jobs" / "trial")
        if code:
            raise TrialFailure("error", f"Harbor exited {code}; inspect {output / 'harbor.log'}")
        proxy_summary = check_proxy_audit(audit, recording=recording)
        tools = (read_codex(telemetry) if agent == "codex" else
                 read_openclaw(Path(result["trial_dir"]) / "agent" / "openclaw-tools.jsonl"))
        commands = signature(tools)
        if not recording and commands != reference:
            _write_json(output / "tools.json", commands)
            raise TrialFailure("divergence", "Tool actions or outcomes differ from the capture")
        seconds = tool_seconds(tools, proxy_summary["model_spans"])
    except (TrialFailure, TraceError) as error:
        status = error.status if isinstance(error, TrialFailure) else "error"
        _write_json(output / "summary.json", {"mode": mode, "status": status, "error": str(error)})
        raise

    summary = {"mode": mode, "status": "ok", "agent": agent, "tool_s": seconds,
               "harbor_s": elapsed, "model_requests": proxy_summary["requests"],
               "output": str(output), **result}
    _write_json(output / "summary.json", summary)
    if recording:
        _write_json(cassette / "tools.json", commands)
        _write_json(cassette / "manifest.json", manifest)
    return summary
