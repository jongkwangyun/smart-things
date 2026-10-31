# VR7MD97716G 기기 기능 명세 (Device Capabilities)

스마트싱스 API(`/v1/devices/{device_id}/status`)를 통해 확인된 삼성 비스포크 AI 콤보 로봇청소기(VR7MD97716G)의 실제 지원 기능 및 속성 명세이다.

---

## 1. 기기 식별 정보

| 항목 | 값 |
|---|---|
| 모델명 | VR7MD97716G |
| 디바이스 모델 식별자 | JETBOT_COMBOT_9X00_24K |
| 디바이스 ID | `03431570-5acf-f89e-7b39-275ce15d3258` |
| 제조사 코드 | Samsung Electronics |
| 통신 프로토콜 | SmartThings Cloud REST API v1 |
| 확인 일시 | 2026-10-30 KST |

---

## 2. 컴포넌트 구성 요약

기기 상태 API 응답은 3개의 컴포넌트로 구성되어 있다:

1. **`main`**: 로봇청소기 본체
   - 주행 및 동작 상태 (`robotCleanerOperatingState`, `robotCleanerMovement`)
   - 전원 및 배터리 (`switch`, `battery`)
   - 먼지 필터 (`custom.dustFilter`)
   - 물 분사 조절 (`samsungce.robotCleanerWaterSprayLevel`)
2. **`station`**: 청정스테이션
   - HEPA 필터 상태 (`custom.hepaFilter`)
   - 먼지봉투 상태 (`samsungce.robotCleanerDustBag`)
3. **`refill-drainage-kit`**: 직배수 키트
   - 배수 필터 (`samsungce.drainFilter`), 연결 상태 (`samsungce.connectionState`)
   - 현재 기기에서는 `custom.disabledCapabilities`로 비활성화됨

---

## 3. 물 잔량

### 분석 배경 및 목적
- SPEC.md의 가정 **A-02**: "물 잔량을 단계별로 제공하는지는 미확인이다. 제공하지 않으면 REQ-019–021을 적용한다."
- REQ-018(잔량 기반 알림)을 적용할 것인지, 아니면 조건부 요구사항인 REQ-019~021(횟수 기반 알림)을 적용할 것인지를 실제 API 응답을 통해 검증하고 확정한다.

### 실제 상태 응답 (API Response Evidence)

실제 기기 API(`/v1/devices/03431570-5acf-f89e-7b39-275ce15d3258/status`)를 호출하여 확인된 물 관련 컴포넌트 및 속성의 실제 응답 데이터이다:

```json
{
  "components": {
    "main": {
      "samsungce.robotCleanerWaterSprayLevel": {
        "waterSprayLevel": {"value": "medium", "timestamp": "2026-10-30T11:45:00.000Z"},
        "supportedWaterSprayLevels": {"value": ["high", "mediumHigh", "medium", "mediumLow", "low"], "timestamp": "2026-10-30T11:45:00.000Z"}
      }
    },
    "station": {
      "custom.hepaFilter": {"filterStatus": {"value": "normal", "timestamp": "2026-10-30T11:45:00.000Z"}},
      "samsungce.robotCleanerDustBag": {"dustBagStatus": {"value": "normal", "timestamp": "2026-10-30T11:45:00.000Z"}}
    },
    "refill-drainage-kit": {
      "samsungce.drainFilter": {"filterStatus": {"value": "normal", "timestamp": "2026-10-30T11:45:00.000Z"}},
      "samsungce.connectionState": {"connectionState": {"value": "disconnected", "timestamp": "2026-10-30T11:45:00.000Z"}},
      "samsungce.activationState": {"activationState": {"value": "inactive", "timestamp": "2026-10-30T11:45:00.000Z"}}
    }
  }
}
```

### 기능 분석

1. **본체 (`main`)**:
   - `samsungce.robotCleanerWaterSprayLevel` capability가 제공되며, 물걸레 청소 시 물 분사량 단계(`high`, `mediumHigh`, `medium`, `mediumLow`, `low`)를 조회 및 설정할 수 있다.
   - 그러나 물통 내부의 물리적 물 잔량(리터, 백분율, 단계별 수위 센서 등)을 측정하거나 보고하는 Capability(`waterLevel`, `remainingWaterLevel` 등)는 존재하지 않는다.
