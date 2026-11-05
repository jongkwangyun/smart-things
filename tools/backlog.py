#!/usr/bin/env python3
"""backlog.json 관리 도구. 표준 라이브러리만 사용한다.

  py tools/backlog.py list
  py tools/backlog.py show LB-001
  py tools/backlog.py find <검색어>
  py tools/backlog.py add --title ... --done-when ... --must ... --where ... --category ... --priority ...
  py tools/backlog.py set LB-001 in_progress
  py tools/backlog.py memo LB-001 "기록할 내용"
  py tools/backlog.py validate
"""
import argparse
import io
import json
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import NoReturn

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "backlog.json"

FIELDS = [
    "id", "reqs", "status", "priority", "category", "title", "done_when",
    "where", "parent", "deps", "doc", "done_at", "must", "memo",
]
TEXT_FIELDS = ("title", "done_when", "where", "must")
NULLABLE_TEXT_FIELDS = ("parent", "doc", "done_at", "memo")
LIST_FIELDS = ("reqs", "deps")
ENUM_FIELDS = ("status", "priority", "category")
META_FIELDS = ("updated", "context_doc", "note")
SEARCH_FIELDS = ("id", "title", "done_when", "must", "memo")
INLINE_LISTS = set(LIST_FIELDS)

ID_RE = re.compile(r"^LB-\d{3}$")
REQ_RE = re.compile(r"^REQ-\d{3}$")


