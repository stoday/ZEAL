"""Install the packaged skill without modifying existing skills implicitly."""

from __future__ import annotations

import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any


def install_skill(args: Any) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.dest:
        root = args.dest.expanduser().resolve()
    else:
        base = Path.home() if args.global_install else Path.cwd()
        if args.platform == "claude":
            root = base / ".claude" / "skills"
        elif args.platform == "antigravity" and args.global_install:
            root = base / ".gemini" / "config" / "skills"
        else:
            root = base / ".agents" / "skills"
    destination = root / "zeal"
    if destination.exists() and not args.force:
        raise SystemExit(f"Skill already exists at {destination}; use --force only after reviewing it.")
    resource = files("zeal").joinpath("skills", "zeal")
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("SKILL.md", "references/agent-cli.md"):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(resource.joinpath(*name.split("/")).read_bytes())
    print(json.dumps({"skill": "zeal", "path": str(destination), "scope": "global" if args.global_install else "custom" if args.dest else "project"}, ensure_ascii=False))


def add_skill_parser(commands: Any) -> None:
    parser = commands.add_parser("install-skill", help="Install the ZEAL skill for a coding agent.")
    parser.add_argument("platform", nargs="?", choices=("codex", "claude", "antigravity"))
    parser.add_argument("--dest", type=Path, help="Custom skills root; ZEAL creates its zeal subdirectory.")
    parser.add_argument("--global", dest="global_install", action="store_true")
    parser.add_argument("--force", action="store_true", help="Overwrite packaged skill files after reviewing existing content.")
    parser.set_defaults(handler=run_install)


def run_install(args: Any) -> None:
    if bool(args.platform) == bool(args.dest) or (args.dest and args.global_install):
        raise SystemExit("Choose one platform or --dest; --global is only for a platform preset.")
    install_skill(args)