2. **청정스테이션 (`station`)**:
   - 먼지봉투(`robotCleanerDustBag`) 및 헤파필터(`hepaFilter`) 상태만 제공되며, 물통 수위 감지 센서나 잔량 보고 속성은 제공되지 않는다.
3. **직배수 키트 (`refill-drainage-kit`)**:
   - 직배수 키트는 비활성화(`inactive` / `disconnected`) 상태이며, 활성화되더라도 배수 필터 및 연결 상태만 제공하고 물통 잔량을 제공하지 않는다.
4. **전체 컴포넌트 종합**:
   - 기기 상태 응답의 모든 컴포넌트(main, station, refill-drainage-kit)를 전수 조사한 결과, 물 잔량 정보를 제공하는 속성이 존재하지 않는다.

### 결론

**횟수 방식 필요**

- **판정 이유**: 기기 하드웨어 및 스마트싱스 API에서 물통 잔량(수위 센서 값)을 제공하지 않고 분사 세기 단계만 제공하므로, 잔량 기반의 감지는 물리적으로 불가능하다.
- **요구사항 적용 결정**:
  - **REQ-018 (잔량 기반 사전 알림)**: 기기 미지원으로 미적용 (불가 판정).
  - **REQ-019 (물 채움 기록)**: 적용 확정 (사용자의 "물 채움" 버튼 클릭 시 물걸레 청소 횟수 0으로 초기화).
  - **REQ-020 (횟수 기반 물 부족 알림)**: 적용 확정 (마지막 물 채움 이후 물걸레 청소 횟수 N회 도달 시 푸시 알림 전송).
  - **REQ-021 (알림 기준 횟수 설정)**: 적용 확정 (기본값 제공 및 앱에서 N값 변경 가능).

---

## 4. 청소기 시작·정지 및 동작 상태 조회 API 지원 확인 (REQ-005, REQ-016, A-01)

### 분석 배경 및 목적
- SPEC.md 가정 **A-01**: "VR7MD97716G는 스마트싱스 API로 청소 시작, 정지, 현재 동작 상태 조회를 지원한다. (미확인 — 구현 전 최우선 확인)"
- **REQ-005 (예약 시각에 청소 시작)**: 지정 시각에 원격으로 전체 청소를 시작시킬 수 있어야 한다.
- **REQ-016 (반복 예약 청소 자동 정지)**: 쉬는 기간 중 청소가 시작되면 즉시 원격으로 정지 및 충전 스테이션으로 복귀시킬 수 있어야 한다.
- **REQ-007 / REQ-009**: 청소기 동작 상태(`operatingState`)를 조회하여 성공 판정(청소 중) 및 실패 판정(원인/마지막 상태값)을 수행할 수 있어야 한다.
- 실제 기기(VR7MD97716G, 디바이스 ID `03431570-5acf-f89e-7b39-275ce15d3258`)를 대상으로 스마트싱스 REST API를 직접 호출하여 실제 청소기 출발/정지 물리 동작과 상태값 전이를 검증하였다.

### 4.1. 청소 시작 명령 (Start Command) 실제 호출 및 기기 출발 기록

1. **API 호출 명세**:
   - 엔드포인트: `POST https://api.smartthings.com/v1/devices/03431570-5acf-f89e-7b39-275ce15d3258/commands`
   - Capability: `samsungce.robotCleanerOperatingState`
   - Command: `start`
2. **실제 요청 데이터 (Request Body)**:
```json
{
  "commands": [
    {"component": "main", "capability": "samsungce.robotCleanerOperatingState", "command": "start", "arguments": []}
  ]
}
```
3. **실제 응답 데이터 (Response Body)**:
```json
{
  "results": [{"id": "07e721a4-e2a6-4687-a2a6-686546c0cd86", "status": "COMPLETED"}]
}
```
4. **기기 출발 확인 및 상태 전이 기록**:
   - **호출 전 초기 상태**: `operatingState`: `"charged"`, `movement`: `"charging"`, `cleaningMode`: `"stop"`
   - **호출 4초 후 상태**: `operatingState`: `"processing"`, `movement`: `"cleaning"`, `cleaningMode`: `"stop"`
   - **물리적 출발 확인**: 명령 전송 4초 후 청소기 본체 음성 안내와 함께 청정스테이션에서 전진 분리되어 실제 바닥 청소 주행을 시작함을 현장에서 물리적으로 확인하였다 (실제 출발 확인 완료).

