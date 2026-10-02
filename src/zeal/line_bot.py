"""Local bootstrap flow for a LINE Messaging API echo bot.

LINE sign-in, MFA, CAPTCHA, and other proof-of-humanity remain human-only. All
other safely recognised controls are automated from a local persistent profile;
the session remains visible with explicit page-input handoffs.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from zeal.browser_gate import BrowserGate


LINE_MANAGER_URL = "https://manager.line.biz/"
LINE_CONSOLE_URL = "https://developers.line.biz/console/"
LINE_OFFICIAL_ACCOUNT_ENTRY_URL = "https://entry.line.biz/form/entry/unverified"
LINE_API_BASE = "https://api.line.me/v2/bot"
NGROK_DOWNLOAD_BASE = "https://bin.equinox.io/c/bNyj1mQVY4c"
NGROK_SIGNUP_URL = "https://dashboard.ngrok.com/signup"
NGROK_AUTHTOKEN_URL = "https://dashboard.ngrok.com/get-started/your-authtoken"

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
LINE_CONTINUE_BUTTON = re.compile(
    r"^\s*了解並繼續使用(?:[\s/]*I understand and want to proceed)?\s*$",
    re.IGNORECASE,
)
MANAGER_WELCOME_TITLE = re.compile(r"^Welcome!\s*\([12]/2\)$", re.IGNORECASE)
MANAGER_WELCOME_STEP = re.compile(r"Welcome!\s*\(([12])/2\)", re.IGNORECASE)


def _terminal_supports_color() -> bool:
    """Use color only in an interactive terminal that can display ANSI codes."""
    if (
        not sys.stdout.isatty()
        or "NO_COLOR" in os.environ
        or os.environ.get("CLICOLOR") == "0"
        or os.environ.get("TERM") == "dumb"
    ):
        return False
    if os.name != "nt":
        return True

    # Windows Console needs virtual terminal processing for ANSI sequences.
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.argtypes = [ctypes.c_int]
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    handle = kernel32.GetStdHandle(-11)
    mode = ctypes.c_uint32()
    if not handle or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))


def _accent(text: str, color: str) -> str:
    if not _terminal_supports_color():
        return text
    return f"\x1b[{color}m{text}\x1b[0m"


def _human_notice(message: str) -> None:
    print(f"{_accent('請你操作', '33;1')}｜{message}")


def _terminal_link(url: str) -> str:
    """Make a link clickable in terminals with OSC 8, retaining a plain URL elsewhere."""
    if _terminal_supports_color() and (
        os.environ.get("WT_SESSION")
        or os.environ.get("TERM_PROGRAM") in {"vscode", "iTerm.app", "WezTerm"}
        or os.environ.get("KONSOLE_VERSION")
    ):
        return f"\x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\"
    return url


class SetupError(RuntimeError):
    """A setup step could not safely continue."""


class LineApiHttpError(SetupError):
    """A documented LINE API call returned a non-success HTTP status."""

    def __init__(self, method: str, path: str, status_code: int) -> None:
        super().__init__(f"LINE Messaging API {method} {path} 回傳 HTTP {status_code}。")
        self.status_code = status_code


def messaging_api_url(channel_id: str) -> str:
    """Build the Console URL without accepting arbitrary browser destinations."""
    channel_id = channel_id.strip()
    if not re.fullmatch(r"\d{6,}", channel_id):
        raise SetupError("Channel ID 應為 LINE Developers Console 顯示的數字。")
    return f"{LINE_CONSOLE_URL}channel/{channel_id}/messaging-api"


def line_api_request(
    token: str, method: str, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Call a documented Messaging API endpoint without logging the access token."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{LINE_API_BASE}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        raise LineApiHttpError(method, path, error.code) from error
    except (OSError, TimeoutError) as error:
        raise SetupError(f"無法連線至 LINE Messaging API（{method} {path}）。") from error
    try:
        result = json.loads(body) if body else {}
    except (ValueError, UnicodeError) as error:
        raise SetupError(f"LINE Messaging API {method} {path} 回傳無法辨識的資料。") from error
    if not isinstance(result, dict):
        raise SetupError(f"LINE Messaging API {method} {path} 回傳無法辨識的資料。")
    return result


def set_and_test_webhook(token: str, webhook_url: str) -> None:
    """Use LINE's public endpoint and its test result as the verification oracle."""
    if not webhook_url.startswith("https://") or len(webhook_url) > 500:
        raise SetupError("Webhook URL 必須是長度不超過 500 字元的 HTTPS 網址。")
    line_api_request(token, "PUT", "/channel/webhook/endpoint", {"endpoint": webhook_url})
    for attempt in range(5):
        result = line_api_request(token, "POST", "/channel/webhook/test", {"endpoint": webhook_url})
        if result.get("success") is True:
            return
        status = result.get("statusCode")
        if status not in (502, 503, 504) or attempt == 4:
            raise SetupError(f"LINE Webhook 驗證未成功（回應狀態：{status if isinstance(status, int) else '未知'}）。")
        print(f"LINE 暫時無法連上 Webhook（{status}）；{attempt + 1} 秒後自動重試。")
        time.sleep(attempt + 1)


def webhook_endpoint_state(token: str) -> dict[str, Any]:
    """A newly set URL may briefly appear absent while LINE's cache updates."""
    try:
        return line_api_request(token, "GET", "/channel/webhook/endpoint")
    except LineApiHttpError as error:
        if error.status_code == 404:
            return {}
        raise


def add_friend_url(token: str) -> str:
    """Get the channel's actual LINE ID before building its add-friend link."""
    basic_id = line_api_request(token, "GET", "/info").get("basicId")
    if not isinstance(basic_id, str) or not re.fullmatch(r"@[A-Za-z0-9._-]{1,40}", basic_id):
        raise SetupError("LINE 未回傳可辨識的官方帳號 Basic ID，無法產生加好友 QR Code。")
    return f"https://line.me/R/ti/p/{urllib.parse.quote(basic_id, safe='')}"


def prompt_add_friend_url() -> str:
    print("請從 LINE Official Account Manager 複製此帳號的 Basic ID（例：@coffee123）。")
    basic_id = input("LINE Basic ID：").strip()
    if not re.fullmatch(r"@[A-Za-z0-9._-]{1,40}", basic_id):
        raise SetupError("Basic ID 格式不正確；請輸入以 @ 開頭的完整值。")
    return f"https://line.me/R/ti/p/{urllib.parse.quote(basic_id, safe='')}"


def prompt_messaging_channel() -> MessagingApiSetup:
    print("請先在目前的 LINE 官方帳號中完成 Messaging API 設定，再複製該 Channel 的 ID。")
    channel_id = input("Messaging API Channel ID（例：2001234567）：").strip()
    if not re.fullmatch(r"[0-9]{6,20}", channel_id):
        raise SetupError("Channel ID 格式不正確；請從該帳號的 Messaging API 設定頁複製。")
    return MessagingApiSetup(channel_id, "手動確認（名稱未讀取）", already_enabled=True)


def write_add_friend_qr(directory: Path, url: str) -> Path:
    """Save a scannable SVG under ZEAL's private runtime directory."""
    import qrcode
    from qrcode.image.svg import SvgPathImage

    image = qrcode.make(url, image_factory=SvgPathImage, box_size=30)
    path = runtime_log_directory(directory) / "add-friend.svg"
    image.save(path)
    return path


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


def _display_width(value: str) -> int:
    """Measure CJK labels in terminal cells for aligned menu columns."""
    return sum(
        0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        for char in value
    )


def _option_rows(options: tuple[str, ...] | list[str], terminal_width: int) -> list[str]:
    number_width = max(2, len(str(len(options))))
    entries = [f"  {index:>{number_width}}. {option}" for index, option in enumerate(options, start=1)]
    if len(entries) < 2:
        return entries

    left, right = entries[::2], entries[1::2]
    left_width = max(map(_display_width, left))
    right_width = max(map(_display_width, right))
    if left_width + 3 + right_width > terminal_width:
        return entries
    return [
        left_entry + " " * (left_width - _display_width(left_entry) + 3) + right_entry
        for left_entry, right_entry in zip(left, right)
    ] + left[len(right):]


def prompt_option(
    label: str, options: tuple[str, ...] | list[str], *, two_columns: bool = False,
    input_label: str | None = None,
) -> str:
    print(f"\n{label}：")
    rows = (
        _option_rows(options, shutil.get_terminal_size(fallback=(100, 24)).columns)
        if two_columns
        else [f"  {index:>2}. {option}" for index, option in enumerate(options, start=1)]
    )
    for row in rows:
        print(row)
    while True:
        try:
            return option_from_choice(input(input_label or f"{label}（輸入編號或完整名稱）："), options)
        except SetupError as error:
            print(error)


