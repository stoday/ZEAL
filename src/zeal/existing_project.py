"""Connect an existing application without generating or owning its runtime."""

from __future__ import annotations

import contextlib
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

from zeal.interaction import input, print, notify


def configure_project_mode(args: Any) -> None:
    from zeal.line_bot import prompt_option, SetupError

    project = getattr(args, "existing_project", None)
    if project is None:
        options = ("由 ZEAL 建立簡易 Python Bot 與環境", "使用自己的專案，不建置 Python 環境")
        selected = prompt_option("是否需要 ZEAL 建置 Bot 專案與環境？", options)
        if selected == options[0]:
            args.existing_project = None
            return
        print("可沿用任何語言的專案；請提供既有資料夾，稍後會確認它的 LINE Webhook。")
        project = input("既有專案資料夾（按 Enter 使用目前目錄）：").strip() or str(Path.cwd())
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise SetupError("既有專案資料夾不存在；請先確認路徑，不會自動建立資料夾。")
    args.existing_project = project
    notify("project", mode="existing", path=str(project))


def save_project_credentials(directory: Path, credentials: Any, channel_id: str) -> Path:
    from zeal.line_bot import SetupError, messaging_api_url
    import json

    messaging_api_url(channel_id)
    if not directory.is_dir():
        raise SetupError("既有專案資料夾已不存在。")
    private = directory / ".zeal-line"
    ignore = directory / ".gitignore"
    if private.is_symlink() or ignore.is_symlink():
        raise SetupError("既有專案的 ZEAL 憑證目錄或 .gitignore 是連結；請先選擇安全位置。")
    content = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if ".zeal-line/" not in content.splitlines():
        with ignore.open("a", encoding="utf-8") as output:
            output.write(("\n" if content and not content.endswith("\n") else "") + ".zeal-line/\n")
    private.mkdir(mode=0o700, exist_ok=True)
    with contextlib.suppress(OSError):
        private.chmod(0o700)
    contents = (
        f"LINE_CHANNEL_SECRET={json.dumps(credentials.channel_secret)}\n"
        f"LINE_CHANNEL_ACCESS_TOKEN={json.dumps(credentials.channel_access_token)}\n"
    )
    number = 1
    while True:
        suffix = "" if number == 1 else f"-{number}"
        target = private / f"channel-{channel_id}{suffix}.env"
        if target.is_symlink():
            raise SetupError("ZEAL 憑證檔案是連結；不會寫入。")
        try:
            with target.open("x", encoding="utf-8") as output:
                output.write(contents)
            with contextlib.suppress(OSError):
                target.chmod(0o600)
            return target
        except FileExistsError:
            if target.is_file() and target.read_text(encoding="utf-8") == contents:
                return target
            number += 1


def prepare_project_integration(args: Any, directory: Path, credentials_file: Path) -> None:
    notify("project", mode="existing", path=str(directory), credentials_file=str(credentials_file))
    print(f"沿用既有專案：{directory}")
    print(f"LINE 憑證已另存：{credentials_file}（不顯示密鑰）")
    print("原專案若已有同一個 Channel 的憑證，可繼續沿用；需要更新時，請由你在原專案預期的位置設定。")
    print("請以原本的指令啟動或部署專案，例如 npm run dev；它需提供 LINE Webhook 接收與回覆功能。")
    print("ZEAL 不會替此專案建立 Python 環境；可使用 Node.js、其他語言或已部署的服務。")
    notify("task", name="prepare_existing_project")
    try:
        input("原專案已使用此 Channel 並啟動 Webhook 服務後，按 Enter 接續：")
    finally:
        notify("task", name=None)


def prompt_callback_url() -> str:
    from zeal.line_bot import SetupError, validate_public_url

    while True:
        value = input("完整公開 HTTPS Webhook 網址（例：https://bot.example.com/api/line/webhook）：").strip()
        try:
            return validate_public_url(value, complete_callback=True)
        except SetupError as error:
            print(error)


def prompt_callback_path() -> str:
    from zeal.line_bot import SetupError

    while True:
        path = input("原專案的 Webhook 路徑（例：/api/line/webhook；按 Enter 使用 /callback）：").strip() or "/callback"
        parsed = urllib.parse.urlsplit(path)
        if (path.startswith("/") and not path.startswith("//") and not parsed.netloc
            and not parsed.scheme and not parsed.query and not parsed.fragment
            and not any(character.isspace() for character in path) and len(path) <= 400):
            return path
        print(SetupError("請輸入以 / 開頭的 Webhook 路徑，不要包含網址、查詢參數或 # 片段。"))


