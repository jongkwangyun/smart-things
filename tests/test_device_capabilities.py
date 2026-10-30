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


if __name__ == "__main__":
    unittest.main()
