from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zeal.agent import AgentError, read_json, status, write_json
from zeal.agent_worker import AgentCancelled, Session, make_handler
from zeal.cli import build_parser, main
from zeal.interaction import use_interaction
from zeal.line_bot import AccountDetails, Credentials, LineConsoleBrowser, MessagingApiSetup, SetupError, _run_setup_with_account, prompt_option, retry_setup_step, run_setup


@pytest.fixture
def session(tmp_path):
    write_json(tmp_path / "status.json", {
        "schema_version": 1, "session": "a" * 32, "state": "running",
        "revision": 0, "step": None, "prompt": None, "messages": [],
        "completed_operations": [], "verification": {}, "result": None,
        "allowed_actions": [], "error": None,
    })
    return Session(tmp_path)


def wait_prompt(session, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = session.snapshot()
        if state["prompt"]:
            return state["prompt"]
        time.sleep(0.01)
    raise AssertionError("Worker did not publish a prompt")


def run_thread(operation):
    values = []

    def call():
        try:
            values.append(operation())
        except BaseException as error:
            values.append(error)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    return thread, values


def test_live_choices_reject_invalid_stale_and_duplicate_answers(session):
    def choose():
        with use_interaction(session):
            return prompt_option("Provider", ("Shop", "Club"))

    thread, values = run_thread(choose)
    try:
        prompt = wait_prompt(session)
        assert prompt["options"] == ["Shop", "Club"]
        with pytest.raises(AgentError, match="current option"):
            session.answer(prompt["id"], "3")
        with pytest.raises(AgentError, match="current prompt"):
            session.answer("old-id", "1")
        session.answer(prompt["id"], "2")
        with pytest.raises(AgentError, match="current prompt"):
            session.answer(prompt["id"], "1")
        thread.join(2)
        assert values == ["Club"]
    finally:
        session.cancel()
        thread.join(2)


def test_cancel_releases_waiting_worker_without_accepting_answer(session):
    thread, values = run_thread(lambda: session.ask("Continue"))
    prompt = wait_prompt(session)
    session.cancel()
    thread.join(2)
    assert not thread.is_alive()
    assert isinstance(values[0], AgentCancelled)
    with pytest.raises(AgentError):
        session.answer(prompt["id"], "")
    session.finish()
    assert session.snapshot()["state"] == "cancelled"


def test_late_cancel_keeps_completed_state(session):
    session.outcome = "completed"
    session.finish()
    assert session.cancel()["state"] == "completed"
    assert session.cancelled is False


def test_secret_is_local_only_and_never_persisted(session):
    secret = 'private-token"with\\escaping'
    thread, values = run_thread(lambda: session.ask("Channel access token", secret=True))
    try:
        prompt = wait_prompt(session)
        assert session.snapshot()["state"] == "awaiting_secret"
        assert "answer" not in session.snapshot()["allowed_actions"]
        with pytest.raises(AgentError) as caught:
            session.answer(prompt["id"], secret)
        assert caught.value.code == "HUMAN_SECRET_REQUIRED"
        session.answer(prompt["id"], secret, local_human=True)
        thread.join(2)
        assert values == [secret]
        session.message(f"error: {secret}")
        assert secret not in (session.directory / "status.json").read_text(encoding="utf-8")
        assert "[REDACTED]" in session.snapshot()["messages"][-1]
    finally:
        session.cancel()
        thread.join(2)


def test_browser_credentials_redact_prior_messages_and_prompts(session):
    secret = 'browser-secret"escaped'
    session.message(f"Previously discovered: {secret}")
    with use_interaction(session):
        Credentials(secret, "browser-access-token")
    assert session.snapshot()["messages"] == ["Previously discovered: [REDACTED]"]
    session.message("Failed browser-access-token")
    assert session.snapshot()["messages"][-1] == "Failed [REDACTED]"


def test_agent_human_handoff_unlocks_and_foregrounds_browser(session, tmp_path):
    page = MagicMock()
    browser = LineConsoleBrowser(True, tmp_path / "profile")
    browser.gate = MagicMock()
    browser.gate.human.side_effect = lambda target, _message: target.bring_to_front()
    with use_interaction(session):
        browser._give_user_control(page, "Login")
    browser.gate.human.assert_called_once_with(page, "Login")
    page.bring_to_front.assert_called_once()
    thread, values = run_thread(lambda: session.ask("Finished login?"))
    try:
        prompt = wait_prompt(session)
        assert prompt["kind"] == "human"
        assert session.snapshot()["state"] == "awaiting_human"
        session.answer(prompt["id"], "")
        thread.join(2)
        with use_interaction(session):
            browser._automate()
        assert session.human is False
        browser.gate.automation.assert_called_once()
    finally:
        session.cancel()
        thread.join(2)


def test_retry_uses_same_operation_and_reports_completed_result(session):
    calls = []

    def operation():
        calls.append(1)
        if len(calls) == 1:
            raise SetupError("Temporary failure")
        return "verified"

    def run():
        with use_interaction(session):
            return retry_setup_step("Inspect remote result", operation)

    thread, values = run_thread(run)
    try:
        prompt = wait_prompt(session)
        assert session.snapshot()["error"]["code"] == "SetupError"
        assert len(calls) == 1
        session.answer(prompt["id"], "1")
        thread.join(2)
        assert values == ["verified"]
        assert len(calls) == 2
        assert session.snapshot()["completed_operations"] == ["Inspect remote result"]
        assert session.snapshot()["error"] is None
    finally:
        session.cancel()
        thread.join(2)


def test_swallowed_cli_setup_failure_is_not_completed(session):
    with use_interaction(session), patch("zeal.line_bot.confirm_setup_start", side_effect=SetupError("Failure")):
        run_setup(argparse.Namespace())
    session.finish()
    assert session.snapshot()["state"] == "failed"
    assert session.snapshot()["verification"] == {}


def test_existing_project_readiness_task_is_visible_to_agent(session, tmp_path):
    from zeal.existing_project import prepare_project_integration
    credential_path = tmp_path / ".zeal-line" / "channel-2001234567.env"

    def run():
        with use_interaction(session):
            prepare_project_integration(argparse.Namespace(), tmp_path, credential_path)

    thread, values = run_thread(run)
    try:
        prompt = wait_prompt(session)
        assert prompt["task"] == "prepare_existing_project"
        assert session.snapshot()["project_context"] == {
            "mode": "existing", "path": str(tmp_path), "credentials_file": str(credential_path),
        }
        session.answer(prompt["id"], "")
        thread.join(2)
        assert values == [None]
        assert session.snapshot()["task"] is None
    finally:
        session.cancel()
        thread.join(2)


@pytest.mark.parametrize("cancel,keep_running", [(False, True), (True, True), (True, False)])
def test_shared_flow_requires_reply_and_preserves_verified_runtime(session, tmp_path, cancel, keep_running):
    args = build_parser().parse_args(["line-bot", "setup"])
    args.keep_running = keep_running
    args.public_url = "https://bot.example.com"
    account = AccountDetails("Coffee", "", 8000)
    browser = MagicMock()
    browser.enable_messaging_api.return_value = MessagingApiSetup("2001234567", "Coffee", True)
    browser.wait_for_credentials.side_effect = lambda _: Credentials("line-secret", "line-token")
    browser.configure_webhook.return_value = True
    browser.show_add_friend_qr.side_effect = lambda _: session.notify("control", human=True)
    project = tmp_path / "bot"

    def flow():
        with use_interaction(session):
            _run_setup_with_account(args, account, browser=browser, manager_url="https://manager.line.biz/account/coffee")
        session.finish()

    with (
        patch("zeal.line_bot.choose_project_destination", return_value=(project, False)),
        patch("zeal.line_bot.choose_credentials_destination", return_value=project),
        patch("zeal.line_bot.start_setup_runtime", return_value=(account, None, None, "https://bot.example.com/callback")),
        patch("zeal.line_bot.add_friend_url", return_value="https://line.me/R/ti/p/@coffee"),
        patch("zeal.line_bot.write_add_friend_qr", return_value=tmp_path / "qr.svg"),
        patch("zeal.line_bot.line_api_request", return_value={"success": True}),
        patch("zeal.line_bot.local_port_listening", return_value=True),
        patch("zeal.line_bot.stop_process") as stop,
    ):
        thread, values = run_thread(flow)
        try:
            prompt = wait_prompt(session)
            assert "Bot" in prompt["label"]
            current = session.snapshot()
            assert current["state"] == "awaiting_human"
            assert current["verification"] == {"webhook_verified": True}
            assert current["result"] is None
            assert "line-token" not in json.dumps(current)
            if cancel:
                session.cancel()
            else:
                session.answer(prompt["id"], "")
            thread.join(3)
            assert not thread.is_alive()
            assert values == [None]
            current = session.snapshot()
            assert current["state"] == ("cancelled" if cancel else "completed")
            if not cancel:
                assert current["verification"]["reply_confirmed"] is True
                assert current["result"] == {"project": str(project)}
            if keep_running:
                stop.assert_not_called()
            else:
                assert stop.call_count == 2
        finally:
            session.cancel()
            thread.join(3)


@pytest.fixture
def local_server(session):
    token = "control-token"
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(session, token))
    session.port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", token
    session.cancel()
    server.shutdown()
    server.server_close()
    thread.join(2)


