from __future__ import annotations

import tempfile
import unittest
import os
import subprocess
import json
from itertools import count
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from zeal.cli import build_parser, main
from zeal.browser_gate import BrowserGate
from zeal.line_bot import (
    AccountDetails,
    Credentials,
    MessagingApiSetup,
    OfficialAccountChoice,
    LINE_OFFICIAL_ACCOUNT_ENTRY_URL,
    LineConsoleBrowser,
    _install_chromium_if_missing,
    NgrokAsset,
    ngrok_asset_for_current_platform,
    NGROK_AUTHTOKEN_URL,
    NGROK_SIGNUP_URL,
    SetupError,
    generated_app,
    format_completion_summary,
    format_runtime_instructions,
    confirm_setup_start,
    setup_step,
    start_background_process,
    run_setup,
    _run_setup_with_account,
    existing_tunnel_url,
    ensure_ngrok_config,
    ngrok_authtoken,
    prepare_ngrok,
    account_creation_detected,
    channel_id_from_url,
    channel_id_from_settings_text,
    human_verification_reason,
    messaging_api_url,
    option_from_choice,
    prompt_option,
    prompt_account_route,
    prompt_public_url,
    validate_public_url,
    account_confirmation_prompt,
    add_friend_url,
    write_add_friend_qr,
    set_and_test_webhook,
    webhook_endpoint_state,
    LineApiHttpError,
    prompt_provider_choice,
    default_browser_profile_directory,
    project_directory,
    prepare_setup_project,
    choose_project_destination,
    choose_credentials_destination,
    verify_existing_credentials,
    write_credentials,
    write_project,
    prompt_account_details,
    prompt_valid_port,
    retry_setup_step,
    change_project_port,
    start_setup_runtime,
)


