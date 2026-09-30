"""Local bootstrap flow for a LINE Messaging API echo bot.

LINE sign-in, MFA, CAPTCHA, and other proof-of-humanity remain human-only. All
other safely recognised controls are automated from a local persistent profile;
the session is visible only while a human checkpoint needs attention.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LINE_MANAGER_URL = "https://manager.line.biz/"
LINE_CONSOLE_URL = "https://developers.line.biz/console/"
LINE_OFFICIAL_ACCOUNT_ENTRY_URL = "https://entry.line.biz/form/entry/unverified"
NGROK_DOWNLOAD_BASE = "https://bin.equinox.io/c/bNyj1mQVY4c"

# These are detection hints, not selectors to click.  A match always hands the
# browser to the account holder; ZEAL never attempts to solve a challenge.
HUMAN_VERIFICATION_MARKERS = (
    "captcha",
    "recaptcha",
    "i'm not a robot",
    "i am not a robot",
    "security check",
    "two-step verification",
    "two-factor authentication",
    "one-time password",
    "sign in",
    "登入",
    "認證碼",
    "驗證碼",
    "兩步驟驗證",
    "雙重驗證",
    "我是人類",
    "不是機器人",
)
ACCOUNT_CREATED_MARKERS = (
    "account created",
    "建立完成",
    "建立成功",
    "帳號已建立",
)
NEW_PROVIDER_OPTION = "建立新的 Provider"

class SetupError(RuntimeError):
    """A setup step could not safely continue."""


def messaging_api_url(channel_id: str) -> str:
    """Build the Console URL without accepting arbitrary browser destinations."""
    channel_id = channel_id.strip()
    if not re.fullmatch(r"\d{6,}", channel_id):
        raise SetupError("Channel ID 應為 LINE Developers Console 顯示的數字。")
    return f"{LINE_CONSOLE_URL}channel/{channel_id}/messaging-api"


def option_from_choice(choice: str, options: tuple[str, ...] | list[str]) -> str:
    """Accept an explicit label or a one-based menu number."""
    normalized = choice.strip()
    if normalized in options:
        return normalized
    if normalized.isdecimal():
        index = int(normalized) - 1
        if 0 <= index < len(options):
            return options[index]
    raise SetupError("請輸入清單中的編號或完整選項名稱。")


def prompt_option(label: str, options: tuple[str, ...] | list[str]) -> str:
    print(f"\n{label}：")
    for index, option in enumerate(options, start=1):
        print(f"  {index:>2}. {option}")
    while True:
        try:
            return option_from_choice(input(f"{label}（輸入編號或完整名稱）："), options)
        except SetupError as error:
            print(error)


def prompt_provider_choice(options: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Let the account holder choose an irreversible Provider at the last responsible moment."""
    if not options:
        raise SetupError("LINE 沒有提供可選的既有 Provider。")
    choice = prompt_option("LINE Provider", (*options, NEW_PROVIDER_OPTION))
    if choice != NEW_PROVIDER_OPTION:
        return choice, None
    while True:
        name = input("新 Provider 名稱：").strip()
        if name:
            return None, name
        print("新 Provider 名稱不可空白。")


@dataclass(frozen=True)
class AccountDetails:
    name: str
    category: str
    port: int
    company_name: str = ""
    email: str = ""


@dataclass(frozen=True)
class Credentials:
    channel_secret: str
    channel_access_token: str


@dataclass(frozen=True)
class MessagingApiSetup:
    channel_id: str
    provider: str


@dataclass(frozen=True)
class NgrokAsset:
    system: str
    architecture: str
    extension: str

    @property
    def url(self) -> str:
        return (
            f"{NGROK_DOWNLOAD_BASE}/ngrok-v3-stable-"
            f"{self.system}-{self.architecture}.{self.extension}"
        )


def prompt_account_details(port: int) -> AccountDetails:
    print("\nLINE Official Account 的資料會在下一步填入 LINE 後台。")
    print("名稱範例：小明咖啡客服、星球讀書會。")
    name = input("官方帳號名稱：").strip()
    if not name:
        raise SetupError("官方帳號名稱不可空白。")
    if len(name) > 20:
        raise SetupError("官方帳號名稱不可超過 20 個字元。")

    print("公司名稱範例：小明咖啡有限公司、星球讀書會。個人可填經營名稱。")
    company_name = input("公司／店鋪名稱：").strip()
    if not company_name:
        raise SetupError("公司／店鋪名稱不可空白。")
    if len(company_name) > 100:
        raise SetupError("公司／店鋪名稱不可超過 100 個字元。")

    print("此信箱會填入 LINE 官方帳號申請表，請使用可收信的地址。")
    email = input("電子郵件帳號：").strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 240:
        raise SetupError("請輸入有效的電子郵件帳號（最長 240 個字元）。")

    if not 1 <= port <= 65535:
        raise SetupError("port 必須介於 1 到 65535。")
    return AccountDetails(
        name=name,
        category="",
        port=port,
        company_name=company_name,
        email=email,
    )


