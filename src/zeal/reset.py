"""Reset the local tools and sign-in state used by ZEAL setup."""

from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from zeal.line_bot import (
    default_browser_profile_directory,
    zeal_bin_directory,
    zeal_data_directory,
)


@dataclass(frozen=True)
class RemovalTarget:
    label: str
    path: Path
    kind: str  # directory, file, or authtoken


class RemovalError(Exception):
    """A removal target could not be identified or removed safely."""


_BROWSER_DIRECTORY = re.compile(r"(?:chromium|chromium_headless_shell|ffmpeg|winldd)-\d+\Z")
_TOKEN_LINE = re.compile(rb"(?m)^(?:\xef\xbb\xbf)?authtoken[ \t]*:[^\r\n]*(?:\r?\n|$)")


def _existing_directory(label: str, path: Path) -> RemovalTarget | None:
    if path.is_symlink():
        raise RemovalError(f"拒絕處理符號連結：{path}")
    if not path.exists():
        return None
    if not path.is_dir():
        raise RemovalError(f"預期是資料夾，實際不是：{path}")
    resolved = path.resolve()
    if resolved in (Path.home().resolve(), Path.cwd().resolve(), Path(resolved.anchor)):
        raise RemovalError(f"拒絕刪除根目錄、家目錄或目前目錄：{resolved}")
    return RemovalTarget(label, resolved, "directory")


def _browser_targets(custom_profile: Path | None) -> list[RemovalTarget]:
    targets: list[RemovalTarget] = []
    profiles = [
        default_browser_profile_directory(),
        zeal_data_directory() / "line-browser-profile",  # older ZEAL versions
    ]
    if custom_profile is not None:
        custom_profile = custom_profile.expanduser().absolute()
        if (custom_profile.exists() and custom_profile.name != ".zeal-line-browser-profile"
            and not (custom_profile / "Local State").is_file()):
            raise RemovalError(f"自訂路徑不像 Chromium 登入資料夾，請手動檢查：{custom_profile}")
        profiles.append(custom_profile)
    for profile in profiles:
        target = _existing_directory("LINE 瀏覽器登入狀態", profile)
        if target is not None:
            targets.append(target)

    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if result.returncode:
        raise RemovalError("無法取得 Playwright Chromium 安裝路徑；未執行刪除。")
    locations = re.findall(r"(?m)^\s*Install location:\s*(.+?)\s*$", result.stdout)
    if not locations:
        raise RemovalError("Playwright 未回報 Chromium 安裝路徑；未執行刪除。")
    for location in locations:
        path = Path(location)
        if not path.is_absolute() or not _BROWSER_DIRECTORY.fullmatch(path.name):
            raise RemovalError(f"Playwright 回報了不安全的安裝路徑：{path}")
        target = _existing_directory("Playwright Chromium 元件", path)
        if target is not None:
            targets.append(target)
    return targets


def _default_ngrok_config_path(system: str, home: Path, env: Mapping[str, str]) -> Path:
    if system == "windows":
        root = env.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(root) / "ngrok" / "ngrok.yml"
    if system == "darwin":
        return home / "Library" / "Application Support" / "ngrok" / "ngrok.yml"
    root = env.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(root) / "ngrok" / "ngrok.yml"


