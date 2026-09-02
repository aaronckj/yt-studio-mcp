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
    tw_p = sub.add_parser("twitch-auth", help="one-time Twitch OAuth consent")
    tw_p.add_argument(
        "--client-id",
        default="",
        help="Twitch application client id (default: $TWITCH_CLIENT_ID)",
    )
    tw_p.add_argument(
        "--client-secret",
        default="",
        help="Twitch application client secret (default: $TWITCH_CLIENT_SECRET). "
        "Prefer the env var: argv is visible in `ps`.",
    )
    tw_p.add_argument(
        "--code",
        default="",
        help="finish a headless flow: paste the ?code=... value from the "
        "redirect URL instead of running the local listener",
    )
    tw_p.add_argument(
        "--print-url",
        action="store_true",
        help="print the authorize URL and exit (open it on any machine)",
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

    if args.command == "twitch-auth":
        import os

        from .twitch_auth import authorize_url, exchange_code, run_twitch_auth

        # Env first so the secret never lands in argv / shell history / `ps`.
        args.client_id = args.client_id or os.environ.get("TWITCH_CLIENT_ID", "")
        args.client_secret = args.client_secret or os.environ.get(
            "TWITCH_CLIENT_SECRET", ""
        )
        if not args.client_id or not args.client_secret:
            raise SystemExit(
                "need a Twitch client id and secret: pass --client-id/--client-secret "
                "or set TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET"
            )

        if args.print_url:
            import secrets as _s

            print(authorize_url(args.client_id, _s.token_urlsafe(16)))
            return
        if args.code:
            print(
                exchange_code(args.client_id, args.client_secret, args.code),
                file=sys.stderr,
            )
            return
        print(run_twitch_auth(args.client_id, args.client_secret), file=sys.stderr)
        return

    from .server import build_app

    build_app().run()


if __name__ == "__main__":
    main()