def project_directory(base: Path, account_name: str) -> Path:
    """Make a readable, cross-platform-safe project directory name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", account_name)
    cleaned = re.sub(r"\s+", "-", cleaned).strip(". -")
    if not cleaned:
        cleaned = "line-account"
    return base / f"line-bot-{cleaned[:80]}"


def generated_app() -> str:
    return '''import base64
import hashlib
import hmac
import os

import requests
from dotenv import load_dotenv
from flask import Flask, abort, request

load_dotenv()

app = Flask(__name__)
CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]


def valid_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply(reply_token: str, text: str) -> None:
    response = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        },
        timeout=10,
    )
    response.raise_for_status()


@app.post("/callback")
def callback():
    raw_body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")
    if not valid_signature(raw_body, signature):
        abort(400)

    for event in (request.get_json(silent=True) or {}).get("events", []):
        if event.get("type") != "message":
            continue
        if event.get("message", {}).get("type") != "text":
            continue
        reply(event["replyToken"], f"你說的是：{event['message']['text']}")

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
'''


def project_files(account: AccountDetails) -> dict[str, str]:
    """The files ZEAL may safely recognise when resuming an unfinished setup."""
    return {
        "app.py": generated_app(),
        "requirements.txt": "flask>=3.0,<4\nrequests>=2.31,<3\npython-dotenv>=1.0,<2\n",
        ".gitignore": ".env\n.venv/\n__pycache__/\n*.py[cod]\n",
        ".env.example": (
            "LINE_CHANNEL_SECRET=replace_me\n"
            "LINE_CHANNEL_ACCESS_TOKEN=replace_me\n"
            f"PORT={account.port}\n"
        ),
        "README.md": (
            f"# {account.name} LINE Bot\n\n"
            "此資料夾由 `zeal line-bot setup` 產生。\n\n"
            "## 開發\n\n"
            "```bash\npython -m venv .venv\n"
            "# Windows: .\\.venv\\Scripts\\Activate.ps1\n"
            "# macOS/Linux: source .venv/bin/activate\n"
            "pip install -r requirements.txt\npython app.py\n```\n\n"
            "`.env` 含有 LINE 憑證，已被 `.gitignore` 排除；請勿提交或分享。\n"
        ),
    }


def write_project(directory: Path, account: AccountDetails) -> None:
    if directory.exists():
        raise SetupError(f"目標資料夾已存在，為避免覆寫而停止：{directory}")
    directory.mkdir(parents=True)
    for name, contents in project_files(account).items():
        (directory / name).write_text(contents, encoding="utf-8")


def prepare_setup_project(directory: Path, account: AccountDetails) -> bool:
    """Reuse only an untouched project from a setup interrupted before credentials."""
    if not directory.exists():
        write_project(directory, account)
        return False
    env_file = directory / ".env"
    if directory.is_symlink() or not directory.is_dir() or env_file.exists() or env_file.is_symlink():
        raise SetupError(f"目標資料夾已存在，為避免覆寫而停止：{directory}")
    try:
        unchanged = all(
            (directory / name).read_text(encoding="utf-8") == contents
            for name, contents in project_files(account).items()
        )
    except (OSError, UnicodeError):
        unchanged = False
    if not unchanged:
        raise SetupError(f"目標資料夾已存在且內容不同，為避免覆寫而停止：{directory}")
    return True


def write_credentials(directory: Path, credentials: Credentials, port: int) -> None:
    # JSON quoting is valid for python-dotenv and keeps spaces/special characters safe.
    contents = (
        f"LINE_CHANNEL_SECRET={json.dumps(credentials.channel_secret)}\n"
        f"LINE_CHANNEL_ACCESS_TOKEN={json.dumps(credentials.channel_access_token)}\n"
        f"PORT={port}\n"
    )
    env_file = directory / ".env"
    env_file.write_text(contents, encoding="utf-8")
    with contextlib.suppress(OSError):
        env_file.chmod(0o600)


def format_completion_summary(
    account: AccountDetails,
    directory: Path,
    callback_url: str,
    messaging: MessagingApiSetup,
    *,
    webhook_configured: bool,
    keep_running: bool,
) -> str:
    """Describe the user's resulting LINE setup without exposing credentials."""
    channel_url = messaging_api_url(messaging.channel_id)
    webhook_state = "已儲存、Verify 成功並啟用 Use webhook" if webhook_configured else "尚待在 Console 手動確認"
    company_name = account.company_name or "未提供"
    runtime_state = (
        "Bot 與 ngrok：持續在目前終端背景執行；以 Ctrl+C 結束。"
        if keep_running
        else "Bot 與 ngrok：此摘要顯示後將停止；下次測試前需重新啟動。"
    )
    return "\n".join(
        (
            "\n========== ZEAL 設定摘要 ==========",
            "ZEAL 已建立並設定以下 LINE Bot：",
            f"官方帳號：{account.name}",
            f"公司／店鋪：{company_name}",
            f"Provider：{messaging.provider}",
            f"Messaging API channel：{messaging.channel_id}",
            f"Webhook 狀態：{webhook_state}",
            runtime_state,
            "",
            "開啟與修改位置：",
            f"- LINE Official Account Manager（帳號、個人檔案、歡迎訊息、自動回覆）：{LINE_MANAGER_URL}",
            f"- LINE Developers Console（channel、Webhook、Use webhook、token）：{channel_url}",
            f"- Webhook URL：{callback_url}",
            f"- Bot 回覆程式：{directory / 'app.py'}",
            f"- 本機設定與憑證：{directory / '.env'}（不會顯示或上傳憑證值）",
            "",
            "提醒：Provider 建立後不能移轉到其他 Provider；如需調整帳號或回覆內容，請從上述 Manager 或 app.py 修改。",
            "====================================",
        )
    )


