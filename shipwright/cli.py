"""Small direct-provider command for smoke tests and local experimentation."""

from __future__ import annotations

import argparse
import sys

from .providers import ProviderError, create_provider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send one prompt through a Shipwright provider.")
    parser.add_argument("prompt", nargs="?", default="Reply with exactly: SHIPWRIGHT_OK")
    parser.add_argument("--provider", help="Override SHIPWRIGHT_PROVIDER for this invocation")
    parser.add_argument("--model", help="Override SHIPWRIGHT_MODEL for this invocation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = None
    if args.provider or args.model:
        import os

        env = dict(os.environ)
        if args.provider:
            env["SHIPWRIGHT_PROVIDER"] = args.provider
        if args.model:
            env["SHIPWRIGHT_MODEL"] = args.model
    try:
        provider = create_provider(env=env)
        response = provider.complete([{"role": "user", "content": args.prompt}])
    except (ProviderError, ValueError) as exc:
        print(f"shipwright-chat: {exc}", file=sys.stderr)
        return 1
    print(response.content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
