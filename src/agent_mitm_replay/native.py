"""Harbor adapters for the pinned, preinstalled native benchmark harnesses."""

import json
import os
import shlex

from harbor.agents.base import BaseAgent
from harbor.agents.installed.base import NonZeroAgentExitCodeError


class NativeReplay(BaseAgent):
    VERSION: str

    def version(self) -> str:
        return self.VERSION

    async def setup(self, environment) -> None:
        result = await environment.exec(command=f"{self.name()} --version")
        versions = (result.stdout or "").split()
        if result.return_code or self.VERSION not in versions:
            raise NonZeroAgentExitCodeError(
                f"Preinstall {self.name()} {self.VERSION} on the task image's PATH; got {result.stdout!r}"
            )

    async def run(self, instruction, environment, context) -> None:
        model = shlex.quote(self.model_name.removeprefix("openai/"))
        prompt = shlex.quote(instruction)
        command = (
            f"set -e; {self.command_line(model, prompt)} "
            ">/logs/agent/native.stdout 2>/logs/agent/native.stderr"
        )
        result = await environment.exec(command=command, env={"OPENAI_API_KEY": os.environ["AMR_TRIAL_TOKEN"]})
        if result.return_code:
            raise NonZeroAgentExitCodeError(f"{self.name()} exited {result.return_code}; see native.stderr")


class CodexReplay(NativeReplay):
    VERSION = "0.169.0"

    @staticmethod
    def name() -> str:
        return "codex"

    @staticmethod
    def command_line(model: str, prompt: str) -> str:
        return (
            "export CODEX_HOME=/tmp/amr-codex; mkdir -m 700 \"$CODEX_HOME\"; "
            "cp /opt/amr/codex.toml \"$CODEX_HOME/config.toml\"; "
            "exec codex exec --json --skip-git-repo-check "
            f"--dangerously-bypass-approvals-and-sandbox --model {model} -- {prompt}"
        )


class OpenClawReplay(NativeReplay):
    VERSION = "2026.11.3"

    @staticmethod
    def name() -> str:
        return "openclaw"

    @staticmethod
    def command_line(model: str, prompt: str) -> str:
        # Use the task image's workdir, not the login user's home workspace.
        return (
            "export OPENCLAW_STATE_DIR=/tmp/amr-openclaw; mkdir -m 700 \"$OPENCLAW_STATE_DIR\"; "
            "cp /opt/amr/openclaw.json \"$OPENCLAW_STATE_DIR/openclaw.json\"; "
            "cp /opt/amr/amr-tool-trace.mjs \"$OPENCLAW_STATE_DIR/\"; "
            f"exec openclaw agent --local --json --model openai/{model} --message {prompt}"
        )


AGENTS = {cls.name(): cls for cls in (CodexReplay, OpenClawReplay)}


def write_config(directory, agent: str, model: str, base_url: str, proxy_url: str, token: str) -> None:
    directory.mkdir(mode=0o755)
    if agent == "codex":
        # JSON string escaping is valid for these simple TOML string values.
        (directory / "codex.toml").write_text(f'''model_provider = "amr"
web_search = "disabled"

[analytics]
enabled = false

[feedback]
enabled = false

[model_providers.amr]
name = "Replay proxy"
base_url = {json.dumps(base_url)}
env_key = "OPENAI_API_KEY"
wire_api = "responses"
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0

[otel]
exporter = {{ otlp-http = {{ endpoint = {json.dumps(proxy_url + '/amr-logs')}, protocol = "json", headers = {{ authorization = {json.dumps('Bearer ' + token)} }} }} }}
log_sse_deltas = false
metrics_exporter = "none"

[otel.tool_result]
max_bytes = 1048576
''')
    else:
        (directory / "openclaw.json").write_text(json.dumps({
            "gateway": {"mode": "local"},
            "agents": {"defaults": {"workspace": ".", "model": f"openai/{model}",
                                     "skipBootstrap": True}},
            "models": {"mode": "replace", "providers": {"openai": {
                "baseUrl": base_url, "apiKey": "${OPENAI_API_KEY}", "api": "openai-completions",
                "models": [{"id": model, "name": model}],
            }}},
            "tools": {"deny": ["message", "group:web"],
                      "exec": {"host": "gateway", "security": "full", "ask": "off"}},
            "plugins": {"allow": ["amr-tool-trace", "openai"],
                        "load": {"paths": ["/tmp/amr-openclaw/amr-tool-trace.mjs"]},
                        "entries": {"amr-tool-trace": {"enabled": True}}},
        }))
        from importlib.resources import files
        plugin = files(__package__).joinpath("amr-tool-trace.mjs")
        (directory / plugin.name).write_bytes(plugin.read_bytes())
