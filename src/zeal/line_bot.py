"""Local bootstrap flow for a LINE Messaging API echo bot.

LINE sign-in, MFA, CAPTCHA, and other proof-of-humanity remain human-only. All
other safely recognised controls are automated from a local persistent profile;
the session remains visible with explicit page-input handoffs.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zeal.browser_gate import BrowserGate


LINE_MANAGER_URL = "https://manager.line.biz/"
LINE_CONSOLE_URL = "https://developers.line.biz/console/"
LINE_OFFICIAL_ACCOUNT_ENTRY_URL = "https://entry.line.biz/form/entry/unverified"
NGROK_DOWNLOAD_BASE = "https://bin.equinox.io/c/bNyj1mQVY4c"
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
    label: str, options: tuple[str, ...] | list[str], *, two_columns: bool = False
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
            return option_from_choice(input(f"{label}（輸入編號或完整名稱）："), options)
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
    print("官方帳號名稱會顯示在顧客的 LINE 聊天室。例：小明咖啡客服、星球讀書會。")
    name = input("官方帳號名稱：").strip()
    if not name:
        raise SetupError("官方帳號名稱不可空白。")
    if len(name) > 20:
        raise SetupError("官方帳號名稱不可超過 20 個字元。")

    print("公司／店鋪名稱會填入 LINE 申請表；它不會自動建立或選定 Provider。")
    print("例：小明咖啡有限公司、小明咖啡；個人可填經營名稱，如星球讀書會。")
    company_name = input("公司／店鋪名稱：").strip()
    if not company_name:
        raise SetupError("公司／店鋪名稱不可空白。")
    if len(company_name) > 100:
        raise SetupError("公司／店鋪名稱不可超過 100 個字元。")

    print("此信箱會填入 LINE 官方帳號申請表，請使用可收信的地址。例：hello@example.com。")
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


def confirm_setup_start() -> None:
    """Explain the guided setup before collecting data or changing anything."""
    print(f"\n{_accent('◆ ZEAL 將協助您申請 LINE 官方帳號，並建立一個能回覆訊息的簡易機器人。', '36;1')}")
    print("ZEAL 會代您開啟並操作瀏覽器、填寫申請資料；需要登入或人類驗證時，會請您親自操作。")
    print()
    if input("若同意開始，請按 Enter；按 Ctrl+C 取消：").strip():
        raise SetupError("尚未開始設定；同意時請直接按 Enter。")


def setup_step(number: int, description: str) -> None:
    print(f"\n{_accent(f'[步驟 {number}/8] {description}', '36;1')}")


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
        "Bot 與 ngrok：指令結束後持續在背景執行。"
        if keep_running
        else "本次啟動的 Bot 與 ngrok：此摘要顯示後將停止。"
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
    bot_pid: int, ngrok_pid: int | None, log_directory: Path
) -> str:
    """Show how to inspect and stop the exact services left by this setup."""
    pids = [bot_pid, *([ngrok_pid] if ngrok_pid is not None else [])]
    bot_log = log_directory / "bot.log"
    ngrok_log = log_directory / "ngrok.log"
    lines = [
        "\n背景程序已啟動：",
        f"Bot PID：{bot_pid}；日誌：{bot_log}",
        (
            f"ngrok PID：{ngrok_pid}；日誌：{ngrok_log}"
            if ngrok_pid is not None
            else "ngrok：使用已執行的連線；請從原先啟動 ngrok 的位置查看日誌與 PID。"
        ),
    ]
    if os.name == "nt":
        joined = ",".join(str(pid) for pid in pids)
        quoted_bot_log = str(bot_log).replace("'", "''")
        lines.extend(
            (
                f"PowerShell 查看程序：Get-Process -Id {joined}",
                f"PowerShell 查看 Bot 日誌：Get-Content -Tail 30 -Wait -LiteralPath '{quoted_bot_log}'",
                f"PowerShell 停止本次啟動的程序：Stop-Process -Id {joined}",
            )
        )
        if ngrok_pid is not None:
            quoted_ngrok_log = str(ngrok_log).replace("'", "''")
            lines.append(
                f"PowerShell 查看 ngrok 日誌：Get-Content -Tail 30 -Wait -LiteralPath '{quoted_ngrok_log}'"
            )
        else:
            lines.append("PowerShell 查找既有 ngrok：Get-Process -Name ngrok")
            lines.append("PowerShell 確認 PID 後停止既有 ngrok：Stop-Process -Id <ngrok PID>")
    else:
        joined = ",".join(str(pid) for pid in pids)
        lines.extend(
            (
                f"查看程序：ps -p {joined} -o pid,command",
                f"查看 Bot 日誌：tail -f {shlex.quote(str(bot_log))}",
                f"停止本次啟動的程序：kill {' '.join(str(pid) for pid in pids)}",
            )
        )
        if ngrok_pid is not None:
            lines.append(f"查看 ngrok 日誌：tail -f {shlex.quote(str(ngrok_log))}")
        else:
            lines.append("查找既有 ngrok：pgrep -af ngrok")
            lines.append("確認 PID 後停止既有 ngrok：kill <ngrok PID>")
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
    if not argument_value and not os.environ.get("NGROK_AUTHTOKEN"):
        print("ngrok Authtoken 用來建立公開 HTTPS 連線，讓 LINE 能把訊息送到本機 Bot。")
        print(f"請從 ngrok Dashboard 複製你的 Authtoken：{_terminal_link(NGROK_AUTHTOKEN_URL)}")
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


def ensure_ngrok_config(binary: Path, argument_token: str | None) -> None:
    """Reuse a working ngrok login, unless the caller supplied a new token."""
    token = argument_token or os.environ.get("NGROK_AUTHTOKEN")
    if token:
        configure_ngrok(binary, token)
        return
    result = subprocess.run(
        [str(binary), "config", "check"], capture_output=True, text=True, check=False
    )
    if result.returncode:
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

    print("正在安裝產生專案的 Python 套件…")
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=directory,
        check=False,
    )
    if result.returncode:
        raise SetupError("無法安裝 Bot 專案套件。請確認目前 Python 環境有 pip 與網路連線。")


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
        try:
            self._launch_context()
        except Exception:
            self.playwright.stop()
            self.playwright = None
            raise
        print(f"ZEAL 瀏覽器登入資料會保存在：{self.profile_directory}")
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

    def open_authenticated_page(self, url: str, purpose: str) -> Any:
        """Open a visible page and hand human-only verification to the user."""
        assert self.context is not None
        page = self.context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        self._automate(f"ZEAL 正在開啟 {purpose}")
        self._accept_information_use_consent(page)
        self._acknowledge_line_continue(page)
        self._dismiss_manager_welcome(page)
        reason = human_verification_reason(self._page_text(page))
        if reason:
            page = self._hand_off_human_verification(page, reason)
            if human_verification_reason(self._page_text(page)):
                raise SetupError(f"{purpose} 仍要求人類驗證；請完成驗證後重新執行。")
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
        human_handoffs = 0
        while True:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                for page in self.pages:
                    if account_creation_detected(page.url, self._page_text(page)):
                        return
                self._wait_for_browser(1000)
            reason = self._account_human_verification_reason()
            if reason:
                human_handoffs += 1
                if human_handoffs > 3:
                    raise SetupError("LINE 持續要求人類驗證；請稍後重試。")
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

    def enable_messaging_api(self, account_name: str) -> MessagingApiSetup:
        """Enable Messaging API and return its generated channel ID.

        Provider ownership is irreversible, so the account holder selects from
        LINE's live list rather than needing to remember a command-line value.
        """
        page = self.open_authenticated_page(LINE_MANAGER_URL, "開啟 LINE Official Account Manager")
        self._automate("ZEAL 正在啟用 Messaging API")
        if not self._manager_account_is_open(page, account_name):
            self._click_first(page, (account_name,))
        self._click_first(page, ("Settings", "設定"))
        self._click_first(page, ("Messaging API", "Messaging API設定", "Messaging API settings"))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            channel_id = channel_id_from_settings_text(self._page_text(page))
            if channel_id:
                return MessagingApiSetup(channel_id=channel_id, provider="既有 Provider")
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
            self._show_manual_page(page.url)
            raise SetupError("無法讀取 Channel access token；請在目前的瀏覽器檢查。")
        token = token_match.group(1)

        self._act(page, lambda: page.get_by_role("button", name="Basic settings", exact=True).click(timeout=8_000))
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

    def configure_webhook(self, webhook_url: str, channel_id: str | None = None) -> bool:
        """Set, verify, and enable a webhook after the user has signed in locally."""
        assert self.context is not None
        page = self.open_authenticated_page(
            messaging_api_url(channel_id) if channel_id else LINE_CONSOLE_URL,
            "設定 Webhook",
        )
        self._automate("ZEAL 正在設定 Webhook")
        stage = "開啟編輯"
        try:
            self._act(page, lambda: page.get_by_role("button", name=re.compile("^Edit$", re.I)).click(timeout=8_000))
            stage = "填寫網址"
            field = page.get_by_role(
                "textbox", name=re.compile("webhook URL", re.I)
            )
            if field.count() == 0:
                field = page.locator('textarea[placeholder*="webhook" i]')
            self._act(page, lambda: field.last.fill(webhook_url))
            stage = "儲存網址"
            self._act(page, lambda: page.get_by_role("button", name=re.compile("^(Update|Save)$", re.I)).click(timeout=8_000))
            stage = "驗證網址"
            self._act(page, lambda: page.get_by_role("button", name=re.compile("^Verify$", re.I)).click(timeout=10_000))
            stage = "確認驗證結果"
            page.get_by_text("Success", exact=True).wait_for(timeout=20_000)
            stage = "關閉驗證結果"
            self._act(page, lambda: page.get_by_role("button", name=re.compile("^OK$", re.I)).click(timeout=5_000))

            stage = "啟用 Webhook"
            use_webhook = page.get_by_text("Use webhook", exact=True)
            webhook_row = use_webhook.locator("xpath=..")
            webhook_input = webhook_row.locator('input[name="active"]')
            if not webhook_input.is_checked():
                self._act(page, lambda: webhook_row.locator("label[for]").click(timeout=5_000))
            return webhook_input.is_checked()
        except Exception as error:
            print(f"Webhook 自動設定停在「{stage}」（{type(error).__name__}）。", file=sys.stderr)
            return False

    def disable_auto_response_messages(self, account_name: str) -> bool:
        """Prevent Manager's default reply from duplicating the Bot's reply."""
        try:
            page = self.open_authenticated_page(LINE_MANAGER_URL, "關閉 LINE 預設自動回覆")
            if not self._manager_account_is_open(page, account_name):
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


def run_setup(args: Any) -> None:
    """Run `zeal line-bot setup`."""
    try:
        confirm_setup_start()
        setup_step(1, "輸入官方帳號資料，建立本機 Bot 專案。")
        account = prompt_account_details(args.port)
    except (KeyboardInterrupt, EOFError):
        print("\n已取消，尚未建立專案或啟動程序。")
        return
    except SetupError as error:
        print(f"\n設定未完成：{error}", file=sys.stderr)
        return
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
    completed = False
    try:
        setup_step(2, "準備 ngrok 公開 HTTPS 網址，供 LINE 呼叫本機 Bot。")
        print(f"取得 ngrok Authtoken：{_terminal_link(NGROK_AUTHTOKEN_URL)}")
        public_url = existing_tunnel_url(account.port)
        if public_url:
            print("已找到連往相同本機埠的 ngrok；沿用目前執行中的連線。")
        else:
            if existing_tunnel_url() is not None:
                raise SetupError("已有指向其他本機埠的 ngrok；請先確認或停止該連線，再重新執行。")
            binary = install_ngrok()
            ensure_ngrok_config(binary, args.ngrok_authtoken)
            ngrok, public_url = tunnel_url(binary, account.port, destination)
        callback_url = f"{public_url}/callback"
        print(f"ngrok 公開網址：{_terminal_link(public_url)}")

        setup_step(3, "開啟 LINE 瀏覽器，建立或接續官方帳號；登入與人類驗證由你完成。")
        with LineConsoleBrowser(args.skip_browser_install, args.browser_profile) as browser:
            account_exists = reused and browser.existing_official_account(account.name)
            if account_exists:
                print("LINE 管理頁已有同名官方帳號；ZEAL 會接續 Messaging API 設定。")
            else:
                if reused:
                    if browser.pages:
                        browser._give_user_control(
                            browser.pages[-1], "請確認 LINE 管理頁是否已有這個官方帳號"
                        )
                    try:
                        answer = input(
                            "LINE 管理頁未找到同名帳號。確認尚未建立且要新建時，輸入「建立」；直接按 Enter 則停止："
                        ).strip()
                    finally:
                        browser._automate()
                    if answer != "建立":
                        print("已停止，沒有重複建立 LINE 官方帳號。")
                        return
                form_page = browser.begin_account_creation(account)
                browser.submit_account_creation(form_page)
                browser.continue_after_account_creation()
            setup_step(4, "啟用 Messaging API，並請你選擇 LINE Provider。")
            messaging = browser.enable_messaging_api(account.name)
            setup_step(5, "讀取 LINE Channel 憑證，寫入本機私有的 .env。")
            credentials = browser.wait_for_credentials(messaging.channel_id)
            write_credentials(destination, credentials, account.port)
            print("已安全寫入 .env（未在終端輸出密鑰）。")

            setup_step(6, "安裝 Bot 相依套件並在背景啟動回覆程式。")
            ensure_target_dependencies(destination)
            app = start_app(destination)
            time.sleep(1)
            if app.poll() is not None:
                raise SetupError(f"Bot 程式未能啟動；請查看日誌：{runtime_log_directory(destination) / 'bot.log'}")

            setup_step(7, "設定 LINE Webhook，驗證並啟用訊息事件。")
            configured = browser.configure_webhook(callback_url, messaging.channel_id)
            if configured:
                print("已送出 Webhook URL、Verify 與 Use webhook 操作。請在瀏覽器確認 Verify 顯示 Success。")
                if browser.disable_auto_response_messages(account.name):
                    print("已關閉 LINE 預設自動回覆，避免與 Bot 同時回覆。")
                else:
                    print("請到 LINE Manager → Settings → Response settings，關閉 Auto-response messages，避免重複回覆。")
            else:
                print("LINE Console 介面未被安全辨識；請手動貼上並驗證下列 Webhook URL：")
                print(callback_url)

            setup_step(8, "用手機加好友並傳送訊息，確認 Bot 回覆。")
            if browser.pages:
                browser._give_user_control(browser.pages[-1], "現在可由你檢查 LINE 設定並測試 Bot")
            input("用手機掃 QR Code 加好友並確認收到回覆後，按 Enter 顯示結果：")
            if app.poll() is not None or (ngrok is not None and ngrok.poll() is not None):
                raise SetupError("Bot 或 ngrok 已停止；請查看 ZEAL 執行日誌後重試。")
            print(format_completion_summary(
                account,
                destination,
                callback_url,
                messaging,
                webhook_configured=configured,
                keep_running=args.keep_running,
            ))
            if args.keep_running:
                print(format_runtime_instructions(
                    app.pid, ngrok.pid if ngrok is not None else None,
                    runtime_log_directory(destination),
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
            configured = browser.configure_webhook(callback_url, channel_id)
            if configured and not browser.disable_auto_response_messages(name):
                print("請到 LINE Manager → Settings → Response settings，關閉 Auto-response messages，避免重複回覆。")

    if configured:
        print("\nWebhook URL 已儲存、Verify 成功，且 Use webhook 已啟用。")
    else:
        print("\nBot 已啟動。請在 LINE Developers Console 設定：")
        print(f"Webhook URL: {callback_url}")
        print("接著按 Verify，開啟 Use webhook，並傳送測試文字給官方帳號。")
    print("請保持此終端、Bot 與 ngrok 執行，直到測試完成。")
