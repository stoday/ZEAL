"""The public command-line interface for ZEAL."""

from __future__ import annotations

import argparse
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Sequence

from zeal.line_bot import run_resume, run_setup


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

    line_bot = commands.add_parser(
        "line-bot",
        help="Create and configure a LINE Messaging API echo bot.",
    )
    line_bot_commands = line_bot.add_subparsers(dest="line_bot_command", required=True)
    setup = line_bot_commands.add_parser(
        "setup",
        help="Create a local project, ngrok tunnel, and assist LINE Console setup.",
    )
    setup.add_argument(
        "--output",
        type=Path,
        help="Directory in which to create line-bot-<account-name> (default: current directory).",
    )
    setup.add_argument(
        "--port",
        type=int,
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
        help="保存 LINE 登入狀態的 Playwright profile 目錄（預設為目前目錄的 .zeal-line-browser-profile）。",
    )
    runtime = setup.add_mutually_exclusive_group()
    runtime.add_argument(
        "--keep-running",
        dest="keep_running",
        action="store_true",
        help="Keep ngrok and the Bot running after setup (default).",
    )
    runtime.add_argument(
        "--stop-after-setup",
        dest="keep_running",
        action="store_false",
        help="Stop ngrok and the Bot when setup finishes.",
    )
    setup.set_defaults(handler=run_setup, keep_running=True)

    resume = line_bot_commands.add_parser(
        "resume",
        help="Finish local setup for an existing LINE Messaging API channel.",
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
        help="LINE Developers Console 的數字 Channel ID；提供後可自動設定 Webhook。",
    )
    resume.add_argument(
        "--skip-browser-install",
        action="store_true",
        help="不下載 Playwright Chromium；瀏覽器尚未存在時會失敗。",
    )
    resume.add_argument(
        "--browser-profile",
        type=Path,
        help="保存 LINE 登入狀態的 Playwright profile 目錄（預設為目前目錄的 .zeal-line-browser-profile）。",
    )
    resume.add_argument(
        "--no-browser",
        action="store_true",
        help="不要開啟瀏覽器，僅印出 Webhook URL 供手動設定。",
    )
    resume.set_defaults(handler=run_resume)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.handler(args)
