#!/usr/bin/env python3
"""Stop 훅: 응답을 끝내기 전에 lint, build, 타입 체크, 코드 파일 크기를 검사한다.

설정은 프로젝트 루트 pyproject.toml 의 [tool.stop_hook] 에서 읽는다.
실패가 하나라도 있으면 내용을 stderr 로 내보내고 exit 2 로 종료를 막는다.
이 훅 때문에 이어진 응답(stop_hook_active)에서는 같은 실패로 다시 막지 않고,
남은 실패를 경고(systemMessage)로 알린 뒤 종료를 허용한다. 경고만 있을 때도 종료는 허용한다.

    python stop_quality_gate.py [--root 프로젝트루트]    (훅 입력 JSON 은 stdin)
"""
import argparse
import fnmatch
import io
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
VENV_PYTHONS = (".venv/Scripts/python.exe", ".venv/bin/python")
CHECKS = ("lint", "build", "typecheck")
MAX_DETAIL_LINES = 40
MAX_LISTED_FILES = 20


@dataclass
class Failure:
    name: str
    summary: str
    detail: str = ""


def read_hook_input() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config(root: Path) -> dict[str, Any]:
    with open(root / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    return data.get("tool", {}).get("stop_hook", {})


def resolve_python(root: Path) -> str:
    for rel in VENV_PYTHONS:
        if (root / rel).is_file():
            return str(root / rel)
    return sys.executable


def tail(text: str) -> str:
    lines = text.strip().splitlines()
    if len(lines) > MAX_DETAIL_LINES:
        lines = [f"... (앞 {len(lines) - MAX_DETAIL_LINES}줄 생략)", *lines[-MAX_DETAIL_LINES:]]
    return "\n".join(lines)


def run_check(name: str, cmd: Any, root: Path, python: str, timeout: int) -> Failure | None:
    if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
        return Failure(name, f"pyproject.toml 의 [tool.stop_hook.commands] 에 {name} 명령이 없음")
    argv = [a.replace("{python}", python) for a in cmd]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NO_COLOR="1")
    try:
        proc = subprocess.run(argv, cwd=root, capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return Failure(name, f"명령을 실행할 수 없음: {argv[0]}")
    except subprocess.TimeoutExpired:
        return Failure(name, f"{timeout}초 안에 끝나지 않음: {' '.join(argv)}")
    if proc.returncode == 0:
        return None
    output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    return Failure(name, f"exit {proc.returncode}: {' '.join(argv)}", tail(output))


def count_lines(path: Path) -> int:
    with open(path, "rb") as f:
        return sum(1 for _ in f)


def check_sizes(root: Path, cfg: dict[str, Any]) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    warn_at = int(cfg.get("size_warn_lines", 300))
    fail_at = int(cfg.get("size_fail_lines", 500))
    exts = tuple(e.lower() for e in cfg.get("code_extensions", [".py"]))
    skip_dirs = set(cfg.get("size_exclude_dirs", []))
    skip_globs = list(cfg.get("size_exclude_globs", []))
    warns: list[tuple[str, int]] = []
    fails: list[tuple[str, int]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for filename in filenames:
            if not filename.lower().endswith(exts):
                continue
            if any(fnmatch.fnmatch(filename, g) for g in skip_globs):
                continue
            path = Path(dirpath) / filename
            try:
                lines = count_lines(path)
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            if lines > fail_at:
                fails.append((rel, lines))
            elif lines > warn_at:
                warns.append((rel, lines))
    return warns, fails


def format_sizes(entries: list[tuple[str, int]]) -> str:
    shown = [f"- {rel}: {lines}줄" for rel, lines in sorted(entries, key=lambda e: -e[1])[:MAX_LISTED_FILES]]
    if len(entries) > MAX_LISTED_FILES:
        shown.append(f"- ... 외 {len(entries) - MAX_LISTED_FILES}개")
    return "\n".join(shown)


def run_all(root: Path) -> tuple[list[Failure], str]:
    """실패 목록과 크기 경고 문구를 돌려준다."""
    try:
        cfg = load_config(root)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return [Failure("config", f"pyproject.toml 을 읽지 못함: {e}")], ""
    python = resolve_python(root)
    timeout = int(cfg.get("command_timeout_seconds", 90))
    commands = cfg.get("commands", {})
    failures = [f for name in CHECKS if (f := run_check(name, commands.get(name), root, python, timeout))]
    warns, fails = check_sizes(root, cfg)
    fail_at = int(cfg.get("size_fail_lines", 500))
    warn_at = int(cfg.get("size_warn_lines", 300))
    if fails:
        failures.append(Failure("size", f"{fail_at}줄 초과 코드 파일 {len(fails)}개", format_sizes(fails)))
    warning = f"{warn_at}줄 초과 코드 파일 {len(warns)}개 (경고)\n{format_sizes(warns)}" if warns else ""
    return failures, warning


def format_failures(failures: list[Failure]) -> str:
    parts = []
    for f in failures:
        parts.append(f"## {f.name}: {f.summary}" + (f"\n{f.detail}" if f.detail else ""))
    return "\n\n".join(parts)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Stop 훅: 종료 전 품질 검사")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    root = parser.parse_args().root.resolve()
    active = bool(read_hook_input().get("stop_hook_active"))

    failures, warning = run_all(root)
    notes = []
    if failures:
        names = ", ".join(f.name for f in failures)
        report = f"[stop-gate] 종료 전 검사 실패: {names}\n\n{format_failures(failures)}"
        if warning:
            report += f"\n\n## size 경고: {warning}"
        if not active:
            print(report + "\n\n위 실패를 고친 뒤 다시 종료하세요.", file=sys.stderr)
            sys.exit(2)
        notes.append(report + "\n\n(이미 한 번 막은 뒤라 종료는 허용합니다. 실패가 남아 있습니다.)")
    elif warning:
        notes.append(f"[stop-gate] {warning}")
    if notes:
        print(json.dumps({"systemMessage": "\n\n".join(notes)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
