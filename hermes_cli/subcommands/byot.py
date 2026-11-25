"""``hermes byot`` — bring your own terminal.

Run this INSIDE the external terminal you want the Hermes web UI to use (e.g.
iTerm on macOS running zsh). It bridges that terminal to the dashboard over
``/api/byot/host``; the web UI Chat tab then attaches via ``?byot=<id>``
instead of the embedded terminal. See ``hermes_cli/byot_bridge.py``.
"""

from __future__ import annotations

import argparse


def build_byot_parser(subparsers) -> None:
    """Attach the ``byot`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "byot",
        help="Bridge this terminal (iTerm & co.) to the Hermes web UI (BYOT)",
        description=(
            "Run inside the external terminal you want to use instead of the web UI's "
            "embedded one (e.g. iTerm on macOS with zsh). Spawns a command — your login "
            "shell by default — in a local PTY, keeps showing it in this terminal, and "
            "streams it to the dashboard's /api/byot/host WebSocket. In the web UI Chat "
            "tab, attach the listed external terminal to mirror and control this session.\n"
            "\n"
            "The dashboard URL defaults to http://127.0.0.1:9119 (override with --url). "
            "Authentication uses the dashboard session token: "
            "--token, or HERMES_DASHBOARD_SESSION_TOKEN."
        ),
        epilog=(
            "Examples:\n"
            "  hermes byot                          # bridge your $SHELL (e.g. zsh)\n"
            "  hermes byot -- hermes chat           # bridge a specific command\n"
            "  hermes byot --url http://host:8642 --token <session-token>\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:9119",
        help="Dashboard base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Dashboard session token (default: env HERMES_DASHBOARD_SESSION_TOKEN)",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Display name for this terminal in the web UI (default: hostname:pid)",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run in the bridged terminal (after `--`); default: $SHELL",
    )
    parser.set_defaults(func=cmd_byot)


def cmd_byot(args) -> int:
    from hermes_cli.byot_bridge import run_byot_bridge

    return run_byot_bridge(
        url=args.url,
        token=args.token or "",
        name=args.name or "",
        command=list(args.command) or None,
    )