def http(url, data=None, headers=None):
    request = urllib.request.Request(url, data=data, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=2)


def test_loopback_auth_origin_and_secret_form(session, local_server):
    base, token = local_server
    with pytest.raises(urllib.error.HTTPError) as denied:
        http(base + "/status")
    assert denied.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as denied:
        http(base + "/status", headers={"Authorization": f"Bearer {token}", "Origin": "https://foreign.example"})
    assert denied.value.code == 403
    thread, values = run_thread(lambda: session.ask("Token <private>", secret=True))
    try:
        prompt = wait_prompt(session)
        with http(prompt["human_url"]) as response:
            page = response.read().decode()
            assert 'type="password"' in page
            assert "Token &lt;private&gt;" in page
            assert response.headers["Cache-Control"] == "no-store"
        body = urllib.parse.urlencode({"prompt_id": prompt["id"], "value": "local-secret"}).encode()
        with http(prompt["human_url"], body, {"Origin": base}) as response:
            assert response.status == 200
        thread.join(2)
        assert values == ["local-secret"]
        with http(base + "/status", headers={"Authorization": f"Bearer {token}"}) as response:
            assert b"local-secret" not in response.read()
        with pytest.raises(urllib.error.HTTPError):
            http(prompt["human_url"], body, {"Origin": base})
    finally:
        session.cancel()
        thread.join(2)