def start_existing_runtime(args: Any, account: Any, directory: Path, credentials: Any
                           ) -> tuple[Any, subprocess.Popen[str] | None, None, str]:
    from zeal.line_bot import (
        SetupError, setup_step, prompt_option, prompt_valid_port, existing_tunnel_url,
        prepare_ngrok, tunnel_url, line_api_request, stop_process,
    )

    while True:
        ngrok = None
        try:
            setup_step(5, "連接既有專案的 Webhook", "可提供已部署的完整 HTTPS 網址，或為本機專案建立測試用公開連線。")
            if not getattr(args, "existing_callback_url", None):
                options = ("使用原專案的完整 HTTPS Webhook 網址", "為本機既有專案建立測試用公開連線")
                selected = prompt_option("既有專案連線方式", options)
                if selected == options[0]:
                    args.existing_callback_url = prompt_callback_url()
                    args.existing_using_ngrok = False
                else:
                    from dataclasses import replace
                    account = replace(account, port=prompt_valid_port(0))
                    path = prompt_callback_path()
                    public = existing_tunnel_url(account.port)
                    if not public:
                        binary = prepare_ngrok(getattr(args, "ngrok_authtoken", None), account.port)
                        if binary is None:
                            public = existing_tunnel_url(account.port)
                            if not public:
                                raise SetupError("既有公開連線已中斷。")
                        else:
                            ngrok, public = tunnel_url(binary, account.port, directory)
                    args.existing_callback_url = f"{public}{path}"
                    args.existing_using_ngrok = True
            callback = args.existing_callback_url
            setup_step(6, "確認原專案可接收 LINE Webhook", "原專案由你或 agent 以既有方式啟動；ZEAL 只向 LINE 驗證這個網址。")
            result = line_api_request(credentials.channel_access_token, "POST", "/channel/webhook/test", {"endpoint": callback})
            if result.get("success") is not True:
                raise SetupError("LINE 無法驗證原專案的 Webhook；請檢查路徑、簽章處理、服務啟動與公開連線。")
            print(f"LINE 已確認原專案可接收 Webhook：{callback}")
            return account, ngrok, None, callback
        except (SetupError, OSError, TimeoutError) as error:
            stop_process(ngrok)
            # A newly owned tunnel was stopped, so its old URL must not be reused.
            if ngrok is not None:
                args.existing_callback_url = None
            notify("retry", step="驗證既有專案 Webhook", code=type(error).__name__)
            print(f"既有專案連線未完成：{error}")
            options = ("原專案已修正，重試驗證", "改用其他 Webhook 網址或連線", "結束設定")
            selected = prompt_option("既有專案接續方式", options)
            if selected == options[2]:
                raise SetupError("已停止驗證既有專案；原專案與服務由原本方式管理。") from error
            if selected == options[1]:
                args.existing_callback_url = None
        except BaseException:
            stop_process(ngrok)
            raise


def completion_summary(account: Any, directory: Path, callback: str, messaging: Any,
                       credentials_file: Path, *, using_ngrok: bool, keep_running: bool,
                       owned_ngrok: bool = False) -> str:
    return "\n".join((
        "\n========== ZEAL 設定摘要 ==========",
        "既有專案已接上 LINE，並已確認收到 Bot 回覆。",
        f"官方帳號：{account.name}", f"Provider：{messaging.provider}",
        f"Messaging API channel：{messaging.channel_id}", f"既有專案：{directory}",
        f"Webhook URL：{callback}", "Webhook 狀態：已驗證並啟用 Use webhook",
        f"獨立憑證檔：{credentials_file}（不會顯示密鑰）",
        "原專案與 Bot 服務由原本的啟動／部署方式管理。",
        (("ZEAL 的測試用公開連線會繼續執行。" if keep_running else "本次由 ZEAL 啟動的公開連線將停止。") if owned_ngrok else "沿用的公開連線由原本方式管理。") if using_ngrok else "使用原專案的公開 HTTPS 網址。",
        "====================================",
    ))
