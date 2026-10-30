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
        "waterSprayLevel": {
          "value": "medium",
          "timestamp": "2026-10-30T11:45:00.000Z"
        },
        "supportedWaterSprayLevels": {
          "value": [
            "high",
            "mediumHigh",
            "medium",
            "mediumLow",
            "low"
          ],
          "timestamp": "2026-10-30T11:45:00.000Z"
        }
      }
    },
    "station": {
      "custom.hepaFilter": {
        "filterStatus": {
          "value": "normal",
          "timestamp": "2026-10-30T11:45:00.000Z"
        }
      },
      "samsungce.robotCleanerDustBag": {
        "dustBagStatus": {
          "value": "normal",
          "timestamp": "2026-10-30T11:45:00.000Z"
        }
      }
    },
    "refill-drainage-kit": {
      "samsungce.drainFilter": {
        "filterStatus": {
          "value": "normal",
          "timestamp": "2026-10-30T11:45:00.000Z"
        }
      },
      "samsungce.connectionState": {
        "connectionState": {
          "value": "disconnected",
          "timestamp": "2026-10-30T11:45:00.000Z"
        }
      },
      "samsungce.activationState": {
        "activationState": {
          "value": "inactive",
          "timestamp": "2026-10-30T11:45:00.000Z"
        }
      }
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
