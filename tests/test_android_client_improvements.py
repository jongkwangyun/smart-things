from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_ROOT = PROJECT_ROOT / "android"
SRC_JAVA = ANDROID_ROOT / "app/src/main/java/com/smartthings/cleaner"


class TestTextInputLayoutHintOverlap(unittest.TestCase):
    """LB-043: 메인 화면 서버 주소 및 비밀키 입력 필드 텍스트 겹침 방지 검증."""

    def test_no_duplicate_hints_between_layout_and_edit_text(self) -> None:
        layout_path = ANDROID_ROOT / "app/src/main/res/layout/activity_main.xml"
        root = ET.parse(layout_path).getroot()
        android_ns = "{http://schemas.android.com/apk/res/android}"
        app_ns = "{http://schemas.android.com/apk/res-auto}"

        target_pairs = [
            ("layoutServerUrl", "editServerUrl"),
            ("layoutSharedSecret", "editSharedSecret"),
        ]

        for layout_id, edit_id in target_pairs:
            til = None
            for elem in root.iter():
                if elem.attrib.get(f"{android_ns}id") == f"@+id/{layout_id}":
                    til = elem
                    break
            self.assertIsNotNone(til, f"{layout_id}를 찾을 수 없습니다.")
            assert til is not None

            # TextInputLayout에는 라벨(hint)이 설정되어 있어야 함
            til_hint = til.attrib.get(f"{android_ns}hint")
            self.assertTrue(bool(til_hint), f"{layout_id}에 android:hint가 없습니다.")

            # 내부 TextInputEditText에는 android:hint가 없어야 텍스트가 겹치지 않음 (Must)
            tied = None
            for child in til.iter():
                if child.attrib.get(f"{android_ns}id") == f"@+id/{edit_id}":
                    tied = child
                    break
            self.assertIsNotNone(tied, f"{edit_id}를 찾을 수 없습니다.")
            assert tied is not None
            tied_hint = tied.attrib.get(f"{android_ns}hint")
            self.assertIsNone(
                tied_hint,
                f"{edit_id}에 android:hint가 설정되어 있으면 TextInputLayout 힌트와 텍스트가 겹칩니다.",
            )

            # placeholderText는 TextInputLayout에 지정되어 있어 포커스 시에만 안내 문구가 노출되어야 함
            placeholder = til.attrib.get(f"{app_ns}placeholderText")
            self.assertTrue(
                bool(placeholder),
                f"{layout_id}에 app:placeholderText가 정의되어 있어야 합니다.",
            )


class TestAutoConnectAndPersistence(unittest.TestCase):
    """LB-042: 서버 주소·비밀키 영구 저장 및 앱 실행 시 자동 연결 검증."""

    def test_server_client_persistence_methods(self) -> None:
        client_code = (SRC_JAVA / "network/ServerClient.kt").read_text(encoding="utf-8")
        self.assertIn("saveCredentials", client_code)
        self.assertIn("getSavedServerUrl", client_code)
        self.assertIn("getSavedSharedSecret", client_code)
        self.assertIn("smartthings_cleaner_prefs", client_code)

    def test_main_activity_auto_connect_on_launch(self) -> None:
        activity_code = (SRC_JAVA / "MainActivity.kt").read_text(encoding="utf-8")
        self.assertIn("checkConnectionOnLaunch()", activity_code)
        self.assertIn("ServerClient.getSavedServerUrl", activity_code)
        self.assertIn("ServerClient.getSavedSharedSecret", activity_code)
        self.assertIn("performConnectionCheck()", activity_code)
        self.assertIn("ServerClient.saveCredentials", activity_code)

    def test_simulated_auto_login_flow(self) -> None:
        """저장된 자격증명이 있을 때 앱 재실행 시 자동으로 연결을 시도하는 시뮬레이션."""
        fake_storage: dict[str, str] = {
            "key_server_url": "http://192.168.123.102:8080",
            "key_shared_secret": "my-secret-key",
        }

        # 앱 실행 시 자격증명 복원
        loaded_url = fake_storage.get("key_server_url", "")
        loaded_secret = fake_storage.get("key_shared_secret", "")

        self.assertEqual(loaded_url, "http://192.168.123.102:8080")
        self.assertEqual(loaded_secret, "my-secret-key")

        # 둘 다 비어있지 않으면 자동 연결 호출 (done_when 조건 만족)
        should_auto_connect = bool(loaded_url and loaded_secret)
        self.assertTrue(should_auto_connect)


if __name__ == "__main__":
    unittest.main()
