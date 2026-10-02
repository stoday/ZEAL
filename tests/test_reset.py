"""Safety and CLI coverage for resetting ZEAL-managed local tools."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from zeal.cli import build_parser
from zeal.reset import (
    RemovalError,
    RemovalTarget,
    _browser_targets,
    _default_ngrok_config_path,
    _existing_directory,
    _ngrok_targets,
    run_reset,
)


def test_reset_cli_accepts_default_all_and_component_flags() -> None:
    parser = build_parser()
    cases = (
        ([], True, True),
        (["--all"], True, True),
        (["--browser"], True, False),
        (["--ngrok"], False, True),
        (["--browser", "--ngrok"], True, True),
    )
    for flags, browser, ngrok in cases:
        args = parser.parse_args(["reset", *flags, "--yes"])
        with patch("zeal.reset.collect_reset_targets", return_value=[]) as collect:
            run_reset(args)
        collect.assert_called_once_with(browser=browser, ngrok=ngrok, custom_profile=None)


def test_browser_targets_include_only_current_chromium_components_and_profiles(tmp_path: Path) -> None:
    project = tmp_path / "line-bot-example"
    project.mkdir()
    (project / ".env").write_text("private", encoding="utf-8")
    default_profile = tmp_path / ".zeal-line-browser-profile"
    legacy_profile = tmp_path / "Zeal" / "line-browser-profile"
    custom_profile = tmp_path / "custom-profile"
    for profile in (default_profile, legacy_profile, custom_profile):
        profile.mkdir(parents=True)
    (custom_profile / "Local State").write_text("{}", encoding="utf-8")
    cache = tmp_path / "ms-playwright"
    names = ("chromium-1243", "chromium_headless_shell-1243", "ffmpeg-1011", "winldd-1007")
    for name in (*names, "firefox-1522"):
        (cache / name).mkdir(parents=True)
    locations = "\n".join(f"  Install location:    {cache / name}" for name in names)
    with (
        patch("zeal.reset.default_browser_profile_directory", return_value=default_profile),
        patch("zeal.reset.zeal_data_directory", return_value=tmp_path / "Zeal"),
        patch("zeal.reset.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=locations)),
    ):
        targets = _browser_targets(custom_profile)
    expected = {profile.resolve() for profile in (default_profile, legacy_profile, custom_profile)}
    expected.update((cache / name).resolve() for name in names)
    assert {target.path for target in targets} == expected
    assert cache / "firefox-1522" not in {target.path for target in targets}
    assert project not in {target.path for target in targets}


def test_browser_targets_reject_unexpected_playwright_path(tmp_path: Path) -> None:
    output = f"Install location: {tmp_path / 'other-data'}\n"
    with (
        patch("zeal.reset.default_browser_profile_directory", return_value=tmp_path / "missing"),
        patch("zeal.reset.zeal_data_directory", return_value=tmp_path / "Zeal"),
        patch("zeal.reset.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=output)),
        pytest.raises(RemovalError, match="不安全"),
    ):
        _browser_targets(None)


def test_custom_profile_must_look_like_chromium_state(tmp_path: Path) -> None:
    ordinary = tmp_path / "ordinary-folder"
    ordinary.mkdir()
    with pytest.raises(RemovalError, match="不像 Chromium"):
        _browser_targets(ordinary)


def test_ngrok_config_defaults_are_cross_platform() -> None:
    home = Path("/home/person")
    assert _default_ngrok_config_path("linux", home, {}) == home / ".config/ngrok/ngrok.yml"
    assert _default_ngrok_config_path("linux", home, {"XDG_CONFIG_HOME": "/tmp/config"}) == Path("/tmp/config/ngrok/ngrok.yml")
    assert _default_ngrok_config_path("darwin", home, {}) == home / "Library/Application Support/ngrok/ngrok.yml"
    assert _default_ngrok_config_path("windows", home, {"LOCALAPPDATA": "C:/Users/person/AppData/Local"}) == Path("C:/Users/person/AppData/Local/ngrok/ngrok.yml")


def test_ngrok_targets_do_not_include_external_binary(tmp_path: Path) -> None:
    bin_dir = tmp_path / "Zeal" / "bin"
    bin_dir.mkdir(parents=True)
    managed = bin_dir / ("ngrok.exe" if os.name == "nt" else "ngrok")
    managed.write_bytes(b"binary")
    external = tmp_path / "external-ngrok"
    external.write_bytes(b"other binary")
    config = tmp_path / "ngrok.yml"
    config.write_text("version: '3'\nauthtoken: secret\nregion: us\n", encoding="utf-8")
    with (
        patch("zeal.reset.zeal_bin_directory", return_value=bin_dir),
        patch("zeal.reset._ngrok_config_path", return_value=config),
    ):
        targets = _ngrok_targets()
    assert {target.path for target in targets} == {managed.resolve(), config.resolve()}
    assert external.exists()


def test_reset_requires_confirmation_and_preserves_project_and_other_ngrok_settings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path / "line-bot-example"
    project.mkdir()
    credentials = project / ".env"
    credentials.write_text("private", encoding="utf-8")
    logs = tmp_path / "runtime" / "example"
    logs.mkdir(parents=True)
    (logs / "bot.log").write_text("running", encoding="utf-8")
    profile = tmp_path / ".zeal-line-browser-profile"
    profile.mkdir()
    (profile / "Cookies").write_text("private", encoding="utf-8")
    config = tmp_path / "ngrok.yml"
    config.write_bytes(b"version: '3'\r\nauthtoken: secret\r\nregion: us\r\n")
    targets = [
        RemovalTarget("LINE browser profile", profile, "directory"),
        RemovalTarget("ngrok token", config, "authtoken"),
    ]
    args = SimpleNamespace(all=False, browser=True, ngrok=True, browser_profile=None, yes=False)
    with (
        patch("zeal.reset.collect_reset_targets", return_value=targets),
        patch("builtins.input", return_value=""),
    ):
        run_reset(args)
    assert profile.exists()
    assert b"authtoken: secret" in config.read_bytes()

    with (
        patch("zeal.reset.collect_reset_targets", return_value=targets),
        patch("builtins.input", return_value="RESET"),
    ):
        run_reset(args)
    assert not profile.exists()
    assert config.read_bytes() == b"version: '3'\r\nregion: us\r\n"
    assert credentials.read_text(encoding="utf-8") == "private"
    assert (logs / "bot.log").read_text(encoding="utf-8") == "running"
    assert "secret" not in capsys.readouterr().out


def test_reset_rejects_root_and_symlink_targets(tmp_path: Path) -> None:
    with pytest.raises(RemovalError):
        _existing_directory("root", Path(tmp_path.anchor))
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available")
    with pytest.raises(RemovalError, match="符號連結"):
        _existing_directory("profile", link)


def test_reset_custom_profile_requires_browser_when_ngrok_only() -> None:
    args = build_parser().parse_args(["reset", "--ngrok", "--browser-profile", "custom"])
    with pytest.raises(SystemExit, match="--browser-profile"):
        run_reset(args)


def test_ngrok_env_token_is_reported_without_printing_it(capsys: pytest.CaptureFixture[str]) -> None:
    args = SimpleNamespace(all=False, browser=False, ngrok=True, browser_profile=None, yes=True)
    with (
        patch("zeal.reset.collect_reset_targets", return_value=[]),
        patch.dict(os.environ, {"NGROK_AUTHTOKEN": "private-token"}),
    ):
        run_reset(args)
    output = capsys.readouterr().out
    assert "NGROK_AUTHTOKEN" in output
    assert "private-token" not in output