def die(msg: str) -> NoReturn:
    print(f"오류: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------- 입출력 ----------

def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        die(f"파일이 없음: {path}")
    except json.JSONDecodeError as e:
        die(f"JSON 문법 오류: {path} ({e})")


def _scalar(v):
    return json.dumps(v, ensure_ascii=False)


def dump(obj, level=0, key=None):
    """기존 backlog.json 서식(들여쓰기 2칸, reqs/deps는 한 줄)을 유지해 직렬화한다."""
    pad = "  " * (level + 1)
    end = "  " * level
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = [f"{pad}{_scalar(k)}: {dump(v, level + 1, k)}" for k, v in obj.items()]
        return "{\n" + ",\n".join(items) + "\n" + end + "}"
    if isinstance(obj, list):
        if not obj:
            return "[]"
        if key in INLINE_LISTS:
            return _scalar(obj)
        items = [pad + dump(v, level + 1) for v in obj]
        return "[\n" + ",\n".join(items) + "\n" + end + "]"
    return _scalar(obj)


def save(path, data):
    data["meta"]["updated"] = date.today().isoformat()
    text = dump(data) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".backlog-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def spec_path(path, data):
    meta = data.get("meta")
    name = meta.get("context_doc") if isinstance(meta, dict) else None
    return path.parent / name if isinstance(name, str) and name else None


def load_spec_reqs(path):
    """SPEC.md의 `### REQ-NNN` 항목을 {id: Deprecated 여부}로 읽는다. 파일이 없으면 None."""
    if path is None or not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    reqs = {}
    for m in re.finditer(r"^### (REQ-\d{3})\b.*?(?=^#{1,3} |\Z)", text, re.M | re.S):
        reqs[m.group(1)] = bool(re.search(r"^Status:\s*Deprecated", m.group(0), re.M))
    return reqs


# ---------- 검증 ----------

def find_cycle(graph):
    state = {}

    def visit(n, path):
        state[n] = 1
        for m in graph.get(n, []):
            if state.get(m) == 1:
                return path[path.index(m):] + [m]
            if m not in state:
                found = visit(m, path + [m])
                if found:
                    return found
        state[n] = 2
        return None

    for n in graph:
        if n not in state:
            found = visit(n, [n])
            if found:
                return found
    return None


def validate(data, spec_reqs=None):
    errors = []
    if not isinstance(data, dict):
        return ["최상위가 객체가 아님"]
    if data.get("$schema_version") != "1":
        errors.append("$schema_version 이 \"1\"이 아님")

    meta = data.get("meta")
    if not isinstance(meta, dict):
        errors.append("meta 누락 또는 객체가 아님")
    else:
        for k in META_FIELDS:
            if not isinstance(meta.get(k), str) or not meta[k].strip():
                errors.append(f"meta.{k} 누락 또는 빈 값")

    valid = {}
    enums = data.get("enums")
    if not isinstance(enums, dict):
        errors.append("enums 누락 또는 객체가 아님")
    else:
        for k in ENUM_FIELDS:
            v = enums.get(k)
            if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
                errors.append(f"enums.{k} 누락 또는 문자열 배열이 아님")
            elif len(set(v)) != len(v):
                errors.append(f"enums.{k} 에 중복 값이 있음")
            else:
                valid[k] = v

    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        errors.append("tasks 누락 또는 배열이 아님")
        return errors

    seen = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            errors.append(f"tasks[{i}]: 객체가 아님")
            continue
        tid = t.get("id")
        label = tid if isinstance(tid, str) else f"tasks[{i}]"
        if not isinstance(tid, str) or not ID_RE.match(tid):
            errors.append(f"{label}: id 형식이 LB-NNN 이 아님")
        elif tid in seen:
            errors.append(f"{label}: id 중복")
        else:
            seen.add(tid)

        for f in FIELDS:
            if f not in t:
                errors.append(f"{label}: 필드 누락 '{f}'")
        for f in t:
            if f not in FIELDS:
                errors.append(f"{label}: 알 수 없는 필드 '{f}'")
        for f in ENUM_FIELDS:
            if f in t and f in valid and t[f] not in valid[f]:
                errors.append(f"{label}: {f} 값 '{t[f]}' 이(가) enums 에 없음")
        for f in TEXT_FIELDS:
            if f in t and (not isinstance(t[f], str) or not t[f].strip()):
                errors.append(f"{label}: {f} 이(가) 비어 있음")
        for f in NULLABLE_TEXT_FIELDS:
            if f in t and t[f] is not None and not isinstance(t[f], str):
                errors.append(f"{label}: {f} 은(는) 문자열 또는 null 이어야 함")
        for f in ("doc", "memo"):
            if t.get(f) == "":
                errors.append(f"{label}: {f} 이(가) 빈 문자열 (없으면 null)")
        for f in LIST_FIELDS:
            if f in t and (not isinstance(t[f], list) or not all(isinstance(x, str) for x in t[f])):
                errors.append(f"{label}: {f} 은(는) 문자열 배열이어야 함")

        task_reqs = t.get("reqs")
        for r in task_reqs if isinstance(task_reqs, list) else []:
            if isinstance(r, str) and not REQ_RE.match(r):
                errors.append(f"{label}: reqs 의 '{r}' 가 REQ-NNN 형식이 아님")

        done_at = t.get("done_at")
        if isinstance(done_at, str):
            try:
                date.fromisoformat(done_at)
            except ValueError:
                errors.append(f"{label}: done_at '{done_at}' 가 YYYY-MM-DD 형식이 아님")
        if "status" in t and "done_at" in t:
            if t["status"] == "done" and done_at is None:
                errors.append(f"{label}: status 가 done 인데 done_at 이 null")
            if t["status"] != "done" and done_at is not None:
                errors.append(f"{label}: status 가 done 이 아닌데 done_at 이 있음")

    parent_graph, dep_graph = {}, {}
    for t in tasks:
        if not isinstance(t, dict) or not isinstance(t.get("id"), str):
            continue
        tid = t["id"]
        parent = t.get("parent")
        if isinstance(parent, str):
            if parent == tid:
                errors.append(f"{tid}: parent 가 자기 자신")
            elif parent not in seen:
                errors.append(f"{tid}: parent '{parent}' 가 존재하지 않음")
            else:
                parent_graph[tid] = [parent]
        deps = t.get("deps")
        if isinstance(deps, list):
            ok = []
            for d in deps:
                if not isinstance(d, str):
                    continue
                if d == tid:
                    errors.append(f"{tid}: deps 에 자기 자신이 있음")
                elif d not in seen:
                    errors.append(f"{tid}: deps '{d}' 가 존재하지 않음")
                else:
                    ok.append(d)
            dep_graph[tid] = ok
    for name, graph in (("parent", parent_graph), ("deps", dep_graph)):
        cycle = find_cycle(graph)
        if cycle:
            errors.append(f"{name} 순환 참조: {' -> '.join(cycle)}")

    if spec_reqs is not None:
        for t in tasks:
            if not isinstance(t, dict) or not isinstance(t.get("reqs"), list):
                continue
            label = t.get("id", "?")
            for r in t["reqs"]:
                if not isinstance(r, str) or not REQ_RE.match(r):
                    continue
                if r not in spec_reqs:
                    errors.append(f"{label}: {r} 이(가) SPEC.md 에 없음")
                elif spec_reqs[r] and t.get("status") != "done":
                    errors.append(f"{label}: {r} 은(는) Deprecated 인데 진행 대상 작업에 연결됨")
    return errors


# ---------- 명령 ----------

def line(t):
    return f"{t.get('id')} · {t.get('status')} · {t.get('title')}"


def split_list(values):
    out: list[str] = []
    for v in values or []:
        out.extend(p.strip() for p in v.split(",") if p.strip())
    return out


def cmd_list(data, args, path):
    for t in data["tasks"]:
        print(line(t))


def cmd_show(data, args, path):
    tid = args.id.upper()
    task = next((t for t in data["tasks"] if t.get("id") == tid), None)
    if task is None:
        die(f"없는 id: {args.id}")
    for f in FIELDS:
        v = task.get(f)
        if isinstance(v, list):
            v = ", ".join(v) if v else "(없음)"
        elif v is None:
            v = "null"
        print(f"{f:<10} {v}")


def cmd_find(data, args, path):
    q = " ".join(args.query).lower()
    hits = 0
    for t in data["tasks"]:
        matched = [f for f in SEARCH_FIELDS if isinstance(t.get(f), str) and q in t[f].lower()]
        if matched:
            hits += 1
            print(line(t))
            print(f"    일치: {', '.join(matched)}")
    if not hits:
        print("일치하는 작업 없음")


def cmd_add(data, args, path):
    nums = [int(t["id"][3:]) for t in data["tasks"]
            if isinstance(t.get("id"), str) and ID_RE.match(t["id"])]
    n = max(nums, default=0) + 1
    if n > 999:
        die("LB-999 를 넘는 id 는 만들 수 없음")
    values = {
        "id": f"LB-{n:03d}",
        "reqs": split_list(args.reqs),
        "status": "todo",
        "priority": args.priority,
        "category": args.category,
        "title": args.title.strip(),
        "done_when": args.done_when.strip(),
        "where": args.where.strip(),
        "parent": args.parent.upper() if args.parent else None,
        "deps": [d.upper() for d in split_list(args.deps)],
        "doc": args.doc,
        "done_at": None,
        "must": args.must.strip(),
        "memo": None,
    }
    task = {f: values[f] for f in FIELDS}
    data["tasks"].append(task)
    errors = validate(data, load_spec_reqs(spec_path(path, data)))
    own = [e for e in errors if e.startswith(task["id"] + ":")]
    if own:
        die("추가 거부 — " + "; ".join(own))
    save(path, data)
    print(f"추가됨: {line(task)}")


def cmd_set(data, args, path):
    tid = args.id.upper()
    task = next((t for t in data["tasks"] if t.get("id") == tid), None)
    if task is None:
        die(f"없는 id: {args.id}")
    allowed = data.get("enums", {}).get("status", [])
    if args.status not in allowed:
        die(f"없는 status: {args.status} (가능: {', '.join(allowed)})")
    old = task["status"]
    if old == args.status:
        print(f"{tid}: 이미 {old} — 변경 없음")
        return
    task["status"] = args.status
    task["done_at"] = date.today().isoformat() if args.status == "done" else None
    save(path, data)
    print(f"{tid}: {old} -> {args.status}")


def cmd_memo(data, args, path):
    tid = args.id.upper()
    task = next((t for t in data["tasks"] if t.get("id") == tid), None)
    if task is None:
        die(f"없는 id: {args.id}")
    text = " ".join(args.text).strip()
    if args.clear:
        if text or args.replace:
            die("--clear 는 텍스트나 --replace 와 함께 쓸 수 없음")
        task["memo"] = None
        save(path, data)
        print(f"{tid}: memo 삭제됨")
        return
    if not text:
        die("기록할 텍스트가 없음 (지우려면 --clear)")
    entry = f"[{date.today().isoformat()}] {text}"
    if args.replace or not task.get("memo"):
        task["memo"] = entry
    else:
        task["memo"] = task["memo"] + "\n" + entry
    save(path, data)
    print(f"{tid}: memo {'교체' if args.replace else '기록'}됨")


def cmd_deps(data, args, path):
    tid = args.id.upper()
    task = next((t for t in data["tasks"] if t.get("id") == tid), None)
    if task is None:
        die(f"없는 id: {args.id}")
    ids = [d.upper() for d in split_list(args.ids)]
    modes = sum(bool(x) for x in (args.add, args.remove, args.clear))
    if modes > 1 or (args.clear and ids) or (not args.clear and not ids):
        die("사용법: deps <id> <선행id...> (교체) | --add | --remove | --clear (id 없이)")
    old = list(task.get("deps") or [])
    if args.clear:
        new: list[str] = []
    elif args.add:
        new = old + [d for d in ids if d not in old]
    elif args.remove:
        new = [d for d in old if d not in ids]
    else:
        new = list(dict.fromkeys(ids))
    spec = load_spec_reqs(spec_path(path, data))
    before = set(validate(data, spec))
    task["deps"] = new
    added = [e for e in validate(data, spec) if e not in before]
    if added:
        die("deps 변경 거부 — " + "; ".join(added))
    save(path, data)
    print(f"{tid}: deps {', '.join(old) or '(없음)'} -> {', '.join(new) or '(없음)'}")


def cmd_validate(data, args, path):
    spec = load_spec_reqs(spec_path(path, data))
    if spec is None:
        print("경고: SPEC.md 를 찾지 못해 REQ 존재 검사를 건너뜀", file=sys.stderr)
    errors = validate(data, spec)
    if errors:
        for e in errors:
            print(f"  - {e}")
        print(f"INVALID ({len(errors)}개 오류)")
        sys.exit(1)
    print("VALID")


def build_parser():
    p = argparse.ArgumentParser(description="backlog.json 관리 도구")
    p.add_argument("--file", type=Path, default=DEFAULT_FILE, help="대상 backlog.json (기본: 저장소 루트)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="모든 작업을 id · status · title 로 출력").set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="작업 전체 내용 출력")
    s.add_argument("id")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("find", help="id/title/done_when/must/memo 에서 검색")
    s.add_argument("query", nargs="+")
    s.set_defaults(fn=cmd_find)

    s = sub.add_parser("add", help="다음 LB-NNN 으로 작업 추가")
    for name in ("title", "done-when", "must", "where", "category", "priority"):
        s.add_argument(f"--{name}", required=True)
    s.add_argument("--parent")
    s.add_argument("--deps", nargs="*", help="공백 또는 쉼표로 구분")
    s.add_argument("--reqs", nargs="*", help="공백 또는 쉼표로 구분")
    s.add_argument("--doc")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("set", help="status 변경")
    s.add_argument("id")
    s.add_argument("status")
    s.set_defaults(fn=cmd_set)

    s = sub.add_parser("memo", help="memo 기록 (기본: 날짜를 붙여 기존 memo 뒤에 추가)")
    s.add_argument("id")
    s.add_argument("text", nargs="*")
    s.add_argument("--replace", action="store_true", help="기존 memo 를 교체")
    s.add_argument("--clear", action="store_true", help="memo 를 null 로 비움")
    s.set_defaults(fn=cmd_memo)

    s = sub.add_parser("deps", help="deps(선행 작업) 변경 (기본: 교체)")
    s.add_argument("id")
    s.add_argument("ids", nargs="*", help="공백 또는 쉼표로 구분")
    s.add_argument("--add", action="store_true", help="기존 deps 에 추가")
    s.add_argument("--remove", action="store_true", help="기존 deps 에서 제거")
    s.add_argument("--clear", action="store_true", help="deps 를 비움")
    s.set_defaults(fn=cmd_deps)

    sub.add_parser("validate", help="필드·enum·id·참조 검사").set_defaults(fn=cmd_validate)
    return p


def main():
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    path = args.file.resolve()
    args.fn(load(path), args, path)


if __name__ == "__main__":
    main()