def prompt_provider_choice(options: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Let the account holder choose an irreversible Provider at the last responsible moment."""
    print("\nProvider 代表管理這項服務與 Messaging API Channel 的經營者，不是管理員帳號。")
    print("例：由「小明咖啡有限公司」管理「小明咖啡客服」與「小明咖啡活動」兩個官方帳號。")
    print("請選實際經營者的 Provider；連結後無法改掛到其他 Provider。")
    choice = prompt_option("LINE Provider", (*options, NEW_PROVIDER_OPTION))
    if choice != NEW_PROVIDER_OPTION:
        return choice, None
    while True:
        print("新 Provider 可用經營者名稱命名。例：小明咖啡有限公司、星球讀書會。")
        name = input("新 Provider 名稱：").strip()
        if name:
            return None, name
        print("新 Provider 名稱不可空白。")


def prompt_account_route() -> str:
    """Choose whether setup creates an Official Account or uses an existing one."""
    print("\n先決定要建立新帳號，或接續你已能管理的官方帳號；ZEAL 會依選擇安排後續步驟。")
    choice = prompt_option("LINE 官方帳號設定方式", ("建立新的官方帳號", "接續既有官方帳號"))
    return "create" if choice == "建立新的官方帳號" else "existing"


def account_confirmation_prompt(name: str) -> str:
    """Confirm the selected account before changing its LINE settings."""
    continue_label = _accent("請按 Enter 確認繼續", "32;1")
    cancel_label = _accent("按 Ctrl+C 離開", "31;1")
    return f"收到！會使用「{name}」進行接下來的設定。{continue_label}（{cancel_label}）："


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
    already_enabled: bool = False


@dataclass(frozen=True)
class OfficialAccountChoice:
    name: str
    manager_url: str


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
    print("官方帳號名稱會顯示在顧客的 LINE 聊天室。例：小明咖啡客服、星球讀書會。")
    while True:
        name = input("官方帳號名稱：").strip()
        if name and len(name) <= 20:
            break
        print("官方帳號名稱須為 1 至 20 個字元，請重新輸入。")

    print("公司／店鋪名稱會填入 LINE 申請表；它不會自動建立或選定 Provider。")
    print("例：小明咖啡有限公司、小明咖啡；個人可填經營名稱，如星球讀書會。")
    while True:
        company_name = input("公司／店鋪名稱：").strip()
        if company_name and len(company_name) <= 100:
            break
        print("公司／店鋪名稱須為 1 至 100 個字元，請重新輸入。")

    print("此信箱會填入 LINE 官方帳號申請表，請使用可收信的地址。例：hello@example.com。")
    while True:
        email = input("電子郵件帳號：").strip()
        if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) and len(email) <= 240:
            break
        print("請輸入有效的電子郵件帳號（最長 240 個字元）。")

    port = prompt_valid_port(port)
    return AccountDetails(
        name=name,
        category="",
        port=port,
        company_name=company_name,
        email=email,
    )


def prompt_valid_port(port: int) -> int:
    """Correct an invalid CLI port in the same setup session."""
    while not 1 <= port <= 65535:
        print("port 必須介於 1 到 65535。")
        try:
            port = int(input("Bot 本機連接埠（例：8000）：").strip())
        except ValueError:
            port = 0
    return port


def retry_setup_step(
    label: str, operation: Any, browser: Any = None, *, fallback: Any = None
) -> Any:
    """Retry a failed step without discarding the current browser or earlier work."""
    while True:
        try:
            return operation()
        except Exception as error:
            recoverable = isinstance(error, (SetupError, OSError, TimeoutError))
            browser_error = type(error).__module__.startswith("playwright.")
            if not recoverable and not browser_error:
                raise
            detail = str(error) if recoverable else type(error).__name__
            print(f"\n{label}未完成：{detail}")
            options = ["重新嘗試此步驟"]
            pages = getattr(browser, "pages", []) if browser is not None else []
            if pages:
                options.append("在目前瀏覽器手動處理後重新辨識")
            if fallback is not None:
                options.append("從終端機手動輸入此步驟的資料")
            options.append("結束設定")
            try:
                choice = prompt_option(f"{label}接續方式", options)
            except (EOFError, OSError):
                raise SetupError(f"{label}未完成：{detail}") from error
            if choice == options[-1]:
                raise SetupError(f"已停止於{label}；先前完成的資料仍保留。") from error
            if fallback is not None and choice == "從終端機手動輸入此步驟的資料":
                try:
                    return fallback()
                except SetupError as fallback_error:
                    print(fallback_error)
                    continue
            if choice == "在目前瀏覽器手動處理後重新辨識":
                browser._give_user_control(pages[-1], "請在此頁處理目前的設定問題")
                try:
                    input("完成後按 Enter，ZEAL 會重新辨識此步驟：")
                finally:
                    browser._automate()


def require_webhook_enabled(value: bool) -> bool:
    if not value:
        raise SetupError("LINE Webhook 尚未確認啟用；請檢查目前頁面後重試。")
    return True


def confirm_setup_start() -> None:
    """Explain the guided setup before collecting data or changing anything."""
    print(f"\n{_accent('◆ ZEAL 將協助您建立或接續 LINE 官方帳號設定步驟，並自動建置一個能回覆訊息的簡易 LINE Bot。', '36;1')}")
    print("ZEAL 會開啟瀏覽器協助設定；若電腦缺少所需的瀏覽器，會協助下載。新建帳號時會依您提供的資料填寫申請表。登入、驗證碼及其他人類驗證仍需由您親自完成。")
    print("由於 LINE 需要一個公開的 HTTPS 網址，才能把訊息送到您電腦上的 Bot。請準備好可以轉送至這台電腦的網址；若沒有，ZEAL 將引導您使用 ngrok 的免費方案建立測試用網址。")
    print("選擇 ngrok 時，ZEAL 會檢查並在需要時下載程式，再引導您註冊、取得 Authtoken。免費方案有使用限制，網址可能在重新啟動後改變。")
    print()
    if input("若同意開始，請按 Enter；按 Ctrl+C 取消：").strip():
        raise SetupError("尚未開始設定；同意時請直接按 Enter。")


def validate_public_url(value: str) -> str:
    """Accept a public HTTPS base URL, or a complete /callback URL."""
    value = value.strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise SetupError("請輸入有效的公開 HTTPS 網址。") from error
    if (parsed.scheme != "https" or not host or not host.strip(".")
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or any(char.isspace() for char in value)):
        raise SetupError("請輸入公開 HTTPS 網址，不要包含帳密、查詢參數或 # 片段。")
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise SetupError("localhost 只有您的電腦能連線；請輸入公開 HTTPS 網址。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.lower().endswith((".local", ".internal", ".test", ".invalid")):
            raise SetupError("請輸入可從網際網路連線的公開 HTTPS 網址。")
    else:
        if not address.is_global:
            raise SetupError("請輸入可從網際網路連線的公開 HTTPS 網址。")
    if port is not None and not 1 <= port <= 65535:
        raise SetupError("HTTPS 網址的連接埠無效。")
    base = value[:-len("/callback")] if parsed.path.endswith("/callback") else value
    if len(f"{base}/callback") > 500:
        raise SetupError("Webhook 網址不可超過 500 字元。")
    return base


def prompt_public_url() -> str | None:
    """Choose the connection before ngrok can be downloaded or configured."""
    print("\nLINE 要透過哪個網址連到這台電腦的 Bot？")
    print("  1. 使用 ngrok 建立測試用公開網址（可使用免費方案）")
    print("  2. 使用已有的公開 HTTPS 網址（須已轉送到這台電腦的 Bot 連接埠）")
    while True:
        choice = input("請輸入 1 或 2（按 Enter 選 1）：").strip()
        if choice in ("", "1"):
            return None
        if choice == "2":
            return prompt_valid_public_url()
        print("請輸入 1 或 2。")


def prompt_valid_public_url() -> str:
    while True:
        value = input("公開 HTTPS 網址（例：https://bot.example.com；可含 /callback）：")
        try:
            return validate_public_url(value)
        except SetupError as error:
            print(error)


def setup_step(number: int, description: str, explanation: str | None = None) -> None:
    print(f"\n{_accent(f'[步驟 {number}/8] {description}', '36;1')}")
    if explanation:
        print(explanation)


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
from datetime import datetime

import requests
from dotenv import load_dotenv
from flask import Flask, abort, request

load_dotenv()

app = Flask(__name__)
CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]


def log_event(message: str) -> None:
    print(f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S%z}] {message}", flush=True)


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
        log_event("拒絕 Webhook：簽章驗證失敗")
        abort(400)

    events = (request.get_json(silent=True) or {}).get("events", [])
    if not events:
        log_event("收到 LINE Webhook 驗證請求，沒有訊息事件")
    for event in events:
        if event.get("type") != "message":
            log_event("略過非訊息事件")
            continue
        if event.get("message", {}).get("type") != "text":
            log_event("略過非文字訊息")
            continue
        log_event("收到文字訊息，正在回覆")
        try:
            reply(event["replyToken"], f"你說的是：{event['message']['text']}")
        except requests.RequestException:
            log_event("回覆 LINE API 失敗，請查看下方錯誤")
            raise
        log_event("已送出文字回覆")

    return "OK", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    log_event(f"Bot 已啟動，等待 LINE Webhook（port {port}）")
    app.run(host="0.0.0.0", port=port)
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
    if directory.exists() or directory.is_symlink():
        raise SetupError(f"目標資料夾已存在：{directory}")
    directory.mkdir(parents=True)
    for name, contents in project_files(account).items():
        (directory / name).write_text(contents, encoding="utf-8")


def prepare_setup_project(
    directory: Path, account: AccountDetails, *, allow_credentials: bool = False
) -> bool:
    """Reuse an unchanged ZEAL project; configured projects need channel validation."""
    if not directory.exists() and not directory.is_symlink():
        write_project(directory, account)
        return False
    env_file = directory / ".env"
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or env_file.is_symlink()
        or (env_file.exists() and (not allow_credentials or not env_file.is_file()))
    ):
        raise SetupError(f"目標資料夾已存在：{directory}")
    try:
        unchanged = all(
            not (directory / name).is_symlink()
            and (directory / name).read_text(encoding="utf-8") == contents
            for name, contents in project_files(account).items()
        )
    except (OSError, UnicodeError):
        unchanged = False
    if not unchanged:
        raise SetupError(f"目標資料夾已存在且內容不同：{directory}")
    return True


def available_sibling(path: Path, suffix: str) -> Path:
    """Suggest a readable, unused sibling without changing the original path."""
    number = 2 if suffix == "-2" else 1
    candidate = path.with_name(f"{path.name}{suffix}")
    while candidate.exists() or candidate.is_symlink():
        number += 1
        candidate = path.with_name(
            f"{path.name}-{number}" if suffix == "-2" else f"{path.name}{suffix}-{number}"
        )
    return candidate


def prompt_alternate_project_directory(original: Path) -> Path:
    """Let the user name another sibling project without leaving setup."""
    while True:
        suffix = input("新專案名稱尾碼（例：活動版，將加在原資料夾名稱後）：").strip()
        if not suffix or not re.fullmatch(r"[\w -]+", suffix, flags=re.UNICODE):
            print("請輸入名稱尾碼；可使用文字、數字、空格、底線或連字號。")
            continue
        candidate = original.with_name(f"{original.name}-{suffix}")
        print(f"將使用新專案：{candidate}")
        return candidate


def choose_project_destination(directory: Path, account: AccountDetails) -> tuple[Path, bool]:
    """Continue a matching project or resolve a local name collision in place."""
    while True:
        try:
            return directory, prepare_setup_project(directory, account, allow_credentials=True)
        except SetupError as error:
            if not (directory.exists() or directory.is_symlink()):
                raise
            suggested = available_sibling(directory, "-2")
            print(f"\n{error}")
            options = (
                f"覆寫現有專案：{directory}",
                f"建立新專案：{suggested}",
                "自行命名新專案",
            )
            choice = prompt_option("本機專案已有資料，請選擇處理方式", options)
            if choice == options[2]:
                directory = prompt_alternate_project_directory(project_directory(directory.parent, account.name))
                continue
            if choice == options[1]:
                directory = suggested
                continue
            backup = available_sibling(directory, ".backup")
            try:
                directory.rename(backup)
            except OSError as rename_error:
                print(f"無法備份現有專案：{directory}（{rename_error}）；請改選新專案位置。")
                continue
            print(f"原專案已備份：{backup}")
            try:
                write_project(directory, account)
            except (OSError, SetupError) as write_error:
                print(f"原專案已備份至 {backup}，但新專案建立失敗：{write_error}；請改選專案位置。")
                continue
            return directory, False


def credential_env_contents(credentials: Credentials, port: int) -> str:
    # JSON quoting is valid for python-dotenv and keeps spaces/special characters safe.
    return (
        f"LINE_CHANNEL_SECRET={json.dumps(credentials.channel_secret)}\n"
        f"LINE_CHANNEL_ACCESS_TOKEN={json.dumps(credentials.channel_access_token)}\n"
        f"PORT={port}\n"
    )


def verify_existing_credentials(directory: Path, credentials: Credentials, port: int) -> None:
    """Reuse a local .env only when it belongs to the selected LINE channel."""
    env_file = directory / ".env"
    if env_file.is_symlink() or not env_file.is_file():
        raise SetupError(f"本機憑證檔案已變動：{env_file}")
    try:
        matches = env_file.read_text(encoding="utf-8") == credential_env_contents(credentials, port)
    except (OSError, UnicodeError):
        matches = False
    if not matches:
        raise SetupError("本機 .env 與所選 LINE Channel 的憑證或本機埠不一致。")


def write_credentials(directory: Path, credentials: Credentials, port: int) -> None:
    contents = credential_env_contents(credentials, port)
    env_file = directory / ".env"
    created = False
    try:
        with env_file.open("x", encoding="utf-8") as output:
            created = True
            output.write(contents)
    except FileExistsError as error:
        raise SetupError(f"本機 .env 已存在：{env_file}") from error
    except OSError:
        if created:
            with contextlib.suppress(OSError):
                env_file.unlink()
        raise
    with contextlib.suppress(OSError):
        env_file.chmod(0o600)


def choose_credentials_destination(
    directory: Path, account: AccountDetails, credentials: Credentials
) -> Path:
    """Resolve a changed .env without asking the user to restart setup."""
    while True:
        env_file = directory / ".env"
        if not (env_file.exists() or env_file.is_symlink()):
            write_credentials(directory, credentials, account.port)
            return directory
        try:
            verify_existing_credentials(directory, credentials, account.port)
        except SetupError as error:
            suggested = available_sibling(project_directory(directory.parent, account.name), "-2")
            print(f"\n{error}")
            options = (
                f"覆寫現有專案與憑證：{directory}",
                f"建立新專案：{suggested}",
                "自行命名新專案",
            )
            choice = prompt_option("本機憑證與所選 Channel 不同，請選擇處理方式", options)
            if choice != options[0]:
                candidate = (
                    suggested if choice == options[1]
                    else prompt_alternate_project_directory(project_directory(directory.parent, account.name))
                )
                directory, _ = choose_project_destination(candidate, account)
                print(f"已選擇新專案：{directory}")
                continue
            backup = available_sibling(directory, ".backup")
            try:
                directory.rename(backup)
            except OSError as rename_error:
                print(f"無法備份現有專案：{directory}（{rename_error}）；請改選新專案位置。")
                continue
            print(f"原專案與憑證已備份：{backup}")
            try:
                write_project(directory, account)
                write_credentials(directory, credentials, account.port)
            except (OSError, SetupError) as write_error:
                print(f"原專案已備份至 {backup}，但新專案建立失敗：{write_error}；請改選專案位置。")
                directory, _ = choose_project_destination(directory, account)
                continue
            print("已寫入所選 Channel 的憑證（未在終端輸出密鑰）。")
            return directory
        print("已確認本機 .env 與所選 LINE Channel 相符；沿用原檔案。")
        return directory


def format_completion_summary(
    account: AccountDetails,
    directory: Path,
    callback_url: str,
    messaging: MessagingApiSetup,
    *,
    webhook_configured: bool,
    keep_running: bool,
    using_ngrok: bool = True,
) -> str:
    """Describe the user's resulting LINE setup without exposing credentials."""
    channel_url = messaging_api_url(messaging.channel_id)
    webhook_state = "已儲存、Verify 成功並啟用 Use webhook" if webhook_configured else "尚待在 Console 手動確認"
    company_name = account.company_name or "未提供"
    services = "Bot 與 ngrok" if using_ngrok else "Bot"
    runtime_state = (
        f"{services}：指令結束後持續在背景執行。"
        if keep_running else f"本次啟動的 {services}：此摘要顯示後將停止。"
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


def format_runtime_instructions(
    bot_pid: int | None,
    ngrok_pid: int | None,
    log_directory: Path,
    *,
    port: int,
    ngrok_binary: str | None = None,
    using_ngrok: bool = True,
) -> str:
    """Show how to inspect, stop, and restart the services left by setup."""
    pids = [pid for pid in (bot_pid, ngrok_pid) if pid is not None]
    bot_log = log_directory / "bot.log"
    ngrok_log = log_directory / "ngrok.log"
    if not using_ngrok:
        lines = [
            "\n背景程序已啟動：",
            f"Bot PID：{bot_pid}；日誌：{bot_log}" if bot_pid is not None
            else f"Bot：沿用本機已執行的程序；日誌：{bot_log}",
            "請保持自備公開網址轉送到本機 Bot；若網址變更，須更新 LINE Webhook URL 並重新驗證。",
        ]
        if os.name == "nt":
            if bot_pid is not None:
                lines.append(f"PowerShell 停止本次啟動的 Bot：Stop-Process -Id {bot_pid}")
            quoted_bot_log = str(bot_log).replace("'", "''")
            lines.append(f"PowerShell 查看 Bot 日誌：Get-Content -Tail 30 -Wait -LiteralPath '{quoted_bot_log}'")
        else:
            if bot_pid is not None:
                lines.append(f"停止本次啟動的 Bot：kill {bot_pid}")
            lines.append(f"查看 Bot 日誌：tail -f {shlex.quote(str(bot_log))}")
        lines.append("若停止了 Bot，請依產生專案的 README.md 重新啟動。")
        return "\n".join(lines)
    lines = [
        "\n背景程序已啟動：",
        (
            f"Bot PID：{bot_pid}；日誌：{bot_log}"
            if bot_pid is not None
            else f"Bot：沿用本機已執行的程序；日誌：{bot_log}"
        ),
        (
            f"ngrok PID：{ngrok_pid}；日誌：{ngrok_log}"
            if ngrok_pid is not None
            else "ngrok：使用已執行的連線；請從原先啟動 ngrok 的位置查看日誌與 PID。"
        ),
    ]
    lines.append("ngrok 本機狀態頁：http://127.0.0.1:4040（可在瀏覽器查看目前通道）。")
    if os.name == "nt":
        joined = ",".join(str(pid) for pid in pids)
        quoted_bot_log = str(bot_log).replace("'", "''")
        if pids:
            lines.append(f"PowerShell 查看本次啟動的程序：Get-Process -Id {joined}")
            lines.append(f"PowerShell 停止本次啟動的程序：Stop-Process -Id {joined}")
        lines.append("PowerShell 檢查 4040 是否仍在監聽：(Test-NetConnection 127.0.0.1 -Port 4040).TcpTestSucceeded")
        lines.append(f"PowerShell 查看 Bot 日誌：Get-Content -Tail 30 -Wait -LiteralPath '{quoted_bot_log}'")
        if bot_pid is None:
            lines.append("PowerShell 查找既有 Bot：Get-NetTCPConnection -LocalPort <Bot 埠> -State Listen")
        if ngrok_pid is not None:
            lines.append(f"PowerShell 只停止 ngrok：Stop-Process -Id {ngrok_pid}")
            quoted_ngrok_log = str(ngrok_log).replace("'", "''")
            lines.append(
                f"PowerShell 查看 ngrok 日誌：Get-Content -Tail 30 -Wait -LiteralPath '{quoted_ngrok_log}'"
            )
        else:
            lines.append("PowerShell 查找既有 ngrok：Get-Process -Name ngrok")
            lines.append("PowerShell 確認 PID 後停止既有 ngrok：Stop-Process -Id <ngrok PID>")
        if ngrok_binary is not None:
            quoted_binary = ngrok_binary.replace("'", "''")
            lines.append(f"PowerShell 重新啟動 ngrok（另開終端）：& '{quoted_binary}' http {port}")
        else:
            lines.append(f"PowerShell 重新啟動既有 ngrok（另開終端）：ngrok http {port}（若不在 PATH，請改用原先執行檔的完整路徑）")
    else:
        joined = ",".join(str(pid) for pid in pids)
        if pids:
            lines.append(f"查看本次啟動的程序：ps -p {joined} -o pid,command")
            lines.append(f"停止本次啟動的程序：kill {' '.join(str(pid) for pid in pids)}")
        lines.append("檢查 4040 是否仍有通道：curl -fsS http://127.0.0.1:4040/api/tunnels")
        lines.append(f"查看 Bot 日誌：tail -f {shlex.quote(str(bot_log))}")
        if bot_pid is None:
            lines.append("查找既有 Bot：lsof -i TCP:<Bot 埠> -sTCP:LISTEN")
        if ngrok_pid is not None:
            lines.append(f"只停止 ngrok：kill {ngrok_pid}")
            lines.append(f"查看 ngrok 日誌：tail -f {shlex.quote(str(ngrok_log))}")
        else:
            lines.append("查找既有 ngrok：pgrep -fl ngrok")
            lines.append("確認 PID 後停止既有 ngrok：kill <ngrok PID>")
        binary = shlex.quote(ngrok_binary) if ngrok_binary is not None else "ngrok"
        lines.append(f"重新啟動 ngrok（另開終端）：{binary} http {port}")
    lines.append("重新啟動後請查看 4040 的 HTTPS 網址；若網址改變，請在 LINE Developers Console 更新 Webhook URL（加上 /callback），再按 Verify 並確認 Use webhook 已開啟。")
    lines.append("若也停止了 Bot，請先依產生專案的 README.md 啟動 Bot。")
    return "\n".join(lines)


def prompt_existing_credentials() -> Credentials:
    """Read credentials from the controlling terminal without echoing them."""
    print("請從 LINE Developers Console 的 Channel 複製憑證：Basic settings 中的 Channel secret，")
    print("以及 Messaging API 中的 Channel access token；ZEAL 會將它們存入本機 .env。")
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


def runtime_log_directory(project: Path) -> Path:
    """Store process output outside the generated project and Git worktree."""
    project_key = hashlib.sha256(os.fsencode(str(project.resolve()))).hexdigest()[:12]
    directory = zeal_data_directory() / "runtime" / project_key
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        directory.chmod(0o700)
    return directory


def start_background_process(command: list[str], log_path: Path, *, cwd: Path | None = None) -> subprocess.Popen[str]:
    """Keep a service alive after the ZEAL command exits, with readable logs."""
    options: dict[str, Any] = {"start_new_session": os.name != "nt"}
    if os.name == "nt":
        options["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    with log_path.open("ab") as output:
        with contextlib.suppress(OSError):
            log_path.chmod(0o600)
        return subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **options,
        )


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
        unpacked = Path(temp_dir) / binary.name
        try:
            urllib.request.urlretrieve(asset.url, archive)
        except OSError as error:
            raise SetupError(f"無法下載 ngrok：{error}") from error

        if asset.extension == "zip":
            with zipfile.ZipFile(archive) as zipped:
                member = next((item for item in zipped.namelist() if item.endswith("ngrok.exe") or item.endswith("/ngrok") or item == "ngrok"), None)
                if member is None:
                    raise SetupError("ngrok 壓縮檔中找不到執行檔。")
                with zipped.open(member) as source, unpacked.open("wb") as target:
                    shutil.copyfileobj(source, target)
        else:
            with tarfile.open(archive, "r:gz") as tarred:
                member = next((item for item in tarred.getmembers() if item.isfile() and Path(item.name).name == "ngrok"), None)
                if member is None:
                    raise SetupError("ngrok 壓縮檔中找不到執行檔。")
                source = tarred.extractfile(member)
                if source is None:
                    raise SetupError("無法解開 ngrok 執行檔。")
                with source, unpacked.open("wb") as target:
                    shutil.copyfileobj(source, target)
        unpacked.replace(binary)
    with contextlib.suppress(OSError):
        binary.chmod(0o700)
    return binary


def prepare_ngrok(argument_token: str | None, port: int) -> Path:
    """Check the local ngrok binary and configuration before any LINE setup."""
    print("\n[啟動前檢查] 確認 ngrok 可用；LINE 稍後需要它連到本機 Bot。")
    try:
        binary = install_ngrok()
        version = subprocess.run(
            [str(binary), "version"], capture_output=True, text=True, check=False, timeout=10
        )
    except SetupError as error:
        raise SetupError(f"{error} 請從 https://ngrok.com/download 手動安裝後重新執行 setup。") from error
    except (OSError, subprocess.TimeoutExpired, zipfile.BadZipFile, tarfile.TarError) as error:
        raise SetupError(
            f"ngrok 無法安裝或執行：{error}。請檢查網路，或從 https://ngrok.com/download 安裝後重新執行 setup。"
        ) from error
    if version.returncode:
        raise SetupError(
            f"ngrok 無法執行：{binary}。請從 https://ngrok.com/download 重新安裝後再執行 setup。"
        )
    print(f"ngrok 執行檔已確認：{binary}")
    if existing_tunnel_url(port):
        print(f"本機 {port} 埠已有 ngrok HTTPS 通道；稍後會沿用。")
    else:
        ensure_ngrok_config(binary, argument_token)
        print("ngrok 設定已確認；稍後會建立 HTTPS 通道。")
    print("現在開始 LINE 官方帳號設定。")
    return binary


def ngrok_authtoken(argument_value: str | None) -> str:
    if not argument_value and not os.environ.get("NGROK_AUTHTOKEN"):
        print("ngrok Authtoken 用來建立公開 HTTPS 連線，讓 LINE 能把訊息送到本機 Bot。")
        print(f"1. 尚無 ngrok 帳號：開啟 {_terminal_link(NGROK_SIGNUP_URL)}，註冊並登入；已有帳號則直接登入。")
        print(f"2. 登入後開啟 {_terminal_link(NGROK_AUTHTOKEN_URL)}，複製頁面上的 Authtoken。")
        print("3. 回到此終端機貼上 Authtoken 並按 Enter；輸入不會顯示，ZEAL 會替你儲存到 ngrok 設定。")
    return argument_value or os.environ.get("NGROK_AUTHTOKEN") or getpass.getpass(
        "貼上 ngrok Authtoken（輸入不會顯示）："
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


def _ngrok_config_has_authtoken(check_output: str) -> bool:
    """A successful `config check` validates syntax, even without an authtoken."""
    match = re.search(r"(?m)^Valid configuration file at (.+)$", check_output)
    if match is None:
        return False
    try:
        config = Path(match.group(1).strip()).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    for line in config.splitlines():
        token = re.match(r"^authtoken\s*:\s*(.*?)\s*$", line)
        if token is None:
            continue
        value = token.group(1).split(" #", 1)[0].strip().strip("\"'")
        if value and value not in ("null", "~"):
            return True
    return False


def ensure_ngrok_config(binary: Path, argument_token: str | None) -> None:
    """Reuse a working ngrok login, unless the caller supplied a new token."""
    token = argument_token or os.environ.get("NGROK_AUTHTOKEN")
    if token:
        configure_ngrok(binary, token)
        return
    result = subprocess.run(
        [str(binary), "config", "check"], capture_output=True, text=True, check=False
    )
    if result.returncode or not _ngrok_config_has_authtoken(result.stdout or ""):
        configure_ngrok(binary, ngrok_authtoken(None))


def tunnel_url(binary: Path, port: int, project: Path) -> tuple[subprocess.Popen[str], str]:
    log_path = runtime_log_directory(project) / "ngrok.log"
    process = start_background_process(
        [str(binary), "http", str(port), "--log=stdout"], log_path
    )
    inspector = "http://127.0.0.1:4040/api/tunnels"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SetupError(f"ngrok 啟動後立即結束；請查看日誌：{log_path}")
        try:
            with urllib.request.urlopen(inspector, timeout=2) as response:
                tunnels = json.load(response).get("tunnels", [])
        except (OSError, ValueError, json.JSONDecodeError):
            time.sleep(0.5)
            continue
        for tunnel in tunnels:
            public_url = tunnel.get("public_url", "")
            if public_url.startswith("https://") and process.poll() is None:
                return process, public_url
        time.sleep(0.5)
    process.terminate()
    raise SetupError(f"30 秒內無法從 ngrok 取得 HTTPS 公開網址；請查看日誌：{log_path}")


def existing_tunnel_url(port: int | None = None) -> str | None:
    """Return an HTTPS tunnel already served by the local ngrok inspector."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as response:
            tunnels = json.load(response).get("tunnels", [])
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    for tunnel in tunnels:
        public_url = tunnel.get("public_url", "")
        address = str((tunnel.get("config") or {}).get("addr", ""))
        with contextlib.suppress(ValueError):
            location = urllib.parse.urlsplit(address if "://" in address else f"//{address}")
            if public_url.startswith("https://") and (port is None or location.port == port):
                return public_url
    return None


def local_port_listening(port: int) -> bool:
    """Check whether a Bot may already be serving the selected local port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def target_python(directory: Path) -> Path:
    return directory / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def start_app(directory: Path) -> subprocess.Popen[str]:
    return start_background_process(
        [str(target_python(directory)), "app.py"],
        runtime_log_directory(directory) / "bot.log",
        cwd=directory,
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

    print("正在確認 Bot 專案的 Python 套件…")
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", "requirements.txt"],
        cwd=directory,
        check=False,
    )
    if result.returncode:
        raise SetupError("無法安裝 Bot 專案套件。請確認目前 Python 環境有 pip 與網路連線。")
    print("Bot 專案套件已備妥。")


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


def channel_id_from_settings_text(page_text: str) -> str | None:
    """Read the labeled Channel ID shown in Manager's Messaging API settings."""
    match = re.search(
        r"(?:Channel\s*ID|頻道\s*ID|チャネル\s*ID)\s*[:：]?\s*(\d{6,})(?!\d)",
        page_text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def account_creation_detected(url: str, page_text: str) -> bool:
    """Recognise the post-submit transition without treating a CAPTCHA as success."""
    if url.startswith(LINE_MANAGER_URL) or url.startswith(LINE_CONSOLE_URL):
        return True
    normalized = page_text.casefold()
    return any(marker.casefold() in normalized for marker in ACCOUNT_CREATED_MARKERS)


def _install_chromium_if_missing(browser_type: Any) -> None:
    """Install Playwright's current headed Chromium when its executable is absent."""
    if Path(browser_type.executable_path).is_file():
        return

    command = [sys.executable, "-m", "playwright", "install"]
    if platform.system() == "Linux":
        command.append("--with-deps")
        print("ZEAL 也會安裝 Linux 所需的系統套件；可能需要管理員權限。", flush=True)
    command.append("chromium")
    print("ZEAL 正在下載 Playwright Chromium，請稍候…", flush=True)
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise SetupError("Playwright Chromium 安裝失敗；請檢查網路與系統安裝權限後重試。")


class LineConsoleBrowser:
    """A persistent visible LINE session shared by automation and human checks."""

    def __init__(self, skip_browser_install: bool, profile_directory: Path | None = None) -> None:
        self.skip_browser_install = skip_browser_install
        self.profile_directory = (profile_directory or default_browser_profile_directory()).expanduser().resolve()
        self.playwright: Any | None = None
        self.context: Any | None = None
        self.gate: BrowserGate | None = None
        self.account_finish_clicked = False

    def __enter__(self) -> "LineConsoleBrowser":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise SetupError("缺少 Playwright。請重新安裝 ZEAL 套件。") from error

        self.playwright = sync_playwright().start()
        try:
            if not self.skip_browser_install:
                _install_chromium_if_missing(self.playwright.chromium)
            self._launch_context()
        except Exception:
            self.playwright.stop()
            self.playwright = None
            raise
        print(f"ZEAL 已啟動瀏覽器（登入資料保存在：{self.profile_directory}）。")
        return self

    def _launch_context(self) -> None:
        assert self.playwright is not None
        self.profile_directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.profile_directory.chmod(0o700)
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_directory), headless=False
            )
        except Exception as error:
            raise SetupError("無法開啟 LINE 瀏覽器；請確認 Chromium 已安裝且目前有桌面顯示環境。") from error
        try:
            self.gate = BrowserGate(self.context)
        except Exception as error:
            self.context.close()
            self.context = None
            raise SetupError("無法啟用瀏覽器操作鎖；請更新 Playwright Chromium 後重試。") from error

    def __exit__(self, exc_type: Any, error: Any, _traceback: Any) -> None:
        try:
            if exc_type is not None and issubclass(exc_type, SetupError) and self.context:
                with contextlib.suppress(Exception):
                    if self.pages:
                        self._give_user_control(self.pages[-1], "ZEAL 已暫停，現在可由你操作此頁面")
                        print(f"\nZEAL 已暫停：{error}")
                        input("瀏覽器會保持開啟供你檢查；完成後按 Enter 關閉：")
        finally:
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

    def _wait_for_browser(self, milliseconds: int) -> None:
        """Give Playwright time to process navigation and challenge events."""
        for page in reversed(self.pages):
            with contextlib.suppress(Exception):
                page.wait_for_timeout(milliseconds)
                return
        time.sleep(milliseconds / 1000)

    def _automate(self, message: str = "ZEAL 正在操作此頁面") -> None:
        if self.gate:
            self.gate.automation(message)

    def _give_user_control(self, page: Any, message: str = "現在可由你操作此頁面") -> None:
        if self.gate:
            self.gate.human(page, message)
        else:
            with contextlib.suppress(AttributeError):
                page.bring_to_front()

    def _act(self, page: Any, operation: Any) -> Any:
        with contextlib.suppress(AttributeError):
            page.bring_to_front()
        return self.gate.action(page, operation) if self.gate else operation()

    def _hand_off_human_verification(self, page: Any, reason: str) -> Any:
        """Let the account holder solve a challenge in the current browser page."""
        self._give_user_control(page, "現在可由你完成 LINE 的驗證")
        print()
        _human_notice(f"偵測到「{reason}」。請在目前的 LINE 瀏覽器完成人類驗證。")
        try:
            input("完成驗證後按 Enter，ZEAL 會在同一個瀏覽器繼續：")
        finally:
            self._automate()
        return page

    def open_authenticated_page(
        self, url: str, purpose: str, *, defer_human_verification: bool = False
    ) -> Any:
        """Open a visible page and hand human-only verification to the user."""
        assert self.context is not None
        page = self.context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        self._automate(f"ZEAL 正在開啟 {purpose}")
        self._accept_information_use_consent(page)
        self._acknowledge_line_continue(page)
        self._dismiss_manager_welcome(page)
        reason = human_verification_reason(self._page_text(page))
        while reason and not defer_human_verification:
            page = self._hand_off_human_verification(page, reason)
            for _ in range(20):
                reason = human_verification_reason(self._page_text(page))
                if not reason:
                    break
                self._wait_for_browser(250)
            if reason:
                print(f"{purpose} 仍要求人類驗證；請在目前頁面完成後再按 Enter。")
        return page

    def _show_manual_page(self, url: str) -> None:
        if self.pages:
            self._give_user_control(self.pages[-1], "ZEAL 已暫停，現在可由你操作此頁面")

    def _accept_information_use_consent(self, page: Any) -> bool:
        """Accept LINE's named information-use consent when it interrupts setup."""
        if "同意我們使用您的資訊" not in self._page_text(page):
            return False
        print("LINE 顯示資訊使用同意頁；ZEAL 正在按「同意」。")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for candidate in (
                page.get_by_role("button", name=re.compile(r"^同意(?:並繼續)?$")),
                page.get_by_text("同意", exact=True),
            ):
                with contextlib.suppress(Exception):
                    self._act(page, lambda: candidate.last.click(timeout=500))
                    print("ZEAL 已按 LINE 資訊使用同意頁的「同意」。")
                    return True
            self._wait_for_browser(100)
        self._show_manual_page(page.url)
        raise SetupError("LINE 資訊使用同意頁的按鈕未被辨識；請在目前的瀏覽器檢查。")

    def _acknowledge_line_continue(self, page: Any) -> bool:
        """Continue past LINE's named acknowledgement notice."""
        host = urllib.parse.urlsplit(page.url).hostname or ""
        if not any(host == domain or host.endswith(f".{domain}") for domain in ("line.biz", "line.me")):
            return False
        page_text = re.sub(r"\s+", "", self._page_text(page))
        if "了解並繼續使用" not in page_text:
            return False
        print("LINE 顯示「了解並繼續使用」提示；ZEAL 正在按下該按鈕。")
        buttons = (
            page.get_by_role("button", name=LINE_CONTINUE_BUTTON),
            page.get_by_text(LINE_CONTINUE_BUTTON),
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for button in buttons:
                with contextlib.suppress(Exception):
                    self._act(page, lambda: button.last.click(timeout=500))
                    print("ZEAL 已按「了解並繼續使用」，接續設定。")
                    return True
            self._wait_for_browser(100)
        self._show_manual_page(page.url)
        raise SetupError("LINE「了解並繼續使用」按鈕未被辨識；請在目前的瀏覽器檢查。")

    def _dismiss_manager_welcome(self, page: Any) -> bool:
        """Close the first-run Manager tour before reaching account settings."""
        if urllib.parse.urlsplit(page.url).hostname != "manager.line.biz":
            return False
        if not MANAGER_WELCOME_STEP.search(self._page_text(page)):
            return False
        print("LINE 顯示首次使用導覽；ZEAL 正在關閉導覽並接續設定。")
        for _ in range(3):
            match = MANAGER_WELCOME_STEP.search(self._page_text(page))
            if not match:
                return True
            heading = page.get_by_text(MANAGER_WELCOME_TITLE).first
            close_buttons = (
                page.get_by_role("button", name=re.compile(r"^(Close|關閉|×|✕)$", re.I)),
                heading.locator("xpath=ancestor::*[.//button][1]").locator("button").first,
            )
            for index, button in enumerate(close_buttons):
                with contextlib.suppress(Exception):
                    if index == 1 and button.inner_text(timeout=250).strip() not in ("", "×", "✕"):
                        continue
                    self._act(page, lambda: button.click(timeout=600))
                    for _ in range(5):
                        if not MANAGER_WELCOME_STEP.search(self._page_text(page)):
                            print("ZEAL 已關閉 LINE 首次使用導覽。")
                            return True
                        self._wait_for_browser(100)
            names = (
                ("Next",)
                if match.group(1) == "1"
                else ("Done", "Finish", "Got it", "Get started", "Start using", "Close", "完成", "開始使用")
            )
            for name in names:
                with contextlib.suppress(Exception):
                    self._act(page, lambda: page.get_by_role("button", name=name, exact=True).click(timeout=600))
                    self._wait_for_browser(100)
                    break
        if not MANAGER_WELCOME_STEP.search(self._page_text(page)):
            print("ZEAL 已完成 LINE 首次使用導覽。")
            return True
        self._show_manual_page(page.url)
        raise SetupError("LINE 首次使用導覽無法自動關閉；請在目前的瀏覽器檢查。")

    def _click_first(self, page: Any, labels: tuple[str, ...], *, required: bool = True) -> bool:
        wait_seconds = 8 if required else 3
        deadline = time.monotonic() + wait_seconds
        acknowledged_notice = False
        while time.monotonic() < deadline:
            if self._accept_information_use_consent(page):
                deadline = time.monotonic() + wait_seconds
                continue
            if not acknowledged_notice and self._acknowledge_line_continue(page):
                acknowledged_notice = True
                deadline = time.monotonic() + wait_seconds
                continue
            if self._dismiss_manager_welcome(page):
                deadline = time.monotonic() + wait_seconds
                continue
            for label in labels:
                candidate = page.get_by_text(label, exact=True)
                with contextlib.suppress(Exception):
                    self._act(page, lambda: candidate.last.click(timeout=500))
                    return True
            self._wait_for_browser(100)
        if required:
            self._show_manual_page(page.url)
            raise SetupError(f"找不到 LINE 操作「{labels[0]}」；請在目前的瀏覽器檢查。")
        return False

    def _provider_options(self, page: Any) -> tuple[str, ...]:
        """Extract displayed Provider labels from LINE's current selection controls."""
        options: list[str] = []
        for radio in page.locator('input[type="radio"]').all():
            if radio.get_attribute("value") == "0":
                continue  # The first radio opens the separate New provider form.
            label = ""
            with contextlib.suppress(Exception):
                label = radio.locator("xpath=following-sibling::label[1]").inner_text().strip()
            if not label:
                with contextlib.suppress(Exception):
                    label = radio.locator("xpath=..").inner_text().strip()
            if label and label not in options:
                options.append(label)
        return tuple(options)

    def _manager_account_is_open(self, page: Any, account_name: str) -> bool:
        """Avoid reselecting the account when Manager already shows its home."""
        if urllib.parse.urlsplit(page.url).hostname != "manager.line.biz":
            return False
        text = self._page_text(page)
        return (
            account_name in text
            and any(label in text for label in ("Settings", "設定"))
            and any(label in text for label in ("Home", "首頁", "ホーム"))
        )

    @staticmethod
    def _selected_account_is_open(page: Any, manager_url: str) -> bool:
        """Ensure a selected account link did not redirect to another account."""
        expected = urllib.parse.urlsplit(manager_url)
        actual = urllib.parse.urlsplit(page.url)
        return (
            actual.hostname == "manager.line.biz"
            and (actual.path == expected.path or actual.path.startswith(f"{expected.path}/"))
        )

    def existing_official_account(self, account_name: str) -> bool:
        """Check LINE's live account list before retrying an interrupted setup."""
        page = self.open_authenticated_page(LINE_MANAGER_URL, "確認既有 LINE 官方帳號")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            for candidate in (page, *reversed(self.pages)):
                self._accept_information_use_consent(candidate)
                self._acknowledge_line_continue(candidate)
                self._dismiss_manager_welcome(candidate)
                with contextlib.suppress(Exception):
                    candidate.get_by_text(account_name, exact=True).first.wait_for(
                        state="visible", timeout=250
                    )
                    return True
            self._wait_for_browser(100)
        return False

    def list_official_accounts(self) -> tuple[OfficialAccountChoice, ...]:
        """Read account links visible to the signed-in user in LINE Manager."""
        page = self.open_authenticated_page(
            LINE_MANAGER_URL, "讀取既有 LINE 官方帳號", defer_human_verification=True
        )
        deadline = time.monotonic() + 8
        handed_off = False
        while True:
            candidates = list(reversed(self.pages)) if self.context else [page]
            manager_pages = [
                candidate
                for candidate in candidates
                if urllib.parse.urlsplit(candidate.url).hostname == "manager.line.biz"
            ]
            for manager_page in manager_pages:
                accounts: list[OfficialAccountChoice] = []
                seen: set[str] = set()
                links: list[Any] = []
                with contextlib.suppress(Exception):
                    links = manager_page.locator('a[href*="/account/"]').all()
                for link in links:
                    with contextlib.suppress(Exception):
                        if not link.is_visible():
                            continue
                        href = urllib.parse.urljoin(
                            manager_page.url, link.get_attribute("href") or ""
                        )
                        parsed = urllib.parse.urlsplit(href)
                        match = re.match(r"^/account/([^/]+)(?:/.*)?$", parsed.path)
                        if parsed.hostname != "manager.line.biz" or not match:
                            continue
                        manager_url = urllib.parse.urlunsplit(
                            ("https", "manager.line.biz", f"/account/{match.group(1)}", "", "")
                        )
                        name = next(
                            (line.strip() for line in link.inner_text().splitlines() if line.strip()),
                            "",
                        )
                        if name and manager_url not in seen:
                            accounts.append(OfficialAccountChoice(name, manager_url))
                            seen.add(manager_url)
                if accounts:
                    if handed_off:
                        self._automate("ZEAL 正在讀取既有 LINE 官方帳號")
                    return tuple(accounts)
            newest_manager = next(
                (index for index, candidate in enumerate(candidates) if candidate in manager_pages),
                len(candidates) - 1,
            )
            checkpoint = next(
                (
                    candidate
                    for candidate in candidates[: newest_manager + 1]
                    if urllib.parse.urlsplit(candidate.url).hostname
                    in ("account.line.biz", "access.line.me")
                    or "使用以下帳號登入" in self._page_text(candidate)
                ),
                None,
            )
            if checkpoint is not None:
                if not handed_off:
                    self._give_user_control(checkpoint, "請在此頁登入 LINE；ZEAL 會自動接續")
                    _human_notice("請在目前的 LINE 瀏覽器完成登入；ZEAL 會自動接續。")
                    handed_off = True
                deadline = time.monotonic() + 8
            else:
                if handed_off:
                    self._automate("ZEAL 正在讀取既有 LINE 官方帳號")
                    handed_off = False
                for manager_page in manager_pages:
                    self._accept_information_use_consent(manager_page)
                    self._acknowledge_line_continue(manager_page)
                    self._dismiss_manager_welcome(manager_page)
                if time.monotonic() >= deadline:
                    break
            self._wait_for_browser(100)
        self._show_manual_page(page.url)
        raise SetupError("無法從 LINE 管理頁讀取可管理的官方帳號清單；請在瀏覽器確認登入帳號與頁面。")

    def current_official_account_url(self) -> str | None:
        """Recover from a changed account-list layout using the opened Manager URL."""
        for page in reversed(self.pages):
            parsed = urllib.parse.urlsplit(page.url)
            match = re.match(r"^/account/([^/]+)(?:/.*)?$", parsed.path)
            if parsed.hostname == "manager.line.biz" and match:
                return f"https://manager.line.biz/account/{match.group(1)}"
        return None

    def fill_official_account_form(self, account: AccountDetails, form_page: Any) -> str:
        """Fill user-provided fields, then select an industry from live LINE options."""
        try:
            self._act(form_page, lambda: form_page.get_by_role("textbox", name="帳號名稱", exact=True).fill(account.name))
            self._act(form_page, lambda: form_page.get_by_role("textbox", name="電子郵件帳號", exact=True).fill(account.email))
            self._act(form_page, lambda: form_page.get_by_role("textbox", name="公司名稱", exact=True).fill(account.company_name))
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

        print("業種會填入官方帳號資料；請依實際經營內容選擇。例：咖啡店選最接近餐飲的分類。")
        major_category = prompt_option("業種大分類", major_options, two_columns=True)
        try:
            self._act(form_page, lambda: major_select.select_option(label=major_category))
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

        print("請在此大分類中選更貼近的細項；以 LINE 目前列出的選項為準。")
        minor_category = prompt_option("業種小分類", minor_options, two_columns=True)
        try:
            self._act(form_page, lambda: minor_select.select_option(label=minor_category))
        except Exception as error:
            raise SetupError("無法將所選業種寫入 LINE 表單。") from error
        print(f"已在 LINE 表單填入帳號資料並選擇業種：{major_category}／{minor_category}")
        return minor_category

    def begin_account_creation(self, account: AccountDetails) -> Any:
        assert self.context is not None
        form_page = None
        for candidate in reversed(getattr(self.context, "pages", [])):
            with contextlib.suppress(Exception):
                if self._is_entry_form(candidate):
                    form_page = candidate
                    break
        if form_page is not None:
            self._automate("ZEAL 正在接續目前的官方帳號表單")
            self.fill_official_account_form(account, form_page)
            return form_page
        page = self.context.new_page()
        page.goto(LINE_OFFICIAL_ACCOUNT_ENTRY_URL, wait_until="domcontentloaded")
        self._give_user_control(page, "若 LINE 要求你操作，請在此頁完成")
        print("\n已開啟 LINE 瀏覽器。官方帳號資料只需在 ZEAL 終端機輸入；ZEAL 會填寫網頁表單。")
        announced_login = False
        while True:
            form_page = self._wait_for_entry_form(seconds=10)
            if form_page is not None:
                self._automate("ZEAL 正在填寫官方帳號表單")
                print("LINE 建立表單已載入；ZEAL 正在填入帳號資料與業種。")
                self.fill_official_account_form(account, form_page)
                return form_page
            if not self.pages or all(candidate.is_closed() for candidate in self.pages):
                raise SetupError("LINE 瀏覽器已關閉；請重新執行設定。")
            if not announced_login:
                _human_notice("若 LINE 要求登入、OTP/MFA 或 CAPTCHA，請在瀏覽器完成；ZEAL 會自動接續。")
                announced_login = True

    def _wait_for_entry_form(self, *, seconds: int) -> Any | None:
        """Wait for the live entry form rather than trusting a login redirect URL."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for candidate in reversed(self.pages):
                with contextlib.suppress(Exception):
                    if self._is_entry_form(candidate):
                        return candidate
            self._wait_for_browser(250)
        return None

    @staticmethod
    def _is_entry_url(url: str) -> bool:
        location = urllib.parse.urlsplit(url)
        return location.hostname == "entry.line.biz" and location.path.startswith(
            "/form/entry/"
        )

    @staticmethod
    def _is_entry_form(page: Any) -> bool:
        location = urllib.parse.urlsplit(page.url)
        return (
            not page.is_closed()
            and LineConsoleBrowser._is_entry_url(page.url)
            and location.path.startswith("/form/entry/unverified")
            and page.locator("select").count() >= 3
        )

    def submit_account_creation(self, page: Any) -> None:
        """Submit both LINE account-creation steps without a terminal pause.

        Only browser controls marked as required are checked.  Optional consent
        (for example, marketing messages) is intentionally left untouched.
        """
        self._automate("ZEAL 正在送出官方帳號建立表單")
        for checkbox in page.locator('input[type="checkbox"][required]').all():
            with contextlib.suppress(Exception):
                if checkbox.is_visible() and checkbox.is_enabled() and not checkbox.is_checked():
                    self._act(page, lambda: checkbox.check(timeout=5_000))

        print("\nZEAL 正在按 LINE 表單的「建立／確定」，前往資料確認頁。")
        if not self._click_first(page, ("Create", "建立", "確定", "Submit"), required=False):
            raise SetupError("ZEAL 未找到 LINE 建立表單的送出按鈕；請在目前的瀏覽器檢查表單。")
        if any(account_creation_detected(current.url, self._page_text(current)) for current in self.pages):
            return
        print("ZEAL 正在按 LINE 確認頁的「完成」。")
        self.account_finish_clicked = self._click_account_finish()
        if self.account_finish_clicked:
            print("ZEAL 已按「完成」，正在等待建立結果。")

    def _click_account_finish(self) -> bool:
        for current in reversed(self.pages):
            if self._click_first(current, ("完成", "Finish", "Complete", "Done"), required=False):
                return True
        return False

    def _account_human_verification_reason(self) -> str | None:
        for page in self.pages:
            reason = human_verification_reason(self._page_text(page))
            if reason in ("sign in", "登入") and urllib.parse.urlsplit(page.url).hostname not in (
                "account.line.biz",
                "access.line.me",
            ):
                reason = None
            if reason:
                return reason
            with contextlib.suppress(Exception):
                if any(
                    marker in frame.url.casefold()
                    for frame in page.frames
                    for marker in ("recaptcha", "hcaptcha", "turnstile")
                ):
                    return "CAPTCHA"
        return None

    def continue_after_account_creation(self) -> None:
        """Wait for account creation and preserve the active page through CAPTCHA."""
        while True:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                for page in self.pages:
                    if account_creation_detected(page.url, self._page_text(page)):
                        return
                self._wait_for_browser(1000)
            reason = self._account_human_verification_reason()
            if reason:
                if not self.pages:
                    raise SetupError("LINE 驗證頁面已遺失；請確認官方帳號是否已建立。")
                self._give_user_control(self.pages[-1], "現在可由你完成 LINE 的驗證")
                _human_notice(f"LINE 要求「{reason}」；請只完成人類驗證，不必按確認頁的「完成」。")
                try:
                    input("驗證完成後按 Enter，ZEAL 會在同一個瀏覽器接手：")
                finally:
                    self._automate()
                if not self.pages:
                    raise SetupError("LINE 驗證視窗已關閉；請確認官方帳號是否已建立。")
                if any(account_creation_detected(page.url, self._page_text(page)) for page in self.pages):
                    return
                if self._click_account_finish():
                    self.account_finish_clicked = True
                    print("ZEAL 已在驗證後按「完成」，正在等待建立結果。")
                continue
            if not self.account_finish_clicked and self._click_account_finish():
                self.account_finish_clicked = True
                print("ZEAL 已按 LINE 確認頁的「完成」，正在等待建立結果。")
                continue
            if self.account_finish_clicked:
                raise SetupError("ZEAL 已按「完成」，但 LINE 未回報建立成功；請到官方帳號管理頁確認結果。")
            raise SetupError("ZEAL 找不到 LINE 確認頁的「完成」按鈕；已停止自動送出，避免重複建立。")

    def enable_messaging_api(
        self, account_name: str, manager_url: str | None = None
    ) -> MessagingApiSetup:
        """Enable Messaging API and return its generated channel ID.

        Provider ownership is irreversible, so the account holder selects from
        LINE's live list rather than needing to remember a command-line value.
        """
        page = self.open_authenticated_page(
            manager_url or LINE_MANAGER_URL, "開啟 LINE Official Account Manager"
        )
        if manager_url and not self._selected_account_is_open(page, manager_url):
            self._show_manual_page(page.url)
            raise SetupError("LINE 未開啟剛選擇的官方帳號；請在瀏覽器確認後重新執行。")
        self._automate("ZEAL 正在啟用 Messaging API")
        if manager_url is None and not self._manager_account_is_open(page, account_name):
            self._click_first(page, (account_name,))
        self._click_first(page, ("Settings", "設定"))
        self._click_first(page, ("Messaging API", "Messaging API設定", "Messaging API settings"))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            channel_id = channel_id_from_settings_text(self._page_text(page))
            if channel_id:
                return MessagingApiSetup(
                    channel_id=channel_id,
                    provider="已綁定（名稱未讀取）",
                    already_enabled=True,
                )
            if "Enable Messaging API" in self._page_text(page) or "啟用 Messaging API" in self._page_text(page):
                break
            self._wait_for_browser(100)
        self._click_first(page, ("Enable Messaging API", "啟用 Messaging API", "Messaging APIを利用する"))

        deadline = time.monotonic() + 8
        options = self._provider_options(page)
        while not options and time.monotonic() < deadline:
            self._wait_for_browser(100)
            options = self._provider_options(page)
        if not options and "New provider" not in self._page_text(page):
            self._show_manual_page(page.url)
            raise SetupError("無法讀取 Provider 清單；請在目前的瀏覽器手動完成。")
        provider, create_provider = prompt_provider_choice(options)

        if create_provider:
            self._click_first(page, ("Create a new provider", "建立新的 Provider", "新しいプロバイダーを作成"))
            field = page.get_by_role("textbox")
            if field.count() == 0:
                self._show_manual_page(page.url)
                raise SetupError("找不到新 Provider 名稱欄位；請在目前的瀏覽器手動完成。")
            self._act(page, lambda: field.last.fill(create_provider))
        else:
            self._click_first(page, (provider or "",))
        self._click_first(page, ("Agree", "Confirm", "Create", "建立", "確定"))
        self._click_first(page, ("OK",))  # Optional Privacy Policy and Terms of Use.
        self._click_first(page, ("OK",))  # Confirm the account and Provider link.

        reason = human_verification_reason(self._page_text(page))
        if reason:
            page = self._hand_off_human_verification(page, reason)
        deadline = time.monotonic() + 8
        channel_id = None
        while time.monotonic() < deadline and not channel_id:
            channel_id = channel_id_from_url(page.url) or channel_id_from_settings_text(self._page_text(page))
            if not channel_id:
                self._wait_for_browser(200)
        if not channel_id:
            page = self.open_authenticated_page(LINE_CONSOLE_URL, "確認 Messaging API channel")
            channel_id = channel_id_from_url(page.url)
        if not channel_id:
            self._show_manual_page(page.url)
            raise SetupError("Messaging API channel 尚未被安全辨識；請在目前的瀏覽器手動確認。")
        return MessagingApiSetup(channel_id=channel_id, provider=create_provider or provider or "未辨識")

    def wait_for_credentials(self, channel_id: str) -> Credentials:
        """Read credentials from the Messaging API page without printing them."""
        page = self.open_authenticated_page(messaging_api_url(channel_id), "讀取 Messaging API 憑證")
        self._automate("ZEAL 正在讀取 Channel 憑證")
        token_pattern = re.compile(r"Channel access token \(long-lived\)\s+([A-Za-z0-9._~+/=-]{40,})")
        deadline = time.monotonic() + 8
        token_match = None
        while time.monotonic() < deadline and not token_match:
            token_match = token_pattern.search(self._page_text(page))
            if not token_match:
                self._wait_for_browser(100)
        if not token_match:
            # Issue only a first token. Never click Reissue: that would revoke an
            # existing credential and could disconnect a running integration.
            issue = page.get_by_role(
                "button", name=re.compile(r"^(Issue|發行|発行)$", re.I)
            )
            if issue.count() == 1:
                self._act(page, lambda: issue.click(timeout=8_000))
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and not token_match:
                    token_match = token_pattern.search(self._page_text(page))
                    if not token_match:
                        self._wait_for_browser(200)
            if not token_match:
                self._show_manual_page(page.url)
                raise SetupError("無法讀取 Channel access token；請在目前瀏覽器發行或顯示長期 token 後重試。")
        token = token_match.group(1)

        try:
            self._act(page, lambda: page.get_by_role("button", name="Basic settings", exact=True).click(timeout=8_000))
        except Exception:
            if self._click_first(page, ("Basic settings", "基本設定", "基本設定項目"), required=False):
                pass
            else:
                self._show_manual_page(page.url)
                raise SetupError("找不到 Basic settings；請在目前瀏覽器開啟後重試。")
        secret_pattern = re.compile(r"Channel secret\s+([0-9a-fA-F]{32})")
        deadline = time.monotonic() + 8
        secret_match = None
        while time.monotonic() < deadline and not secret_match:
            secret_match = secret_pattern.search(self._page_text(page))
            if not secret_match:
                self._wait_for_browser(100)
        if not secret_match:
            self._show_manual_page(page.url)
            raise SetupError("無法讀取 Channel secret；請在目前的瀏覽器檢查。")
        return Credentials(channel_secret=secret_match.group(1), channel_access_token=token)

    def _enable_use_webhook_console(self, channel_id: str) -> None:
        page = self.open_authenticated_page(messaging_api_url(channel_id), "啟用 Use webhook")
        self._automate("ZEAL 正在啟用 Use webhook")
        row = page.get_by_text("Use webhook", exact=True).locator("xpath=..")
        webhook_input = row.locator('input[name="active"]')
        if webhook_input.count() != 1:
            raise SetupError("LINE Console 找不到 Use webhook 開關。")
        if not webhook_input.is_checked():
            self._act(page, lambda: row.locator("label[for]").click(timeout=8_000))

    def _enable_use_webhook_manager(self, account_name: str, manager_url: str | None) -> None:
        page = self.open_authenticated_page(manager_url or LINE_MANAGER_URL, "啟用 LINE Webhook")
        if manager_url and not self._selected_account_is_open(page, manager_url):
            raise SetupError("LINE Manager 未開啟所選官方帳號。")
        self._automate("ZEAL 正在 LINE Manager 啟用 Webhook")
        if manager_url is None and not self._manager_account_is_open(page, account_name):
            self._click_first(page, (account_name,))
        self._click_first(page, ("Settings", "設定"))
        self._click_first(page, ("Response settings", "回應設定"))
        webhook = page.locator('div.webhook-setting shared-switch input[type="checkbox"]')
        if webhook.count() != 1:
            raise SetupError("LINE Manager 找不到 Webhook 開關。")
        if not webhook.is_checked():
            toggle = page.locator('div.webhook-setting shared-switch label[data-scope="switch"]')
            self._act(page, lambda: toggle.click(timeout=8_000))

    def show_add_friend_qr(self, qr_path: Path) -> None:
        """Bring the generated add-friend QR into the visible LINE browser."""
        assert self.context is not None
        html_path = qr_path.with_suffix(".html")
        html_path.write_text(
            "<!doctype html><html lang='zh-Hant'><meta charset='utf-8'>"
            "<title>LINE 加好友 QR Code</title>"
            "<style>body{font:18px system-ui,sans-serif;text-align:center;margin:32px}"
            "img{width:min(80vw,480px);height:auto}</style>"
            "<h1>用手機 LINE 掃描 QR Code 加好友</h1>"
            f"<img src='{qr_path.name}' alt='LINE 加好友 QR Code'></html>",
            encoding="utf-8",
        )
        page = self.context.new_page()
        page.goto(html_path.as_uri(), wait_until="domcontentloaded")
        self._give_user_control(page, "請用手機 LINE 掃描此加好友 QR Code，並傳訊息測試 Bot")

    def configure_webhook(
        self,
        webhook_url: str,
        channel_id: str,
        channel_access_token: str,
        account_name: str,
        manager_url: str | None = None,
    ) -> bool:
        """Set and test through LINE's public API, then enable and verify the switch."""
        set_and_test_webhook(channel_access_token, webhook_url)
        print("LINE Messaging API 已確認 Webhook URL 可收到驗證請求。")
        state = webhook_endpoint_state(channel_access_token)
        if state.get("active") is not True:
            try:
                self._enable_use_webhook_console(channel_id)
            except Exception as console_error:
                print(
                    f"LINE Console 開關未完成（{type(console_error).__name__}）；"
                    "ZEAL 正在改由 LINE Manager 啟用。"
                )
            for _ in range(5):
                state = webhook_endpoint_state(channel_access_token)
                if state.get("active") is True:
                    break
                time.sleep(1)
            if state.get("active") is not True:
                print("LINE Console 尚未回報開啟；ZEAL 正在改由 LINE Manager 啟用。")
                try:
                    self._enable_use_webhook_manager(account_name, manager_url)
                except Exception as manager_error:
                    raise SetupError(
                        f"LINE Console 與 Manager 的 Webhook 開關均未成功辨識"
                        f"（{type(manager_error).__name__}）。"
                    ) from manager_error

        deadline = time.monotonic() + 65
        while time.monotonic() < deadline:
            state = webhook_endpoint_state(channel_access_token)
            if state.get("endpoint") == webhook_url and state.get("active") is True:
                print("已確認 LINE Webhook URL 為本次網址，且 Use webhook 已啟用。")
                return True
            time.sleep(2)
        raise SetupError("LINE 尚未回報本次 Webhook URL 與 Use webhook 已啟用；請查看目前瀏覽器狀態。")

    def disable_auto_response_messages(
        self, account_name: str, manager_url: str | None = None
    ) -> bool:
        """Prevent Manager's default reply from duplicating the Bot's reply."""
        try:
            page = self.open_authenticated_page(
                manager_url or LINE_MANAGER_URL, "關閉 LINE 預設自動回覆"
            )
            if manager_url and not self._selected_account_is_open(page, manager_url):
                return False
            if manager_url is None and not self._manager_account_is_open(page, account_name):
                self._click_first(page, (account_name,))
            self._click_first(page, ("Settings", "設定"))
            self._click_first(page, ("Response settings", "回應設定"))
            if not page.url.endswith("/setting/response"):
                return False

            webhook = page.locator('div.webhook-setting shared-switch input[type="checkbox"]')
            auto_reply = page.locator('div.auto-response-setting shared-switch input[type="checkbox"]')
            if webhook.count() != 1 or auto_reply.count() != 1:
                return False

            def loaded() -> bool:
                for _ in range(40):
                    if webhook.is_checked():
                        return True
                    self._wait_for_browser(200)
                return False

            if not loaded():
                return False
            if auto_reply.is_checked():
                toggle = page.locator('div.auto-response-setting shared-switch label[data-scope="switch"]')
                if toggle.count() != 1:
                    return False
                self._act(page, lambda: toggle.click(timeout=5_000))
                for _ in range(20):
                    if not auto_reply.is_checked():
                        break
                    self._wait_for_browser(200)
                if auto_reply.is_checked():
                    return False
                self._wait_for_browser(400)

            page.reload(wait_until="domcontentloaded")
            return loaded() and not auto_reply.is_checked()
        except Exception as error:
            print(f"無法確認 LINE 預設自動回覆已關閉（{type(error).__name__}）。", file=sys.stderr)
            return False


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if not process or process.poll() is not None:
        return
    process.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    if process.poll() is None:
        process.kill()


def change_project_port(directory: Path, credentials: Credentials, old_port: int, new_port: int) -> None:
    """Update only the .env that this setup just validated, using an atomic swap."""
    verify_existing_credentials(directory, credentials, old_port)
    env_file = directory / ".env"
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".env-port-", dir=directory)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(credential_env_contents(credentials, new_port))
        with contextlib.suppress(OSError):
            temporary.chmod(0o600)
        os.replace(temporary, env_file)
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def start_setup_runtime(
    args: Any, account: AccountDetails, directory: Path, credentials: Credentials
) -> tuple[AccountDetails, subprocess.Popen[str] | None, subprocess.Popen[str] | None, str]:
    """Start the chosen public connection and Bot, with in-place recovery."""
    while True:
        ngrok: subprocess.Popen[str] | None = None
        app: subprocess.Popen[str] | None = None
        public_url = getattr(args, "public_url", None)
        try:
            setup_step(
                5, "建立公開 HTTPS 通道",
                f"LINE 無法直接連到你的電腦；公開網址須轉送到本機 Bot 的 {account.port} 埠。",
            )
            if public_url is None:
                print(f"取得 ngrok Authtoken：{_terminal_link(NGROK_AUTHTOKEN_URL)}")
                public_url = existing_tunnel_url(account.port)
                if public_url:
                    print("已找到對應此連接埠的 ngrok 通道，將沿用目前網址。")
                else:
                    binary = install_ngrok()
                    if not getattr(args, "ngrok_configured", False):
                        ensure_ngrok_config(binary, args.ngrok_authtoken)
                        args.ngrok_configured = True
                    ngrok, public_url = tunnel_url(binary, account.port, directory)
                print(f"ngrok 公開網址：{_terminal_link(public_url)}")
            else:
                print(f"自備公開網址：{_terminal_link(public_url)}")
            callback_url = f"{public_url}/callback"

            setup_step(
                6, "啟動本機 Bot",
                f"Bot 會在本機 {account.port} 埠接收 LINE 訊息並回覆；ZEAL 會確認它已啟動或沿用現有程序。",
            )
            connection_tested = False
            if local_port_listening(account.port):
                result = line_api_request(
                    credentials.channel_access_token, "POST", "/channel/webhook/test",
                    {"endpoint": callback_url},
                )
                if result.get("success") is not True:
                    raise SetupError(f"連接埠 {account.port} 已被使用，LINE 無法驗證該服務。")
                connection_tested = True
                print(f"連接埠 {account.port} 的現有 Bot 已通過 LINE 測試，沿用現有程序。")
            else:
                ensure_target_dependencies(directory)
                app = start_app(directory)
                time.sleep(1)
                if app.poll() is not None:
                    raise SetupError(f"Bot 啟動後立即結束；請查看日誌：{runtime_log_directory(directory) / 'bot.log'}")
                print(f"Bot 已在本機 {account.port} 埠啟動；接下來會將 LINE Webhook 指向這個服務。")
            if getattr(args, "public_url", None) is not None:
                if not connection_tested:
                    result = line_api_request(
                        credentials.channel_access_token, "POST", "/channel/webhook/test",
                        {"endpoint": callback_url},
                    )
                    if result.get("success") is not True:
                        raise SetupError("LINE 無法透過自備網址連到 Bot；請確認 HTTPS 網址與轉送設定。")
                print("LINE 已透過自備網址成功連到 Bot。")
            return account, ngrok, app, callback_url
        except (SetupError, OSError, TimeoutError, zipfile.BadZipFile, tarfile.TarError) as error:
            stop_process(app)
            stop_process(ngrok)
            print(f"\n公開網址或 Bot 未完成：{error}")
            options = ("重試公開網址與 Bot", "改用其他本機連接埠", "重新輸入 ngrok Authtoken", "改用自備 HTTPS 網址", "結束設定")
            try:
                choice = prompt_option("服務接續方式", options)
            except (EOFError, OSError):
                raise error
            if choice == options[4]:
                raise SetupError("已停止於公開網址／Bot 啟動；本機專案與憑證仍保留。") from error
            if choice == options[2]:
                args.ngrok_authtoken = getpass.getpass("新的 ngrok Authtoken（輸入時不顯示）：").strip()
                args.ngrok_configured = False
                args.public_url = None
            if choice == options[3]:
                args.public_url = prompt_valid_public_url()
            if choice == options[1]:
                new_port = prompt_valid_port(0)
                if new_port == account.port:
                    print("請選擇與目前不同的連接埠。")
                    continue
                retry_setup_step(
                    "更新 Bot 連接埠",
                    lambda: change_project_port(directory, credentials, account.port, new_port),
                )
                account = replace(account, port=new_port)


@contextlib.contextmanager
def open_line_browser(args: Any) -> Any:
    """Allow Chromium installation or launch to be retried in this invocation."""
    with contextlib.ExitStack() as stack:
        browser = retry_setup_step(
            "開啟 LINE 瀏覽器",
            lambda: stack.enter_context(
                LineConsoleBrowser(args.skip_browser_install, args.browser_profile)
            ),
        )
        yield browser


def run_setup(args: Any) -> None:
    """Run `zeal line-bot setup`."""
    try:
        confirm_setup_start()
        args.public_url = prompt_public_url()
        if args.public_url is None:
            prepare_ngrok(args.ngrok_authtoken, args.port)
            args.ngrok_configured = True
        else:
            print("將使用自備 HTTPS 網址；啟動 Bot 後會請 LINE 測試能否連上。")
        route = prompt_account_route()
        if route == "create":
            setup_step(
                1, "準備新官方帳號資料",
                "請填入 LINE 申請表需要的名稱與聯絡資料；稍後 ZEAL 會在瀏覽器協助建立帳號。",
            )
            account = prompt_account_details(args.port)
            print(f"「{account.name}」的申請資料已備妥；接下來會開啟 LINE 瀏覽器。")
            _run_setup_with_account(args, account)
        else:
            args.port = prompt_valid_port(args.port)
            setup_step(
                1, "選擇既有官方帳號",
                "ZEAL 會開啟瀏覽器，列出你目前能管理的 LINE 官方帳號；選定後會接續該帳號的 Bot 設定。",
            )
            with open_line_browser(args) as browser:
                while True:
                    try:
                        choices = browser.list_official_accounts()
                        break
                    except SetupError as error:
                        print(f"\n{error}")
                        options = (
                            "在同一個瀏覽器重新讀取帳號清單",
                            "在瀏覽器開啟要接續的官方帳號後繼續",
                            "改為建立新的官方帳號",
                            "取消設定",
                        )
                        action = prompt_option("目前無法讀取官方帳號清單", options)
                        if action == options[0]:
                            continue
                        if action == options[2]:
                            account = prompt_account_details(args.port)
                            _run_setup_with_account(args, account, browser=browser)
                            return
                        if action == options[3]:
                            return
                        if browser.pages:
                            browser._give_user_control(
                                browser.pages[-1], "請在此瀏覽器開啟要接續的官方帳號"
                            )
                        input("在瀏覽器開啟官方帳號首頁後，按 Enter 讓 ZEAL 讀取網址：")
                        manager_url = browser.current_official_account_url()
                        if manager_url is None:
                            print("尚未找到 LINE 官方帳號網址；請留在同一個瀏覽器再試。")
                            continue
                        name = input("這個官方帳號的名稱（例：小明咖啡客服）：").strip()
                        if not name:
                            print("官方帳號名稱不可空白；請重新選擇。")
                            continue
                        browser._automate("ZEAL 正在接續所選官方帳號")
                        choices = (OfficialAccountChoice(name, manager_url),)
                        break
                labels = tuple(
                    f"{choice.name} ({choice.manager_url.rsplit('/', 1)[-1]})"
                    if sum(item.name == choice.name for item in choices) > 1
                    else choice.name
                    for choice in choices
                )
                selected_label = prompt_option(
                    "請選擇以下已存在的 LINE 官方帳號",
                    labels,
                    input_label="請輸入編號或完整名稱：",
                )
                selected = choices[labels.index(selected_label)]
                while True:
                    answer = input(account_confirmation_prompt(selected.name)).strip()
                    if not answer:
                        break
                    print("請直接按 Enter 繼續；若要離開，請按 Ctrl+C。")
                account = AccountDetails(selected.name, "", args.port)
                _run_setup_with_account(args, account, browser=browser, manager_url=selected.manager_url)
    except (KeyboardInterrupt, EOFError):
        print("\n已取消，尚未建立專案或啟動程序。")
    except SetupError as error:
        print(f"\n設定未完成：{error}", file=sys.stderr)


def _run_setup_with_account(
    args: Any,
    account: AccountDetails,
    *,
    browser: LineConsoleBrowser | None = None,
    manager_url: str | None = None,
) -> None:
    """Complete setup after the account is created or selected."""
    output_root = (args.output or Path.cwd()).resolve()
    destination = project_directory(output_root, account.name)
    try:
        destination, reused = retry_setup_step(
            "建立本機專案", lambda: choose_project_destination(destination, account)
        )
    except SetupError as error:
        print(f"\n設定未完成：{error}", file=sys.stderr)
        return
    existing_credentials = (destination / ".env").is_file()
    project_status = (
        "沿用已設定的本機專案" if existing_credentials
        else "接續前次未完成的專案" if reused else "已建立專案"
    )
    print(f"\n{project_status}：{destination}")

    ngrok: subprocess.Popen[str] | None = None
    app: subprocess.Popen[str] | None = None
    completed = False
    try:
        account_action = (
            "ZEAL 會在瀏覽器確認剛才選定的帳號，並接續它的設定。"
            if manager_url
            else "ZEAL 會先檢查有沒有同名帳號；若沒有，才在瀏覽器送出新帳號申請。"
        )
        setup_step(
            2, "確認 LINE 官方帳號",
            account_action + "LINE 登入、驗證碼和人類驗證仍由你在瀏覽器完成。",
        )
        browser_context = (
            contextlib.nullcontext(browser)
            if browser is not None
            else open_line_browser(args)
        )
        with browser_context as browser:
            account_exists = manager_url is None and retry_setup_step(
                "辨識官方帳號", lambda: browser.existing_official_account(account.name), browser
            )
            if not manager_url and not account_exists and reused:
                options = (
                    "重新辨識 LINE 管理頁",
                    "已在瀏覽器開啟同名帳號，接續設定",
                    "確認建立新的官方帳號",
                    "結束設定",
                )
                while True:
                    choice = prompt_option("尚未找到同名官方帳號", options)
                    if choice == options[0]:
                        account_exists = retry_setup_step(
                            "辨識官方帳號", lambda: browser.existing_official_account(account.name), browser
                        )
                        if account_exists:
                            break
                        print("目前仍未找到同名官方帳號。")
                    elif choice == options[1]:
                        pages = getattr(browser, "pages", [])
                        if pages:
                            browser._give_user_control(pages[-1], "請開啟同名 LINE 官方帳號")
                        input("開啟同名帳號後按 Enter，ZEAL 會重新辨識：")
                        manager_url = browser.current_official_account_url()
                        browser._automate()
                        if manager_url:
                            break
                        print("目前頁面不是可辨識的 LINE 官方帳號管理頁。")
                    elif choice == options[2]:
                        break
                    else:
                        return
            if manager_url:
                print(f"已選擇既有 LINE 官方帳號：{account.name}")
            elif account_exists:
                print("LINE 管理頁已有同名官方帳號；ZEAL 會接續 Messaging API 設定。")
            else:
                def current_form_or_result() -> Any | None:
                    for current in getattr(browser, "pages", []):
                        with contextlib.suppress(Exception):
                            if account_creation_detected(current.url, browser._page_text(current)):
                                return None
                            if (urllib.parse.urlsplit(current.url).hostname == "entry.line.biz"
                                and not browser._is_entry_form(current)
                                and current.get_by_text("完成", exact=True).count()):
                                return None
                    return browser.begin_account_creation(account)

                form_page = retry_setup_step(
                    "填寫官方帳號表單", current_form_or_result, browser
                )
                if form_page is not None:
                    try:
                        browser.submit_account_creation(form_page)
                    except Exception as error:
                        if not isinstance(error, (SetupError, OSError, TimeoutError)) and not type(error).__module__.startswith("playwright."):
                            raise
                        print("LINE 表單送出狀態未確認；請檢查目前頁面。")
                        browser._give_user_control(form_page, "請檢查 LINE 表單或確認頁")
                        try:
                            input("在瀏覽器處理後按 Enter，ZEAL 會檢查建立結果：")
                        finally:
                            browser._automate()
                retry_setup_step("確認官方帳號建立結果", browser.continue_after_account_creation, browser)
            print(f"官方帳號「{account.name}」已確認；接下來會檢查它能否將訊息交給 Bot。")
            setup_step(
                3, "確認 Messaging API 與 Provider",
                "Messaging API 讓 LINE 將訊息交給 Bot 並接收回覆。Provider 是此 Channel 所屬服務的經營者；若尚未啟用 API，ZEAL 會請你選擇或建立 Provider，綁定後無法移轉。",
            )
            messaging = retry_setup_step(
                "啟用 Messaging API",
                lambda: (
                    browser.enable_messaging_api(account.name, manager_url)
                    if manager_url else browser.enable_messaging_api(account.name)
                ),
                browser,
                fallback=prompt_messaging_channel,
            )
            if messaging.already_enabled:
                print(
                    f"這個帳號的 Messaging API 已啟用（Channel ID：{messaging.channel_id}）。"
                    "Provider 已綁定，ZEAL 會沿用現有 Channel，不需要重新選擇。"
                )
            else:
                print(f"Messaging API 已就緒（Channel ID：{messaging.channel_id}）；接下來會準備 Bot 使用的憑證。")
            setup_step(
                4, "保存 Channel 憑證",
                "Bot 需要 Channel secret 驗證 LINE 訊息，並使用 access token 傳送回覆。ZEAL 會將憑證保存在本機專案的 .env，不會顯示密鑰。",
            )
            credentials = retry_setup_step(
                "讀取 Channel 憑證",
                lambda: browser.wait_for_credentials(messaging.channel_id),
                browser,
                fallback=prompt_existing_credentials,
            )
            destination = retry_setup_step(
                "寫入本機憑證",
                lambda: choose_credentials_destination(destination, account, credentials),
            )
            if not existing_credentials:
                print("已安全寫入 .env（未在終端輸出密鑰）。")
            else:
                print("已確認本機 .env 的憑證與所選 Channel 相符；接下來會啟動連線與 Bot。")

            account, ngrok, app, callback_url = start_setup_runtime(
                args, account, destination, credentials
            )

            setup_step(
                7, "設定並驗證 Webhook",
                "ZEAL 會把 Bot 的公開 HTTPS 網址交給 LINE，測試 LINE 能送達訊息，並確認 Use webhook 已啟用。",
            )
            configured = retry_setup_step(
                "設定 LINE Webhook",
                lambda: require_webhook_enabled(browser.configure_webhook(
                    callback_url, messaging.channel_id, credentials.channel_access_token,
                    account.name, manager_url,
                )),
                browser,
            )
            auto_response_disabled = (
                browser.disable_auto_response_messages(account.name, manager_url)
                if manager_url else browser.disable_auto_response_messages(account.name)
            )
            if auto_response_disabled:
                print("已關閉 LINE 預設自動回覆，避免與 Bot 同時回覆。")
            else:
                print("請到 LINE Manager → Settings → Response settings，關閉 Auto-response messages，避免重複回覆。")

            setup_step(
                8, "實際測試 Bot 回覆",
                "最後請用手機掃描加好友 QR Code，傳一則訊息給官方帳號；收到 Bot 回覆後，這次設定才算完成。",
            )
            friend_url = retry_setup_step(
                "讀取 LINE Basic ID", lambda: add_friend_url(credentials.channel_access_token),
                fallback=prompt_add_friend_url,
            )
            qr_path = retry_setup_step(
                "產生加好友 QR Code", lambda: write_add_friend_qr(destination, friend_url)
            )
            print(f"加好友連結：{_terminal_link(friend_url)}")
            print(f"加好友 QR Code：{qr_path}")
            retry_setup_step("顯示加好友 QR Code", lambda: browser.show_add_friend_qr(qr_path), browser)
            while True:
                ending = (
                    "Bot 會持續在背景執行" if args.keep_running
                    else "本次啟動的 Bot 將停止"
                )
                input(f"用手機掃描 QR Code，傳送訊息並確認收到 Bot 回覆；按 Enter 完成設定並結束指令（{ending}）：")
                if getattr(args, "public_url", None) is not None:
                    try:
                        result = line_api_request(
                            credentials.channel_access_token, "POST", "/channel/webhook/test",
                            {"endpoint": callback_url},
                        )
                        connection_ready = result.get("success") is True
                    except SetupError:
                        connection_ready = False
                else:
                    connection_ready = existing_tunnel_url(account.port) is not None
                if ((app is None or app.poll() is None)
                    and local_port_listening(account.port)
                    and (ngrok is None or ngrok.poll() is None)
                    and connection_ready):
                    break
                print("Bot 或公開連線已停止；ZEAL 將在本次設定中重新啟動並驗證 Webhook。")
                stop_process(app)
                stop_process(ngrok)
                app = ngrok = None
                account, ngrok, app, callback_url = start_setup_runtime(
                    args, account, destination, credentials
                )
                configured = retry_setup_step(
                    "重新驗證 LINE Webhook",
                    lambda: require_webhook_enabled(browser.configure_webhook(
                        callback_url, messaging.channel_id, credentials.channel_access_token,
                        account.name, manager_url,
                    )),
                    browser,
                )
            print(format_completion_summary(
                account,
                destination,
                callback_url,
                messaging,
                webhook_configured=configured,
                keep_running=args.keep_running,
                using_ngrok=getattr(args, "public_url", None) is None,
            ))
            if args.keep_running:
                cached_ngrok = zeal_bin_directory() / ("ngrok.exe" if os.name == "nt" else "ngrok")
                ngrok_binary = (
                    str(ngrok.args[0]) if ngrok is not None
                    else shutil.which("ngrok") or (str(cached_ngrok) if cached_ngrok.is_file() else None)
                )
                print(format_runtime_instructions(
                    app.pid if app is not None else None,
                    ngrok.pid if ngrok is not None else None,
                    runtime_log_directory(destination),
                    port=account.port,
                    ngrok_binary=ngrok_binary,
                    using_ngrok=getattr(args, "public_url", None) is None,
                ))
            completed = True
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。已建立的專案會保留；若已寫入 .env，請自行決定是否保留。")
    except SetupError as error:
        print(f"\n設定未完成：{error}", file=sys.stderr)
    finally:
        if not completed or not args.keep_running:
            stop_process(app)
            stop_process(ngrok)


def run_resume(args: Any) -> None:
    """Create and start a local project for an existing channel, without new LINE setup."""
    if not args.name:
        print("請填 LINE Manager 中既有官方帳號的名稱，ZEAL 會用它命名本機 Bot 專案。")
        print("例：小明咖啡客服、星球讀書會。")
    name = (args.name or input("既有 LINE Official Account 名稱：")).strip()
    if not name:
        raise SetupError("官方帳號名稱不可空白。")
    if not 1 <= args.port <= 65535:
        raise SetupError("port 必須介於 1 到 65535。")

    account = AccountDetails(name=name, category="Existing Messaging API channel", port=args.port)
    directory = project_directory((args.output or Path.cwd()).resolve(), name)
    directory, reused = choose_project_destination(directory, account)
    print(f"{'接續既有專案' if reused else '已建立專案'}：{directory}")

    credentials = prompt_existing_credentials()
    directory = choose_credentials_destination(directory, account, credentials)

    ensure_target_dependencies(directory)
    app = start_app(directory)
    time.sleep(1)
    if app.poll() is not None:
        raise SetupError("Bot 程式未能啟動。請檢查產生專案的 .env 與相依套件。")

    public_url = args.webhook_url or existing_tunnel_url(args.port)
    if public_url is None:
        binary = install_ngrok()
        ensure_ngrok_config(binary, args.ngrok_authtoken)
        _, public_url = tunnel_url(binary, args.port, directory)

    callback_url = f"{public_url}/callback"
    if not args.channel_id:
        print("Channel ID 是 LINE Developers Console 中 Messaging API Channel 的數字編號。")
        print("例：2001234567；留空則改為手動設定 Webhook。")
        if not args.no_browser:
            print("填入後 ZEAL 會嘗試自動設定 Webhook。")
    channel_id = (args.channel_id or input("LINE Channel ID（留空則改為手動設定 Webhook）：")).strip()
    configured = False
    if channel_id and not args.no_browser:
        with LineConsoleBrowser(args.skip_browser_install, args.browser_profile) as browser:
            configured = browser.configure_webhook(
                callback_url, channel_id, credentials.channel_access_token, name,
            )
            if configured and not browser.disable_auto_response_messages(name):
                print("請到 LINE Manager → Settings → Response settings，關閉 Auto-response messages，避免重複回覆。")

    if configured:
        print("\nWebhook URL 已儲存、Verify 成功，且 Use webhook 已啟用。")
    else:
        print("\nBot 已啟動。請在 LINE Developers Console 設定：")
        print(f"Webhook URL: {callback_url}")
        print("接著按 Verify，開啟 Use webhook，並傳送測試文字給官方帳號。")
    print("請保持此終端、Bot 與 ngrok 執行，直到測試完成。")
