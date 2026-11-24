"""Pure local command-line entry points; no notebook or background service."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import __version__


def parser():
    result = argparse.ArgumentParser(prog="factorlab", description="Local daily-factor research")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text in (("run", "Run one configured experiment"),
                            ("scan", "Evaluate an explicit parameter grid on train/validation")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("config", type=Path)
        command.add_argument("--out", type=Path, help="Override output parent directory")
    commands.add_parser("factors", help="List built-in factor definitions")
    demo = commands.add_parser("demo", help="Create and run a fictional offline example")
    demo.add_argument("--out", type=Path, required=True, help="New directory; existing data is not replaced")
    from .backtest.window import add_parser
    add_parser(commands)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "factors":
            from .factors import CATALOG
            for name, spec in CATALOG.items():
                print(f"{name}: {spec['hypothesis']} [input: {spec['column']}]")
            return 0
        if args.command == "backtest":
            from .backtest.window import run_command
            print(json.dumps(run_command(args), ensure_ascii=False, indent=2))
            return 0
        if args.command == "demo":
            from .demo import create_demo
            from .experiments import run_file
            config = create_demo(args.out)
            output = run_file(config)
        elif args.command == "run":
            from .experiments import run_file
            output = run_file(args.config, output_root=args.out)
        else:
            from .experiments import scan_file
            output = scan_file(args.config, output_root=args.out)
        record = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        print(json.dumps({"status": record["status"], "output": str(output),
                          "report": str(output / "report.html")}, ensure_ascii=False, indent=2))
        return 2 if record["status"] == "completed_with_failures" else 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        print(f"factorlab: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