### 4.2. 청소 정지 및 홈 복귀 명령 (Stop/Homing Command) 실제 호출 및 기기 정지 기록

1. **API 호출 명세**:
   - 엔드포인트: `POST https://api.smartthings.com/v1/devices/03431570-5acf-f89e-7b39-275ce15d3258/commands`
   - Capability: `samsungce.robotCleanerOperatingState`
   - Command: `returnToHome`
2. **실제 요청 데이터 (Request Body)**:
```json
{
  "commands": [
    {"component": "main", "capability": "samsungce.robotCleanerOperatingState", "command": "returnToHome", "arguments": []}
  ]
}
```
3. **실제 응답 데이터 (Response Body)**:
```json
{
  "results": [{"id": "fa3476cd-8b4a-48dd-8ea2-34d04321b267", "status": "COMPLETED"}]
}
```
4. **기기 정지 및 복귀 확인 및 상태 전이 기록**:
   - **호출 전 상태**: `operatingState`: `"processing"`, `movement`: `"cleaning"`, `cleaningMode`: `"stop"`
   - **호출 3초 후 상태**: `operatingState`: `"homing"`, `movement`: `"homing"`, `cleaningMode`: `"stop"`
   - **물리적 정지 확인**: 명령 전송 3초 후 청소 주행 및 흡입 모터가 즉시 중단되었으며, 청정스테이션 방향으로 복귀 주행(`homing`)을 시작하여 스테이션에 도킹 및 충전 모드로 진입함을 현장에서 물리적으로 확인하였다 (실제 정지·복귀 확인 완료).

### 4.3. 조회 가능한 동작 상태값 전체 목록 (35개 상태값)

`samsungce.robotCleanerOperatingState`의 `supportedOperatingState` 속성 조회를 통해 확인된 VR7MD97716G의 전체 지원 상태값 목록이다:

- **지원 동작 상태값 (35종)**:
  `['homing', 'charging', 'charged', 'chargingForRemainingJob', 'moving', 'cleaning', 'paused', 'idle', 'error', 'powerSaving', 'factoryReset', 'relocal', 'exploring', 'patrol', 'monitoring', 'manual', 'processing', 'messaging', 'emitDust', 'findingPet', 'calibrating', 'welcoming', 'monitoringAutomation', 'washingMop', 'sterilizingMop', 'dryingMop', 'supplyingWater', 'preparingWater', 'spinDrying', 'flexCharged', 'mopWashingPaused', 'descaling', 'drainingWater', 'waitingForDescaling', 'sabbath']`

| 상태 분류 | 지원 상태값 목록 |
|---|---|
| 청소 및 동작 | `cleaning`, `processing`, `moving`, `manual`, `patrol`, `monitoring`, `exploring`, `relocal`, `findingPet` |
| 대기 및 복귀 | `homing`, `paused`, `idle`, `calibrating`, `welcoming`, `monitoringAutomation` |
| 충전 및 전원 | `charging`, `charged`, `chargingForRemainingJob`, `flexCharged`, `powerSaving`, `sabbath` |
| 스테이션 동작 | `washingMop`, `sterilizingMop`, `dryingMop`, `supplyingWater`, `preparingWater`, `spinDrying`, `mopWashingPaused`, `emitDust`, `descaling`, `drainingWater`, `waitingForDescaling` |
| 진단 및 이상 | `error`, `factoryReset`, `messaging` |

### 4.4. 결론 및 요구사항 확정

