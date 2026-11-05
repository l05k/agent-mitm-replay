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
        return key, json.loads(raw)
    except ValueError:
        return key, raw


def _mount(value: str) -> tuple[str, str]:
    source, separator, target = value.partition(":")
    if not separator or not source or not target.startswith("/"):
        raise argparse.ArgumentTypeError("Expected HOST_DIR:/container/path")
    return source, target


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a Harbor trace, validate once, then measure offline replay.")
    subcommands = parser.add_subparsers(dest="mode", required=True)
    for mode in ("record", "validate", "replay"):
        command = subcommands.add_parser(mode)
        command.add_argument("--task", type=Path, required=True)
        command.add_argument("--cassette", type=Path, required=True)
        command.add_argument("--output", type=Path)
        command.add_argument("--mount", action="append", type=_mount, default=[], metavar="HOST_DIR:CONTAINER_DIR",
                             help="Bind-mount a host directory into the task container")
        if mode == "record":
            command.add_argument("--environment-kwarg", action="append", type=_key_value, default=[], metavar="KEY=VALUE")
            command.add_argument("--extra-instruction", action="append")
            command.add_argument("--artifact", action="append")
            command.add_argument("--model", required=True, help="LiteLLM model name, e.g. openai/gpt-4o-mini")
            command.add_argument("--upstream", required=True, help="Provider origin, e.g. https://api.openai.com")
            command.add_argument("--agent-kwarg", action="append", type=_key_value, default=[], metavar="KEY=VALUE")
        if mode != "replay":
            command.add_argument("--minimum-reward", type=float)
    smoke = subcommands.add_parser("smoke", help="Record, validate, and measure a Docker trial with a scripted model")
    smoke.add_argument("--output", type=Path, default=Path("smoke-output"))
    args = parser.parse_args()
    try:
        if args.mode == "smoke":
            from .smoke import run_smoke
            result = run_smoke(args.output)
        else:
            result = run_trial(
                args.mode, task=args.task, cassette=args.cassette,
                model=getattr(args, "model", None), upstream=getattr(args, "upstream", None),
                output=args.output, agent_kwargs=dict(getattr(args, "agent_kwarg", [])),
                environment_kwargs=dict(getattr(args, "environment_kwarg", [])), mounts=args.mount,
                extra_instructions=getattr(args, "extra_instruction", None), artifacts=getattr(args, "artifact", None),
                minimum_reward=getattr(args, "minimum_reward", None),
            )
        print(json.dumps(result, indent=2))
        return 0
    except (ReplayError, OSError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; child processes stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
