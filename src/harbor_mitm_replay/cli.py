from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .runner import ReplayError, run_trial


def _key_value(value: str) -> tuple[str, object]:
    key, separator, raw = value.partition("=")
    if not separator or not key:
        raise argparse.ArgumentTypeError("Expected KEY=VALUE")
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = raw
    return key, parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Record and replay one Harbor Terminus-2 trial with mitmproxy.")
    subcommands = parser.add_subparsers(dest="mode", required=True)
    for mode in ("record", "replay"):
        command = subcommands.add_parser(mode)
        command.add_argument("--task", type=Path, required=True)
        command.add_argument("--cassette", type=Path, required=True)
        command.add_argument("--output", type=Path)
        command.add_argument("--minimum-reward", type=float, default=1.0)
        command.add_argument("--agent-kwarg", action="append", type=_key_value, default=[], metavar="KEY=VALUE")
        command.add_argument("--environment-kwarg", action="append", type=_key_value, default=[], metavar="KEY=VALUE")
        command.add_argument("--extra-instruction", action="append")
        command.add_argument("--artifact", action="append")
        if mode == "record":
            command.add_argument("--model", required=True, help="LiteLLM model name, e.g. openai/gpt-4o-mini")
            command.add_argument("--upstream", required=True, help="Provider origin, e.g. https://api.openai.com")
    smoke = subcommands.add_parser("smoke", help="Run a real Docker Harbor trial with a local scripted model")
    smoke.add_argument("--output", type=Path, default=Path("smoke-output"))
    smoke.add_argument("--image", help="Existing image with bash, tmux and C.UTF-8 (skip Dockerfile build)")
    args = parser.parse_args()
    try:
        if args.mode == "smoke":
            from .smoke import run_smoke
            result = run_smoke(args.output, image=args.image)
        else:
            result = run_trial(
                args.mode, task=args.task, cassette=args.cassette,
                model=getattr(args, "model", None), upstream=getattr(args, "upstream", None),
                output=args.output, agent_kwargs=dict(args.agent_kwarg),
                environment_kwargs=dict(args.environment_kwarg),
                extra_instructions=args.extra_instruction, artifacts=args.artifact,
                minimum_reward=args.minimum_reward,
            )
        print(json.dumps(result, indent=2))
        return 0
    except (ReplayError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; child processes stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
