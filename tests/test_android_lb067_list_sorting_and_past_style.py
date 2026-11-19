from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_ROOT = PROJECT_ROOT / "android"
RES_LAYOUT = ANDROID_ROOT / "app/src/main/res/layout"
RES_VALUES = ANDROID_ROOT / "app/src/main/res/values"
SRC_JAVA = ANDROID_ROOT / "app/src/main/java/com/smartthings/cleaner"
TEST_JAVA = ANDROID_ROOT / "app/src/test/java/com/smartthings/cleaner"


class TestAndroidLb067ListSortingAndPastStyle(unittest.TestCase):
    """LB-067: 예약·쉬는 기간 목록 시작 시각 내림차순 정렬 및 지난 항목 회색 배경 표시 검증."""

    def test_file_line_counts_under_300(self) -> None:
        files = [
            SRC_JAVA / "ListDisplayLogic.kt",
            SRC_JAVA / "ReservationListActivity.kt",
            SRC_JAVA / "OffPeriodListActivity.kt",
            TEST_JAVA / "ListDisplayLogicTest.kt",
            RES_LAYOUT / "item_reservation.xml",
            RES_LAYOUT / "item_off_period.xml",
            RES_VALUES / "colors.xml",
            Path(__file__),
        ]
        for path in files:
            self.assertTrue(path.is_file(), f"파일 누락: {path}")
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(line_count, 300, f"{path.name} 줄 수 초과 ({line_count} > 300)")

    def test_colors_xml_defines_past_card_colors(self) -> None:
        colors_tree = ET.parse(RES_VALUES / "colors.xml")
        colors = {elem.attrib["name"]: elem.text for elem in colors_tree.findall("color")}
        self.assertIn("surface_past", colors)
        self.assertIn("border_past", colors)
        self.assertNotEqual(colors["surface_past"], colors["surface"])

    def test_item_layouts_expose_card_ids(self) -> None:
        ns = "http://schemas.android.com/apk/res/android"
        res_root = ET.parse(RES_LAYOUT / "item_reservation.xml").getroot()
        off_root = ET.parse(RES_LAYOUT / "item_off_period.xml").getroot()
        self.assertEqual(res_root.attrib.get(f"{{{ns}}}id"), "@+id/cardReservationItem")
        self.assertEqual(off_root.attrib.get(f"{{{ns}}}id"), "@+id/cardOffPeriodItem")

    def test_activities_wire_sorting_and_past_background(self) -> None:
        res_kt = (SRC_JAVA / "ReservationListActivity.kt").read_text(encoding="utf-8")
        self.assertIn("ListDisplayLogic.partitionAndSortReservations", res_kt)
        self.assertIn("ListDisplayLogic.isPastReservation", res_kt)
        self.assertIn("R.color.surface_past", res_kt)

        off_kt = (SRC_JAVA / "OffPeriodListActivity.kt").read_text(encoding="utf-8")
        self.assertIn("ListDisplayLogic.sortOffPeriodsDescending", off_kt)
        self.assertIn("ListDisplayLogic.isPastOffPeriod", off_kt)
        self.assertIn("R.color.surface_past", off_kt)


if __name__ == "__main__":
    unittest.main()