def prompt_existing_credentials() -> Credentials:
    """Read credentials from the controlling terminal without echoing them."""
    secret = getpass.getpass("Channel secret（輸入不會顯示）：").strip()
    token = getpass.getpass("Channel access token（輸入不會顯示）：").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", secret):
        raise SetupError("Channel secret 格式不正確；請從 Basic settings 複製完整值。")
    if len(token) < 40:
        raise SetupError("Channel access token 格式不正確；請從 Messaging API 分頁複製完整值。")
    return Credentials(channel_secret=secret, channel_access_token=token)


def ngrok_asset_for_current_platform() -> NgrokAsset:
    systems = {"windows": "windows", "darwin": "darwin", "linux": "linux"}
    architectures = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "x64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    system = systems.get(platform.system().lower())
    architecture = architectures.get(platform.machine().lower())
    if not system or not architecture:
        raise SetupError(
            "不支援的作業系統或 CPU 架構："
            f"{platform.system()} / {platform.machine()}"
        )
    return NgrokAsset(system, architecture, "zip" if system != "linux" else "tgz")


def zeal_bin_directory() -> Path:
    return zeal_data_directory() / "bin"


def zeal_data_directory() -> Path:
    """Return ZEAL's user-private application-data root on each platform."""
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "Zeal"
    if platform.system().lower() == "darwin":
        return Path.home() / "Library" / "Application Support" / "Zeal"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "zeal"


def default_browser_profile_directory() -> Path:
    """Keep the browser state beside the command's working tree, not in a Bot project."""
    return Path.cwd().resolve() / ".zeal-line-browser-profile"


def install_ngrok() -> Path:
    existing = shutil.which("ngrok")
    if existing:
        return Path(existing)

    asset = ngrok_asset_for_current_platform()
    destination = zeal_bin_directory()
    destination.mkdir(parents=True, exist_ok=True)
    binary = destination / ("ngrok.exe" if asset.system == "windows" else "ngrok")
    if binary.exists():
        return binary

    print(f"找不到 ngrok，正從官方來源下載：{asset.system}/{asset.architecture}")
    with tempfile.TemporaryDirectory(prefix="zeal-ngrok-") as temp_dir:
        archive = Path(temp_dir) / f"ngrok.{asset.extension}"
        try:
            urllib.request.urlretrieve(asset.url, archive)
        except OSError as error:
            raise SetupError(f"無法下載 ngrok：{error}") from error

        if asset.extension == "zip":
            with zipfile.ZipFile(archive) as zipped:
                member = next((item for item in zipped.namelist() if item.endswith("ngrok.exe") or item.endswith("/ngrok") or item == "ngrok"), None)
                if member is None:
                    raise SetupError("ngrok 壓縮檔中找不到執行檔。")
                with zipped.open(member) as source, binary.open("wb") as target:
                    shutil.copyfileobj(source, target)
        else:
            with tarfile.open(archive, "r:gz") as tarred:
                member = next((item for item in tarred.getmembers() if item.isfile() and Path(item.name).name == "ngrok"), None)
                if member is None:
                    raise SetupError("ngrok 壓縮檔中找不到執行檔。")
                source = tarred.extractfile(member)
                if source is None:
                    raise SetupError("無法解開 ngrok 執行檔。")
                with source, binary.open("wb") as target:
                    shutil.copyfileobj(source, target)
    with contextlib.suppress(OSError):
        binary.chmod(0o700)
    return binary


def ngrok_authtoken(argument_value: str | None) -> str:
    return argument_value or os.environ.get("NGROK_AUTHTOKEN") or getpass.getpass(
        "ngrok Authtoken（輸入不會顯示；可先設定 NGROK_AUTHTOKEN）："
    )


