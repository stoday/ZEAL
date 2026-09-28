from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zeal.cli import build_parser
from zeal.line_bot import (
    AccountDetails,
    Credentials,
    MessagingApiSetup,
    LINE_OFFICIAL_ACCOUNT_ENTRY_URL,
    LineConsoleBrowser,
    NgrokAsset,
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
    write_credentials,
    write_project,
)


class LineBotProjectTests(unittest.TestCase):
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

    def test_browser_restart_closes_visible_context_before_reusing_profile_headlessly(self) -> None:
        class FakeContext:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class FakeChromium:
            def __init__(self) -> None:
                self.headless_values: list[bool] = []

            def launch_persistent_context(self, **kwargs: object) -> FakeContext:
                self.headless_values.append(bool(kwargs["headless"]))
                return FakeContext()

        class FakePlaywright:
            def __init__(self) -> None:
                self.chromium = FakeChromium()

        browser = LineConsoleBrowser(True, Path(tempfile.gettempdir()) / "zeal-test-profile")
        old_context = FakeContext()
        fake_playwright = FakePlaywright()
        browser.context = old_context
        browser.playwright = fake_playwright

        browser.restart(headless=True)

        self.assertTrue(old_context.closed)
        self.assertTrue(browser.headless)
        self.assertEqual(fake_playwright.chromium.headless_values, [True])

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
                self.last = self

            def click(self, **_: object) -> None:
                self.clicked = True

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

        browser.submit_account_creation(page)

        self.assertTrue(required.checked)
        self.assertTrue(button.clicked)


if __name__ == "__main__":
    unittest.main()
