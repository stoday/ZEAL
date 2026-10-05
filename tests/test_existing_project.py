from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zeal.cli import build_parser
from zeal.existing_project import configure_project_mode, prompt_callback_path, prompt_callback_url, save_project_credentials, start_existing_runtime
from zeal.line_bot import AccountDetails, Credentials, MessagingApiSetup, SetupError, _run_setup_with_account, validate_public_url


def test_interactive_existing_project_choice_keeps_arbitrary_source(tmp_path):
    source = tmp_path / "server.js"
    source.write_text("existing JavaScript", encoding="utf-8")
    args = build_parser().parse_args(["line-bot", "setup"])
    with patch("builtins.input", side_effect=["2", str(tmp_path)]), redirect_stdout(StringIO()):
        configure_project_mode(args)
    assert args.existing_project == tmp_path
    assert source.read_text() == "existing JavaScript"
    assert not (tmp_path / "app.py").exists()
    assert not (tmp_path / ".venv").exists()


def test_missing_existing_directory_is_not_created(tmp_path):
    target = tmp_path / "missing"
    args = build_parser().parse_args(["line-bot", "setup", "--existing-project", str(target)])
    with pytest.raises(SetupError, match="不存在"):
        configure_project_mode(args)
    assert not target.exists()


@pytest.mark.parametrize("command", [["line-bot", "setup"], ["agent", "start"]])
def test_existing_project_is_mutually_exclusive_with_generated_output(command):
    with pytest.raises(SystemExit):
        build_parser().parse_args([*command, "--output", "generated", "--existing-project", "old"])


def test_separate_credentials_do_not_overwrite_original_or_previous_channel(tmp_path):
    original = tmp_path / ".env"
    original.write_text("DATABASE_URL=preserve\nPORT=3000\n", encoding="utf-8")
    ignore = tmp_path / ".gitignore"
    ignore.write_text("node_modules/", encoding="utf-8")
    first = save_project_credentials(tmp_path, Credentials("secret-one", "token-one"), "2001234567")
    assert first == tmp_path / ".zeal-line" / "channel-2001234567.env"
    assert save_project_credentials(tmp_path, Credentials("secret-one", "token-one"), "2001234567") == first
    second = save_project_credentials(tmp_path, Credentials("secret-two", "token-two"), "2001234567")
    assert second != first
    assert "token-one" in first.read_text()
    assert "token-two" in second.read_text()
    assert original.read_text() == "DATABASE_URL=preserve\nPORT=3000\n"
    assert "PORT=" not in first.read_text()
    assert ignore.read_text() == "node_modules/\n.zeal-line/\n"


@pytest.mark.parametrize("endpoint", ["https://bot.example.com/api/line/webhook", "https://bot.example.com/callback/", "https://bot.example.com/"])
def test_full_callback_keeps_custom_path_and_trailing_slash(endpoint):
    with patch("builtins.input", return_value=endpoint):
        assert prompt_callback_url() == endpoint
    assert validate_public_url(endpoint, complete_callback=True) == endpoint


def test_full_callback_length_limit_does_not_append_callback():
    endpoint = "https://bot.example.com/" + "x" * (500 - len("https://bot.example.com/"))
    assert validate_public_url(endpoint, complete_callback=True) == endpoint
    with pytest.raises(SetupError):
        validate_public_url(endpoint + "x", complete_callback=True)


def test_callback_path_rejects_other_host_and_query_then_accepts_custom_path():
    with patch("builtins.input", side_effect=["//other.example/path", "/hook?token=secret", "/api/line/webhook"]), redirect_stdout(StringIO()):
        assert prompt_callback_path() == "/api/line/webhook"


def test_open_port_is_not_treated_as_line_readiness(tmp_path):
    args = argparse.Namespace(existing_callback_url=None)
    account = AccountDetails("Coffee", "", 8000)
    endpoint = "https://bot.example.com/api/line/webhook"
    with (
        patch("builtins.input", side_effect=["1", endpoint, "1"]),
        patch("zeal.line_bot.line_api_request", side_effect=[{"success": False}, {"success": True}]) as verify,
        patch("zeal.line_bot.local_port_listening", return_value=True) as port,
        patch("zeal.line_bot.ensure_target_dependencies") as dependencies,
        patch("zeal.line_bot.start_app") as start,
        redirect_stdout(StringIO()),
    ):
        result = start_existing_runtime(args, account, tmp_path, Credentials("secret", "token"))
    assert result == (account, None, None, endpoint)
    assert verify.call_count == 2
    assert all(call.args[3] == {"endpoint": endpoint} for call in verify.call_args_list)
    port.assert_not_called()
    dependencies.assert_not_called()
    start.assert_not_called()


