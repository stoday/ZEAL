"""The public command-line interface for ZEAL."""

from __future__ import annotations

import argparse
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Sequence

from zeal.line_bot import run_resume, run_setup
from zeal.reset import run_reset
from zeal.agent import add_agent_parser
from zeal.skill_install import add_skill_parser


def _setup_port(value: str) -> int:
    """Let the guided setup correct a malformed --port interactively."""
    try:
        return int(value)
    except ValueError:
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zeal",
        description="Bootstrap small developer workflows safely.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {package_version('zeal-builder')}",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add_agent_parser(commands)
    add_skill_parser(commands)

    line_bot = commands.add_parser(
        "line-bot",
        help="Set up a LINE Messaging API echo bot.",
    )
    line_bot_commands = line_bot.add_subparsers(dest="line_bot_command", required=True)
    setup = line_bot_commands.add_parser(
        "setup",
        help="Create or continue an Official Account and get a reply from a local Bot.",
    )
    project_target = setup.add_mutually_exclusive_group()
    project_target.add_argument(
        "--output",
        type=Path,
        help="Directory in which to create line-bot-<account-name> (default: current directory).",
    )
    project_target.add_argument(
        "--existing-project", type=Path,
        help="Use an existing application without creating a Python Bot environment.",
    )
    setup.add_argument(
        "--port",
        type=_setup_port,
        default=8000,
        help="Local port for the generated Flask app (default: 8000).",
    )
    setup.add_argument(
        "--ngrok-authtoken",
        help="ngrok authtoken. Prefer NGROK_AUTHTOKEN or the hidden prompt instead.",
    )
    setup.add_argument(
        "--skip-browser-install",
        action="store_true",
        help="Do not download Playwright Chromium; fail if it is unavailable.",
    )
    setup.add_argument(
        "--browser-profile",
        type=Path,
        help="Playwright profile directory for saved LINE sign-in state (default: .zeal-line-browser-profile in the current directory).",
    )
    runtime = setup.add_mutually_exclusive_group()
    runtime.add_argument(
        "--keep-running",
        dest="keep_running",
        action="store_true",
        help="Keep services started by ZEAL running after setup (default).",
    )
    runtime.add_argument(
        "--stop-after-setup",
        dest="keep_running",
        action="store_false",
        help="Stop services started by ZEAL when setup finishes; preserve existing app processes.",
    )
    setup.set_defaults(handler=run_setup, keep_running=True)

    resume = line_bot_commands.add_parser(
        "resume",
        help="Advanced: use credentials you already have for a Messaging API channel.",
    )
    resume.add_argument(
        "--name",
        help="Existing LINE Official Account name (asked interactively when omitted).",
    )
    resume.add_argument(
        "--output",
        type=Path,
        help="Directory in which to create line-bot-<account-name> (default: current directory).",
    )
    resume.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Local port for the generated Flask app (default: 8000).",
    )
    resume.add_argument(
        "--webhook-url",
        help="Reuse this public HTTPS URL instead of detecting or starting ngrok.",
    )
    resume.add_argument(
        "--ngrok-authtoken",
        help="ngrok authtoken, used only when ZEAL must start ngrok.",
    )
    resume.add_argument(
        "--channel-id",
        help="Numeric Channel ID from LINE Developers Console; enables automatic Webhook setup.",
    )
    resume.add_argument(
        "--skip-browser-install",
        action="store_true",
        help="Do not download Playwright Chromium; fail if it is unavailable.",
    )
    resume.add_argument(
        "--browser-profile",
        type=Path,
        help="Playwright profile directory for saved LINE sign-in state (default: .zeal-line-browser-profile in the current directory).",
    )
    resume.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser; print the Webhook URL for manual setup.",
    )
    resume.set_defaults(handler=run_resume)

    reset = commands.add_parser(
        "reset",
        help="Clear ZEAL's local browser and ngrok state while keeping Bot projects.",
    )
    reset.add_argument(
        "--all", action="store_true",
        help="Clear both browser and ngrok state (the default).",
    )
    reset.add_argument(
        "--browser", action="store_true",
        help="Remove Playwright Chromium components and ZEAL's LINE browser profile.",
    )
    reset.add_argument(
        "--ngrok", action="store_true",
        help="Remove ZEAL's ngrok binary and the authtoken from ngrok's configuration.",
    )
    reset.add_argument(
        "--browser-profile", type=Path,
        help="Also remove a custom setup profile (requires browser reset).",
    )
    reset.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt after reviewing the targets.",
    )
    reset.set_defaults(handler=run_reset)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.handler(args)