def configure_ngrok(binary: Path, token: str) -> None:
    if not token:
        raise SetupError("需要 ngrok Authtoken。請至 https://dashboard.ngrok.com/get-started/your-authtoken 取得。")
    result = subprocess.run(
        [str(binary), "config", "add-authtoken", token],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise SetupError("ngrok Authtoken 設定失敗；請確認 token 後重試。")


def tunnel_url(binary: Path, port: int) -> tuple[subprocess.Popen[str], str]:
    process = subprocess.Popen(
        [str(binary), "http", str(port), "--log=stdout"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    inspector = "http://127.0.0.1:4040/api/tunnels"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SetupError("ngrok 啟動後立即結束。請檢查網路或 Authtoken。")
        try:
            with urllib.request.urlopen(inspector, timeout=2) as response:
                tunnels = json.load(response).get("tunnels", [])
        except (OSError, ValueError, json.JSONDecodeError):
            time.sleep(0.5)
            continue
        for tunnel in tunnels:
            public_url = tunnel.get("public_url", "")
            if public_url.startswith("https://"):
                return process, public_url
        time.sleep(0.5)
    process.terminate()
    raise SetupError("30 秒內無法從 ngrok 取得 HTTPS 公開網址。")


def existing_tunnel_url() -> str | None:
    """Return an HTTPS tunnel already served by the local ngrok inspector."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as response:
            tunnels = json.load(response).get("tunnels", [])
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    for tunnel in tunnels:
        public_url = tunnel.get("public_url", "")
        if public_url.startswith("https://"):
            return public_url
    return None


def target_python(directory: Path) -> Path:
    return directory / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def start_app(directory: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(target_python(directory)), "app.py"],
        cwd=directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def ensure_target_dependencies(directory: Path) -> None:
    python = target_python(directory)
    if not python.exists():
        print("正在建立產生 Bot 專案專用的 .venv…")
        result = subprocess.run(
            [sys.executable, "-m", "venv", ".venv"],
            cwd=directory,
            check=False,
        )
        if result.returncode or not python.exists():
            raise SetupError("無法建立產生 Bot 專案的 .venv。")

    print("正在安裝產生專案的 Python 套件…")
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=directory,
        check=False,
    )
    if result.returncode:
        raise SetupError("無法安裝 Bot 專案套件。請確認目前 Python 環境有 pip 與網路連線。")


def _page_values(page: Any) -> list[str]:
    values: list[str] = []
    for item in page.locator("input").all():
        with contextlib.suppress(Exception):
            value = item.input_value()
            if value:
                values.append(value.strip())
    with contextlib.suppress(Exception):
        values.extend(match.strip() for match in page.locator("body").inner_text().split())
    return values


def extract_credentials(pages: list[Any]) -> Credentials | None:
    """Extract only values with the expected shapes; never print them."""
    secret_pattern = re.compile(r"^[0-9a-fA-F]{32}$")
    token_pattern = re.compile(r"^[A-Za-z0-9._~+/=-]{40,}$")
    secrets: list[str] = []
    tokens: list[str] = []
    for page in pages:
        for value in _page_values(page):
            if secret_pattern.fullmatch(value):
                secrets.append(value)
            elif token_pattern.fullmatch(value):
                tokens.append(value)
    if not secrets or not tokens:
        return None
    return Credentials(channel_secret=secrets[-1], channel_access_token=tokens[-1])


def human_verification_reason(page_text: str) -> str | None:
    """Return a human-only checkpoint found in visible page text, if any."""
    normalized = page_text.casefold()
    for marker in HUMAN_VERIFICATION_MARKERS:
        if marker.casefold() in normalized:
            return marker
    return None


def channel_id_from_url(url: str) -> str | None:
    match = re.search(r"/channel/(\d{6,})/messaging-api(?:[/?#]|$)", url)
    return match.group(1) if match else None


def account_creation_detected(url: str, page_text: str) -> bool:
    """Recognise the post-submit transition without treating a CAPTCHA as success."""
    if url.startswith(LINE_MANAGER_URL) or url.startswith(LINE_CONSOLE_URL):
        return True
    normalized = page_text.casefold()
    return any(marker.casefold() in normalized for marker in ACCOUNT_CREATED_MARKERS)


class LineConsoleBrowser:
    """A persistent LINE session that is visible only at human-only checkpoints."""

    def __init__(self, skip_browser_install: bool, profile_directory: Path | None = None) -> None:
        self.skip_browser_install = skip_browser_install
        self.profile_directory = (profile_directory or default_browser_profile_directory()).expanduser().resolve()
        self.playwright: Any | None = None
        self.context: Any | None = None
        self.headless = False

    def __enter__(self) -> "LineConsoleBrowser":
        if not self.skip_browser_install:
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=False,
            )
            if result.returncode:
                raise SetupError("Playwright Chromium 安裝失敗。可修正網路後重試。")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise SetupError("缺少 Playwright。請重新安裝 ZEAL 套件。") from error

        self.playwright = sync_playwright().start()
        self._launch_context(headless=False)
        print(f"ZEAL 瀏覽器登入資料會保存在：{self.profile_directory}")
        return self

    def _launch_context(self, *, headless: bool) -> None:
        assert self.playwright is not None
        self.profile_directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.profile_directory.chmod(0o700)
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_directory),
            headless=headless,
        )
        self.headless = headless

    def restart(self, *, headless: bool) -> None:
        """Persist the visible session, then reopen the same profile in one mode."""
        if self.context:
            self.context.close()
        self.context = None
        self._launch_context(headless=headless)

    def __exit__(self, *_: object) -> None:
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()

    @property
    def pages(self) -> list[Any]:
        assert self.context is not None
        return list(self.context.pages)

    @staticmethod
    def _page_text(page: Any) -> str:
        with contextlib.suppress(Exception):
            return page.locator("body").inner_text()
        return ""

    def _hand_off_human_verification(self, url: str, reason: str) -> Any:
        """Show a fresh visible browser only for login, MFA, or CAPTCHA."""
        if self.headless:
            self.restart(headless=False)
        assert self.context is not None
        page = self.context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        print(f"\n偵測到「{reason}」。已切回可見瀏覽器，請只完成 LINE 的人類驗證。")
        input("完成驗證且已回到 LINE 頁面後按 Enter；ZEAL 會切回 headless 繼續：")
        self.restart(headless=True)
        assert self.context is not None
        page = self.context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        return page

    def open_authenticated_page(self, url: str, purpose: str) -> Any:
        """Open a page headlessly, with a bounded visible hand-off if LINE asks for proof of humanity."""
        assert self.context is not None
        page = self.context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        reason = human_verification_reason(self._page_text(page))
        if reason:
            page = self._hand_off_human_verification(url, reason)
            if human_verification_reason(self._page_text(page)):
                raise SetupError(f"{purpose} 仍要求人類驗證；請完成驗證後重新執行。")
        return page

    def _show_manual_page(self, url: str) -> None:
        if self.headless:
            self.restart(headless=False)
            assert self.context is not None
            page = self.context.new_page()
            page.goto(url, wait_until="domcontentloaded")

    def _click_first(self, page: Any, labels: tuple[str, ...], *, required: bool = True) -> bool:
        for label in labels:
            candidate = page.get_by_text(label, exact=True)
            with contextlib.suppress(Exception):
                # click() waits for a delayed Manager/Console control.  Calling
                # count() first would return zero while the next screen is still
                # rendering and turn a valid transition into a false fallback.
                candidate.last.click(timeout=8_000)
                return True
        if required:
            self._show_manual_page(page.url)
            raise SetupError("LINE 介面未被安全辨識；已切回可見瀏覽器供手動完成。")
        return False

    def _provider_options(self, page: Any) -> tuple[str, ...]:
        """Extract displayed Provider labels from LINE's current selection controls."""
        options: list[str] = []
        for radio in page.locator('input[type="radio"]').all():
            label = ""
            with contextlib.suppress(Exception):
                label = radio.locator("xpath=following-sibling::label[1]").inner_text().strip()
            if not label:
                with contextlib.suppress(Exception):
                    label = radio.locator("xpath=..").inner_text().strip()
            if label and label not in options:
                options.append(label)
        return tuple(options)

    def fill_official_account_form(self, account: AccountDetails, form_page: Any) -> str:
        """Fill user-provided fields, then select an industry from live LINE options."""
        try:
            form_page.get_by_role("textbox", name="帳號名稱", exact=True).fill(account.name)
            form_page.get_by_role("textbox", name="電子郵件帳號", exact=True).fill(account.email)
            form_page.get_by_role("textbox", name="公司名稱", exact=True).fill(account.company_name)
        except Exception as error:
            raise SetupError("無法辨識 LINE 表單的帳號名稱、Email 或公司名稱欄位。") from error

        selects = form_page.locator("select")
        if selects.count() < 3:
            raise SetupError("LINE 建立表單的業種下拉選單未被辨識；請改在瀏覽器手動選擇。")
        major_select = selects.nth(1)
        minor_select = selects.nth(2)
        try:
            major_options = tuple(
                item.strip()
                for item in major_select.locator("option").all_text_contents()
                if item.strip() and item.strip() != "選擇業種大分類"
            )
        except Exception as error:
            raise SetupError("無法讀取 LINE 表單的業種大分類。") from error
        if not major_options:
            raise SetupError("LINE 表單目前沒有可選的業種大分類。")

        major_category = prompt_option("業種大分類", major_options)
        try:
            major_select.select_option(label=major_category)
            # LINE populates the dependent select asynchronously.  Waiting for
            # its first non-placeholder option avoids treating a valid category
            # as though it had no minor categories.
            minor_select.locator("option").nth(1).wait_for(
                state="attached", timeout=10_000
            )
            minor_options = tuple(
                item.strip()
                for item in minor_select.locator("option").all_text_contents()
                if item.strip() and item.strip() != "選擇業種小分類"
            )
        except Exception as error:
            raise SetupError("無法讀取 LINE 表單的業種小分類。") from error
        if not minor_options:
            raise SetupError("此業種目前沒有可選的小分類；請在瀏覽器手動確認。")

        minor_category = prompt_option("業種小分類", minor_options)
        try:
            minor_select.select_option(label=minor_category)
        except Exception as error:
            raise SetupError("無法將所選業種寫入 LINE 表單。") from error
        print(f"已在 LINE 表單填入帳號資料並選擇業種：{major_category}／{minor_category}")
        return minor_category

    def begin_account_creation(self, account: AccountDetails) -> Any:
        assert self.context is not None
        page = self.context.new_page()
        page.goto(LINE_OFFICIAL_ACCOUNT_ENTRY_URL, wait_until="domcontentloaded")
        print("\n已開啟由 ZEAL 控制的可見瀏覽器，並直接前往「建立LINE官方帳號」表單。")
        print("若畫面要求登入，請自行完成 LINE 登入、OTP/MFA 或 CAPTCHA。")
        print("登入完成後按 Enter；ZEAL 會重新開啟建立表單，並填入：")
        print(f"     名稱：{account.name}")
        print(f"     公司／店鋪名稱：{account.company_name}")
        print("     電子郵件帳號：已由 CLI 安全讀取，將直接填入表單。")
        print("     業種大分類與小分類：ZEAL 會從 LINE 表單即時讀取選項後詢問。")
        print("\n登入、OTP/MFA 與 CAPTCHA 由你本人完成；ZEAL 不會記錄密碼、Cookie 或 OTP。")
        input("完成登入後按 Enter，讓 ZEAL 前往建立表單：")
        while True:
            form_page = self._wait_for_entry_form(seconds=2)
            if form_page is None:
                if page.is_closed():
                    page = self.context.new_page()
                try:
                    page.goto(LINE_OFFICIAL_ACCOUNT_ENTRY_URL, wait_until="domcontentloaded")
                except Exception:
                    # A temporary redirect or network error should not close the
                    # visible login session while the user is still signing in.
                    pass
                form_page = self._wait_for_entry_form(seconds=10)
            if form_page is not None:
                self.fill_official_account_form(account, form_page)
                return form_page
            print("尚未看到 LINE 官方帳號建立表單；瀏覽器會保持開啟，請完成登入或驗證。")
            input("完成後按 Enter 再試一次（Ctrl+C 可取消）：")

    def _wait_for_entry_form(self, *, seconds: int) -> Any | None:
        """Wait for the live entry form rather than trusting a login redirect URL."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for candidate in reversed(self.pages):
                with contextlib.suppress(Exception):
                    location = urllib.parse.urlsplit(candidate.url)
                    if (
                        not candidate.is_closed()
                        and location.hostname == "entry.line.biz"
                        and location.path.startswith("/form/entry/unverified")
                        and candidate.locator("select").count() >= 3
                    ):
                        return candidate
            time.sleep(0.25)
        return None

    def submit_account_creation(self, page: Any) -> None:
        """Validate the form, then let the account holder review LINE's confirmation.

        Only browser controls marked as required are checked.  Optional consent
        (for example, marketing messages) is intentionally left untouched.
        """
        for checkbox in page.locator('input[type="checkbox"][required]').all():
            with contextlib.suppress(Exception):
                if checkbox.is_visible() and checkbox.is_enabled() and not checkbox.is_checked():
                    checkbox.check(timeout=5_000)

        print("\nZEAL 將送出建立表單；若 LINE 顯示登入、OTP/MFA 或 CAPTCHA，請只完成該人類驗證。")
        self._click_first(page, ("Create", "建立", "確定", "Submit"))
        reason = human_verification_reason(self._page_text(page))
        if reason:
            print(f"偵測到「{reason}」。請在目前可見瀏覽器完成驗證，但先不要再按建立。")
            input("完成人類驗證後按 Enter；ZEAL 會送出已填好的表單：")
            self._click_first(page, ("Create", "建立", "確定", "Submit"))

        if any(account_creation_detected(current.url, self._page_text(current)) for current in self.pages):
            return
        print("\n若 LINE 要求人類驗證，請先自行完成；接著在可見瀏覽器核對資料與同意事項。")
        input("確認無誤後按 Enter，ZEAL 會按「完成」送出建立申請：")
        if any(account_creation_detected(current.url, self._page_text(current)) for current in self.pages):
            return
        clicked = False
        for current in reversed(self.pages):
            if self._click_first(current, ("完成", "Finish", "Complete", "Done"), required=False):
                clicked = True
                break
        if not clicked:
            print("ZEAL 未找到確認頁的「完成」按鈕；請在目前可見瀏覽器自行按下。")
        input("若 LINE 要求圖片驗證，請自行完成；若仍在確認頁，請再按「完成」。看到建立成功後按 Enter：")

    def continue_after_account_creation(self) -> None:
        """Wait for LINE's success state, then persist it and switch headless."""
        while True:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                for page in self.pages:
                    if account_creation_detected(page.url, self._page_text(page)):
                        self.restart(headless=True)
                        return
                time.sleep(1)
            print("尚未偵測到官方帳號建立完成；瀏覽器會保持開啟。")
            input("請完成 LINE 確認或人類驗證後按 Enter 再檢查（Ctrl+C 可取消）：")

    def enable_messaging_api(self, account_name: str) -> MessagingApiSetup:
        """Enable Messaging API and return its generated channel ID.

        Provider ownership is irreversible, so the account holder selects from
        LINE's live list rather than needing to remember a command-line value.
        """
        page = self.open_authenticated_page(LINE_MANAGER_URL, "開啟 LINE Official Account Manager")
        self._click_first(page, (account_name,))
        self._click_first(page, ("Messaging API", "Messaging API設定", "Messaging API settings"))
        self._click_first(page, ("Enable Messaging API", "啟用 Messaging API", "Messaging APIを利用する"))

        options = self._provider_options(page)
        if not options:
            self._show_manual_page(page.url)
            raise SetupError("無法讀取 Provider 清單；已切回可見瀏覽器供手動完成。")
        provider, create_provider = prompt_provider_choice(options)

        if create_provider:
            self._click_first(page, ("Create a new provider", "建立新的 Provider", "新しいプロバイダーを作成"))
            field = page.get_by_role("textbox")
            if field.count() == 0:
                self._show_manual_page(page.url)
                raise SetupError("找不到新 Provider 名稱欄位；已切回可見瀏覽器供手動完成。")
            field.last.fill(create_provider)
        else:
            self._click_first(page, (provider or "",))
        self._click_first(page, ("Confirm", "Create", "建立", "確定"))

        reason = human_verification_reason(self._page_text(page))
        if reason:
            page = self._hand_off_human_verification(page.url, reason)
        channel_id = channel_id_from_url(page.url)
        if not channel_id:
            page = self.open_authenticated_page(LINE_CONSOLE_URL, "確認 Messaging API channel")
            channel_id = channel_id_from_url(page.url)
        if not channel_id:
            self._show_manual_page(page.url)
            raise SetupError("Messaging API channel 尚未被安全辨識；已切回可見瀏覽器供手動確認。")
        return MessagingApiSetup(channel_id=channel_id, provider=create_provider or provider or "未辨識")

    def wait_for_credentials(self, channel_id: str) -> Credentials:
        """Read credentials from the Messaging API page without printing them."""
        page = self.open_authenticated_page(messaging_api_url(channel_id), "讀取 Messaging API 憑證")
        credentials = extract_credentials(self.pages)
        if credentials:
            return credentials
        self._click_first(page, ("Issue", "發行", "発行"), required=False)
        credentials = extract_credentials(self.pages)
        if credentials:
            return credentials
        self._show_manual_page(page.url)
        raise SetupError("無法自動讀取或發行兩個憑證；已切回可見瀏覽器供手動處理。")

    def configure_webhook(self, webhook_url: str, channel_id: str | None = None) -> bool:
        """Set, verify, and enable a webhook after the user has signed in locally."""
        assert self.context is not None
        page = self.open_authenticated_page(
            messaging_api_url(channel_id) if channel_id else LINE_CONSOLE_URL,
            "設定 Webhook",
        )
        try:
            page.get_by_role("button", name=re.compile("^Edit$", re.I)).click(timeout=8_000)
            field = page.get_by_role(
                "textbox", name=re.compile("webhook URL", re.I)
            )
            if field.count() == 0:
                field = page.locator('textarea[placeholder*="webhook" i]')
            field.last.fill(webhook_url)
            page.get_by_role("button", name=re.compile("^(Update|Save)$", re.I)).click(timeout=8_000)
            page.get_by_role("button", name=re.compile("^Verify$", re.I)).click(timeout=10_000)
            page.get_by_text(re.compile("^Success$", re.I)).wait_for(timeout=10_000)
            page.get_by_role("button", name=re.compile("^OK$", re.I)).click(timeout=5_000)

            use_webhook = page.get_by_text(re.compile("^Use webhook$", re.I))
            webhook_row = use_webhook.locator("xpath=..")
            webhook_input = webhook_row.locator('input[name="active"]')
            if not webhook_input.is_checked():
                webhook_row.locator("label[for]").click(timeout=5_000)
            return webhook_input.is_checked()
        except Exception:
            return False


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if not process or process.poll() is not None:
        return
    process.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    if process.poll() is None:
        process.kill()


def run_setup(args: Any) -> None:
    """Run `zeal line-bot setup`."""
    account = prompt_account_details(args.port)
    output_root = (args.output or Path.cwd()).resolve()
    destination = project_directory(output_root, account.name)
    try:
        reused = prepare_setup_project(destination, account)
    except SetupError as error:
        print(f"\n設定未完成：{error}", file=sys.stderr)
        return
    print(f"\n{'接續前次未完成的專案' if reused else '已建立專案'}：{destination}")

    ngrok: subprocess.Popen[str] | None = None
    app: subprocess.Popen[str] | None = None
    try:
        binary = install_ngrok()
        configure_ngrok(binary, ngrok_authtoken(args.ngrok_authtoken))
        ngrok, public_url = tunnel_url(binary, account.port)
        callback_url = f"{public_url}/callback"
        print(f"ngrok 公開網址：{public_url}")

        with LineConsoleBrowser(args.skip_browser_install, args.browser_profile) as browser:
            form_page = browser.begin_account_creation(account)
            browser.submit_account_creation(form_page)
            browser.continue_after_account_creation()
            messaging = browser.enable_messaging_api(account.name)
            credentials = browser.wait_for_credentials(messaging.channel_id)
            write_credentials(destination, credentials, account.port)
            print("已安全寫入 .env（未在終端輸出密鑰）。")

            ensure_target_dependencies(destination)
            app = start_app(destination)
            time.sleep(1)
            if app.poll() is not None:
                raise SetupError("Bot 程式未能啟動。請檢查產生專案的 .env 與相依套件。")

            configured = browser.configure_webhook(callback_url)
            if configured:
                print("已送出 Webhook URL、Verify 與 Use webhook 操作。請在瀏覽器確認 Verify 顯示 Success。")
            else:
                print("LINE Console 介面未被安全辨識；請手動貼上並驗證下列 Webhook URL：")
                print(callback_url)

            input("用手機掃 QR Code 加好友，傳送任意文字並確認收到回聲後，按 Enter 結束：")
            print(format_completion_summary(
                account,
                destination,
                callback_url,
                messaging,
                webhook_configured=configured,
                keep_running=args.keep_running,
            ))
    except KeyboardInterrupt:
        print("\n已取消。已建立的專案會保留；若已寫入 .env，請自行決定是否保留。")
    except SetupError as error:
        print(f"\n設定未完成：{error}", file=sys.stderr)
    finally:
        if not args.keep_running:
            stop_process(app)
            stop_process(ngrok)
        elif app and ngrok:
            print("app.py 與 ngrok 仍在本終端背景執行；按 Ctrl+C 結束 ZEAL 不會自動停止它們。")


def run_resume(args: Any) -> None:
    """Create and start a local project for an existing channel, without new LINE setup."""
    name = (args.name or input("既有 LINE Official Account 名稱：")).strip()
    if not name:
        raise SetupError("官方帳號名稱不可空白。")
    if not 1 <= args.port <= 65535:
        raise SetupError("port 必須介於 1 到 65535。")

    account = AccountDetails(name=name, category="Existing Messaging API channel", port=args.port)
    directory = project_directory((args.output or Path.cwd()).resolve(), name)
    write_project(directory, account)
    print(f"已建立專案：{directory}")

    credentials = prompt_existing_credentials()
    write_credentials(directory, credentials, args.port)
    print("已安全寫入 .env（未在終端輸出憑證）。")

    ensure_target_dependencies(directory)
    app = start_app(directory)
    time.sleep(1)
    if app.poll() is not None:
        raise SetupError("Bot 程式未能啟動。請檢查產生專案的 .env 與相依套件。")

    public_url = args.webhook_url or existing_tunnel_url()
    if public_url is None:
        binary = install_ngrok()
        configure_ngrok(binary, ngrok_authtoken(args.ngrok_authtoken))
        _, public_url = tunnel_url(binary, args.port)

    callback_url = f"{public_url}/callback"
    channel_id = (args.channel_id or input("LINE Channel ID（留空則改為手動設定 Webhook）：")).strip()
    configured = False
    if channel_id and not args.no_browser:
        with LineConsoleBrowser(args.skip_browser_install, args.browser_profile) as browser:
            browser.restart(headless=True)
            configured = browser.configure_webhook(callback_url, channel_id)

    if configured:
        print("\nWebhook URL 已儲存、Verify 成功，且 Use webhook 已啟用。")
    else:
        print("\nBot 已啟動。請在 LINE Developers Console 設定：")
        print(f"Webhook URL: {callback_url}")
        print("接著按 Verify，開啟 Use webhook，並傳送測試文字給官方帳號。")
    print("請保持此終端、Bot 與 ngrok 執行，直到測試完成。")
