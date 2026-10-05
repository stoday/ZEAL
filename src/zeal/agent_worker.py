"""Loopback control server; Playwright stays on the setup worker's main thread."""

from __future__ import annotations

import argparse
import hmac
import html
import json
import os
import secrets
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from zeal.agent import AgentError, TERMINAL, read_json, write_json
from zeal.interaction import use_interaction


class AgentCancelled(KeyboardInterrupt):
    pass


class Session:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.condition = threading.Condition()
        self.state = read_json(directory / "status.json")
        self.cancelled = False
        self.human = False
        self.outcome: str | None = None
        self.secret_values: set[str] = set()
        self.answer_value: str | None = None
        self.human_token = secrets.token_urlsafe(32)
        self.port = 0

    def clean(self, text: str) -> str:
        for value in sorted(self.secret_values, key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return text

    def sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.clean(value)
        if isinstance(value, list):
            return [self.sanitize(item) for item in value]
        if isinstance(value, dict):
            return {key: self.sanitize(item) for key, item in value.items()}
        return value

    def save(self) -> None:
        self.state["revision"] += 1
        write_json(self.directory / "status.json", self.state)

    def snapshot(self) -> dict[str, Any]:
        with self.condition:
            return json.loads(json.dumps(self.state))

    def checkpoint(self) -> None:
        if self.cancelled:
            raise AgentCancelled()

    def message(self, text: str) -> None:
        with self.condition:
            self.state["messages"] = (self.state["messages"] + [self.clean(text)])[-30:]
            self.save()

    def notify(self, event: str, **fields: Any) -> None:
        if event == "checkpoint":
            self.checkpoint()
            return
        with self.condition:
            if event == "secrets":
                self.secret_values.update(value for value in fields["values"] if value)
                # Remove a newly discovered credential from any earlier messages.
                self.state = self.sanitize(self.state)
            elif event == "control":
                self.human = fields["human"]
            elif event == "step":
                self.checkpoint()
                self.state["step"] = fields
                self.state["error"] = None
            elif event == "retry":
                self.state["error"] = {
                    "code": fields["code"], "step": fields["step"],
                    "retry_policy": "Only choose an offered recovery action; do not restart the job.",
                }
            elif event == "operation_completed":
                label = fields["step"]
                if label not in self.state["completed_operations"]:
                    self.state["completed_operations"].append(label)
                self.state["error"] = None
            elif event == "failed":
                self.outcome = "failed"
                self.state["error"] = {"code": fields["code"], "message": "Setup did not complete. Review the sanitized messages."}
            elif event == "verified":
                self.state["verification"].update(fields)
            elif event == "project":
                self.state["project_context"] = fields
            elif event == "task":
                self.state["task"] = fields["name"]
            elif event == "complete":
                self.outcome = "completed"
                self.state["verification"].update({key: fields[key] for key in ("webhook_verified", "reply_confirmed")})
                self.state["result"] = {"project": fields["project"]}
                for key in ("project_mode", "credentials_file"):
                    if key in fields:
                        self.state["result"][key] = fields[key]
            self.save()

    def ask(self, prompt: str, *, secret: bool = False, options: Any = None) -> str:
        with self.condition:
            self.checkpoint()
            kind = "secret" if secret else "human" if self.human else "choice" if options else "input"
            prompt_id = secrets.token_hex(16)
            question: dict[str, Any] = {
                "id": prompt_id, "kind": kind, "label": self.clean(prompt),
                "options": options or [],
            }
            if self.state.get("task"):
                question["task"] = self.state["task"]
            if secret or self.human:
                question["human_url"] = f"http://127.0.0.1:{self.port}/human/{self.human_token}"
            self.state.update({
                "state": "awaiting_secret" if secret else "awaiting_human" if self.human else "awaiting_input",
                "prompt": question,
                "allowed_actions": ["status", "cancel", "open_human_url"] if secret else ["status", "answer", "cancel"],
            })
            self.answer_value = None
            self.save()
            while self.answer_value is None:
                self.condition.wait()
                self.checkpoint()
            answer = self.answer_value
            self.answer_value = None
            if secret and answer:
                self.secret_values.update({answer, answer.strip()} - {""})
            return answer

    def answer(self, prompt_id: Any, value: Any, *, local_human: bool = False) -> dict[str, Any]:
        with self.condition:
            prompt = self.state["prompt"]
            if self.cancelled or not prompt or prompt["id"] != prompt_id:
                raise AgentError("STALE_PROMPT", "Read status and answer only the current prompt.")
            if prompt["kind"] == "secret" and not local_human:
                raise AgentError("HUMAN_SECRET_REQUIRED", "Enter this secret in the local password form; never through the agent.")
            if not isinstance(value, str) or len(value) > 4096:
                raise AgentError("INVALID_ANSWER", "Answer must be text of at most 4096 characters.")
            if prompt["options"]:
                from zeal.line_bot import SetupError, option_from_choice
                try:
                    value = option_from_choice(value, prompt["options"])
                except SetupError:
                    raise AgentError("INVALID_CHOICE", "Use a current option label or one-based number.") from None
            self.answer_value = value
            self.state.update({"prompt": None, "state": "running", "allowed_actions": ["status", "cancel"]})
            self.save()
            self.condition.notify_all()
            return self.snapshot()

    def cancel(self) -> dict[str, Any]:
        with self.condition:
            if self.state["state"] in TERMINAL:
                return self.snapshot()
            self.cancelled = True
            self.state.update({"state": "cancelling", "prompt": None, "allowed_actions": ["status"]})
            self.save()
            self.condition.notify_all()
            return self.snapshot()

    def finish(self) -> None:
        with self.condition:
            final = "cancelled" if self.cancelled else self.outcome or "cancelled"
            self.state.update({"state": final, "prompt": None, "allowed_actions": []})
            self.save()


def make_handler(session: Session, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass  # Never log local URLs, headers, request bodies, or secrets.

        def send(self, status: int, body: str, content_type: str = "application/json") -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(encoded)

        def safe_origin(self) -> bool:
            expected = f"127.0.0.1:{session.port}"
            return self.headers.get("Host") == expected and self.headers.get("Origin") in (None, f"http://{expected}")

        def authorized(self) -> bool:
            return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}")

        def do_GET(self) -> None:
            if not self.safe_origin():
                self.send(403, '{}')
            elif self.path == "/status" and self.authorized():
                self.send(200, json.dumps(session.snapshot(), ensure_ascii=False))
            elif self.path == f"/human/{session.human_token}":
                with session.condition:
                    prompt = session.state["prompt"]
                    if not prompt or prompt["kind"] not in {"secret", "human"}:
                        self.send(409, "This prompt has ended.", "text/plain")
                        return
                    label = html.escape(prompt["label"])
                    field = "password" if prompt["kind"] == "secret" else "text"
                    body = (
                        '<!doctype html><html lang="zh-Hant"><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width, initial-scale=1">'
                        '<title>ZEAL 本機輸入</title><h1>ZEAL 本機輸入</h1>'
                        '<p>請直接在此頁輸入。密鑰不會傳送至 agent，也不會寫入工作狀態。</p>'
                        '<p>若是 LINE 驗證，請先在 LINE 瀏覽器完成，再回到此頁繼續。</p>'
                        f'<form method="post"><input type="hidden" name="prompt_id" value="{prompt["id"]}">'
                        f'<label>{label}<br><input type="{field}" name="value" maxlength="4096" autocomplete="off" autofocus></label>'
                        '<p><button type="submit">繼續</button></p></form></html>'
                    )
                self.send(200, body, "text/html")
            else:
                self.send(403, '{}')

        def do_POST(self) -> None:
            human = self.path == f"/human/{session.human_token}"
            if not self.safe_origin() or not (human or self.authorized()):
                self.send(403, '{}')
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 65536:
                    raise ValueError()
                body = self.rfile.read(size).decode("utf-8")
                if human:
                    values = urllib.parse.parse_qs(body, keep_blank_values=True)
                    session.answer(values.get("prompt_id", [None])[0], values.get("value", [""])[0], local_human=True)
                    self.send(200, "已接續。可關閉此頁並回到 agent。", "text/plain")
                elif self.path == "/answer":
                    values = json.loads(body)
                    result = session.answer(values.get("prompt_id"), values.get("value"))
                    self.send(200, json.dumps(result, ensure_ascii=False))
                elif self.path == "/cancel":
                    self.send(200, json.dumps(session.cancel(), ensure_ascii=False))
                else:
                    self.send(404, '{}')
            except AgentError as error:
                self.send(409, json.dumps({"code": error.code, "message": str(error)}))
            except (ValueError, AttributeError):
                self.send(400, json.dumps({"code": "INVALID_REQUEST", "message": "Invalid request."}))

    return Handler


def run_worker(directory: Path) -> None:
    from zeal.line_bot import run_setup

    session = Session(directory)
    control = read_json(directory / "control.json")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(session, control["token"]))
    server.daemon_threads = True
    session.port = server.server_port
    write_json(directory / "control.json", {**control, "port": session.port, "pid": os.getpid()})
    config = read_json(directory / "config.json")
    config["output"] = Path(config["output"]) if config["output"] else None
    config["browser_profile"] = Path(config["browser_profile"]) if config["browser_profile"] else None
    config["existing_project"] = Path(config["existing_project"]) if config.get("existing_project") else None
    config["ngrok_authtoken"] = None
    environment_token = os.environ.get("NGROK_AUTHTOKEN", "")
    session.secret_values.update({environment_token, environment_token.strip()} - {""})
    session.state["state"] = "running"
    session.save()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with use_interaction(session):
            run_setup(argparse.Namespace(**config))
    except KeyboardInterrupt:
        session.cancelled = True
    except Exception as error:
        session.notify("failed", code=type(error).__name__)
    finally:
        session.finish()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    run_worker(Path(sys.argv[1]))
