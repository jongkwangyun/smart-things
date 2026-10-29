from __future__ import annotations

import unittest
from datetime import UTC, datetime

from server.time_utils import KST, format_kst, now_kst, parse_kst, to_kst


class TestTimeUtils(unittest.TestCase):
    def test_kst_timezone_offset(self) -> None:
        now = now_kst()
        self.assertIsNotNone(now.tzinfo)
        offset = now.utcoffset()
        self.assertIsNotNone(offset)
        assert offset is not None
        self.assertEqual(offset.total_seconds(), 9 * 3600)

    def test_to_kst_from_naive(self) -> None:
        naive = datetime(2026, 10, 29, 15, 30, 0)
        kst_dt = to_kst(naive)
        self.assertEqual(kst_dt.tzinfo, KST)
        self.assertEqual(kst_dt.hour, 15)

    def test_to_kst_from_utc(self) -> None:
        utc_dt = datetime(2026, 10, 29, 6, 0, 0, tzinfo=UTC)
        kst_dt = to_kst(utc_dt)
        self.assertEqual(kst_dt.tzinfo, KST)
        self.assertEqual(kst_dt.hour, 15)
        self.assertEqual(kst_dt.day, 29)

    def test_format_and_parse_kst(self) -> None:
        orig = datetime(2026, 11, 1, 9, 30, 0, tzinfo=KST)
        iso = format_kst(orig)
        self.assertIn("+09:00", iso)

        parsed = parse_kst(iso)
        self.assertEqual(parsed, orig)
        self.assertEqual(parsed.tzinfo, KST)

    def test_parse_kst_from_naive_string(self) -> None:
        parsed = parse_kst("2026-11-01T09:30:00")
        self.assertEqual(parsed.tzinfo, KST)
        self.assertEqual(parsed.hour, 9)


if __name__ == "__main__":
    unittest.main()
