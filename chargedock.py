"""Entry point. The polling child and elevated helper intentionally don't load Qt."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path


def arguments():
    parser = argparse.ArgumentParser(description="电量岛 · ChargeDock")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--dji-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--quit", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-output", type=Path)
    parser.add_argument("--poll-key", choices=("mouse", "keyboard", "headset"), action="append")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--asus-action", choices=("enable", "disable"))
    parser.add_argument("--result", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--snapshot-theme", choices=("light", "dark"), default="light")
    parser.add_argument("--snapshot-settings", action="store_true")
    parser.add_argument("--smoke-test", type=Path)
    parser.add_argument("--test-exit-ms", type=int)
    return parser.parse_args()


def main():
    args = arguments()
    if args.dji_worker:
        if args.data_dir is None:
            return 2
        from dji_worker import run
        return run(args.data_dir)
    if args.asus_action:
        if args.result is None:
            return 2
        from asus_control import helper_action
        return helper_action(args.asus_action, args.result)
    if args.poll_output:
        from core import poll_to_file
        return poll_to_file(args.poll_output, args.poll_key)
    if args.once:
        from core import read_all
        value = json.dumps([asdict(r) for r in read_all()], ensure_ascii=False)
        if args.result:
            args.result.write_text(value, encoding="utf-8")
        elif sys.stdout:
            print(value)
        return 0
    from ui import run
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
