from __future__ import annotations

import tempfile
import unittest
import os
import subprocess
import json
import sys
from contextlib import redirect_stdout
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
    LINE_OFFICIAL_ACCOUNT_ENTRY_URL,
    LineConsoleBrowser,
    _install_chromium_if_missing,
    NgrokAsset,
    NGROK_AUTHTOKEN_URL,
    SetupError,
    generated_app,
    format_completion_summary,
    format_runtime_instructions,
    confirm_setup_start,
    setup_step,
    start_background_process,
    run_setup,
    existing_tunnel_url,
    ensure_ngrok_config,
    account_creation_detected,
    channel_id_from_url,
    channel_id_from_settings_text,
    human_verification_reason,
    messaging_api_url,
    option_from_choice,
    prompt_option,
    prompt_provider_choice,
    default_browser_profile_directory,
    project_directory,
    prepare_setup_project,
    write_credentials,
    write_project,
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

        self.assertIn("申請 LINE 官方帳號", output.getvalue())
        self.assertIn("簡易機器人", output.getvalue())
        self.assertIn("操作瀏覽器", output.getvalue())
        self.assertIn("登入或人類驗證", output.getvalue())
        self.assertNotIn("ngrok", output.getvalue())
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
                    patch("zeal.line_bot.prompt_account_details") as account_prompt,
                    redirect_stdout(StringIO()),
                ):
                    run_setup(args)
                account_prompt.assert_not_called()

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
        with (
            patch.dict(os.environ, {"NGROK_AUTHTOKEN": ""}),
            patch("zeal.line_bot.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as check,
            patch("zeal.line_bot.ngrok_authtoken") as prompt,
            patch("zeal.line_bot.configure_ngrok") as configure,
        ):
            ensure_ngrok_config(Path("ngrok"), None)

        self.assertEqual(check.call_args.args[0], ["ngrok", "config", "check"])
        prompt.assert_not_called()
        configure.assert_not_called()

    def test_successful_setup_leaves_started_services_running(self) -> None:
        class FakeProcess:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def poll(self) -> None:
                return None

        class FakeBrowser:
            pages: list[Any] = []

            def __enter__(self) -> FakeBrowser:
                return self

            def __exit__(self, *_: object) -> None:
                pass

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

            def configure_webhook(self, _: str, __: str) -> bool:
                return True

            def disable_auto_response_messages(self, _: str) -> bool:
                return True

        with tempfile.TemporaryDirectory() as temporary:
            args = build_parser().parse_args(["line-bot", "setup", "--output", temporary])
            account = AccountDetails("測試", "", 8000, "公司", "test@example.com")
            ngrok, bot = FakeProcess(1111), FakeProcess(2222)
            output = StringIO()
            with (
                redirect_stdout(output),
                patch("builtins.input", side_effect=["", ""]),
                patch("zeal.line_bot.prompt_account_details", return_value=account),
                patch("zeal.line_bot.prepare_setup_project", return_value=False),
                patch("zeal.line_bot.existing_tunnel_url", return_value=None),
                patch("zeal.line_bot.install_ngrok", return_value=Path("ngrok")),
                patch("zeal.line_bot.ensure_ngrok_config"),
                patch("zeal.line_bot.tunnel_url", return_value=(ngrok, "https://example.ngrok.app")),
                patch("zeal.line_bot.LineConsoleBrowser", return_value=FakeBrowser()),
                patch("zeal.line_bot.write_credentials"),
                patch("zeal.line_bot.ensure_target_dependencies"),
                patch("zeal.line_bot.start_app", return_value=bot),
                patch("zeal.line_bot.runtime_log_directory", return_value=Path(temporary)),
                patch("zeal.line_bot.time.sleep"),
                patch("zeal.line_bot.stop_process") as stop,
            ):
                run_setup(args)

            stop.assert_not_called()
            self.assertIn("[步驟 1/8]", output.getvalue())
            self.assertIn("[步驟 8/8]", output.getvalue())
            self.assertIn(NGROK_AUTHTOKEN_URL, output.getvalue())
            self.assertIn("Bot PID：2222", output.getvalue())
            self.assertIn("ngrok PID：1111", output.getvalue())

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
        instructions = format_runtime_instructions(1111, 2222, Path("C:/zeal/logs"))
        self.assertIn("Bot PID：1111", instructions)
        self.assertIn("ngrok PID：2222", instructions)
        self.assertIn("bot.log", instructions)
        self.assertIn("ngrok.log", instructions)
        self.assertIn("1111", instructions.split("停止本次啟動的程序：")[1])

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
        self.assertEqual([call.args[1] for call in click.call_args_list], [
            ("Settings", "設定"),
            ("Messaging API", "Messaging API設定", "Messaging API settings"),
        ])
        provider_prompt.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