def test_lost_worker_does_not_replay_saved_prompt(session):
    write_json(session.directory / "control.json", {"port": None, "token": "unused"})
    session.state["prompt"] = {"id": "old", "kind": "input"}
    session.save()
    result = status(session.directory)
    assert result["state"] == "worker_lost"
    assert result["prompt"] is None
    assert result["allowed_actions"] == ["abandon"]


def test_windows_transient_file_sharing_does_not_lose_state(tmp_path):
    target = tmp_path / "status.json"
    replace = Path.replace
    calls = []

    def busy_once(path, destination):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("Concurrent reader")
        return replace(path, destination)

    with patch.object(Path, "replace", busy_once), patch("zeal.agent.time.sleep"):
        write_json(target, {"state": "awaiting_input"})
    assert len(calls) == 2
    original = Path.read_text
    calls.clear()

    def read_busy_once(path, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("Replacement in progress")
        return original(path, **kwargs)

    with patch.object(Path, "read_text", read_busy_once), patch("zeal.agent.time.sleep"):
        assert read_json(target) == {"state": "awaiting_input"}
    assert len(calls) == 2


@pytest.mark.parametrize("platform,folder", [("codex", ".agents"), ("claude", ".claude"), ("antigravity", ".agents")])
def test_install_project_skill_preserves_existing_content(tmp_path, monkeypatch, capsys, platform, folder):
    monkeypatch.chdir(tmp_path)
    main(["install-skill", platform])
    installed = tmp_path / folder / "skills" / "zeal"
    assert (installed / "SKILL.md").is_file()
    assert (installed / "references" / "agent-cli.md").is_file()
    (installed / "SKILL.md").write_text("User changes", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["install-skill", platform])
    assert (installed / "SKILL.md").read_text() == "User changes"
    (installed / "custom.txt").write_text("Keep")
    main(["install-skill", platform, "--force"])
    assert "name: zeal" in (installed / "SKILL.md").read_text(encoding="utf-8")
    assert (installed / "custom.txt").read_text() == "Keep"


def test_install_global_and_custom_destinations(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for platform, parts in [("codex", (".agents", "skills")), ("claude", (".claude", "skills")), ("antigravity", (".gemini", "config", "skills"))]:
        main(["install-skill", platform, "--global"])
        assert tmp_path.joinpath(*parts, "zeal", "SKILL.md").is_file()
    custom = tmp_path / "custom skills"
    main(["install-skill", "--dest", str(custom)])
    assert (custom / "zeal" / "SKILL.md").is_file()
    with pytest.raises(SystemExit):
        main(["install-skill", "codex", "--dest", str(custom)])
    with pytest.raises(SystemExit):
        main(["install-skill", "--dest", str(custom), "--global"])


def test_real_detached_worker_prompts_and_cancels_across_cli_calls(tmp_path):
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PYTHONIOENCODING": "utf-8"}

    def cli(*args, success=True):
        result = subprocess.run(
            [sys.executable, "-c", "from zeal.cli import main; main()", "agent", *args],
            cwd=tmp_path, env=environment, capture_output=True, timeout=15,
        )
        assert result.returncode == (0 if success else 1), result.stderr.decode("utf-8", errors="replace")
        return json.loads(result.stdout.decode("utf-8"))

    started = cli("start", "--skip-browser-install")
    session_id = started["session"]
    try:
        current = cli("status", "--session", session_id, "--wait", "2")
        assert current["state"] == "awaiting_input"
        first = current["prompt"]["id"]
        cli("answer", "--session", session_id, "--prompt", first)
        current = cli("status", "--session", session_id, "--wait", "2")
        assert len(current["prompt"]["options"]) == 2
        duplicate = cli("answer", "--session", session_id, "--prompt", first, success=False)
        assert duplicate["error"]["code"] == "STALE_PROMPT"
        competing = cli("start", success=False)
        assert competing["error"]["code"] == "SESSION_ACTIVE"
        cli("answer", "--session", session_id, "--prompt", current["prompt"]["id"], "--value", "1")
        current = cli("status", "--session", session_id, "--wait", "2")
        assert current["prompt"]["kind"] == "input"
        assert current["step"]["number"] == 1
    finally:
        cli("cancel", "--session", session_id)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = cli("status", "--session", session_id)
            if current["state"] == "cancelled":
                break
            time.sleep(0.1)
        assert current["state"] == "cancelled"
        assert not (tmp_path / ".zeal-line-browser-profile").exists()
        assert ".zeal-agent/" in (tmp_path / ".gitignore").read_text()
