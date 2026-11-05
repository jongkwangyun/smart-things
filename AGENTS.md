# 명령어

- 이 섹션에는 pyproject.toml 에 정의된 명령만 적는다.
- 개발 도구 설치(dependency-groups.dev: ruff, mypy): `uv sync --no-python-downloads`
- lint: `.venv\Scripts\python.exe -m ruff check .`
- 타입 체크: `.venv\Scripts\python.exe -m mypy`
- build: `.venv\Scripts\python.exe -m compileall -q -x '[\\/](\.venv|\.git|node_modules|\.mypy_cache|\.ruff_cache)([\\/]|$)' .`

# 구조

- BRIEF.md — 프로젝트 목적, 사용자, 제약, 성공 기준의 기준.
- SPEC.md — 요구사항(REQ-NNN), 결정(D-NN), 가정(A-NN), 수용 기준(AC-N)의 기준.
- backlog.json — 구현 작업(LB-NNN)의 상태, 완료 조건(done_when), 핵심 규칙(must), 연결된 REQ(reqs)의 기준.
- tools/backlog.py — backlog.json 을 읽고 쓰는 유일한 도구. 하위 명령은 list, show, find, add, set, memo, deps, validate.
- pyproject.toml — 개발 도구(ruff, mypy) 설정과 Stop 훅의 검사 명령·코드 파일 크기 기준(300줄 초과 경고, 500줄 초과 실패)의 기준.
- .codex/hooks/block_backlog_access.py — backlog.json 을 tools/backlog.py 없이 읽거나 고치는 도구 호출을 막는 PreToolUse 훅.
- .codex/hooks/stop_quality_gate.py — 응답 종료 전에 lint, build, 타입 체크, 코드 파일 크기를 검사하는 Stop 훅.
- .codex/settings.json, .claude/settings.json — 위 두 훅을 Codex, Claude Code 에 각각 등록하는 설정.
- uv.lock — `uv sync` 가 만든 의존성 잠금 파일.
- server/, android/, docs/ — backlog.json 의 where 가 가리키는 구현·문서 위치. 아직 만들어지지 않았다.

# 항상 지킬 것

- backlog.json 은 손으로 고치지 않고 tools/backlog.py 로만 읽고 쓴다.
- 작업 시작 시 `py tools/backlog.py set <id> in_progress`, 완료 시 `set <id> done` 으로 상태를 바꾼다.
- 작업을 done 또는 blocked 로 바꿀 때, must 와 다르게 구현했을 때는 `py tools/backlog.py memo <id> "내용"` 으로 memo 에 기록한다.
- 작업의 완료 판단 기준은 그 작업의 done_when 이고, 구현 중 지킬 규칙은 must 이다.
- 완료 전에 실제 결과를 검증한다. UI 는 화면, 데이터는 저장값이나 API 응답, 로직은 테스트나 실행 결과로 확인한다.
- 검증하지 못한 작업은 완료로 처리하지 않고, 검증하지 못한 항목을 보고에 명시한다.
- 기기(VR7MD97716G) 동작에 대한 주장은 실제 호출한 결과만 근거로 삼는다. 문서나 추정으로 채우지 않는다.
- 스마트싱스 토큰, 공유 비밀키, Firebase 인증 파일은 저장소에 커밋하지 않는다.
- SPEC.md 의 `# Out of Scope` 에 적힌 항목은 구현하지 않는다.
- 모든 일시는 한국 표준시(KST) 기준으로 다룬다.
- SPEC.md 의 REQ ID 는 재정렬하거나 다른 의미로 재사용하지 않는다. 제거되는 요구는 `Status: Deprecated` 로 남긴다.
- backlog.json 을 바꾼 뒤에는 `py tools/backlog.py validate` 가 `VALID` 를 출력하는지 확인한다.
- 코드 파일은 500줄을 넘기지 않는다. 300줄을 넘으면 경고다. generated, migrations, vendor 등은 제외한다.
- Stop 훅이 종료를 막으면 출력된 실패(lint, build, typecheck, size)를 고친 뒤 다시 종료한다.
- 훅을 바꾸면 .codex/settings.json 과 .claude/settings.json 에 같은 내용으로 반영한다.

# 막히면

- Windows 에서는 `python3` 대신 `py` 로 실행한다. `python3` 은 Microsoft Store 스텁일 수 있다.
- lint 나 타입 체크가 `No module named ruff` 또는 `No module named mypy` 로 실패하면 `uv sync --no-python-downloads` 로 .venv 를 만든다.
- 도구가 오류를 내면 `py tools/backlog.py validate` 를 먼저 실행해 backlog.json 이 깨졌는지 확인한다.
- status 는 todo, in_progress, blocked, done 만 쓸 수 있다. id 는 `LB-` 뒤 숫자 3자리 형식이다.
- 작업이 선행 작업에 막히면 deps 에 적힌 작업의 상태를 `py tools/backlog.py show <id>` 로 확인한다.
- 기기 API 관련 작업이 막히면 docs/device-capabilities.md 가 있는지 먼저 확인하고, 있으면 그 내용을 읽는다.
- 집 밖 접속이 안 되면 미니PC 전원과 서버 상태 확인 주소 응답, 공유 비밀키, 스마트싱스 인증 만료 여부를 순서대로 확인한다.
- 위 확인으로 풀리지 않으면 `py tools/backlog.py set <id> blocked` 로 상태를 바꾸고, 막힌 원인과 시도한 것을 memo 에 기록한 뒤 사용자에게 보고한다.
