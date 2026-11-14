"""Runtime entry point. Account setup is a separate owner operation."""
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telegram-read-only")
    parser.add_argument("--allowlist", help="owner-managed policy path")
    parser.add_argument("command", choices=["serve"])
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    from .server import serve

    serve(args.allowlist)


if __name__ == "__main__":
    main()
