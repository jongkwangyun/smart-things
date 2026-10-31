from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


class TestDeviceCapabilitiesDoc(unittest.TestCase):
    """docs/device-capabilities.md의 구조, 실제 응답 근거, 물 잔량 결론 검증 (LB-002)."""

    def setUp(self) -> None:
        self.doc_path = Path("docs/device-capabilities.md")
        self.assertTrue(self.doc_path.is_file(), f"{self.doc_path} 파일이 존재해야 합니다.")
        self.content = self.doc_path.read_text(encoding="utf-8")
        self.lines = self.content.splitlines()

    def test_file_line_limits(self) -> None:
        """문서 파일 크기가 300줄 이하(경고 기준)인지 검증."""
        line_count = len(self.lines)
        self.assertLessEqual(line_count, 300, f"문서 줄 수가 300줄을 초과했습니다: {line_count}")
        self.assertLessEqual(line_count, 500, f"문서 줄 수가 500줄을 초과했습니다: {line_count}")

    def test_device_identification_present(self) -> None:
        """기기 식별 정보(모델명, 디바이스 식별자, UUID)가 포함되어 있는지 검증."""
        self.assertIn("VR7MD97716G", self.content)
        self.assertIn("JETBOT_COMBOT_9X00_24K", self.content)
        self.assertIn("03431570-5acf-f89e-7b39-275ce15d3258", self.content)

    def test_water_level_section_exists(self) -> None:
        """'물 잔량' 절이 독립된 섹션으로 존재하는지 검증 (done_when)."""
        self.assertTrue(
            any(line.strip().startswith("##") and "물 잔량" in line for line in self.lines),
            "docs/device-capabilities.md에 '물 잔량' 제목을 가진 절이 있어야 합니다.",
        )

    def test_water_level_actual_response_json_evidence(self) -> None:
        """'물 잔량' 절에 실제 SmartThings API 응답 JSON 근거가 포함되어 있고 유효한지 검증."""
        # JSON 코드 블록 추출
        json_matches = re.findall(r"```json\s*(\{.*?\})\s*```", self.content, re.DOTALL)
        self.assertGreaterEqual(len(json_matches), 1, "실제 API 응답 JSON 블록이 포함되어야 합니다.")

        parsed_data = None
        for block in json_matches:
            try:
                data = json.loads(block)
                if "components" in data:
                    parsed_data = data
                    break
            except json.JSONDecodeError:
                continue

        self.assertIsNotNone(parsed_data, "응답 JSON 블록이 올바른 JSON 형식이어야 합니다.")
        assert parsed_data is not None

        components = parsed_data.get("components", {})
        self.assertIn("main", components)
        self.assertIn("station", components)

        main_comp = components["main"]
        self.assertIn("samsungce.robotCleanerWaterSprayLevel", main_comp)
        spray_level = main_comp["samsungce.robotCleanerWaterSprayLevel"]
        self.assertIn("waterSprayLevel", spray_level)
        self.assertIn("supportedWaterSprayLevels", spray_level)

    def test_water_level_conclusion_and_requirement_decision(self) -> None:
        """'물 잔량' 절에 명확한 결론('횟수 방식 필요') 및 요구사항 결정이 기록되어 있는지 검증."""
        # done_when: 결론('잔량 방식 가능' 또는 '횟수 방식 필요')
        self.assertIn("횟수 방식 필요", self.content)
        self.assertNotIn("잔량 방식 가능", self.content)

        # must: 결론이 REQ-018 진행 또는 REQ-019–021(조건부) 착수를 결정하므로 근거 응답을 그대로 남긴다 (A-02)
        self.assertIn("REQ-018", self.content)
        self.assertIn("REQ-019", self.content)
        self.assertIn("REQ-020", self.content)
        self.assertIn("REQ-021", self.content)
        self.assertIn("A-02", self.content)

    def test_start_stop_command_section_exists(self) -> None:
        """청소 시작·정지 및 상태 조회 절이 존재하는지 검증 (LB-001 done_when)."""
        self.assertTrue(
            any(line.strip().startswith("##") and "청소기 시작·정지" in line for line in self.lines),
            "docs/device-capabilities.md에 '청소기 시작·정지 및 동작 상태 조회' 절이 있어야 합니다.",
        )

    def test_start_command_request_response_and_departure_evidence(self) -> None:
        """실제 청소 시작 명령 요청/응답 및 4초 후 출발 확인 기록 검증."""
        self.assertIn("samsungce.robotCleanerOperatingState", self.content)
        self.assertIn('"command": "start"', self.content)
        self.assertIn("07e721a4-e2a6-4687-a2a6-686546c0cd86", self.content)
        self.assertIn("COMPLETED", self.content)
        self.assertIn("operatingState", self.content)
        self.assertIn('"processing"', self.content)
        self.assertIn('"cleaning"', self.content)
        self.assertIn("실제 출발 확인 완료", self.content)

    def test_stop_command_request_response_and_homing_evidence(self) -> None:
        """실제 청소 정지/복귀 명령 요청/응답 및 3초 후 정지 확인 기록 검증."""
        self.assertIn('"command": "returnToHome"', self.content)
        self.assertIn("fa3476cd-8b4a-48dd-8ea2-34d04321b267", self.content)
        self.assertIn('"homing"', self.content)
        self.assertIn("실제 정지·복귀 확인 완료", self.content)

    def test_queryable_operating_states_list(self) -> None:
        """조회 가능한 동작 상태값 35종 전체가 문서에 목록으로 존재하는지 검증."""
        expected_states = [
            "homing", "charging", "charged", "chargingForRemainingJob", "moving",
            "cleaning", "paused", "idle", "error", "powerSaving", "factoryReset",
            "relocal", "exploring", "patrol", "monitoring", "manual", "processing",
            "messaging", "emitDust", "findingPet", "calibrating", "welcoming",
            "monitoringAutomation", "washingMop", "sterilizingMop", "dryingMop",
            "supplyingWater", "preparingWater", "spinDrying", "flexCharged",
            "mopWashingPaused", "descaling", "drainingWater", "waitingForDescaling",
            "sabbath",
        ]
        for state in expected_states:
            self.assertIn(f"'{state}'", self.content, f"동작 상태값 {state}가 문서에 명시되어야 합니다.")

    def test_requirements_and_assumptions_references(self) -> None:
        """REQ-005, REQ-016, A-01 참조 및 검증 결론이 문서에 기록되어 있는지 검증."""
        self.assertIn("REQ-005", self.content)
        self.assertIn("REQ-016", self.content)
        self.assertIn("A-01", self.content)

    def test_error_info_section_exists(self) -> None:
        """'오류 정보' 절이 문서에 독립된 절로 존재하는지 검증 (LB-003 done_when)."""
        self.assertTrue(
            any(line.strip().startswith("##") and "오류 정보" in line for line in self.lines),
            "docs/device-capabilities.md에 '오류 정보' 제목을 가진 절이 있어야 합니다.",
        )

    def test_error_info_table_items_and_evidence(self) -> None:
        """오류 정보 표에 배터리 부족, 먼지통, 오프라인 및 실제 응답 예시가 포함되어 있는지 검증."""
        self.assertIn("기기 오프라인", self.content)
        self.assertIn("배터리 부족", self.content)
        self.assertIn("먼지통", self.content)
        self.assertIn("모터 필터", self.content)
        self.assertIn("일반 동작 오류", self.content)
        self.assertIn("/health", self.content)
        self.assertIn('"state":"OFFLINE"', self.content.replace(" ", ""))
        self.assertIn('"battery":', self.content)
        self.assertIn('"dustBagStatus":', self.content)
        self.assertIn("REQ-009", self.content)
        self.assertIn("A-03", self.content)

    def test_impossible_items_marked_and_unknown_cause_rule(self) -> None:
        """확인 불가 항목은 '불가'로 명시하고 앱은 '원인 미확인'과 마지막 상태값을 쓰는지 검증 (must, A-03)."""
        self.assertIn("불가", self.content)
        self.assertIn("원인 미확인", self.content)
        self.assertIn("operatingState", self.content)

    def test_start_origin_section_exists(self) -> None:
        """'시작 원인' 절이 문서에 독립된 절로 존재하는지 검증 (LB-004 done_when)."""
        self.assertTrue(
            any(line.strip().startswith("##") and "시작 원인" in line for line in self.lines),
            "docs/device-capabilities.md에 '시작 원인' 제목을 가진 절이 있어야 합니다.",
        )

    def test_start_origin_comparison_table_and_attributes(self) -> None:
        """시작 원인 절에 반복 예약과 수동 시작의 상태/이벤트 응답 비교가 포함되어 있는지 검증."""
        self.assertIn("반복 예약", self.content)
        self.assertIn("수동 시작", self.content)
        self.assertIn("samsungce.robotCleanerOperatingState", self.content)
        self.assertIn("robotCleanerMovement", self.content)
        self.assertIn("samsungce.robotCleanerReservation", self.content)
        self.assertIn("operatingState", self.content)

    def test_start_origin_conclusion_and_req017_review(self) -> None:
        """시작 원인 결론이 '구분 불가'이고 REQ-017 재검토 필요성이 안내되어 있는지 검증 (must, A-04)."""
        self.assertIn("구분 불가", self.content)
        self.assertIn("REQ-017", self.content)
        self.assertIn("A-04", self.content)
        self.assertIn("재검토", self.content)

    def test_water_usage_section_exists(self) -> None:
        """'물 사용량' 절이 문서에 독립된 절로 존재하는지 검증 (LB-037 done_when)."""
        self.assertTrue(
            any(line.strip().startswith("##") and "물 사용량" in line for line in self.lines),
            "docs/device-capabilities.md에 '물 사용량' 제목을 가진 절이 있어야 합니다.",
        )

    def test_water_usage_capacity_and_confirmed_n(self) -> None:
        """물통 용량, 1회 사용량, 제안 N값 및 확정 N값이 문서에 기록되어 있는지 검증 (LB-037 done_when, must)."""
        # 용량 수치 검증 (청정스테이션 정수통 4.0L, 오수통 3.6L, 본체 80ml)
        self.assertIn("4.0 L", self.content)
        self.assertIn("3.6 L", self.content)
        self.assertIn("80 ml", self.content)
        # 1회 사용량 근거 (약 300 ~ 400 ml)
        self.assertIn("300 ~ 400 ml", self.content)
        # 제안 N값 및 사용자 확정값 (7회)
        self.assertIn("7회", self.content)
        self.assertIn("REQ-021", self.content)
        self.assertIn("A-05", self.content)
        self.assertIn("최종 확정", self.content)



if __name__ == "__main__":
    unittest.main()