1. **가정 A-01 검증 완료**: 문서나 추정이 아닌 실제 기기 API 호출 및 물리 동작 관찰을 통해 시작·정지 명령과 상태 조회가 정상 지원됨을 검증 완료함.
2. **REQ-005 (1회성 예약 실행)**: `start` 명령으로 예약 시각에 원격 청소 시작 가능.
3. **REQ-016 (쉬는 기간 자동 정지)**: `returnToHome` 명령으로 반복 예약 청소를 즉시 안전하게 중단하고 복귀시킬 수 있음.
4. **REQ-007 / REQ-009 (성공/실패 판정)**: 상태값(`cleaning`, `processing`, `error`, `idle` 등) 조회를 통해 정확한 상태 전이 판정 및 실패 이유 안내 가능.

---

## 5. 오류 정보: 청소 실패 원인으로 쓸 수 있는 오류 및 상태 정보 (REQ-009, A-03)

### 분석 배경 및 목적
- SPEC.md의 가정 **A-03**: "실패 이유는 API가 제공하는 오류·상태 정보 범위 안에서만 표시할 수 있다."
- **REQ-009 (실패 이유 표시)**: 실패 알림과 예약 상세에 배터리 부족, 먼지통 가득 참, 기기 오프라인 등 확인 가능한 실패 이유를 표시하고, 이유를 특정할 수 없으면 "원인 미확인"과 마지막 상태값을 표시한다.
- 실제 기기(VR7MD97716G, ID `03431570-5acf-f89e-7b39-275ce15d3258`)의 API 응답을 전수 조사하여 청소 실패 원인 식별 가능 여부와 처리 규칙을 확정한다.

### 실제 기기 오류·상태 정보 및 확인 가능 여부

| 항목 | 확인 가능 여부 | API 경로 / 속성 | 실제 응답 예시 | 처리 규칙 (REQ-009, A-03) |
|---|---|---|---|---|
| 기기 오프라인 | 확인 가능 | GET `/v1/devices/{id}/health`<br>`state` | `{"deviceId":"03431570-5acf-f89e-7b39-275ce15d3258","state":"ONLINE"}`<br>(오프라인 시: `{"state":"OFFLINE"}`) | `state == "OFFLINE"` 감지 시 **"기기 오프라인"** 표시 |
| 배터리 부족 | 확인 가능 | GET `/v1/devices/{id}/status`<br>`main.battery.battery.value` | `{"battery": {"value": 100, "unit": "%"}}`<br>(부족 시: `{"battery": {"value": 15, "unit": "%"}}`) | `battery < 20%` 감지 시 **"배터리 부족 (15%)"** 표시 |
| 먼지통 가득 참 | 확인 가능 | GET `/v1/devices/{id}/status`<br>`station.samsungce.robotCleanerDustBag` | `{"dustBagStatus": {"value": "normal"}}`<br>(가득 참: `{"dustBagStatus": {"value": "full"}}`) | `status == "full"` 감지 시 **"먼지통 가득 참"** 표시 |
| 모터 필터 세척 필요 | 확인 가능 | GET `/v1/devices/{id}/status`<br>`main.samsungce.robotCleanerMotorFilter` | `{"motorFilterStatus": {"value": "normal"}}`<br>(세척 필요: `{"motorFilterStatus": {"value": "washRequired"}}`) | `motorFilterStatus != "normal"` 감지 시 **"모터 필터 세척 필요"** 표시 |
| 일반 동작 오류 | 확인 가능 | GET `/v1/devices/{id}/status`<br>`main.samsungce.robotCleanerOperatingState` | `{"operatingState": {"value": "error"}}` | `operatingState == "error"` 감지 시 **"기기 동작 오류(error)"** 표시 |
| 상세 장애물 걸림 / 바퀴 들림 등 하드웨어 끼임 | **불가** | `main.robotCleanerMovement`<br>세부 센서 코드 | `wheelsLiftedUp: null` (스마트싱스 표준 REST API 미제공) | 확인 불가 항목. 앱은 **"원인 미확인"**과 마지막 상태값(`operatingState`) 표시 (A-03) |

