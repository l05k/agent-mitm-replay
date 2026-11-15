import argparse
import json
from pathlib import Path
import sys

from .native import AGENTS
from .runner import ReplayError, run_trial


def _mount(value: str) -> tuple[str, str]:
    source, separator, target = value.partition(":")
    if not separator or not source or not target.startswith("/"):
        raise argparse.ArgumentTypeError("Expected HOST_DIR:/container/path")
    return source, target


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture or measure a native Harbor agent trajectory.")
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
    smoke = commands.add_parser("smoke", help="Exercise both native harnesses in Docker with scripted replies")
    smoke.add_argument("--output", type=Path, required=True)
    smoke.add_argument("--image", required=True, help="Prebuilt Docker image containing both pinned harnesses")
    args = parser.parse_args()
    try:
        if args.mode == "smoke":
            from .smoke import run_smoke
            result = run_smoke(args.output, args.image)
        else:
            params = vars(args)
            mode, params['mounts'] = params.pop('mode'), params.pop('mount')
            result = run_trial(mode, **params)
    except (ReplayError, OSError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
