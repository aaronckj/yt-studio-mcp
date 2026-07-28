"""CLI entry: `yt-studio-mcp` serves; `yt-studio-mcp auth` authorizes."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        from .scan_cli import main as scan_main

        raise SystemExit(scan_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "collect":
        from .collect_cli import main as collect_main

        raise SystemExit(collect_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        from .watch_cli import main as watch_main

        raise SystemExit(watch_main(sys.argv[2:]))

    parser = argparse.ArgumentParser(prog="yt-studio-mcp")
    sub = parser.add_subparsers(dest="command")
    auth_p = sub.add_parser("auth", help="one-time interactive OAuth consent")
    auth_p.add_argument(
        "--client-secret",
        required=True,
        help="path to the OAuth client_secret.json downloaded from Google Cloud",
    )
    sub.add_parser("serve", help="run the MCP server (default)")
    sub.add_parser("scan", help="headless spam scan for cron (see scan --help)")
    sub.add_parser("collect", help="freeze giveaway entries at close (see collect --help)")
    sub.add_parser("watch", help="live-stream moderation loop (see watch --help)")
    args = parser.parse_args()

    if args.command == "auth":
        from .auth import run_auth_flow

        print(run_auth_flow(args.client_secret), file=sys.stderr)
        return

    from .server import build_app

    build_app().run()


if __name__ == "__main__":
    main()
