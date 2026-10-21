import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import string
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from PySide6.QtNetwork import QLocalServer
from core import Reading, validate_config, load_config
import i18n
from ui import Controller


class TranslationTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("zh-CN")

    def test_four_catalogues_have_the_same_keys_and_placeholders(self):
        source = i18n.catalogue("zh-CN")
        self.assertGreaterEqual(len(source), 100)
        fields = lambda text: {field for _, field, _, _ in string.Formatter().parse(text) if field}
        for code in ("zh-CN", "ja-JP", "en-US", "ko-KR"):
            catalogue = i18n.catalogue(code)
            self.assertEqual(set(catalogue), set(source))
            for key, value in catalogue.items():
                self.assertTrue(value.strip(), (code, key))
                self.assertEqual(fields(key), fields(value), (code, key))

    def test_follow_system_and_unsupported_locale_fallback(self):
        for locale, expected in (("ja_JP", "ja-JP"), ("zh_TW", "zh-CN"),
                                 ("ko-KR", "ko-KR"), ("fr_FR", "en-US")):
            with patch.object(i18n, "system_language", return_value=locale):
                self.assertEqual(i18n.set_language("system"), expected)

    def test_formatting_preserves_numbers(self):
        i18n.set_language("en-US")
        self.assertEqual(i18n.tr("{device}剩余 {level}%", device="Mouse", level=17), "Mouse: 17% remaining")

    def test_missing_key_and_translation_fallback(self):
        i18n.set_language("ko-KR")
        self.assertEqual(i18n.tr("custom-device-name"), "custom-device-name")
        real = i18n.catalogue
        with patch.object(i18n, "catalogue", side_effect=lambda code: {} if code == "ko-KR" else real(code)):
            self.assertEqual(i18n.tr("已连接"), "Connected")

    def test_backend_error_prefix_is_translated_without_changing_detail(self):
        i18n.set_language("en-US")
        self.assertEqual(i18n.translate_message("读取失败：OS error 5"), "Read failed: OS error 5")

    def test_invalid_language_settings_are_sanitized(self):
        for bad in ([], {}, 3, None, "not-a-language"):
            self.assertEqual(validate_config({"language": bad})["language"], "system")
        self.assertEqual(validate_config({"language": "ja-JP"})["language"], "ja-JP")


class LanguageUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.directory = Path(self.folder.name)
        (self.directory / "settings.json").write_text('{"language":"zh-CN"}', encoding="utf-8")
        self.server = QLocalServer()
        self.server.listen("ChargeDock-language-" + uuid.uuid4().hex)
        args = SimpleNamespace(data_dir=self.directory, snapshot=Path("test"), snapshot_theme="light",
                               background=True, smoke_test=None)
        self.controller = Controller(self.app, args, self.server)
        self.panel = self.controller.panel

    def tearDown(self):
        self.controller.cleanup()
        self.panel.deleteLater()
        self.app.processEvents()
        self.folder.cleanup()
        i18n.set_language("zh-CN")

    def test_language_choice_updates_labels_menu_and_saved_config(self):
        self.panel.show_settings()
        choice = self.panel.language_choice
        for code in ("en-US", "ja-JP", "ko-KR", "zh-CN"):
            choice.setCurrentIndex(choice.findData(code))
            self.assertEqual(self.panel.heading.text(), i18n.tr("偏好设置"))
            self.assertEqual(self.controller.menu.actions()[0].text(), i18n.tr("显示电量"))
            self.assertEqual(self.panel.asus_disable.text(), i18n.tr("关闭显示"))
            self.assertEqual(load_config(self.controller.config_path)["language"], code)
            self.assertEqual(len(self.controller.findChildren(QSystemTrayIcon)), 1)

    def test_language_switch_preserves_readings_and_does_not_alert_or_poll(self):
        self.controller.receive([Reading("mouse", "魔刃 X", "", 74),
            Reading("keyboard", "夜魔", "", 50), Reading("headset", "赛睿耳机", "", 75, approximate=True)], "")
        with patch.object(self.controller.tray, "showMessage") as alert, \
             patch.object(self.controller.poller, "start") as poll:
            self.controller.setting("language", "en-US")
            alert.assert_not_called(); poll.assert_not_called()
        self.assertEqual(self.panel.cards["headset"].percentage.text(), "~ 75%")
        self.assertEqual(self.panel.cards["mouse"].state_text.text(), "Connected")
        self.assertEqual(self.panel.cards["headset"].title.text(), "Headset")
        self.assertEqual(self.panel.language_choice.currentData(), "en-US")
        self.assertIn("Chakram X: 74%", self.controller.tray.toolTip())

    def test_runtime_errors_are_retranslated(self):
        self.panel.settings_status.setText("未更改成功：未找到奥创电量通知组件")
        self.controller.setting("language", "en-US")
        # The explicit language change writes its own saved confirmation.
        self.panel.settings_status.setText("未更改成功：未找到奥创电量通知组件")
        self.assertEqual(self.panel.settings_status.text(), "Change failed: Armoury Crate battery component not found")


if __name__ == "__main__":
    unittest.main()
