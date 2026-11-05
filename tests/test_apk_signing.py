"""APK 서명이 모든 빌드에서 같게 유지되는지 검사한다. 서명이 바뀌면 폰에서 업데이트 설치가 거부된다."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRADLE = ROOT / "android" / "app" / "build.gradle.kts"
KEYSTORE = ROOT / "android" / "app" / "smartthings-debug.keystore"
PINNED = ROOT / "android" / "app" / "signing.sha256"
WORKFLOW = ROOT / ".github" / "workflows" / "build-apk.yml"


class ApkSigningTest(unittest.TestCase):
    def test_debug_build_uses_committed_keystore(self) -> None:
        text = GRADLE.read_text(encoding="utf-8")
        self.assertIn('file("smartthings-debug.keystore")', text)
        self.assertIn('signingConfig = signingConfigs.getByName("debug")', text)
        self.assertTrue(KEYSTORE.is_file())

    def test_pinned_fingerprint_is_sha256_hex(self) -> None:
        value = PINNED.read_text(encoding="utf-8").strip()
        self.assertRegex(value, r"^[0-9a-f]{64}$")

    def test_ci_verifies_pinned_fingerprint(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("signing.sha256", text)
        self.assertIn("apksigner", text)

    def test_version_code_increases_in_ci(self) -> None:
        self.assertIn("GITHUB_RUN_NUMBER", GRADLE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