class LineBotProjectTests(unittest.TestCase):
    def test_line_bot_help_is_english(self) -> None:
        for command in ("setup", "resume"):
            with self.subTest(command=command):
                output = StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as exit_result:
                    build_parser().parse_args(["line-bot", command, "--help"])
                self.assertEqual(exit_result.exception.code, 0)
                self.assertIn("--browser-profile", output.getvalue())
                self.assertNotRegex(output.getvalue(), r"[\u3400-\u9fff]")

    def test_setup_keeps_services_running_by_default(self) -> None:
        parser = build_parser()
        self.assertTrue(parser.parse_args(["line-bot", "setup"]).keep_running)
        self.assertFalse(parser.parse_args(["line-bot", "setup", "--stop-after-setup"]).keep_running)

    def test_setup_introduction_waits_for_enter(self) -> None:
        output = StringIO()
        with redirect_stdout(output), patch("builtins.input", return_value="") as prompt:
            confirm_setup_start()

        self.assertIn("建立或接續 LINE 官方帳號", output.getvalue())
        self.assertIn("簡易 LINE Bot", output.getvalue())
        self.assertIn("開啟瀏覽器協助設定", output.getvalue())
        self.assertIn("其他人類驗證", output.getvalue())
        self.assertIn("公開的 HTTPS 網址", output.getvalue())
        self.assertIn("ngrok 的免費方案", output.getvalue())
        prompt.assert_called_once()
        self.assertIn("按 Enter", prompt.call_args.args[0])
        with patch("builtins.input", return_value="no"), redirect_stdout(StringIO()):
            with self.assertRaisesRegex(SetupError, "尚未開始設定"):
                confirm_setup_start()

    def test_setup_guidance_colors_only_interactive_output(self) -> None:
        plain = StringIO()
        with redirect_stdout(plain), patch("builtins.input", return_value=""):
            confirm_setup_start()
            setup_step(1, "輸入官方帳號資料。")
        self.assertIn("◆ ZEAL", plain.getvalue())
        self.assertIn("[步驟 1/8]", plain.getvalue())
        self.assertNotIn("\x1b[", plain.getvalue())

        colored = StringIO()
        with (
            redirect_stdout(colored),
            patch("zeal.line_bot._terminal_supports_color", return_value=True),
        ):
            setup_step(2, "準備 ngrok。")
        self.assertIn("\x1b[36;1m[步驟 2/8] 準備 ngrok。\x1b[0m", colored.getvalue())

        class InteractiveOutput(StringIO):
            def isatty(self) -> bool:
                return True

        from zeal.line_bot import _terminal_supports_color

        with redirect_stdout(InteractiveOutput()), patch.dict(os.environ, {"NO_COLOR": ""}):
            self.assertFalse(_terminal_supports_color())

    def test_ngrok_dashboard_link_stays_copyable(self) -> None:
        from zeal.line_bot import _terminal_link

        with patch("zeal.line_bot._terminal_supports_color", return_value=False):
            self.assertEqual(_terminal_link(NGROK_AUTHTOKEN_URL), NGROK_AUTHTOKEN_URL)
        with (
            patch("zeal.line_bot._terminal_supports_color", return_value=True),
            patch.dict(os.environ, {"WT_SESSION": "terminal"}),
        ):
            self.assertEqual(
                _terminal_link(NGROK_AUTHTOKEN_URL),
                f"\x1b]8;;{NGROK_AUTHTOKEN_URL}\x1b\\{NGROK_AUTHTOKEN_URL}\x1b]8;;\x1b\\",
            )

    def test_setup_introduction_precedes_account_questions(self) -> None:
        args = build_parser().parse_args(["line-bot", "setup"])
        for interruption in (KeyboardInterrupt, EOFError):
            with self.subTest(interruption=interruption.__name__):
                with (
                    patch("zeal.line_bot.confirm_setup_start", side_effect=interruption),
                    patch("zeal.line_bot.prepare_ngrok") as preflight,
                    patch("zeal.line_bot.prompt_account_details") as account_prompt,
                    redirect_stdout(StringIO()),
                ):
                    run_setup(args)
                account_prompt.assert_not_called()
                preflight.assert_not_called()

    def test_ngrok_preflight_runs_before_account_selection(self) -> None:
        args = build_parser().parse_args(["line-bot", "setup"])
        with (
            patch("builtins.input", return_value=""),
            patch("zeal.line_bot.prepare_ngrok", side_effect=SetupError("ngrok unavailable")) as preflight,
            patch("zeal.line_bot.prompt_account_route") as route,
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            run_setup(args)
        preflight.assert_called_once_with(None, 8000)
        route.assert_not_called()

    def test_public_url_choice_skips_ngrok_and_reaches_account_setup(self) -> None:
        args = build_parser().parse_args(["line-bot", "setup"])
        account = AccountDetails("咖啡客服", "", 8000)
        with (
            patch("builtins.input", side_effect=["", "2", "https://bot.example.com/callback", "1"]),
            patch("zeal.line_bot.prepare_ngrok") as preflight,
            patch("zeal.line_bot.prompt_account_details", return_value=account),
            patch("zeal.line_bot._run_setup_with_account") as complete,
            redirect_stdout(StringIO()),
        ):
            run_setup(args)
        preflight.assert_not_called()
        self.assertEqual(args.public_url, "https://bot.example.com")
        complete.assert_called_once_with(args, account)

    def test_public_url_rejects_local_or_malformed_addresses(self) -> None:
        for value in ("http://bot.example.com", "https://localhost", "https://127.0.0.1",
                      "https://bot.example.com?secret=1", "https://name:pass@bot.example.com",
                      "https://bot.example.com:99999"):
            with self.subTest(value=value), self.assertRaises(SetupError):
                validate_public_url(value)
        self.assertEqual(validate_public_url("https://bot.example.com/callback"), "https://bot.example.com")
        with patch("builtins.input", side_effect=["2", "http://wrong.example", "https://bot.example.com"]), redirect_stdout(StringIO()):
            self.assertEqual(prompt_public_url(), "https://bot.example.com")

    def test_public_url_is_tested_by_line_after_bot_starts_without_ngrok(self) -> None:
        class RunningApp:
            def poll(self) -> None:
                return None

        args = build_parser().parse_args(["line-bot", "setup"])
        args.public_url = "https://bot.example.com"
        account = AccountDetails("咖啡客服", "", 8000)
        credentials = Credentials("a" * 32, "T" * 50)
        with (
            patch("zeal.line_bot.install_ngrok") as install,
            patch("zeal.line_bot.existing_tunnel_url") as tunnel,
            patch("zeal.line_bot.local_port_listening", return_value=False),
            patch("zeal.line_bot.ensure_target_dependencies"),
            patch("zeal.line_bot.start_app", return_value=RunningApp()),
            patch("zeal.line_bot.time.sleep"),
            patch("zeal.line_bot.line_api_request", return_value={"success": True}) as line_test,
            redirect_stdout(StringIO()),
        ):
            _, ngrok, app, callback = start_setup_runtime(args, account, Path("C:/bot"), credentials)
        install.assert_not_called()
        tunnel.assert_not_called()
        line_test.assert_called_once_with(
            "T" * 50, "POST", "/channel/webhook/test",
            {"endpoint": "https://bot.example.com/callback"},
        )
        self.assertIsNone(ngrok)
        self.assertIsNotNone(app)
        self.assertEqual(callback, "https://bot.example.com/callback")

    def test_public_url_line_test_failure_does_not_continue(self) -> None:
        class RunningApp:
            def poll(self) -> None:
                return None

        args = build_parser().parse_args(["line-bot", "setup"])
        args.public_url = "https://bot.example.com"
        with (
            patch("zeal.line_bot.local_port_listening", return_value=False),
            patch("zeal.line_bot.ensure_target_dependencies"),
            patch("zeal.line_bot.start_app", return_value=RunningApp()),
            patch("zeal.line_bot.time.sleep"),
            patch("zeal.line_bot.line_api_request", return_value={"success": False}),
            patch("zeal.line_bot.prompt_option", return_value="結束設定"),
            patch("zeal.line_bot.stop_process") as stop,
            redirect_stdout(StringIO()),
        ):
            with self.assertRaisesRegex(SetupError, "Bot 啟動"):
                start_setup_runtime(
                    args, AccountDetails("咖啡客服", "", 8000), Path("C:/bot"),
                    Credentials("a" * 32, "T" * 50),
                )
        stop.assert_called()

    def test_ngrok_preflight_checks_binary_and_configuration(self) -> None:
        binary = Path("/tools/ngrok")
        output = StringIO()
        with (
            patch("zeal.line_bot.install_ngrok", return_value=binary) as install,
            patch("zeal.line_bot.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run,
            patch("zeal.line_bot.existing_tunnel_url", return_value=None),
            patch("zeal.line_bot.ensure_ngrok_config") as configure,
            redirect_stdout(output),
        ):
            self.assertEqual(prepare_ngrok(None, 8000), binary)
        install.assert_called_once_with()
        self.assertEqual(run.call_args.args[0], [str(binary), "version"])
        configure.assert_called_once_with(binary, None)
        self.assertIn("ngrok 執行檔已確認", output.getvalue())

        with (
            patch("zeal.line_bot.install_ngrok", return_value=binary),
            patch("zeal.line_bot.subprocess.run", return_value=subprocess.CompletedProcess([], 1)),
            patch("zeal.line_bot.ensure_ngrok_config") as configure,
            redirect_stdout(StringIO()),
        ):
            with self.assertRaisesRegex(SetupError, "ngrok 無法執行"):
                prepare_ngrok(None, 8000)
        configure.assert_not_called()

    def test_ngrok_preflight_reuses_existing_tunnel_without_token_prompt(self) -> None:
        with (
            patch("zeal.line_bot.install_ngrok", return_value=Path("ngrok")),
            patch("zeal.line_bot.subprocess.run", return_value=subprocess.CompletedProcess([], 0)),
            patch("zeal.line_bot.existing_tunnel_url", return_value="https://example.ngrok.app") as tunnel,
            patch("zeal.line_bot.ensure_ngrok_config") as configure,
            redirect_stdout(StringIO()),
        ):
            prepare_ngrok(None, 8123)
        tunnel.assert_called_once_with(8123)
        configure.assert_not_called()

    def test_setup_route_is_chosen_interactively(self) -> None:
        with patch("builtins.input", return_value="1"):
            self.assertEqual(prompt_account_route(), "create")
        with patch("builtins.input", return_value="2"):
            self.assertEqual(prompt_account_route(), "existing")

    def test_existing_account_confirmation_explains_enter_and_ctrl_c(self) -> None:
        with patch("zeal.line_bot._terminal_supports_color", return_value=True):
            prompt = account_confirmation_prompt("測試帳號")
        self.assertIn("收到！會使用「測試帳號」進行接下來的設定", prompt)
        self.assertIn("\x1b[32;1m請按 Enter 確認繼續\x1b[0m", prompt)
        self.assertIn("\x1b[31;1m按 Ctrl+C 離開\x1b[0m", prompt)

        with patch("zeal.line_bot._terminal_supports_color", return_value=False):
            plain = account_confirmation_prompt("測試帳號")
        self.assertNotIn("\x1b[", plain)
        self.assertIn("請按 Enter 確認繼續（按 Ctrl+C 離開）", plain)

    def test_existing_account_choice_precedes_project_creation(self) -> None:
        class FakeBrowser:
            def __enter__(self) -> FakeBrowser:
                return self

            def __exit__(self, *_: object) -> None:
                pass

            def list_official_accounts(self) -> tuple[OfficialAccountChoice, ...]:
                return (
                    OfficialAccountChoice("咖啡客服", "https://manager.line.biz/account/one"),
                    OfficialAccountChoice("讀書會", "https://manager.line.biz/account/two"),
                )

        args = build_parser().parse_args(["line-bot", "setup"])
        with (
            patch("builtins.input", side_effect=["", "", "2", "2", ""]) as prompt,
            patch("zeal.line_bot.prepare_ngrok"),
            patch("zeal.line_bot.LineConsoleBrowser", return_value=FakeBrowser()),
            patch("zeal.line_bot.prompt_account_details") as new_account_prompt,
            patch("zeal.line_bot._run_setup_with_account") as complete,
            redirect_stdout(StringIO()),
        ):
            run_setup(args)
        new_account_prompt.assert_not_called()
        self.assertIn("請輸入編號或完整名稱：", prompt.call_args_list[3].args[0])
        self.assertEqual(complete.call_args.args[1], AccountDetails("讀書會", "", 8000))
        self.assertEqual(
            complete.call_args.kwargs["manager_url"], "https://manager.line.biz/account/two"
        )

    def test_existing_account_confirmation_ctrl_c_cancels(self) -> None:
        class FakeBrowser:
            def __enter__(self) -> FakeBrowser:
                return self

            def __exit__(self, *_: object) -> None:
                pass

            def list_official_accounts(self) -> tuple[OfficialAccountChoice, ...]:
                return (OfficialAccountChoice("讀書會", "https://manager.line.biz/account/two"),)

        args = build_parser().parse_args(["line-bot", "setup"])
        output = StringIO()
        with (
            patch("builtins.input", side_effect=["", "", "2", "1", KeyboardInterrupt]),
            patch("zeal.line_bot.prepare_ngrok"),
            patch("zeal.line_bot.LineConsoleBrowser", return_value=FakeBrowser()),
            patch("zeal.line_bot._run_setup_with_account") as complete,
            redirect_stdout(output),
        ):
            run_setup(args)
        complete.assert_not_called()
        self.assertIn("已取消", output.getvalue())

    def test_unreadable_account_list_can_create_in_same_browser(self) -> None:
        class FakeBrowser:
            def __enter__(self) -> FakeBrowser:
                return self

            def __exit__(self, *_: object) -> None:
                pass

            def list_official_accounts(self) -> tuple[OfficialAccountChoice, ...]:
                raise SetupError("清單版面未辨識")

        browser = FakeBrowser()
        account = AccountDetails("新帳號", "", 8000)
        args = build_parser().parse_args(["line-bot", "setup"])
        with (
            patch("builtins.input", side_effect=["", "", "2", "3"]),
            patch("zeal.line_bot.prepare_ngrok"),
            patch("zeal.line_bot.LineConsoleBrowser", return_value=browser),
            patch("zeal.line_bot.prompt_account_details", return_value=account),
            patch("zeal.line_bot._run_setup_with_account") as complete,
            redirect_stdout(StringIO()),
        ):
            run_setup(args)
        complete.assert_called_once_with(args, account, browser=browser)

    def test_existing_bot_is_reused_after_line_webhook_test(self) -> None:
        class FakeBrowser:
            pages: list[Any] = []

            def enable_messaging_api(self, _: str, __: str) -> MessagingApiSetup:
                return MessagingApiSetup("2001234567", "既有 Provider", already_enabled=True)

            def wait_for_credentials(self, _: str) -> Credentials:
                return Credentials("a" * 32, "T" * 50)

            def configure_webhook(self, *_: object) -> bool:
                return True

            def disable_auto_response_messages(self, *_: object) -> bool:
                return True

            def show_add_friend_qr(self, _: Path) -> None:
                pass

        account = AccountDetails("讀書會", "", 8000)
        with tempfile.TemporaryDirectory() as temporary:
            destination = project_directory(Path(temporary), account.name)
            write_project(destination, account)
            write_credentials(destination, Credentials("a" * 32, "T" * 50), 8000)
            args = build_parser().parse_args(["line-bot", "setup", "--output", temporary])
            output = StringIO()
            with (
                patch("builtins.input", return_value=""),
                patch("zeal.line_bot.existing_tunnel_url", return_value="https://example.ngrok.app"),
                patch("zeal.line_bot.local_port_listening", return_value=True),
                patch("zeal.line_bot.line_api_request", return_value={"success": True}) as test_request,
                patch("zeal.line_bot.ensure_target_dependencies") as dependencies,
                patch("zeal.line_bot.start_app") as start,
                patch("zeal.line_bot.add_friend_url", return_value="https://line.me/R/ti/p/%40testbot"),
                patch("zeal.line_bot.write_add_friend_qr", return_value=Path(temporary) / "add-friend.svg"),
                redirect_stdout(output),
            ):
                _run_setup_with_account(
                    args, account, browser=FakeBrowser(),
                    manager_url="https://manager.line.biz/account/test",
                )
            test_request.assert_called_once_with(
                "T" * 50, "POST", "/channel/webhook/test",
                {"endpoint": "https://example.ngrok.app/callback"},
            )
            dependencies.assert_not_called()
            start.assert_not_called()
            self.assertIn("沿用現有程序", output.getvalue())
            self.assertIn("[步驟 8/8]", output.getvalue())

    def test_selected_account_skips_creation_and_opens_its_manager_url(self) -> None:
        class FakeBrowser:
            def enable_messaging_api(self, name: str, manager_url: str) -> MessagingApiSetup:
                self.selected = (name, manager_url)
                raise SetupError("stop after account selection")

            def begin_account_creation(self, _: AccountDetails) -> None:
                raise AssertionError("An existing account must not be created again")

        with tempfile.TemporaryDirectory() as temporary:
            args = build_parser().parse_args(["line-bot", "setup", "--output", temporary])
            browser = FakeBrowser()
            with (
                patch("zeal.line_bot.prepare_setup_project", return_value=False),
                patch("zeal.line_bot.existing_tunnel_url", return_value="https://example.ngrok.app"),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                _run_setup_with_account(
                    args,
                    AccountDetails("讀書會", "", 8000),
                    browser=browser,
                    manager_url="https://manager.line.biz/account/two",
                )
        self.assertEqual(browser.selected, ("讀書會", "https://manager.line.biz/account/two"))

    def test_selected_account_with_existing_project_keeps_its_env(self) -> None:
        class FakeBrowser:
            def enable_messaging_api(self, _: str, __: str) -> MessagingApiSetup:
                return MessagingApiSetup("2001234567", "已綁定（名稱未讀取）", already_enabled=True)

            def wait_for_credentials(self, _: str) -> Credentials:
                return Credentials("a" * 32, "T" * 50)

            def begin_account_creation(self, _: AccountDetails) -> None:
                raise AssertionError("The account already exists")

        account = AccountDetails("讀書會", "", 8000)
        with tempfile.TemporaryDirectory() as temporary:
            destination = project_directory(Path(temporary), account.name)
            write_project(destination, account)
            write_credentials(destination, Credentials("a" * 32, "T" * 50), 8000)
            original = (destination / ".env").read_bytes()
            args = build_parser().parse_args(["line-bot", "setup", "--output", temporary])
            output = StringIO()
            with (
                patch("zeal.line_bot.existing_tunnel_url", return_value="https://example.ngrok.app"),
                patch("zeal.line_bot.local_port_listening", return_value=False),
                patch("zeal.line_bot.ensure_target_dependencies", side_effect=SetupError("stop after validation")) as dependencies,
                redirect_stdout(output),
                redirect_stderr(StringIO()),
            ):
                _run_setup_with_account(
                    args, account, browser=FakeBrowser(),
                    manager_url="https://manager.line.biz/account/two",
                )
            dependencies.assert_called_once_with(destination)
            self.assertEqual((destination / ".env").read_bytes(), original)
            self.assertIn("Messaging API 讓 LINE 將訊息交給 Bot", output.getvalue())
            self.assertIn("Provider 是此 Channel 所屬服務的經營者", output.getvalue())
            self.assertIn("Provider 已綁定，ZEAL 會沿用現有 Channel", output.getvalue())

    def test_background_process_uses_log_file_and_detached_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "bot.log"
            with patch("zeal.line_bot.subprocess.Popen") as launch:
                start_background_process(["python", "app.py"], log_path)

            options = launch.call_args.kwargs
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertNotEqual(options["stdout"], subprocess.PIPE)
            self.assertTrue(log_path.exists())
            if os.name == "nt":
                self.assertTrue(options["creationflags"] & subprocess.DETACHED_PROCESS)
            else:
                self.assertTrue(options["start_new_session"])

    def test_existing_ngrok_tunnel_must_match_bot_port(self) -> None:
        payload = json.dumps({"tunnels": [
            {"public_url": "https://wrong.example", "config": {"addr": "http://localhost:9000"}},
            {"public_url": "https://right.example", "config": {"addr": "http://127.0.0.1:8000"}},
        ]}).encode()
        with patch(
            "zeal.line_bot.urllib.request.urlopen",
            side_effect=lambda *_args, **_kwargs: BytesIO(payload),
        ):
            self.assertEqual(existing_tunnel_url(8000), "https://right.example")
            self.assertIsNone(existing_tunnel_url(7000))

    def test_existing_ngrok_config_does_not_ask_for_token_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "ngrok.yml"
            config.write_text("version: 3\nauthtoken: saved-token\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"NGROK_AUTHTOKEN": ""}),
                patch("zeal.line_bot.subprocess.run", return_value=subprocess.CompletedProcess([], 0, f"Valid configuration file at {config}\n")) as check,
                patch("zeal.line_bot.ngrok_authtoken") as prompt,
                patch("zeal.line_bot.configure_ngrok") as configure,
            ):
                ensure_ngrok_config(Path("ngrok"), None)

        self.assertEqual(check.call_args.args[0], ["ngrok", "config", "check"])
        prompt.assert_not_called()
        configure.assert_not_called()

    def test_ngrok_config_check_without_token_prompts_before_line_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "ngrok.yml"
            for contents in (None, "version: 3\n"):
                with self.subTest(contents=contents):
                    if contents is not None:
                        config.write_text(contents, encoding="utf-8")
                    with (
                        patch.dict(os.environ, {"NGROK_AUTHTOKEN": ""}),
                        patch("zeal.line_bot.subprocess.run", return_value=subprocess.CompletedProcess([], 0, f"Valid configuration file at {config}\n")),
                        patch("zeal.line_bot.ngrok_authtoken", return_value="new-token") as prompt,
                        patch("zeal.line_bot.configure_ngrok") as configure,
                    ):
                        ensure_ngrok_config(Path("ngrok"), None)
                    prompt.assert_called_once_with(None)
                    configure.assert_called_once_with(Path("ngrok"), "new-token")

    def test_ngrok_token_prompt_explains_registration_and_dashboard(self) -> None:
        output = StringIO()
        with (
            patch.dict(os.environ, {"NGROK_AUTHTOKEN": ""}),
            patch("zeal.line_bot.getpass.getpass", return_value="new-token") as hidden_input,
            redirect_stdout(output),
        ):
            self.assertEqual(ngrok_authtoken(None), "new-token")
        self.assertIn(NGROK_SIGNUP_URL, output.getvalue())
        self.assertIn(NGROK_AUTHTOKEN_URL, output.getvalue())
        self.assertIn("回到此終端機貼上", output.getvalue())
        hidden_input.assert_called_once()

    def test_successful_setup_leaves_started_services_running(self) -> None:
        class FakeProcess:
            def __init__(self, pid: int) -> None:
                self.pid = pid
                self.args = ["ngrok"]

            def poll(self) -> None:
                return None

        class FakeBrowser:
            pages: list[Any] = []

            def __enter__(self) -> FakeBrowser:
                return self

            def __exit__(self, *_: object) -> None:
                pass

            def existing_official_account(self, _: str) -> bool:
                return False

            def begin_account_creation(self, _: AccountDetails) -> object:
                return object()

            def submit_account_creation(self, _: object) -> None:
                pass

            def continue_after_account_creation(self) -> None:
                pass

            def enable_messaging_api(self, _: str) -> MessagingApiSetup:
                return MessagingApiSetup("2011770531", "Studio")

            def wait_for_credentials(self, _: str) -> Credentials:
                return Credentials("secret", "token")

            def configure_webhook(self, *_: object) -> bool:
                return True

            def disable_auto_response_messages(self, _: str) -> bool:
                return True

            def show_add_friend_qr(self, path: Path) -> None:
                self.qr_path = path

        with tempfile.TemporaryDirectory() as temporary:
            args = build_parser().parse_args(["line-bot", "setup", "--output", temporary])
            account = AccountDetails("測試", "", 8000, "公司", "test@example.com")
            ngrok, bot = FakeProcess(1111), FakeProcess(2222)
            output = StringIO()
            with (
                redirect_stdout(output),
                patch("builtins.input", side_effect=["", "", "1", ""]),
                patch("zeal.line_bot.prepare_ngrok"),
                patch("zeal.line_bot.prompt_account_details", return_value=account),
                patch("zeal.line_bot.prepare_setup_project", return_value=False),
                patch("zeal.line_bot.existing_tunnel_url", side_effect=[None, "https://example.ngrok.app"]),
                patch("zeal.line_bot.local_port_listening", side_effect=[False, True]),
                patch("zeal.line_bot.install_ngrok", return_value=Path("ngrok")),
                patch("zeal.line_bot.ensure_ngrok_config"),
                patch("zeal.line_bot.tunnel_url", return_value=(ngrok, "https://example.ngrok.app")),
                patch("zeal.line_bot.LineConsoleBrowser", return_value=FakeBrowser()),
                patch("zeal.line_bot.write_credentials"),
                patch("zeal.line_bot.ensure_target_dependencies"),
                patch("zeal.line_bot.start_app", return_value=bot),
                patch("zeal.line_bot.runtime_log_directory", return_value=Path(temporary)),
                patch("zeal.line_bot.add_friend_url", return_value="https://line.me/R/ti/p/%40testbot"),
                patch("zeal.line_bot.write_add_friend_qr", return_value=Path(temporary) / "add-friend.svg"),
                patch("zeal.line_bot.time.sleep"),
                patch("zeal.line_bot.stop_process") as stop,
            ):
                run_setup(args)

            stop.assert_not_called()
            for number in range(1, 9):
                self.assertIn(f"[步驟 {number}/8]", output.getvalue())
            self.assertIn("Messaging API 讓 LINE 將訊息交給 Bot", output.getvalue())
            self.assertIn("LINE 無法直接連到你的電腦", output.getvalue())
            self.assertIn("收到 Bot 回覆後，這次設定才算完成", output.getvalue())
            self.assertIn(NGROK_AUTHTOKEN_URL, output.getvalue())
            self.assertIn("Bot PID：2222", output.getvalue())
            self.assertIn("ngrok PID：1111", output.getvalue())
            self.assertIn("加好友 QR Code：", output.getvalue())
            self.assertIn("https://line.me/R/ti/p/%40testbot", output.getvalue())

    def test_webhook_uses_line_api_test_result_instead_of_console_success_text(self) -> None:
        url = "https://example.ngrok.app/callback"
        with patch(
            "zeal.line_bot.line_api_request",
            side_effect=[{}, {"success": True, "statusCode": 200}],
        ) as request:
            set_and_test_webhook("private-token", url)
        self.assertEqual(request.call_args_list[0].args, (
            "private-token", "PUT", "/channel/webhook/endpoint", {"endpoint": url},
        ))
        self.assertEqual(request.call_args_list[1].args, (
            "private-token", "POST", "/channel/webhook/test", {"endpoint": url},
        ))
        with patch("zeal.line_bot.line_api_request", return_value={"success": False, "statusCode": 400}):
            with self.assertRaisesRegex(SetupError, "400"):
                set_and_test_webhook("private-token", url)

    def test_webhook_falls_back_to_manager_and_checks_active_state(self) -> None:
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        url = "https://example.ngrok.app/callback"
        active = {"endpoint": url, "active": True}
        inactive = {"endpoint": url, "active": False}
        with (
            patch("zeal.line_bot.set_and_test_webhook") as verify,
            patch("zeal.line_bot.line_api_request", side_effect=[inactive] * 6 + [active]) as request,
            patch.object(browser, "_enable_use_webhook_console", side_effect=TimeoutError),
            patch.object(browser, "_enable_use_webhook_manager") as manager,
            patch("zeal.line_bot.time.sleep"),
            redirect_stdout(StringIO()),
        ):
            self.assertTrue(browser.configure_webhook(url, "2001234567", "private-token", "測試帳號"))
        verify.assert_called_once_with("private-token", url)
        manager.assert_called_once_with("測試帳號", None)
        self.assertEqual(request.call_count, 7)

    def test_webhook_state_tolerates_temporary_404_after_update(self) -> None:
        with patch(
            "zeal.line_bot.line_api_request",
            side_effect=LineApiHttpError("GET", "/channel/webhook/endpoint", 404),
        ):
            self.assertEqual(webhook_endpoint_state("private-token"), {})
        with patch(
            "zeal.line_bot.line_api_request",
            side_effect=LineApiHttpError("GET", "/channel/webhook/endpoint", 401),
        ):
            with self.assertRaises(LineApiHttpError):
                webhook_endpoint_state("private-token")

    def test_add_friend_link_and_qr_use_channel_basic_id(self) -> None:
        with patch("zeal.line_bot.line_api_request", return_value={"basicId": "@testbot"}):
            url = add_friend_url("private-token")
        self.assertEqual(url, "https://line.me/R/ti/p/%40testbot")
        with tempfile.TemporaryDirectory() as temporary:
            with patch("zeal.line_bot.runtime_log_directory", return_value=Path(temporary)):
                qr_path = write_add_friend_qr(Path(temporary), url)
            self.assertTrue(qr_path.is_file())
            self.assertIn(b"<svg", qr_path.read_bytes())

    def test_qr_page_is_opened_in_visible_browser(self) -> None:
        class FakePage:
            def goto(self, url: str, **_: object) -> None:
                self.url = url

        page = FakePage()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = type("FakeContext", (), {"new_page": lambda self: page})()
        with tempfile.TemporaryDirectory() as temporary:
            svg = Path(temporary) / "add-friend.svg"
            svg.write_text("<svg></svg>", encoding="utf-8")
            with patch.object(browser, "_give_user_control") as handoff:
                browser.show_add_friend_qr(svg)
            self.assertEqual(page.url, svg.with_suffix(".html").as_uri())
            self.assertIn("add-friend.svg", svg.with_suffix(".html").read_text(encoding="utf-8"))
            handoff.assert_called_once()

    def test_version_flag_uses_installed_package_version(self) -> None:
        output = StringIO()
        with (
            patch("zeal.cli.package_version", return_value="9.8.7") as version,
            redirect_stdout(output),
            self.assertRaises(SystemExit) as exited,
        ):
            main(["--version"])

        version.assert_called_once_with("zeal-builder")
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(output.getvalue(), "zeal 9.8.7\n")

    def test_project_directory_keeps_a_readable_unicode_name(self) -> None:
        path = project_directory(Path("C:/work"), "小明 咖啡/客服")
        self.assertEqual(path.name, "line-bot-小明-咖啡-客服")

    def test_ngrok_asset_builds_the_official_stable_url(self) -> None:
        asset = NgrokAsset("linux", "arm64", "tgz")
        self.assertEqual(
            asset.url,
            "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.tgz",
        )

    def test_ngrok_download_asset_matches_each_supported_os(self) -> None:
        cases = (
            ("Windows", "AMD64", "windows", "zip"),
            ("Darwin", "arm64", "darwin", "zip"),
            ("Linux", "x86_64", "linux", "tgz"),
        )
        for system, machine, expected_system, extension in cases:
            with (
                self.subTest(system=system),
                patch("zeal.line_bot.platform.system", return_value=system),
                patch("zeal.line_bot.platform.machine", return_value=machine),
            ):
                asset = ngrok_asset_for_current_platform()
                self.assertEqual(asset.system, expected_system)
                self.assertEqual(asset.extension, extension)

    def test_generated_project_excludes_and_quotes_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "line-bot-test"
            account = AccountDetails("測試 Bot", "教育", 8000)
            write_project(destination, account)
            write_credentials(destination, Credentials("a" * 32, "token-value"), 8000)

            self.assertIn(".env", (destination / ".gitignore").read_text(encoding="utf-8"))
            self.assertIn("LINE_CHANNEL_SECRET=\"", (destination / ".env").read_text(encoding="utf-8"))
            self.assertIn("valid_signature", (destination / "app.py").read_text(encoding="utf-8"))

    def test_generated_app_has_valid_python_syntax(self) -> None:
        compile(generated_app(), "app.py", "exec")

    def test_existing_destination_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "line-bot-test"
            destination.mkdir()
            with self.assertRaisesRegex(Exception, "已存在"):
                write_project(destination, AccountDetails("測試", "教育", 8000))

    def test_unfinished_setup_can_reuse_only_an_unchanged_project(self) -> None:
        account = AccountDetails("測試", "", 8000)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "line-bot-test"
            self.assertFalse(prepare_setup_project(destination, account))
            self.assertTrue(prepare_setup_project(destination, account))

            app_file = destination / "app.py"
            app_file.write_text("# user edit\n", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "內容不同"):
                prepare_setup_project(destination, account)
            self.assertEqual(app_file.read_text(encoding="utf-8"), "# user edit\n")

            app_file.write_text(generated_app(), encoding="utf-8")
            (destination / ".env").write_text("secret", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "已存在"):
                prepare_setup_project(destination, account)

    def test_selected_account_reuses_only_matching_local_credentials(self) -> None:
        account = AccountDetails("測試", "", 8000)
        credentials = Credentials("a" * 32, "T" * 50)
        with tempfile.TemporaryDirectory() as temporary:
            destination = project_directory(Path(temporary), account.name)
            write_project(destination, account)
            write_credentials(destination, credentials, account.port)
            original = (destination / ".env").read_bytes()

            self.assertTrue(prepare_setup_project(destination, account, allow_credentials=True))
            verify_existing_credentials(destination, credentials, account.port)
            self.assertEqual((destination / ".env").read_bytes(), original)

            with self.assertRaisesRegex(SetupError, "憑證或本機埠不一致"):
                verify_existing_credentials(
                    destination, Credentials("b" * 32, "T" * 50), account.port
                )
            with self.assertRaisesRegex(SetupError, "憑證或本機埠不一致"):
                verify_existing_credentials(destination, credentials, 9000)
            with self.assertRaisesRegex(SetupError, "已存在"):
                write_credentials(destination, credentials, account.port)
            self.assertEqual((destination / ".env").read_bytes(), original)

            (destination / "app.py").write_text("# edited\n", encoding="utf-8")
            with self.assertRaisesRegex(SetupError, "內容不同"):
                prepare_setup_project(destination, account, allow_credentials=True)

    def test_conflicting_project_offers_numbered_name_without_restart(self) -> None:
        account = AccountDetails("測試", "", 8000)
        with tempfile.TemporaryDirectory() as temporary:
            destination = project_directory(Path(temporary), account.name)
            destination.mkdir()
            (destination / "app.py").write_text("# my bot\n", encoding="utf-8")
            (Path(temporary) / f"{destination.name}-2").mkdir()
            output = StringIO()
            with redirect_stdout(output), patch("builtins.input", return_value="2"):
                selected, reused = choose_project_destination(destination, account)
            self.assertEqual(selected.name, f"{destination.name}-3")
            self.assertFalse(reused)
            self.assertTrue((selected / "app.py").is_file())
            self.assertEqual((destination / "app.py").read_text(encoding="utf-8"), "# my bot\n")
            self.assertIn(str(selected), output.getvalue())

    def test_project_overwrite_preserves_original_in_backup(self) -> None:
        account = AccountDetails("測試", "", 8000)
        with tempfile.TemporaryDirectory() as temporary:
            destination = project_directory(Path(temporary), account.name)
            destination.mkdir()
            (destination / "app.py").write_text("# my bot\n", encoding="utf-8")
            (destination / ".env").write_text("private", encoding="utf-8")
            with redirect_stdout(StringIO()), patch("builtins.input", return_value="1"):
                selected, reused = choose_project_destination(destination, account)
            self.assertEqual(selected, destination)
            self.assertFalse(reused)
            self.assertEqual((Path(temporary) / f"{destination.name}.backup" / ".env").read_text(encoding="utf-8"), "private")
            self.assertIn("valid_signature", (destination / "app.py").read_text(encoding="utf-8"))

    def test_conflicting_project_accepts_custom_name_without_cancel_option(self) -> None:
        account = AccountDetails("測試", "", 8000)
        with tempfile.TemporaryDirectory() as temporary:
            destination = project_directory(Path(temporary), account.name)
            destination.mkdir()
            (destination / "app.py").write_text("# keep\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output), patch("builtins.input", side_effect=["3", "活動版"]):
                selected, reused = choose_project_destination(destination, account)
            self.assertEqual(selected.name, f"{destination.name}-活動版")
            self.assertFalse(reused)
            self.assertTrue((selected / "app.py").is_file())
            self.assertEqual((destination / "app.py").read_text(encoding="utf-8"), "# keep\n")
            self.assertNotIn("取消設定", output.getvalue())

    def test_conflicting_credentials_can_use_new_project_or_backup_and_replace(self) -> None:
        account = AccountDetails("測試", "", 8000)
        old = Credentials("old-secret", "old-token")
        new = Credentials("new-secret", "new-token")
        with tempfile.TemporaryDirectory() as temporary:
            destination = project_directory(Path(temporary), account.name)
            write_project(destination, account)
            write_credentials(destination, old, account.port)
            original = (destination / ".env").read_bytes()
            output = StringIO()
            with redirect_stdout(output), patch("builtins.input", return_value="2"):
                selected = choose_credentials_destination(destination, account, new)
            self.assertEqual(selected.name, f"{destination.name}-2")
            self.assertEqual((destination / ".env").read_bytes(), original)
            verify_existing_credentials(selected, new, account.port)
            self.assertIn(str(selected), output.getvalue())
            self.assertNotIn("old-token", output.getvalue())
            self.assertNotIn("new-token", output.getvalue())

            with redirect_stdout(StringIO()), patch("builtins.input", return_value="1"):
                self.assertEqual(choose_credentials_destination(destination, account, new), destination)
            self.assertEqual((Path(temporary) / f"{destination.name}.backup" / ".env").read_bytes(), original)
            verify_existing_credentials(destination, new, account.port)

    def test_conflicting_credentials_accepts_custom_name_without_cancel_option(self) -> None:
        account = AccountDetails("測試", "", 8000)
        old = Credentials("old-secret", "old-token")
        new = Credentials("new-secret", "new-token")
        with tempfile.TemporaryDirectory() as temporary:
            destination = project_directory(Path(temporary), account.name)
            write_project(destination, account)
            write_credentials(destination, old, account.port)
            original = (destination / ".env").read_bytes()
            output = StringIO()
            with redirect_stdout(output), patch("builtins.input", side_effect=["3", "活動版"]):
                selected = choose_credentials_destination(destination, account, new)
            self.assertEqual(selected.name, f"{destination.name}-活動版")
            self.assertEqual((destination / ".env").read_bytes(), original)
            verify_existing_credentials(selected, new, account.port)
            self.assertNotIn("取消設定", output.getvalue())

    def test_new_project_choice_continues_existing_line_account(self) -> None:
        class FakeBrowser:
            def __enter__(self) -> FakeBrowser:
                return self

            def __exit__(self, *_: object) -> None:
                pass

            def existing_official_account(self, _: str) -> bool:
                return True

            def begin_account_creation(self, _: AccountDetails) -> None:
                raise AssertionError("The LINE account must not be created twice")

            def enable_messaging_api(self, name: str) -> None:
                self.continued_account = name
                raise SetupError("stop after account selection")

        account = AccountDetails("測試", "", 8000)
        with tempfile.TemporaryDirectory() as temporary:
            destination = project_directory(Path(temporary), account.name)
            destination.mkdir()
            (destination / "app.py").write_text("# custom\n", encoding="utf-8")
            browser = FakeBrowser()
            args = build_parser().parse_args(["line-bot", "setup", "--output", temporary])
            with (
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
                patch("builtins.input", side_effect=["2", "3"]),
                patch("zeal.line_bot.LineConsoleBrowser", return_value=browser),
            ):
                _run_setup_with_account(args, account)
            self.assertEqual(browser.continued_account, account.name)
            self.assertTrue((Path(temporary) / f"{destination.name}-2" / "app.py").is_file())
            self.assertEqual((destination / "app.py").read_text(encoding="utf-8"), "# custom\n")

    def test_logged_in_account_form_is_filled_in_visible_browser(self) -> None:
        class FakePage:
            url = LINE_OFFICIAL_ACCOUNT_ENTRY_URL

            def goto(self, url: str, **_: object) -> None:
                self.url = url

        page = FakePage()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = type("FakeContext", (), {"new_page": lambda self: page})()
        account = AccountDetails("測試", "", 8000, "公司", "test@example.com")
        with (
            patch.object(browser, "_wait_for_entry_form", return_value=page),
            patch.object(browser, "fill_official_account_form") as fill,
            patch("builtins.input") as prompt,
        ):
            result = browser.begin_account_creation(account)

        self.assertIs(result, page)
        fill.assert_called_once_with(account, page)
        prompt.assert_not_called()

    def test_login_redirect_fills_form_in_same_visible_page(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.url = "about:blank"
                self.goto_count = 0

            def goto(self, url: str, **_: object) -> None:
                self.goto_count += 1
                self.url = "https://account.line.biz/login"

            def is_closed(self) -> bool:
                return False

        class FakeContext:
            def __init__(self, page: FakePage) -> None:
                self.page = page
                self.closed = False

            @property
            def pages(self) -> list[FakePage]:
                return [self.page]

            def new_page(self) -> FakePage:
                return self.page

            def close(self) -> None:
                self.closed = True

        page = FakePage()
        context = FakeContext(page)
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = context
        account = AccountDetails("測試", "", 8000, "公司", "test@example.com")

        with (
            patch("builtins.input") as prompt,
            patch.object(browser, "_wait_for_entry_form", side_effect=[None, page]),
            patch.object(browser, "fill_official_account_form") as fill,
        ):
            result = browser.begin_account_creation(account)

        self.assertIs(result, page)
        self.assertEqual(page.goto_count, 1)
        prompt.assert_not_called()
        fill.assert_called_once_with(account, page)
        self.assertFalse(context.closed)

    def test_entry_form_wait_requires_the_real_line_host_and_loaded_selects(self) -> None:
        class FakeSelects:
            def __init__(self, count: int) -> None:
                self.count_value = count

            def count(self) -> int:
                return self.count_value

        class FakePage:
            def __init__(self, url: str, selects: int) -> None:
                self.url = url
                self.selects = selects

            def is_closed(self) -> bool:
                return False

            def locator(self, selector: str) -> FakeSelects:
                self.assert_selector = selector
                return FakeSelects(self.selects)

        real_form = FakePage(LINE_OFFICIAL_ACCOUNT_ENTRY_URL, 3)
        lookalike = FakePage("https://example.invalid/?next=entry.line.biz/form/entry/unverified", 3)
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = type("FakeContext", (), {"pages": [real_form, lookalike]})()

        self.assertIs(browser._wait_for_entry_form(seconds=1), real_form)

    def test_resume_is_a_public_cli_command(self) -> None:
        args = build_parser().parse_args(
            ["line-bot", "resume", "--name", "zeal-bot", "--channel-id", "2011770531"]
        )
        self.assertEqual(args.line_bot_command, "resume")
        self.assertEqual(args.name, "zeal-bot")
        self.assertEqual(args.channel_id, "2011770531")

    def test_messaging_api_url_accepts_numeric_channel_id_only(self) -> None:
        self.assertEqual(
            messaging_api_url("2011770531"),
            "https://developers.line.biz/console/channel/2011770531/messaging-api",
        )
        with self.assertRaisesRegex(Exception, "Channel ID"):
            messaging_api_url("https://example.invalid")

    def test_channel_id_is_read_from_manager_settings_label(self) -> None:
        page_text = "LINE ID @examplebot\nChannel Info\nChannel ID\n2001234567\nProvider 測試"
        self.assertEqual(channel_id_from_settings_text(page_text), "2001234567")
        self.assertIsNone(channel_id_from_settings_text("LINE ID @examplebot\nProvider 測試"))

    def test_category_menu_accepts_number_or_exact_text(self) -> None:
        options = ("餐飲", "生活相關服務")
        self.assertEqual(option_from_choice("2", options), "生活相關服務")
        self.assertEqual(option_from_choice("餐飲", options), "餐飲")
        with self.assertRaisesRegex(Exception, "清單"):
            option_from_choice("資訊服務", options)

    def test_category_menu_uses_two_columns_when_the_terminal_is_wide(self) -> None:
        from zeal.line_bot import _display_width

        options = ("餐飲", "醫療", "生活相關服務", "教育", "公眾人物", "其他")
        output = StringIO()
        with (
            redirect_stdout(output),
            patch("builtins.input", return_value="5"),
            patch("zeal.line_bot.shutil.get_terminal_size", return_value=os.terminal_size((80, 24))),
        ):
            self.assertEqual(prompt_option("業種大分類", options, two_columns=True), "公眾人物")

        rows = [
            line for line in output.getvalue().splitlines()
            if line.startswith(("   1.", "   3.", "   5."))
        ]
        self.assertEqual(len(rows), 3)
        self.assertIn("2. 醫療", rows[0])
        self.assertIn("4. 教育", rows[1])
        self.assertIn("6. 其他", rows[2])
        right_positions = [
            _display_width(row[:row.index(marker)])
            for row, marker in zip(rows, ("   2.", "   4.", "   6."))
        ]
        self.assertEqual(len(set(right_positions)), 1)

        narrow_output = StringIO()
        with (
            redirect_stdout(narrow_output),
            patch("builtins.input", return_value="5"),
            patch("zeal.line_bot.shutil.get_terminal_size", return_value=os.terminal_size((25, 24))),
        ):
            self.assertEqual(prompt_option("業種大分類", options, two_columns=True), "公眾人物")
        self.assertEqual(
            len([line for line in narrow_output.getvalue().splitlines() if line.startswith("  ")]),
            len(options),
        )

    def test_account_details_retain_cli_form_values(self) -> None:
        account = AccountDetails(
            name="zeal-go",
            category="生活相關服務",
            port=8000,
            company_name="Zeal Studio",
            email="hello@example.com",
        )
        self.assertEqual(account.company_name, "Zeal Studio")
        self.assertEqual(account.email, "hello@example.com")

    def test_completion_summary_reports_user_settings_without_credentials(self) -> None:
        account = AccountDetails(
            name="zeal-go",
            category="餐飲",
            port=8000,
            company_name="Zeal Studio",
            email="hello@example.com",
        )
        summary = format_completion_summary(
            account,
            Path("C:/work/line-bot-zeal-go"),
            "https://example.ngrok.app/callback",
            MessagingApiSetup(channel_id="2011770531", provider="Zeal Studio"),
            webhook_configured=True,
            keep_running=True,
        )
        self.assertIn("官方帳號：zeal-go", summary)
        self.assertIn("公司／店鋪：Zeal Studio", summary)
        self.assertIn("Provider：Zeal Studio", summary)
        self.assertIn("https://developers.line.biz/console/channel/2011770531/messaging-api", summary)
        self.assertIn(str(Path("C:/work/line-bot-zeal-go") / "app.py"), summary)
        self.assertIn("指令結束後持續在背景執行", summary)
        self.assertNotIn("hello@example.com", summary)
        self.assertNotIn("channel_secret", summary.casefold())

    def test_runtime_instructions_identify_both_processes(self) -> None:
        instructions = format_runtime_instructions(
            1111, 2222, Path("C:/zeal/logs"), port=8765, ngrok_binary="C:/Program Files/ngrok/ngrok.exe"
        )
        self.assertIn("Bot PID：1111", instructions)
        self.assertIn("ngrok PID：2222", instructions)
        self.assertIn("bot.log", instructions)
        self.assertIn("ngrok.log", instructions)
        self.assertIn("1111", instructions.split("停止本次啟動的程序：")[1])
        self.assertIn("http://127.0.0.1:4040", instructions)
        if os.name == "nt":
            self.assertIn("Stop-Process -Id 2222", instructions)
            self.assertIn("& 'C:/Program Files/ngrok/ngrok.exe' http 8765", instructions)
        else:
            self.assertIn("kill 2222", instructions)
            self.assertIn("'C:/Program Files/ngrok/ngrok.exe' http 8765", instructions)
        self.assertIn("更新 Webhook URL", instructions)

    def test_runtime_instructions_for_existing_ngrok_do_not_claim_ownership(self) -> None:
        instructions = format_runtime_instructions(None, None, Path("C:/zeal/logs"), port=8000)
        self.assertIn("使用已執行的連線", instructions)
        self.assertIn("ngrok http 8000", instructions)
        self.assertNotIn("只停止 ngrok", instructions)

    def test_custom_url_summary_and_runtime_do_not_claim_ngrok(self) -> None:
        summary = format_completion_summary(
            AccountDetails("咖啡客服", "", 8000), Path("C:/bot"),
            "https://bot.example.com/callback",
            MessagingApiSetup(channel_id="2011770531", provider="Studio"),
            webhook_configured=True, keep_running=True, using_ngrok=False,
        )
        instructions = format_runtime_instructions(
            1234, None, Path("C:/logs"), port=8000, using_ngrok=False,
        )
        self.assertIn("Bot：指令結束後持續在背景執行", summary)
        self.assertNotIn("ngrok", summary)
        self.assertIn("自備公開網址", instructions)
        self.assertNotIn("ngrok", instructions)

    def test_default_browser_profile_is_in_the_working_tree(self) -> None:
        self.assertEqual(
            default_browser_profile_directory(),
            Path.cwd() / ".zeal-line-browser-profile",
        )

    def test_account_creation_uses_the_direct_line_entry_url(self) -> None:
        self.assertEqual(
            LINE_OFFICIAL_ACCOUNT_ENTRY_URL,
            "https://entry.line.biz/form/entry/unverified",
        )

    def test_category_menu_accepts_live_form_option(self) -> None:
        self.assertEqual(
            option_from_choice("公眾人物、專業人士", ("公眾人物、專業人士", "餐飲")),
            "公眾人物、專業人士",
        )

    def test_human_verification_detection_never_treats_webhook_verify_as_a_challenge(self) -> None:
        self.assertIsNone(human_verification_reason("Webhook URL  Verify  Success"))
        self.assertEqual(human_verification_reason("請輸入驗證碼以繼續"), "驗證碼")
        self.assertEqual(human_verification_reason("Complete the CAPTCHA"), "captcha")

    def test_channel_id_is_extracted_only_from_messaging_api_urls(self) -> None:
        self.assertEqual(
            channel_id_from_url("https://developers.line.biz/console/channel/2011770531/messaging-api"),
            "2011770531",
        )
        self.assertIsNone(channel_id_from_url("https://developers.line.biz/console/"))

    def test_account_creation_detection_requires_a_redirect_or_success_message(self) -> None:
        self.assertFalse(account_creation_detected("https://entry.line.biz/form/entry/unverified", "填寫資料"))
        self.assertTrue(account_creation_detected("https://manager.line.biz/", ""))
        self.assertTrue(account_creation_detected("https://entry.line.biz/form/entry/unverified", "帳號已建立"))

    def test_setup_rejects_removed_provider_flags(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["line-bot", "setup", "--provider", "one"])

    def test_provider_can_be_chosen_interactively_without_a_cli_flag(self) -> None:
        with patch("builtins.input", return_value="2"):
            provider, created = prompt_provider_choice(("Company A", "Company B"))
        self.assertEqual(provider, "Company B")
        self.assertIsNone(created)

    def test_interactive_provider_creation_requires_a_name(self) -> None:
        with patch("builtins.input", side_effect=["3", "", "New Company"]):
            provider, created = prompt_provider_choice(("Company A", "Company B"))
        self.assertIsNone(provider)
        self.assertEqual(created, "New Company")

    def test_missing_chromium_installs_linux_dependencies_with_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            browser_type = type("BrowserType", (), {"executable_path": str(Path(temporary) / "chrome")})()
            output = StringIO()
            with (
                patch("zeal.line_bot.platform.system", return_value="Linux"),
                patch("zeal.line_bot.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run,
                redirect_stdout(output),
            ):
                _install_chromium_if_missing(browser_type)

        run.assert_called_once_with(
            [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
            check=False,
        )
        self.assertIn("正在下載 Playwright Chromium", output.getvalue())
        self.assertIn("Linux 所需的系統套件", output.getvalue())

    def test_chromium_install_uses_os_command_only_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "chrome"
            browser_type = type("BrowserType", (), {"executable_path": str(executable)})()
            with (
                patch("zeal.line_bot.platform.system", return_value="Windows"),
                patch("zeal.line_bot.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run,
                redirect_stdout(StringIO()) as output,
            ):
                _install_chromium_if_missing(browser_type)
                executable.touch()
                _install_chromium_if_missing(browser_type)

        run.assert_called_once_with(
            [sys.executable, "-m", "playwright", "install", "chromium"], check=False
        )
        self.assertEqual(output.getvalue().count("正在下載 Playwright Chromium"), 1)

    def test_browser_starts_visible_with_persistent_profile(self) -> None:
        class FakeContext:
            pages: list[object] = []

            def add_init_script(self, **_: object) -> None:
                pass

            def on(self, *_: object) -> None:
                pass

            def close(self) -> None:
                pass

        class FakeChromium:
            def __init__(self) -> None:
                self.options: dict[str, object] = {}

            def launch_persistent_context(self, **kwargs: object) -> FakeContext:
                self.options = kwargs
                return FakeContext()

        class FakePlaywright:
            def __init__(self) -> None:
                self.chromium = FakeChromium()

            def stop(self) -> None:
                pass

        fake_playwright = FakePlaywright()
        controller = type("FakeController", (), {"start": lambda self: fake_playwright})()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        with patch("playwright.sync_api.sync_playwright", return_value=controller):
            with browser:
                self.assertFalse(fake_playwright.chromium.options["headless"])
                self.assertEqual(
                    fake_playwright.chromium.options["user_data_dir"],
                    str(browser.profile_directory),
                )
                self.assertNotIn("args", fake_playwright.chromium.options)

    def test_information_use_consent_is_accepted_before_manager_navigation(self) -> None:
        class FakeButton:
            def __init__(self, page: Any, action: str) -> None:
                self.page = page
                self.action = action
                self.last = self

            def click(self, **_: object) -> None:
                self.page.actions.append(self.action)
                if self.action == "consent":
                    self.page.consent_visible = False

        class FakePage:
            url = "https://manager.line.biz/"

            def __init__(self) -> None:
                self.consent_visible = True
                self.actions: list[str] = []

            def get_by_role(self, role: str, **_: object) -> FakeButton:
                self.assert_role = role
                return FakeButton(self, "consent")

            def get_by_text(self, text: str, **_: object) -> FakeButton:
                return FakeButton(self, "account" if text == "測試帳號" else "consent")

        page = FakePage()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = type("FakeContext", (), {"pages": [page]})()
        with patch.object(
            browser,
            "_page_text",
            side_effect=lambda _: "同意我們使用您的資訊" if page.consent_visible else "",
        ):
            self.assertTrue(browser._click_first(page, ("測試帳號",)))

        self.assertEqual(page.actions, ["consent", "account"])

    def test_line_continue_notice_before_manager_navigation(self) -> None:
        class FakeButton:
            def __init__(self, page: Any, action: str) -> None:
                self.page = page
                self.action = action
                self.last = self

            def click(self, **_: object) -> None:
                self.page.actions.append(self.action)
                if self.action == "terms":
                    self.page.terms_visible = False

        class FakePage:
            url = "https://manager.line.biz/"
            button_text = "了解並繼續使用\nI understand and want to proceed"

            def __init__(self) -> None:
                self.terms_visible = True
                self.actions: list[str] = []

            def get_by_role(self, role: str, **kwargs: object) -> FakeButton:
                self.assert_role = (role, kwargs)
                if not kwargs["name"].fullmatch(self.button_text):
                    raise AssertionError("雙語按鈕名稱未被辨識")
                return FakeButton(self, "terms")

            def get_by_text(self, text: str, **_: object) -> FakeButton:
                return FakeButton(self, "account" if text == "測試帳號" else "other")

        page = FakePage()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        with patch.object(
            browser,
            "_page_text",
            side_effect=lambda _: (
                "LINE官方帳號使用條款更新啟事。了解並繼續使用\nI understand and want to proceed"
                if page.terms_visible else ""
            ),
        ):
            self.assertTrue(browser._click_first(page, ("測試帳號",)))

        self.assertEqual(page.actions, ["terms", "account"])
        self.assertEqual(page.assert_role[0], "button")
        self.assertIsNotNone(page.assert_role[1]["name"].fullmatch(page.button_text))

        page.url = "https://example.com/"
        page.terms_visible = True
        page.actions.clear()
        with patch.object(browser, "_page_text", return_value="LINE 官方帳號使用條款。了解並繼續使用"):
            self.assertFalse(browser._acknowledge_line_continue(page))
        self.assertEqual(page.actions, [])

    def test_manager_welcome_closes_before_settings_navigation(self) -> None:
        class FakeButton:
            first: Any

            def __init__(self, page: Any, action: str) -> None:
                self.page = page
                self.action = action
                self.first = self
                self.last = self

            def inner_text(self, **_: object) -> str:
                return ""

            def click(self, **_: object) -> None:
                if self.action == "unnamed_close":
                    self.page.welcome_visible = False
                elif self.action == "named_close":
                    raise RuntimeError("The close icon has no accessible name")
                self.page.actions.append(self.action)

        class FakeHeading:
            first: Any

            def __init__(self, page: Any) -> None:
                self.page = page
                self.first = self

            def locator(self, _: str) -> FakeHeading | FakeButton:
                return FakeButton(self.page, "unnamed_close") if _ == "button" else self

        class FakePage:
            url = "https://manager.line.biz/account/test"

            def __init__(self) -> None:
                self.welcome_visible = True
                self.actions: list[str] = []

            def get_by_role(self, _: str, **__: object) -> FakeButton:
                return FakeButton(self, "named_close")

            def get_by_text(self, label: Any, **_: object) -> FakeHeading | FakeButton:
                return FakeHeading(self) if not isinstance(label, str) else FakeButton(self, label)

        page = FakePage()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        with patch.object(
            browser,
            "_page_text",
            side_effect=lambda _: (
                "Welcome! (1/2)\n官方帳號測試 Home Settings"
                if page.welcome_visible else "官方帳號測試 Home Settings"
            ),
        ):
            self.assertTrue(browser._dismiss_manager_welcome(page))
            self.assertTrue(browser._manager_account_is_open(page, "官方帳號測試"))
            self.assertTrue(browser._click_first(page, ("Settings",)))

        self.assertEqual(page.actions, ["unnamed_close", "Settings"])

    def test_existing_manager_account_opens_settings_before_messaging_api(self) -> None:
        page = type(
            "FakePage",
            (),
            {"url": "https://developers.line.biz/console/channel/2001234567/messaging-api"},
        )()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        with (
            patch.object(browser, "open_authenticated_page", return_value=page),
            patch.object(browser, "_automate"),
            patch.object(browser, "_manager_account_is_open", return_value=True),
            patch.object(browser, "_click_first", return_value=True) as click,
            patch.object(browser, "_provider_options", return_value=("Existing",)),
            patch.object(browser, "_page_text", return_value="Enable Messaging API"),
            patch("zeal.line_bot.prompt_provider_choice", return_value=("Existing", None)),
        ):
            result = browser.enable_messaging_api("測試帳號")

        self.assertEqual(result.channel_id, "2001234567")
        self.assertFalse(result.already_enabled)
        labels = [call.args[1] for call in click.call_args_list]
        self.assertEqual(labels[:3], [
            ("Settings", "設定"),
            ("Messaging API", "Messaging API設定", "Messaging API settings"),
            ("Enable Messaging API", "啟用 Messaging API", "Messaging APIを利用する"),
        ])
        self.assertEqual(labels[-2:], [("OK",), ("OK",)])

    def test_enabled_messaging_api_reuses_channel_without_provider_prompt(self) -> None:
        page = type("FakePage", (), {"url": "https://manager.line.biz/account/test/setting/messaging-api"})()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        with (
            patch.object(browser, "open_authenticated_page", return_value=page),
            patch.object(browser, "_automate"),
            patch.object(browser, "_manager_account_is_open", return_value=True),
            patch.object(browser, "_click_first") as click,
            patch.object(browser, "_page_text", return_value="Status\nEnabled\nChannel ID\n2001234567"),
            patch("zeal.line_bot.prompt_provider_choice") as provider_prompt,
        ):
            result = browser.enable_messaging_api("測試帳號")

        self.assertEqual(result.channel_id, "2001234567")
        self.assertTrue(result.already_enabled)
        self.assertEqual(result.provider, "已綁定（名稱未讀取）")
        self.assertEqual([call.args[1] for call in click.call_args_list], [
            ("Settings", "設定"),
            ("Messaging API", "Messaging API設定", "Messaging API settings"),
        ])
        provider_prompt.assert_not_called()

    def test_selected_account_url_is_used_for_messaging_api(self) -> None:
        page = type("FakePage", (), {
            "url": "https://manager.line.biz/account/chosen/setting/messaging-api"
        })()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        manager_url = "https://manager.line.biz/account/chosen"
        with (
            patch.object(browser, "open_authenticated_page", return_value=page) as open_page,
            patch.object(browser, "_automate"),
            patch.object(browser, "_click_first") as click,
            patch.object(browser, "_page_text", return_value="Status\nEnabled\nChannel ID\n2001234567"),
        ):
            result = browser.enable_messaging_api("讀書會", manager_url)
        self.assertEqual(result.channel_id, "2001234567")
        self.assertEqual(open_page.call_args.args[0], manager_url)
        self.assertEqual(click.call_args_list[0].args[1], ("Settings", "設定"))

        page.url = "https://manager.line.biz/account/other"
        with (
            patch.object(browser, "open_authenticated_page", return_value=page),
            patch.object(browser, "_show_manual_page"),
            patch.object(browser, "_click_first") as click,
        ):
            with self.assertRaisesRegex(SetupError, "未開啟剛選擇的官方帳號"):
                browser.enable_messaging_api("讀書會", manager_url)
        click.assert_not_called()

    def test_credentials_come_from_token_and_basic_settings_of_same_channel(self) -> None:
        class FakePage:
            text = "Channel access token (long-lived)\n" + "T" * 50

            def get_by_role(self, role: str, **kwargs: object) -> Any:
                self.asserted_role = (role, kwargs)
                return self

            def click(self, **_: object) -> None:
                self.text = "Channel secret\n" + "a" * 32

        page = FakePage()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        with (
            patch.object(browser, "open_authenticated_page", return_value=page),
            patch.object(browser, "_automate"),
            patch.object(browser, "_page_text", side_effect=lambda _: page.text),
            patch.object(browser, "_act", side_effect=lambda _page, operation: operation()),
        ):
            credentials = browser.wait_for_credentials("2001234567")

        self.assertEqual(credentials.channel_secret, "a" * 32)
        self.assertEqual(credentials.channel_access_token, "T" * 50)
        self.assertEqual(page.asserted_role[0], "button")

    def test_auto_response_is_disabled_only_after_webhook_settings_load(self) -> None:
        state = {"loaded": False, "auto_reply": True, "reloads": 0}

        class Control:
            def __init__(self, kind: str) -> None:
                self.kind = kind

            def count(self) -> int:
                return 1

            def is_checked(self) -> bool:
                return state["loaded"] if self.kind == "webhook" else state["auto_reply"]

            def click(self, **_: object) -> None:
                self.assert_kind = self.kind
                state["auto_reply"] = False

        class FakePage:
            url = "https://manager.line.biz/account/test/setting/response"

            def locator(self, selector: str) -> Control:
                if "webhook-setting" in selector:
                    return Control("webhook")
                return Control("auto_reply")

            def reload(self, **_: object) -> None:
                state["loaded"] = False
                state["reloads"] += 1

        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        with (
            patch.object(browser, "open_authenticated_page", return_value=FakePage()),
            patch.object(browser, "_manager_account_is_open", return_value=True),
            patch.object(browser, "_click_first"),
            patch.object(browser, "_act", side_effect=lambda _page, operation: operation()),
            patch.object(browser, "_wait_for_browser", side_effect=lambda _: state.update(loaded=True)),
        ):
            self.assertTrue(browser.disable_auto_response_messages("測試帳號"))

        self.assertFalse(state["auto_reply"])
        self.assertEqual(state["reloads"], 1)

    def test_interrupted_setup_finds_existing_account_before_creation(self) -> None:
        class FakeLocator:
            first: Any

            def __init__(self) -> None:
                self.first = self

            def wait_for(self, **kwargs: object) -> None:
                self.waited = kwargs

        class FakePage:
            url = "https://manager.line.biz/"

            def __init__(self) -> None:
                self.locator = FakeLocator()

            def get_by_text(self, text: str, **kwargs: object) -> FakeLocator:
                self.searched = (text, kwargs)
                return self.locator

        page = FakePage()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = type("FakeContext", (), {"pages": [page]})()
        with patch.object(browser, "open_authenticated_page", return_value=page):
            self.assertTrue(browser.existing_official_account("測試帳號"))

        self.assertEqual(page.searched, ("測試帳號", {"exact": True}))
        self.assertEqual(page.locator.waited["state"], "visible")

    def test_existing_account_list_uses_visible_manager_account_links(self) -> None:
        class FakeLink:
            def __init__(self, href: str, label: str, visible: bool = True) -> None:
                self.href, self.label, self.visible = href, label, visible

            def is_visible(self) -> bool:
                return self.visible

            def get_attribute(self, _: str) -> str:
                return self.href

            def inner_text(self) -> str:
                return self.label

        class FakeLinks:
            def all(self) -> list[FakeLink]:
                return [
                    FakeLink("/account/first", "咖啡客服\n@coffee"),
                    FakeLink("https://manager.line.biz/account/second", "讀書會"),
                    FakeLink("/account/first/setting", "咖啡客服"),
                    FakeLink("https://other.example/account/third", "其他網站"),
                    FakeLink("/account/hidden", "隱藏帳號", False),
                ]

        class FakePage:
            url = "https://manager.line.biz/"

            def locator(self, _: str) -> FakeLinks:
                return FakeLinks()

        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        with (
            patch.object(browser, "open_authenticated_page", return_value=FakePage()),
            patch.object(browser, "_accept_information_use_consent", return_value=False),
            patch.object(browser, "_acknowledge_line_continue", return_value=False),
            patch.object(browser, "_dismiss_manager_welcome", return_value=False),
        ):
            choices = browser.list_official_accounts()
        self.assertEqual(choices, (
            OfficialAccountChoice("咖啡客服", "https://manager.line.biz/account/first"),
            OfficialAccountChoice("讀書會", "https://manager.line.biz/account/second"),
        ))

    def test_existing_account_list_hands_login_popup_to_user_and_continues(self) -> None:
        class FakeLink:
            def is_visible(self) -> bool:
                return True

            def get_attribute(self, _: str) -> str:
                return "/account/second"

            def inner_text(self) -> str:
                return "讀書會"

        class FakeLinks:
            def __init__(self, page: Any) -> None:
                self.page = page

            def all(self) -> list[FakeLink]:
                return [FakeLink()] if self.page.ready else []

        class FakePage:
            def __init__(self, url: str) -> None:
                self.url = url
                self.ready = False

            def locator(self, _: str) -> FakeLinks:
                return FakeLinks(self)

        manager = FakePage("https://manager.line.biz/")
        login = FakePage("https://account.line.biz/login")
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = type("FakeContext", (), {"pages": [manager, login]})()

        def complete_login(_: int) -> None:
            login.url = "https://manager.line.biz/"
            login.ready = True

        with (
            patch.object(browser, "open_authenticated_page", return_value=manager) as open_page,
            patch.object(browser, "_give_user_control") as hand_off,
            patch.object(browser, "_automate") as automate,
            patch.object(browser, "_wait_for_browser", side_effect=complete_login),
            patch("builtins.input") as prompt,
            redirect_stdout(StringIO()),
        ):
            choices = browser.list_official_accounts()

        self.assertEqual(choices, (OfficialAccountChoice("讀書會", "https://manager.line.biz/account/second"),))
        self.assertTrue(open_page.call_args.kwargs["defer_human_verification"])
        hand_off.assert_called_once_with(login, "請在此頁登入 LINE；ZEAL 會自動接續")
        automate.assert_called_once()
        prompt.assert_not_called()

    def test_unreadable_account_list_stops_before_selection(self) -> None:
        class EmptyLinks:
            def all(self) -> list[object]:
                return []

        class FakePage:
            url = "https://manager.line.biz/"

            def locator(self, _: str) -> EmptyLinks:
                return EmptyLinks()

        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        with (
            patch.object(browser, "open_authenticated_page", return_value=FakePage()),
            patch.object(browser, "_accept_information_use_consent", return_value=False),
            patch.object(browser, "_acknowledge_line_continue", return_value=False),
            patch.object(browser, "_dismiss_manager_welcome", return_value=False),
            patch.object(browser, "_show_manual_page") as show_page,
            patch.object(browser, "_wait_for_browser"),
            patch("zeal.line_bot.time.monotonic", side_effect=[0, 1, 9]),
        ):
            with self.assertRaisesRegex(SetupError, "無法從 LINE 管理頁讀取"):
                browser.list_official_accounts()
        show_page.assert_called_once_with(FakePage.url)

    def test_late_information_consent_is_handled_during_account_lookup(self) -> None:
        class FakeLocator:
            def __init__(self, page: Any, consent: bool = False) -> None:
                self.page = page
                self.consent = consent
                self.first = self
                self.last = self

            def wait_for(self, **_: object) -> None:
                if not self.page.consented:
                    self.page.consent_visible = True
                    raise TimeoutError()

            def click(self, **_: object) -> None:
                assert self.consent
                self.page.consented = True
                self.page.consent_visible = False

        class FakePage:
            url = "https://manager.line.biz/"

            def __init__(self) -> None:
                self.consented = False
                self.consent_visible = False

            def get_by_text(self, text: str, **_: object) -> FakeLocator:
                return FakeLocator(self, consent=text == "同意")

            def get_by_role(self, *_: object, **__: object) -> FakeLocator:
                return FakeLocator(self, consent=True)

        page = FakePage()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = type("FakeContext", (), {"pages": [page]})()
        with (
            patch.object(browser, "open_authenticated_page", return_value=page),
            patch.object(
                browser,
                "_page_text",
                side_effect=lambda _: "同意我們使用您的資訊" if page.consent_visible else "",
            ),
        ):
            self.assertTrue(browser.existing_official_account("測試帳號"))

        self.assertTrue(page.consented)

    def test_setup_error_keeps_browser_open_until_user_inspects_it(self) -> None:
        class FakePage:
            def bring_to_front(self) -> None:
                self.front = True

        class FakeContext:
            def __init__(self) -> None:
                self.pages = [FakePage()]
                self.closed = False

            def close(self) -> None:
                self.closed = True

        context = FakeContext()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = context
        browser.playwright = type("FakePlaywright", (), {"stop": lambda self: None})()

        def inspect(_: str) -> str:
            self.assertFalse(context.closed)
            self.assertTrue(context.pages[0].front)
            return ""

        with patch("builtins.input", side_effect=inspect) as prompt:
            browser.__exit__(SetupError, SetupError("介面改變"), None)

        prompt.assert_called_once()
        self.assertTrue(context.closed)

    def test_browser_gate_hands_page_input_to_user_and_restores_lock(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.ignored: list[bool] = []

            def send(self, method: str, params: dict[str, bool]) -> None:
                self.ignored.append(params["ignore"])

        class FakePage:
            def __init__(self) -> None:
                self.front = False
                self.mode = ""

            def is_closed(self) -> bool:
                return False

            def on(self, *_: object) -> None:
                pass

            def evaluate(self, _: str, params: list[str]) -> None:
                self.mode = params[0]

            def bring_to_front(self) -> None:
                self.front = True

        class FakeContext:
            def __init__(self) -> None:
                self.pages = [FakePage(), FakePage()]
                self.sessions = {page: FakeSession() for page in self.pages}
                self.script = ""

            def add_init_script(self, *, script: str) -> None:
                self.script = script

            def on(self, *_: object) -> None:
                pass

            def new_cdp_session(self, page: FakePage) -> FakeSession:
                return self.sessions[page]

        context = FakeContext()
        gate = BrowserGate(context)
        page, other = context.pages
        self.assertIn("zeal-browser-gate", context.script)
        self.assertEqual(context.sessions[page].ignored, [True])
        gate.human(page)
        self.assertTrue(page.front)
        self.assertEqual(page.mode, "human")
        self.assertEqual(other.mode, "automation")
        self.assertEqual(context.sessions[page].ignored[-1], False)
        self.assertEqual(context.sessions[other].ignored[-1], True)
        gate.automation()
        self.assertEqual(context.sessions[page].ignored[-1], True)
        self.assertEqual(gate.action(page, lambda: "done"), "done")
        self.assertEqual(context.sessions[page].ignored[-2:], [False, True])

        def failed_action() -> None:
            raise ValueError("failed")

        with self.assertRaisesRegex(ValueError, "failed"):
            gate.action(page, failed_action)
        self.assertEqual(context.sessions[page].ignored[-2:], [False, True])

    def test_account_submission_checks_required_terms_but_not_optional_consent(self) -> None:
        class FakeCheckbox:
            def __init__(self) -> None:
                self.checked = False

            def is_visible(self) -> bool:
                return True

            def is_enabled(self) -> bool:
                return True

            def is_checked(self) -> bool:
                return self.checked

            def check(self, **_: object) -> None:
                self.checked = True

        class FakeLocator:
            def __init__(self, values: list[FakeCheckbox] | None = None) -> None:
                self.values = values or []

            def all(self) -> list[FakeCheckbox]:
                return self.values

            def inner_text(self) -> str:
                return ""

        class FakeButton:
            def __init__(self) -> None:
                self.clicked = False
                self.click_count = 0
                self.last = self

            def click(self, **_: object) -> None:
                self.clicked = True
                self.click_count += 1

        class FakePage:
            url = "https://entry.line.biz/form/entry/unverified"

            def __init__(self, required: FakeCheckbox, button: FakeButton) -> None:
                self.required = required
                self.button = button

            def locator(self, selector: str) -> FakeLocator:
                if selector == 'input[type="checkbox"][required]':
                    return FakeLocator([self.required])
                return FakeLocator()

            def get_by_text(self, label: str, *, exact: bool) -> FakeButton:
                self.last_label = label
                return self.button

        required = FakeCheckbox()
        button = FakeButton()
        page = FakePage(required, button)
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = type("FakeContext", (), {"pages": [page]})()

        with patch("builtins.input") as prompt:
            browser.submit_account_creation(page)

        self.assertTrue(required.checked)
        self.assertTrue(button.clicked)
        self.assertEqual(button.click_count, 2)
        self.assertEqual(page.last_label, "完成")
        self.assertTrue(browser.account_finish_clicked)
        prompt.assert_not_called()

    def test_account_creation_keeps_same_page_after_human_verification(self) -> None:
        class FakePage:
            url = LINE_OFFICIAL_ACCOUNT_ENTRY_URL

            def bring_to_front(self) -> None:
                self.brought_to_front = True

        page = FakePage()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = type("FakeContext", (), {"pages": [page]})()

        def mark_created(_: str) -> str:
            page.url = "https://manager.line.biz/"
            return ""

        with (
            patch("zeal.line_bot.time.monotonic", side_effect=[0, 16, 20, 20]),
            patch("builtins.input", side_effect=mark_created) as prompt,
            patch.object(browser, "_page_text", return_value=""),
            patch.object(browser, "_account_human_verification_reason", return_value="CAPTCHA"),
        ):
            browser.continue_after_account_creation()

        prompt.assert_called_once()
        self.assertTrue(page.brought_to_front)
        self.assertIs(browser.pages[0], page)

    def test_account_creation_stops_on_stalled_input_form(self) -> None:
        class FakeSelects:
            def count(self) -> int:
                return 3

        class FakePage:
            url = LINE_OFFICIAL_ACCOUNT_ENTRY_URL

            def is_closed(self) -> bool:
                return False

            def locator(self, _: str) -> FakeSelects:
                return FakeSelects()

        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = type("FakeContext", (), {"pages": [FakePage()]})()
        with (
            patch("zeal.line_bot.time.monotonic", side_effect=[0, 16]),
            patch.object(browser, "_page_text", return_value=""),
            patch.object(browser, "_click_account_finish", return_value=False),
            patch.object(browser, "_show_manual_page") as show_manual,
        ):
            with self.assertRaisesRegex(SetupError, "找不到 LINE 確認頁"):
                browser.continue_after_account_creation()
        show_manual.assert_not_called()

    def test_account_creation_retries_finish_after_human_verification(self) -> None:
        class FakePage:
            url = LINE_OFFICIAL_ACCOUNT_ENTRY_URL

            def bring_to_front(self) -> None:
                self.brought_to_front = True

        page = FakePage()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = type("FakeContext", (), {"pages": [page]})()

        def click_finish() -> bool:
            page.url = "https://manager.line.biz/"
            return True

        with (
            patch("zeal.line_bot.time.monotonic", side_effect=[0, 16, 20, 21]),
            patch.object(browser, "_page_text", return_value=""),
            patch.object(browser, "_account_human_verification_reason", return_value="CAPTCHA"),
            patch.object(browser, "_click_account_finish", side_effect=click_finish) as finish,
            patch("builtins.input", return_value="") as prompt,
        ):
            browser.continue_after_account_creation()

        self.assertTrue(page.brought_to_front)
        self.assertIs(browser.pages[0], page)
        prompt.assert_called_once()
        finish.assert_called_once()
        self.assertTrue(browser.account_finish_clicked)

    def test_account_creation_does_not_repeat_finish_without_success(self) -> None:
        page = type("FakePage", (), {"url": LINE_OFFICIAL_ACCOUNT_ENTRY_URL})()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = type("FakeContext", (), {"pages": [page]})()
        browser.account_finish_clicked = True

        with (
            patch("zeal.line_bot.time.monotonic", side_effect=[0, 16]),
            patch.object(browser, "_page_text", return_value=""),
            patch.object(browser, "_account_human_verification_reason", return_value=None),
            patch.object(browser, "_click_account_finish") as finish,
            patch.object(browser, "_show_manual_page") as show_manual,
        ):
            with self.assertRaisesRegex(SetupError, "未回報建立成功"):
                browser.continue_after_account_creation()

        finish.assert_not_called()
        show_manual.assert_not_called()


class LineBotRecoveryTests(unittest.TestCase):
    def test_account_form_retry_reuses_the_current_browser_page(self) -> None:
        class ExistingForm:
            url = LINE_OFFICIAL_ACCOUNT_ENTRY_URL

            def is_closed(self) -> bool:
                return False

            def locator(self, _: str) -> Any:
                return type("Selects", (), {"count": lambda self: 3})()

        page = ExistingForm()
        context = type("Context", (), {"pages": [page], "new_page": lambda self: self.fail()})()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        browser.context = context
        with patch.object(browser, "fill_official_account_form") as fill:
            self.assertIs(browser.begin_account_creation(AccountDetails("咖啡客服", "", 8000)), page)
        fill.assert_called_once()

    def test_missing_first_token_is_issued_without_reissuing(self) -> None:
        token = "T" * 50
        secret = "a" * 32

        class Control:
            def __init__(self, page: Any, kind: str) -> None:
                self.page, self.kind = page, kind

            def count(self) -> int:
                return 1

            def click(self, **_: object) -> None:
                if self.kind == "issue":
                    self.page.text = f"Channel access token (long-lived) {token}"
                else:
                    self.page.text = f"Channel secret {secret}"

        class FakePage:
            text = "Channel access token (long-lived)"

            def get_by_role(self, _: str, *, name: Any, **__: object) -> Control:
                return Control(self, "basic" if isinstance(name, str) else "issue")

        page = FakePage()
        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        with (patch.object(browser, "open_authenticated_page", return_value=page),
              patch.object(browser, "_page_text", side_effect=lambda current: current.text),
              patch("zeal.line_bot.time.monotonic", side_effect=count()),
              patch.object(browser, "_wait_for_browser")):
            credentials = browser.wait_for_credentials("2001234567")
        self.assertEqual(credentials, Credentials(secret, token))

    def test_invalid_account_fields_are_reasked_at_the_same_question(self) -> None:
        answers = iter(["", "咖啡客服", "", "小明咖啡", "bad", "hello@example.com", "8000"])
        with patch("builtins.input", side_effect=lambda _: next(answers)), redirect_stdout(StringIO()):
            account = prompt_account_details(0)
        self.assertEqual((account.name, account.company_name, account.email, account.port),
                         ("咖啡客服", "小明咖啡", "hello@example.com", 8000))

    def test_malformed_cli_port_reaches_guided_correction(self) -> None:
        args = build_parser().parse_args(["line-bot", "setup", "--port", "oops"])
        self.assertEqual(args.port, 0)
        with patch("builtins.input", side_effect=["70000", "8123"]), redirect_stdout(StringIO()):
            self.assertEqual(prompt_valid_port(args.port), 8123)

    def test_browser_step_can_hand_control_back_and_retry(self) -> None:
        class FakeBrowser:
            pages = [object()]

            def _give_user_control(self, *_: object) -> None:
                self.handed_off = True

            def _automate(self) -> None:
                self.resumed = True

        browser = FakeBrowser()
        attempts = 0

        def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise SetupError("page changed")
            return "ready"

        with (patch("zeal.line_bot.prompt_option", return_value="在目前瀏覽器手動處理後重新辨識"),
              patch("builtins.input", return_value=""), redirect_stdout(StringIO())):
            self.assertEqual(retry_setup_step("LINE page", operation, browser), "ready")
        self.assertEqual(attempts, 2)
        self.assertTrue(browser.handed_off)
        self.assertTrue(browser.resumed)

    def test_port_change_updates_only_validated_env(self) -> None:
        credentials = Credentials("a" * 32, "T" * 50)
        account = AccountDetails("咖啡客服", "", 8000)
        with tempfile.TemporaryDirectory() as temporary:
            directory = project_directory(Path(temporary), account.name)
            write_project(directory, account)
            write_credentials(directory, credentials, 8000)
            change_project_port(directory, credentials, 8000, 8123)
            verify_existing_credentials(directory, credentials, 8123)
            self.assertFalse(list(directory.glob(".env-port-*")))

    def test_occupied_port_can_change_without_restarting_setup(self) -> None:
        credentials = Credentials("a" * 32, "T" * 50)
        account = AccountDetails("咖啡客服", "", 8000)
        args = build_parser().parse_args(["line-bot", "setup"])

        class RunningApp:
            def poll(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary:
            directory = project_directory(Path(temporary), account.name)
            write_project(directory, account)
            write_credentials(directory, credentials, 8000)
            with (patch("zeal.line_bot.existing_tunnel_url", return_value="https://example.ngrok.app"),
                  patch("zeal.line_bot.local_port_listening", side_effect=lambda port: port == 8000),
                  patch("zeal.line_bot.line_api_request", return_value={"success": False}),
                  patch("zeal.line_bot.prompt_option", return_value="改用其他本機連接埠"),
                  patch("zeal.line_bot.prompt_valid_port", return_value=8123),
                  patch("zeal.line_bot.ensure_target_dependencies"),
                  patch("zeal.line_bot.start_app", return_value=RunningApp()),
                  patch("zeal.line_bot.time.sleep"), redirect_stdout(StringIO())):
                updated, ngrok, app, callback = start_setup_runtime(args, account, directory, credentials)
            self.assertEqual(updated.port, 8123)
            self.assertIsNone(ngrok)
            self.assertIsNotNone(app)
            self.assertEqual(callback, "https://example.ngrok.app/callback")
            verify_existing_credentials(directory, credentials, 8123)


if __name__ == "__main__":
    unittest.main()
