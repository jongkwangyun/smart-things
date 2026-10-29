#!/usr/bin/env python3
"""PreToolUse 훅: backlog.json 을 tools/backlog.py 없이 읽거나 고치려는 도구 호출을 차단한다.

stdin 으로 훅 입력(JSON)을 받는다. 차단하면 안내문을 stderr 로 내보내고 exit 2 로 끝난다.
입력을 해석하지 못하면 exit 1 (차단하지 않는 오류)로 끝난다.
"""
import io
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "backlog.json"
MESSAGE = "백로그는 tools/backlog.py로만 읽고 수정할 수 있습니다. list/set/validate를 쓰세요."

PATH_KEYS = ("file_path", "path", "notebook_path")
NAME_RE = re.compile(r"backlog\.json", re.I)
TOOL_RE = re.compile(r"tools[\\/]+backlog\.py", re.I)
REDIRECT_RE = re.compile(r">\s*\S*backlog\.json", re.I)
GIT_OK_RE = re.compile(r"\s*git\s+(add|commit|status)\b")
SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|&\r\n]")
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to):\s*(.+?)\s*$", re.M)


def is_target(raw, cwd):
    if not isinstance(raw, str) or not raw.strip():
        return False
    p = Path(raw)
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    try:
        return os.path.normcase(os.path.realpath(p)) == os.path.normcase(os.path.realpath(TARGET))
    except OSError:
        return False


def shell_violation(cmd):
    """backlog.json 을 언급하는 명령 조각이 tools/backlog.py 호출이 아니면 위반이다."""
    for seg in SEGMENT_SPLIT_RE.split(cmd):
        if not NAME_RE.search(seg):
            continue
        if TOOL_RE.search(seg):
            if REDIRECT_RE.search(seg):
                return True
            continue
        if GIT_OK_RE.match(seg):
            continue
        return True
    return False


def violates(data):
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    cwd = data.get("cwd")
    if any(is_target(tool_input.get(k), cwd) for k in PATH_KEYS):
        return True
    glob = tool_input.get("glob")
    if isinstance(glob, str) and NAME_RE.search(glob):
        return True
    cmd = tool_input.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    if isinstance(cmd, str):
        if any(is_target(m, cwd) for m in PATCH_FILE_RE.findall(cmd)):
            return True
        return shell_violation(cmd)
    return False


def main():
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as e:
        print(f"block_backlog_access: 훅 입력을 해석하지 못함 ({e})", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print("block_backlog_access: 훅 입력이 JSON 객체가 아님", file=sys.stderr)
        sys.exit(1)
    if violates(data):
        print(MESSAGE, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
