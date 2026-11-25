"""Record and replay model responses for Harbor's built-in agents."""

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

AGENTS = {"codex": "0.169.0", "openclaw": "2026.7.1", "pi": "0.85.1"}
CN_MIRRORS = {
    "node": "https://npmmirror.com/mirrors/node",
    "npm": "https://registry.npmmirror.com",
    "nvm": "https://gitee.com/mirrors/nvm.git",
}


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
    if phases.get("agent_execution", 0) <= 0:
        raise TrialFailure("error", f"Missing or invalid Harbor agent_execution timing: {path}")
    return {"trial_dir": str(path.parent), "phase_seconds": phases,
            "rewards": (trial.get("verifier_result") or {}).get("rewards") or {}}


def command_trace(trial_dir: Path) -> list:
    """Compare Harbor's standard tool calls, not agent-specific telemetry."""
    trajectory = _read_json(trial_dir / "agent" / "trajectory.json")
    return [[{"function_name": call["function_name"], "arguments": call["arguments"]}
             for call in step["tool_calls"]]
            for step in trajectory["steps"] if step.get("tool_calls")]


def run_trial(mode: str, *, task: Path, cassette: Path, agent: str | None = None,
              model: str | None = None, upstream: str | None = None,
              output: Path | None = None, mounts: list[tuple[str, str]] | None = None,
              env: dict[str, str] | None = None,
              install_mirrors: dict[str, str] | None = None,
              agent_version: str | None = None, model_api: str | None = None) -> dict:
    if mode not in {"record", "replay"}:
        raise ReplayError(f"Unknown mode: {mode}")
    recording = mode == "record"
    digest = task_digest(task)
    if recording:
        if agent not in AGENTS or not model or not upstream:
            raise ReplayError("record requires --agent, --model and --upstream")
        agent_version = agent_version or AGENTS[agent]
        model_api = model_api or ("responses" if agent == "codex" else "chat-completions")
        if model_api not in {"responses", "chat-completions"}:
            raise ReplayError(f"Unsupported model API: {model_api}")
        if agent != "codex" and model_api != "chat-completions":
            raise ReplayError("Pi and OpenClaw require chat-completions in this runner")
        install_mirrors = dict(install_mirrors or {})
        for name, address in install_mirrors.items():
            parsed = urlsplit(address)
            if name not in CN_MIRRORS or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ReplayError(f"Invalid installer mirror: {name}={address}")
        manifest = {"schema_version": 4, "agent": agent, "agent_version": agent_version,
                    "model_api": model_api, "model": model, "upstream": upstream, "task_sha256": digest,
                    "mounts": [target for _, target in mounts or []], "install_mirrors": install_mirrors}
    else:
        manifest = _read_json(cassette / "manifest.json")
        if manifest["schema_version"] != 4:
            raise ReplayError("Record a new cassette with Harbor's built-in agents")
        agent, model, upstream = manifest["agent"], manifest["model"], manifest["upstream"]
        install_mirrors = manifest["install_mirrors"]
        agent_version, model_api = manifest["agent_version"], manifest["model_api"]
        if manifest["task_sha256"] != digest:
            raise ReplayError("Task inputs differ from the recording")
        if manifest["mounts"] != [target for _, target in mounts or []]:
            raise ReplayError("Task mount destinations differ from the recording")
        reference = _read_json(cassette / "commands.json")
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
    model_path = api_prefix + ("/responses" if model_api == "responses" else "/chat/completions")
    audit = output / "proxy-audit.jsonl"
    token = uuid.uuid4().hex
    bin_dir = Path(sys.executable).parent
    proxy_command = [
        str(bin_dir / "mitmdump"), "-q", "--mode", f"reverse:{url.scheme}://{url.netloc}",
        "--listen-host", gateway, "--listen-port", str(port),
        "--set", "connection_strategy=lazy", "--set", "store_streamed_bodies=true",
        "--set", f"confdir={output / 'mitm-config'}", "-s", str(Path(__file__).with_name("addon.py")),
        "--set", f"amr_mode={mode}", "--set", f"amr_flows={cassette.resolve() / 'flows.mitm'}",
        "--set", f"amr_audit={audit}", "--set", f"amr_model_path={model_path}",
    ]
    sources = [{"type": "bind", "source": str(Path(source).resolve()), "target": target,
                "bind": {"create_host_path": False}} for source, target in mounts or []]
    command = [str(bin_dir / "harbor"), "run", "--yes", "--path", str(task),
               "--agent", agent, "--model", model,
               "--n-concurrent", "1", "--n-attempts", "1", "--max-retries", "0",
               "--jobs-dir", str(output / "jobs"), "--job-name", "trial",
               "--environment-kwarg", "auto_mounting=true",
               "--allow-agent-host", gateway, "--mounts", json.dumps(sources)]
    if install_mirrors:
        names = {"node": "NVM_NODEJS_ORG_MIRROR", "npm": "npm_config_registry", "nvm": "NVM_SOURCE"}
        installer_env = {names[name]: value for name, value in install_mirrors.items()}
        if "nvm" in install_mirrors:
            installer_env["METHOD"] = "git"
        overlay = output / "installer-mirrors.json"
        _write_json(overlay, {"services": {"main": {"environment": installer_env}}})
        command += ["--extra-docker-compose", str(overlay)]
    options = {"version": agent_version}
    if agent == "pi":
        options["model_api"] = "openai-completions"
        options["thinking"] = "off"
    elif agent == "codex":
        options["config"] = {"model_provider": "replay", "model_providers": {"replay": {
            "name": "Replay proxy", "base_url": base_url, "env_key": "OPENAI_API_KEY",
            "wire_api": "responses" if model_api == "responses" else "chat", "supports_websockets": False,
            "request_max_retries": 0, "stream_max_retries": 0,
        }}}
    elif agent == "openclaw":
        model_id = model.removeprefix("openai/")
        options["openclaw_config"] = {
            "agents": {"defaults": {"model": {"primary": model}}},
            "logging": {"level": "silent", "consoleLevel": "silent"},
            "models": {"providers": {"openai": {
                "api": "openai-completions",
                "models": [{"id": model_id, "name": model_id, "api": "openai-completions"}],
            }}},
        }
    for name, value in options.items():
        command += ["--agent-kwarg", f"{name}={json.dumps(value)}"]
    for name, value in {"OPENAI_BASE_URL": base_url, "OPENAI_API_BASE": base_url,
                        "OPENAI_API_KEY": token, "NO_PROXY": f"{gateway},127.0.0.1,localhost"}.items():
        command += ["--agent-env", f"{name}={value}"]
    environment = child_env(gateway, env)
    key = environment.pop("OPENAI_API_KEY", "")
    environment["OPENAI_API_KEY"] = token
    proxy_env = {**environment, "AMR_TRIAL_TOKEN": token, "AMR_UPSTREAM_KEY": key if recording else ""}
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
        commands = command_trace(Path(result["trial_dir"]))
        if not recording and commands != reference:
            raise TrialFailure("divergence", "Harbor tool-call trajectory differs from the recording")
    except (TrialFailure, OSError) as error:
        status = error.status if isinstance(error, TrialFailure) else "error"
        detail = str(error)
        if audit.is_file():
            for line in audit.read_text().splitlines():
                event = json.loads(line)
                if event.get("event") == "error" and "status" in event:
                    upstream_error = event.get("upstream_error", {})
                    detail += (f"; upstream {event['method']} {event['path']} HTTP {event['status']}"
                               f": {json.dumps(upstream_error, ensure_ascii=False)}")
                    break
        _write_json(output / "summary.json", {"mode": mode, "status": status, "error": detail})
        raise TrialFailure(status, detail) from error

    summary = {"mode": mode, "status": "ok", "agent": agent,
               "agent_execution_s": result["phase_seconds"]["agent_execution"],
               "harbor_s": elapsed, "model_requests": proxy_summary["requests"],
               "output": str(output), **result}
    _write_json(output / "summary.json", summary)
    if recording:
        _write_json(cassette / "commands.json", commands)
        _write_json(cassette / "manifest.json", manifest)
    return summary