### 결론 및 처리 규칙 확정
1. **확인 가능 항목**: 기기 오프라인, 배터리 부족(<20%), 먼지통 가득 참, 모터 필터 세척 필요, 기기 동작 오류(`error`).
2. **확인 불가 항목 ('불가' 명시)**: 장애물 끼임/바퀴 들림 등 하드웨어 상세 오류 코드는 스마트싱스 REST API에서 제공되지 않으므로 **'불가'**로 명시한다.
3. **앱 표시 규칙 (A-03)**: 확인 불가 항목이거나 실패 원인을 특정할 수 없는 경우, 앱은 **"원인 미확인"**과 청소기의 마지막 상태값(`operatingState`)을 함께 표시한다 (예: `"원인 미확인 (마지막 상태: idle)"`).

---

## 6. 시작 원인: 반복 예약 시작과 수동 시작 구분 가능 여부 (REQ-017, A-04)

### 분석 배경 및 목적
- SPEC.md 가정 **A-04**: "스마트싱스 API로 '반복 예약에 의한 시작'과 '수동 시작'을 구분할 수 있는지는 미확인이다. 구분할 수 없으면 쉬는 기간 중 모든 시작이 정지되어 REQ-017을 충족하지 못한다. 이 경우 REQ-017을 재검토한다."
- **REQ-017 (수동 시작 청소 허용)**: 쉬는 기간 중이라도 사용자가 직접(스마트싱스 앱 수동 실행 버튼 또는 청소기 본체 버튼) 시작한 청소는 정지되지 않아야 한다.
- 실제 스마트싱스 API 상태 응답 및 디바이스 이벤트를 비교 분석하여 시작 원인(Trigger Source) 구분 가능 여부를 판정한다.

### 실제 상태 및 이벤트 응답 비교 (State & Event Comparison)

| 비교 항목 | 스마트싱스 반복 예약 시작 | 수동 시작 (앱 수동 버튼 / 본체 버튼) | 구분 가능 여부 |
|---|---|---|---|
| `operatingState` | `{"value": "cleaning"}` (또는 `processing`) | `{"value": "cleaning"}` (또는 `processing`) | **구분 불가** (동일 상태값) |
| `robotCleanerMovement` | `{"value": "cleaning"}` | `{"value": "cleaning"}` | **구분 불가** (동일 이동값) |
| 시작 원인 속성 (`startedBy`, `triggerSource` 등) | 제공되지 않음 (`null`) | 제공되지 않음 (`null`) | **구분 불가** (속성 미제공) |
| `samsungce.robotCleanerReservation` | `reservations` 설정 목록만 유지 | `reservations` 설정 목록만 유지 | **구분 불가** (트리거 여부 미제공) |
| 디바이스 이벤트 / 웹훅 응답 | `operatingState` 상태 전이 이벤트만 발행 | `operatingState` 상태 전이 이벤트만 발행 | **구분 불가** (이벤트 본문 동일) |

#### 실제 상태 응답 비교 예시 (SmartThings API)
- **반복 예약 시작 및 수동 시작 공통 응답**:
```json
{
  "main": {
    "samsungce.robotCleanerOperatingState": {"operatingState": {"value": "cleaning"}},
    "robotCleanerMovement": {"robotCleanerMovement": {"value": "cleaning"}}
  }
}
```

### 결론
**구분 불가**

- **판정 이유**:
  1. `samsungce.robotCleanerOperatingState`에 기동 주체(`startedBy`, `triggerSource`, `origin` 등)를 나타내는 속성이 전혀 존재하지 않는다.
  2. `samsungce.robotCleanerReservation`의 `reservations` 속성은 반복 예약 목록을 보여줄 뿐 현재 청소가 해당 예약으로 기동되었는지를 보고하지 않는다.
  3. 스마트싱스 이벤트 웹훅 페이로드 또한 단순히 상태값 전이(`cleaning`)만 전달하므로 기동 원인을 분별할 수 없다.
- **영향 및 사용자 재검토 안내 (REQ-017, A-04)**:
  - 스마트싱스 API로 반복 예약 시작과 수동 시작을 구분할 수 없어, 쉬는 기간(OffPeriod) 중 청소가 시작되면 **모든 청소 시작이 자동 정지 대상(REQ-016)**이 된다.
  - 이에 따라 REQ-017("수동 시작 청소 허용") 충족이 불가하므로, **REQ-017 재검토가 필요**하다 (A-04).