def _ngrok_config_path() -> Path:
    """Ask ngrok for the active config, falling back to its v3 default."""
    binary = shutil.which("ngrok")
    if binary is None:
        bundled = zeal_bin_directory() / ("ngrok.exe" if os.name == "nt" else "ngrok")
        if bundled.is_file():
            binary = str(bundled)
    if binary is not None:
        try:
            result = subprocess.run(
                [binary, "config", "check"], capture_output=True, text=True,
                check=False, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            match = re.search(r"(?m)^Valid configuration file at (.+)$", result.stdout)
            if match is not None:
                return Path(match.group(1).strip()).expanduser()
    return _default_ngrok_config_path(platform.system().lower(), Path.home(), os.environ)


def _ngrok_targets() -> list[RemovalTarget]:
    targets: list[RemovalTarget] = []
    binary = zeal_bin_directory() / ("ngrok.exe" if os.name == "nt" else "ngrok")
    if binary.is_symlink():
        raise RemovalError(f"拒絕處理符號連結：{binary}")
    if binary.exists():
        if not binary.is_file():
            raise RemovalError(f"預期是 ngrok 執行檔，實際不是：{binary}")
        targets.append(RemovalTarget("ZEAL 下載的 ngrok", binary.resolve(), "file"))

    config = _ngrok_config_path()
    if config.is_symlink():
        raise RemovalError(f"拒絕處理符號連結：{config}")
    if config.exists():
        if not config.is_file():
            raise RemovalError(f"預期是 ngrok 設定檔，實際不是：{config}")
        if config.stat().st_size > 1024 * 1024:
            raise RemovalError(f"ngrok 設定檔過大，請手動檢查：{config}")
        if _TOKEN_LINE.search(config.read_bytes()):
            targets.append(RemovalTarget("ngrok 設定中的 Authtoken", config.resolve(), "authtoken"))
    return targets


def collect_reset_targets(*, browser: bool, ngrok: bool, custom_profile: Path | None = None) -> list[RemovalTarget]:
    targets: list[RemovalTarget] = []
    if browser:
        targets.extend(_browser_targets(custom_profile))
    if ngrok:
        targets.extend(_ngrok_targets())
    # A custom profile can equal the default or the legacy location.
    return list(dict.fromkeys(targets))


def _remove_authtoken(config: Path) -> None:
    original = config.read_bytes()
    updated = _TOKEN_LINE.sub(b"", original)
    if updated == original:
        return
    mode = stat.S_IMODE(config.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".zeal-ngrok-", dir=config.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(updated)
        temporary.chmod(mode)
        temporary.replace(config)
    finally:
        temporary.unlink(missing_ok=True)


def run_reset(args: Any) -> None:
    all_components = args.all or not (args.browser or args.ngrok)
    browser = all_components or args.browser
    ngrok = all_components or args.ngrok
    if args.browser_profile is not None and not browser:
        raise SystemExit("--browser-profile 需要搭配 --browser。")
    try:
        targets = collect_reset_targets(
            browser=browser, ngrok=ngrok, custom_profile=args.browser_profile,
        )
    except (OSError, RemovalError, subprocess.TimeoutExpired) as error:
        raise SystemExit(f"無法列出清理目標：{error}") from error

    if not targets:
        print("沒有找到所選項目的安裝或登入資料。")
        if ngrok and os.environ.get("NGROK_AUTHTOKEN"):
            print("NGROK_AUTHTOKEN 環境變數仍有設定；請自行從終端機或系統設定移除。")
        return
    print("將清理以下項目：")
    for target in targets:
        print(f"- {target.label}：{target.path}")
    if browser:
        print("Playwright 快取可能由其他專案共用；Linux 系統瀏覽器相依套件不會移除。")
    if ngrok:
        print("只會移除 ZEAL 下載的 ngrok；PATH 中另外安裝的 ngrok 會保留。")
        if os.environ.get("NGROK_AUTHTOKEN"):
            print("NGROK_AUTHTOKEN 環境變數仍有設定；請自行從終端機或系統設定移除。")
    print("Bot 專案、.env 及 ZEAL 執行日誌會保留。請先關閉正在使用的瀏覽器或 ngrok。")
    if not args.yes:
        try:
            answer = input("輸入 RESET 確認清理；直接按 Enter 取消：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消，未刪除任何資料。")
            return
        if answer != "RESET":
            print("已取消，未刪除任何資料。")
            return

    failures: list[str] = []
    for target in targets:
        try:
            if target.kind == "directory":
                shutil.rmtree(target.path)
            elif target.kind == "file":
                target.path.unlink()
            else:
                _remove_authtoken(target.path)
        except OSError as error:
            failures.append(f"{target.path}：{error}")
            continue
        print(f"已清理：{target.label}：{target.path}")
    if failures:
        raise SystemExit("部分項目無法清理；請確認相關程序已關閉：\n" + "\n".join(failures))
