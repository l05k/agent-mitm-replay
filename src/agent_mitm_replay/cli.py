import argparse
import json
from pathlib import Path
import sys

from .runner import AGENTS, CN_MIRRORS, ReplayError, run_trial


def _mount(value: str) -> tuple[str, str]:
    source, separator, target = value.partition(":")
    if not separator or not source or not target.startswith("/"):
        raise argparse.ArgumentTypeError("Expected HOST_DIR:/container/path")
    return source, target


def _mirror_options(parser) -> None:
    parser.add_argument("--install-mirror", choices=("official", "cn"), default="official")
    parser.add_argument("--node-mirror", help="Node.js binary mirror base URL")
    parser.add_argument("--npm-registry", help="npm registry URL")
    parser.add_argument("--nvm-source", help="NVM git repository URL (bootstrap URL is controlled by Harbor)")


def _mirrors(args) -> dict[str, str]:
    result = dict(CN_MIRRORS) if args.install_mirror == "cn" else {}
    for key, value in (("node", args.node_mirror), ("npm", args.npm_registry), ("nvm", args.nvm_source)):
        if value:
            result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture or measure a Harbor built-in agent trajectory.")
    commands = parser.add_subparsers(dest="mode", required=True)
    for mode in ("record", "replay"):
        command = commands.add_parser(mode)
        command.add_argument("--task", type=Path, required=True)
        command.add_argument("--cassette", type=Path, required=True)
        command.add_argument("--output", type=Path)
        command.add_argument("--mount", action="append", type=_mount, default=[])
        if mode == "record":
            command.add_argument("--agent", choices=AGENTS, required=True)
            command.add_argument("--model", required=True, help="openai/<model>")
            command.add_argument("--upstream", required=True, help="Provider base URL, including the API prefix")
            command.add_argument("--agent-version", help="Native CLI version installed by Harbor")
            command.add_argument("--model-api", choices=("responses", "chat-completions"),
                                 help="Wire protocol; defaults to Responses for Codex, Chat Completions otherwise")
            _mirror_options(command)
    smoke = commands.add_parser("smoke", help="Exercise Harbor's built-in agents in Docker with scripted replies")
    smoke.add_argument("--output", type=Path, required=True)
    smoke.add_argument("--image", required=True, help="Base Docker image; Harbor installs the selected pinned agents")
    smoke.add_argument("--agent", choices=AGENTS, action="append",
                       help="Built-in agent to check (repeatable; default: all)")
    _mirror_options(smoke)
    args = parser.parse_args()
    try:
        if args.mode == "smoke":
            from .smoke import run_smoke
            result = run_smoke(args.output, args.image, agents=args.agent, install_mirrors=_mirrors(args))
        else:
            result = run_trial(
                args.mode, task=args.task, cassette=args.cassette, output=args.output, mounts=args.mount,
                agent=getattr(args, "agent", None), model=getattr(args, "model", None),
                upstream=getattr(args, "upstream", None),
                agent_version=getattr(args, "agent_version", None), model_api=getattr(args, "model_api", None),
                install_mirrors=_mirrors(args) if args.mode == "record" else None,
            )
    except (ReplayError, OSError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
