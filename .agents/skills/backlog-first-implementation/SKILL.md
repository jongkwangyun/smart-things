---
name: backlog-first-implementation
description: 이 저장소에서 코드나 파일을 바꾸는 구현 요청(기능 추가, 수정, 버그 수정, 리팩터링)을 받으면, 코드 변경 전에 tools/backlog.py 로 backlog 를 확인하고 작업을 재사용하거나 등록한 뒤 in_progress, 검증, done 순서로 진행한다. 질문, 조사, 리뷰, 계획만 하는 요청에는 쓰지 않는다.
---

# 적용 범위

- 코드나 파일을 바꾸는 요청에만 적용한다. 질문, 조사, 리뷰, 계획만 하는 요청은 backlog 를 확인하거나 등록하지 않는다.
- 대화가 계획에서 구현으로 넘어가면 그 시점부터 적용한다.
- backlog.json 은 직접 읽거나 고치지 않고 `py tools/backlog.py` 로만 다룬다.

# 순서

1. 코드 변경 전에 `py tools/backlog.py find <키워드>` 로 기존 작업을 찾는다. 동의어로 2~3번 검색하고, 필요하면 `list` 로 전체를 훑는다. 후보는 `show <id>` 로 done_when, must, memo 를 읽는다.
2. 같은 요구의 `todo` 또는 `in_progress` 작업이 있으면 새로 만들지 않고 재사용한다.
   - 같은 요구의 `blocked` 작업이 있으면 memo 의 막힌 이유를 사용자에게 알리고 지시를 받는다.
   - 같은 요구의 `done` 작업이 있으면 아래 "done 작업과 같은 요구"를 따른다.
3. 없으면 독립적으로 검증 가능한 단위로 나눠 `add` 한다.
   `py tools/backlog.py add --title "..." --done-when "..." --must "..." --where "..." --category "..." --priority "..." [--reqs REQ-NNN] [--deps LB-NNN] [--parent LB-NNN]`
   - done_when 은 화면이나 파일에서 눈으로 확인할 수 있는 결과로 쓴다.
   - must 는 지킬 규칙을 한두 줄로 쓴다.
   - category, priority 는 같은 영역의 기존 작업을 `show` 해서 같은 값을 쓴다.
   - reqs 에는 SPEC.md 에서 직접 관련된 REQ 만 넣는다.
   - 요구가 SPEC.md 의 `# Out of Scope` 에 해당하면 등록하지 말고 사용자에게 알린다.
4. 구현을 시작하기 전에 `py tools/backlog.py set <id> in_progress` 로 바꾼다. deps 중 done 이 아닌 작업이 있으면 먼저 처리하거나 사용자에게 알린다.
5. 구현한 뒤 done_when 을 실제 결과로 검증한다. UI 는 화면, 데이터는 저장값이나 API 응답, 로직은 테스트나 실행 결과로 확인한다. must 와 다르게 구현했다면 `py tools/backlog.py memo <id> "..."` 로 기록한다.
6. 검증된 경우에만 `py tools/backlog.py memo <id> "검증: <실행한 것과 결과>"` 를 남기고 `set <id> done` 으로 바꾼다.
   - 검증하지 못했으면 done 으로 바꾸지 않는다. `in_progress` 로 두고 최종 보고에 미검증 항목을 적는다.
   - 막혔으면 memo 에 이유를 적고 `set <id> blocked` 로 바꾼다.
7. backlog 를 바꾼 뒤에는 `py tools/backlog.py validate` 가 `VALID` 를 출력하는지 확인한다.

# done 작업과 같은 요구

기존 done 작업의 상태는 바꾸지 않는다.

1. `show <id>` 의 done_when 으로 현재 충족 여부를 실제로 확인한다.
2. 충족하면 새 작업을 만들지 않고 이미 충족한다고 보고한다.
3. 충족하지 않거나(회귀) 요구가 늘었으면(추가 요구) 새 작업을 `add` 한다. 새 id 를 받은 뒤 양쪽에 memo 로 잇는다.
   - `py tools/backlog.py memo <기존 id> "회귀 또는 추가 요구: <새 id> <이유>"`
   - `py tools/backlog.py memo <새 id> "회귀 또는 추가 요구: 기존 <기존 id>"`
