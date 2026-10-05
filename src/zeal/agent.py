"""Short-lived JSON CLI calls coordinating a persistent local setup worker."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TERMINAL = {"completed", "failed", "cancelled", "abandoned"}


class AgentError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False))
    for attempt in range(6):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            # Windows can briefly deny replacement while another CLI reads it.
            time.sleep(0.05)


def protect_workspace_state() -> None:
    """Add bounded exclusions without replacing the user's ignore rules."""
    ignore = Path.cwd() / ".gitignore"
    content = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    additions = [entry for entry in (".zeal-agent/", ".zeal-line-browser-profile/") if entry not in content.splitlines()]
    if additions:
        with ignore.open("a", encoding="utf-8") as output:
            output.write(("\n" if content and not content.endswith("\n") else "") + "\n".join(additions) + "\n")


def session_directory(session: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", session):
        raise AgentError("INVALID_SESSION", "Session must be the ID returned by start.")
    directory = Path.cwd() / ".zeal-agent" / session
    if not directory.is_dir():
        raise AgentError("SESSION_NOT_FOUND", "Run in the workspace where the session was started.")
    return directory


def read_json(path: Path) -> Any:
    for attempt in range(6):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if attempt < 5:
                time.sleep(0.05)
    raise AgentError("STATE_UNAVAILABLE", "Local session state is unavailable.") from None


def request(directory: Path, action: str, payload: Any = None) -> Any:
    control = read_json(directory / "control.json")
    port = control.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise AgentError("WORKER_UNAVAILABLE", "The setup worker has not connected.")
    data = json.dumps(payload or {}).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{action}", data=data,
        headers={"Authorization": f"Bearer {control['token']}", "Content-Type": "application/json"},
    )
    # Local control must not pass through environment proxy settings.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=3) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        result = json.load(error)
        raise AgentError(result["code"], result["message"]) from None
    except (OSError, ValueError):
        raise AgentError("WORKER_UNAVAILABLE", "The worker is unreachable; do not replay completed actions.") from None


def status(directory: Path) -> dict[str, Any]:
    saved = read_json(directory / "status.json")
    if saved["state"] in TERMINAL:
        return saved
    try:
        return request(directory, "status")
    except AgentError as error:
        if error.code != "WORKER_UNAVAILABLE":
            raise
        return {
            **saved, "state": "worker_lost", "prompt": None,
            "allowed_actions": ["abandon"],
            "error": {"code": error.code, "message": str(error)},
        }


def start(args: Any) -> dict[str, Any]:
    protect_workspace_state()
    root = Path.cwd() / ".zeal-agent"
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    lock = root / "start.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise AgentError("START_IN_PROGRESS", "Another start is in progress; inspect .zeal-agent/start.lock if its launcher exited.") from None
    os.close(fd)
    try:
        for saved in root.glob("*/status.json"):
            previous = read_json(saved)
            if previous["state"] not in TERMINAL:
                raise AgentError("SESSION_ACTIVE", f"Use status/cancel for session {previous['session']}; do not start a second controller.")
        session = secrets.token_hex(16)
        directory = root / session
        directory.mkdir(mode=0o700)
        initial = {
            "schema_version": 1, "session": session, "state": "starting",
            "step": None, "completed_operations": [], "prompt": None,
            "error": None, "messages": [], "verification": {},
            "result": None, "allowed_actions": ["status", "cancel"], "revision": 0,
        }
        write_json(directory / "status.json", initial)
        write_json(directory / "control.json", {"token": secrets.token_urlsafe(32), "port": None})
        write_json(directory / "config.json", {
            "output": str(args.output.resolve()) if args.output else None,
            "existing_project": str(args.existing_project.resolve()) if args.existing_project else None,
            "port": args.port, "skip_browser_install": args.skip_browser_install,
            "browser_profile": str(args.browser_profile.resolve()) if args.browser_profile else None,
            "keep_running": not args.stop_after_setup,
        })
        options: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        }
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "zeal.agent_worker", str(directory)],
                cwd=Path.cwd(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, **options,
            )
        except OSError:
            write_json(directory / "status.json", {**initial, "state": "failed", "error": {"code": "LAUNCH_FAILED"}})
            raise AgentError("LAUNCH_FAILED", "Unable to start the local setup worker.") from None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            saved = read_json(directory / "status.json")
            if saved["state"] != "starting":
                return status(directory)
            if process.poll() is not None:
                write_json(directory / "status.json", {**initial, "state": "failed", "error": {"code": "WORKER_EXITED"}})
                raise AgentError("WORKER_EXITED", "Worker exited before connecting. Check the installed ZEAL package.")
            time.sleep(0.1)
        return status(directory)
    finally:
        lock.unlink(missing_ok=True)


def run_agent(args: Any) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.agent_command == "start":
            result = start(args)
        else:
            directory = session_directory(args.session)
            if args.agent_command == "status":
                deadline = time.monotonic() + args.wait
                while True:
                    result = status(directory)
                    if result["state"] not in {"running", "starting", "cancelling"} or time.monotonic() >= deadline:
                        break
                    time.sleep(0.2)
            elif args.agent_command == "answer":
                result = request(directory, "answer", {"prompt_id": args.prompt, "value": args.value})
            elif args.agent_command == "cancel":
                current = status(directory)
                result = current if current["state"] in TERMINAL else request(directory, "cancel", {})
            else:
                current = status(directory)
                if current["state"] != "worker_lost":
                    raise AgentError("WORKER_PRESENT", "Only a lost worker can be abandoned; use cancel for a live worker.")
                result = {**current, "state": "abandoned", "allowed_actions": [], "prompt": None}
                write_json(directory / "status.json", result)
        print(json.dumps(result, ensure_ascii=False))
    except AgentError as error:
        print(json.dumps({"schema_version": 1, "error": {"code": error.code, "message": str(error)}}, ensure_ascii=False))
        raise SystemExit(1) from None


def add_agent_parser(commands: Any) -> None:
    parser = commands.add_parser("agent", help="Coordinate a persistent setup worker using JSON.")
    actions = parser.add_subparsers(dest="agent_command", required=True)
    begin = actions.add_parser("start", help="Start guided LINE setup in this workspace.")
    target = begin.add_mutually_exclusive_group()
    target.add_argument("--output", type=Path)
    target.add_argument("--existing-project", type=Path, help="Connect an existing application without generating a Python environment.")
    begin.add_argument("--port", type=int, default=8000)
    begin.add_argument("--browser-profile", type=Path)
    begin.add_argument("--skip-browser-install", action="store_true")
    begin.add_argument("--stop-after-setup", action="store_true")
    begin.set_defaults(handler=run_agent)
    for name in ("status", "answer", "cancel", "abandon"):
        action = actions.add_parser(name)
        action.add_argument("--session", required=True)
        if name == "status":
            action.add_argument("--wait", type=int, choices=range(0, 31), default=0, metavar="0..30")
        if name == "answer":
            action.add_argument("--prompt", required=True, help="Current prompt ID; stale answers are rejected.")
            action.add_argument("--value", default="", help="Non-secret answer; omit to confirm Enter.")
        if name == "abandon":
            action.add_argument("--closed-browser", required=True, action="store_true", help="Confirm the old browser has been closed before releasing lost control state.")
        action.set_defaults(handler=run_agent)
