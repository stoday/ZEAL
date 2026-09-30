from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
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
    NgrokAsset,
    SetupError,
    generated_app,
    format_completion_summary,
    account_creation_detected,
    channel_id_from_url,
    human_verification_reason,
    messaging_api_url,
    option_from_choice,
    prompt_provider_choice,
    default_browser_profile_directory,
    project_directory,
    prepare_setup_project,
    write_credentials,
    write_project,
)


class LineBotProjectTests(unittest.TestCase):
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

    def test_category_menu_accepts_number_or_exact_text(self) -> None:
        options = ("餐飲", "生活相關服務")
        self.assertEqual(option_from_choice("2", options), "生活相關服務")
        self.assertEqual(option_from_choice("餐飲", options), "餐飲")
        with self.assertRaisesRegex(Exception, "清單"):
            option_from_choice("資訊服務", options)

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
        self.assertIn("持續在目前終端背景執行", summary)
        self.assertNotIn("hello@example.com", summary)
        self.assertNotIn("channel_secret", summary.casefold())

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

    def test_interrupted_setup_finds_existing_account_before_creation(self) -> None:
        class FakeLocator:
            first: Any

            def __init__(self) -> None:
                self.first = self

            def wait_for(self, **kwargs: object) -> None:
                self.waited = kwargs

        class FakePage:
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