def test_local_existing_app_uses_actual_port_and_custom_path(tmp_path):
    args = argparse.Namespace(existing_callback_url=None, ngrok_authtoken=None)
    with (
        patch("builtins.input", side_effect=["2", "3000", "/api/line/webhook"]),
        patch("zeal.line_bot.existing_tunnel_url", return_value="https://existing.ngrok.app") as tunnel,
        patch("zeal.line_bot.line_api_request", return_value={"success": True}) as verify,
        patch("zeal.line_bot.start_app") as start,
        patch("zeal.line_bot.prepare_ngrok") as prepare,
        redirect_stdout(StringIO()),
    ):
        result = start_existing_runtime(args, AccountDetails("Coffee", "", 8000), tmp_path, Credentials("secret", "token"))
    assert result[0].port == 3000
    assert result[1:3] == (None, None)
    assert result[3] == "https://existing.ngrok.app/api/line/webhook"
    tunnel.assert_called_once_with(3000)
    verify.assert_called_once()
    start.assert_not_called()
    prepare.assert_not_called()


def test_cancel_stops_only_owned_tunnel_before_verification(tmp_path):
    args = argparse.Namespace(existing_callback_url=None, ngrok_authtoken=None)
    owned = MagicMock()
    with (
        patch("builtins.input", side_effect=["2", "3000", "/hook"]),
        patch("zeal.line_bot.existing_tunnel_url", return_value=None),
        patch("zeal.line_bot.prepare_ngrok", return_value=Path("ngrok")),
        patch("zeal.line_bot.tunnel_url", return_value=(owned, "https://new.ngrok.app")),
        patch("zeal.line_bot.line_api_request", side_effect=KeyboardInterrupt),
        patch("zeal.line_bot.stop_process") as stop,
        redirect_stdout(StringIO()),
        pytest.raises(KeyboardInterrupt),
    ):
        start_existing_runtime(args, AccountDetails("Coffee", "", 8000), tmp_path, Credentials("secret", "token"))
    stop.assert_called_once_with(owned)


@pytest.mark.parametrize("keep_running", [True, False])
def test_existing_project_completes_without_generating_installing_or_owning_app(tmp_path, keep_running):
    source_files = {"server.js": "Existing LINE webhook", "package.json": '{"scripts":{"dev":"node server.js"}}', ".env": "PORT=3000\nDATABASE_URL=keep\n"}
    for name, content in source_files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    args = build_parser().parse_args(["line-bot", "setup", "--existing-project", str(tmp_path)])
    args.keep_running = keep_running
    browser = MagicMock()
    browser.enable_messaging_api.return_value = MessagingApiSetup("2001234567", "Coffee", True)
    browser.wait_for_credentials.return_value = Credentials("private-secret", "private-token")
    browser.configure_webhook.return_value = True
    endpoint = "https://node.example.com/api/line/webhook/"
    output = StringIO()
    with (
        patch("builtins.input", side_effect=["", "1", endpoint, ""]),
        patch("zeal.line_bot.write_project") as generate,
        patch("zeal.line_bot.choose_credentials_destination") as overwrite,
        patch("zeal.line_bot.ensure_target_dependencies") as install,
        patch("zeal.line_bot.start_app") as start,
        patch("zeal.line_bot.local_port_listening") as port,
        patch("zeal.line_bot.line_api_request", return_value={"success": True}),
        patch("zeal.line_bot.add_friend_url", return_value="https://line.me/R/ti/p/@coffee"),
        patch("zeal.line_bot.write_add_friend_qr", return_value=tmp_path / "qr.svg"),
        patch("zeal.line_bot.stop_process") as stop,
        redirect_stdout(output),
    ):
        _run_setup_with_account(args, AccountDetails("Coffee", "", 8000), browser=browser, manager_url="https://manager.line.biz/account/coffee")
    for mock in (generate, overwrite, install, start, port):
        mock.assert_not_called()
    assert all(call.args[0] is None for call in stop.call_args_list)
    browser.configure_webhook.assert_called_once()
    assert browser.configure_webhook.call_args.args[0] == endpoint
    assert "已確認收到 Bot 回覆" in output.getvalue()
    assert "private-token" not in output.getvalue()
    for name, content in source_files.items():
        assert (tmp_path / name).read_text(encoding="utf-8") == content
    assert not (tmp_path / "app.py").exists()
    assert not (tmp_path / ".venv").exists()
    assert not (tmp_path / "requirements.txt").exists()
    assert (tmp_path / ".zeal-line" / "channel-2001234567.env").is_file()
