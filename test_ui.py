"""Interaction tests run on an isolated, offscreen Qt surface."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtNetwork import QLocalServer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from core import Reading, load_config
from ui import Controller, MenuChoice, Poller
from unittest.mock import patch


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.folder.cleanup)
        self.server = QLocalServer()
        self.server.listen("ChargeDock-test-" + uuid.uuid4().hex)
        args = SimpleNamespace(data_dir=Path(self.folder.name), snapshot=Path("test"),
                               snapshot_theme="light", background=True, smoke_test=None)
        self.controller = Controller(self.app, args, self.server)
        self.panel = self.controller.panel
        self.addCleanup(self.dispose)

    def dispose(self):
        self.controller.cleanup()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_one_tray_and_tool_window(self):
        self.assertEqual(len(self.controller.findChildren(QSystemTrayIcon)), 1)
        self.assertTrue(self.panel.windowFlags() & Qt.WindowType.Tool)

    def test_mouse_focus_has_no_blue_outline_or_layout_shift(self):
        self.panel.show()
        self.app.processEvents()
        button = self.panel.refresh_button
        before = button.geometry()
        with patch.object(self.controller, "refresh"):
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(button.geometry(), before)
        image = button.grab().toImage()
        self.assertFalse(any(image.pixelColor(x, y).name() == self.panel.colors["blue"]
                             for x in range(image.width()) for y in range(image.height())))

    def test_tab_does_not_move_focus_or_draw_a_ring(self):
        self.panel.show()
        self.panel.activateWindow()
        self.app.processEvents()
        self.panel.settings_button.setFocus()
        for key, modifiers in ((Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier),
                               (Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)):
            QTest.keyClick(self.panel.settings_button, key, modifiers)
            self.app.processEvents()
            self.assertIs(self.app.focusWidget(), self.panel.settings_button)
        image = self.panel.settings_button.grab().toImage()
        self.assertFalse(any(image.pixelColor(x, y).name() == self.panel.colors["blue"]
                             for x in range(image.width()) for y in range(image.height())))

    def test_readings_reach_three_cards(self):
        self.controller.receive([Reading("mouse", "鼠标", "", 78),
                                 Reading("keyboard", "键盘", "", 50),
                                 Reading("headset", "耳机", "", 88, approximate=True)], "")
        self.assertEqual(self.panel.cards["mouse"].percentage.text(), "78%")
        self.assertEqual(self.panel.cards["headset"].percentage.text(), "约 88%")
        self.assertNotIn("headset", self.panel.thresholds)

    def test_threshold_and_switch_are_persisted(self):
        self.panel.thresholds["mouse"].setValue(35)
        self.panel.switches["keyboard"].setChecked(False)
        saved = load_config(self.controller.config_path)
        self.assertEqual(saved["alerts"]["mouse"]["threshold"], 35)
        self.assertFalse(saved["alerts"]["keyboard"]["enabled"])
        self.assertFalse(self.panel.thresholds["keyboard"].isEnabled())

    def test_threshold_editor_has_room_for_visible_text(self):
        self.panel.show_settings()
        self.panel.show()
        self.app.processEvents()
        for spin in self.panel.thresholds.values():
            self.assertGreaterEqual(spin.lineEdit().width(), 40)
            self.assertEqual(spin.lineEdit().text(), "20 %")

    def test_switch_clicks_across_the_whole_visual_track(self):
        self.panel.show_settings()
        self.panel.show()
        self.app.processEvents()
        switch = self.panel.switches["mouse"]
        for position in (QPoint(4, 16), QPoint(22, 16), QPoint(39, 16),
                         QPoint(8, 8), QPoint(35, 24), QPoint(42, 16)):
            previous = switch.isChecked()
            QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=position)
            self.app.processEvents()
            self.assertNotEqual(switch.isChecked(), previous, str(position))
            self.assertEqual(load_config(self.controller.config_path)["alerts"]["mouse"]["enabled"], not previous)

    def test_switch_label_is_clickable(self):
        self.panel.show_settings()
        self.panel.show()
        self.app.processEvents()
        switch = self.panel.switches["mouse"]
        caption = next(item for item in self.panel.findChildren(__import__('PySide6.QtWidgets', fromlist=['QLabel']).QLabel)
                       if item.text() == "鼠标")
        before = switch.isChecked()
        QTest.mouseClick(caption, Qt.MouseButton.LeftButton, pos=caption.rect().center())
        self.assertNotEqual(switch.isChecked(), before)

    def test_threshold_arrow_hit_areas_match_the_artwork(self):
        self.panel.show_settings()
        self.panel.show()
        self.app.processEvents()
        spin = self.panel.thresholds["mouse"]
        QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=QPoint(spin.width() - 7, 7))
        self.assertEqual(spin.value(), 25)
        QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=QPoint(spin.width() - 7, spin.height() - 7))
        self.assertEqual(spin.value(), 20)

    def test_header_button_edges_are_clickable(self):
        self.panel.show()
        self.app.processEvents()
        button = self.panel.settings_button
        for point in (QPoint(5, 5), QPoint(18, 18), QPoint(30, 30)):
            self.panel.show_main()
            self.app.processEvents()
            QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=point)
            self.assertEqual(self.panel.stack.currentWidget(), self.panel.settings_page)

    def test_asus_buttons_have_full_click_targets(self):
        self.panel.show_settings()
        self.panel.show()
        self.app.processEvents()
        with patch.object(self.controller, "change_asus") as action:
            for button, command in ((self.panel.asus_enable, "enable"), (self.panel.asus_disable, "disable")):
                button.setEnabled(True)
                for point in (QPoint(7, 7), button.rect().center(), QPoint(button.width()-7, button.height()-7)):
                    action.reset_mock()
                    QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=point)
                    action.assert_called_once_with(command)

    def test_menu_choice_selection_and_full_button_hit_area(self):
        self.panel.show_settings()
        self.panel.show()
        self.app.processEvents()
        choice = next(c for c in self.panel.findChildren(MenuChoice) if c.accessibleName() == "自动刷新")
        for point in (QPoint(5, 16), QPoint(choice.width()-5, 16)):
            QTest.mouseClick(choice, Qt.MouseButton.LeftButton, pos=point)
            self.app.processEvents()
            self.assertTrue(choice.choice_menu.isVisible())
            choice.choice_menu.hide()
        choice.open_menu()
        self.app.processEvents()
        action = choice.choice_menu.actions()[0]
        QTest.mouseClick(choice.choice_menu, Qt.MouseButton.LeftButton,
                         pos=choice.choice_menu.actionGeometry(action).center())
        self.app.processEvents()
        self.assertEqual(choice.currentData(), 30)
        self.assertEqual(load_config(self.controller.config_path)["interval"], 30)

    def test_wake_is_coalesced_and_waits_for_inflight_query(self):
        self.controller.request_wake_refresh()
        self.assertTrue(self.controller.wake_timer.isActive())
        self.controller.poller.active = True
        self.controller.refresh_after_wake()
        self.assertTrue(self.controller.wake_timer.isActive())
        self.controller.poller.active = False
        with patch.object(self.controller, "refresh") as refresh:
            self.controller.refresh_after_wake()
            refresh.assert_called_once()

    def test_headset_only_result_keeps_mouse_and_keyboard(self):
        self.controller.readings["mouse"].level = 78
        self.controller.readings["keyboard"].level = 50
        self.controller.receive([Reading("headset", "耳机", "", 75, approximate=True)], "")
        self.assertEqual(self.controller.readings["mouse"].level, 78)
        self.assertEqual(self.controller.readings["keyboard"].level, 50)
        self.assertEqual(self.controller.readings["headset"].level, 75)

    def test_dark_light_and_settings_navigation(self):
        for theme in ("dark", "light"):
            self.panel.apply_theme(theme)
            self.panel.show_settings()
            self.assertEqual(self.panel.stack.currentWidget(), self.panel.settings_page)
            self.panel.show_main()
            self.assertEqual(self.panel.stack.currentWidget(), self.panel.main_page)

    def test_escape_hides_but_does_not_quit(self):
        self.panel.show()
        self.panel.activateWindow()
        self.app.processEvents()
        QTest.keyClick(self.panel, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertFalse(self.panel.isVisible())
        self.assertTrue(self.server.isListening())

    def test_timeout_preserves_last_reading(self):
        self.controller.readings["mouse"].level = 78
        self.controller.receive([], "设备响应超时")
        self.assertEqual(self.panel.cards["mouse"].percentage.text(), "78%")
        self.assertEqual(self.panel.cards["mouse"].state_text.text(), "上次读取")

    def test_sleep_is_not_zero_battery(self):
        card = self.panel.cards["keyboard"]
        card.render(Reading("keyboard", "夜魔", "AZOTH", error="设备未回复，请唤醒设备"), self.controller.config)
        self.assertEqual(card.percentage.text(), "—")
        self.assertEqual(card.state_text.text(), "等待唤醒")

    def test_worker_timeout_is_bounded(self):
        poller = Poller(Path(self.folder.name))
        results = []
        poller.completed.connect(lambda readings, error: results.append(error))
        self.assertTrue(poller.start())
        self.assertFalse(poller.start())
        poller.watchdog.start(1)
        for _ in range(100):
            QTest.qWait(10)
            if results:
                break
        self.assertTrue(results)
        self.assertIn("超时", results[0])
        self.assertFalse(poller.active)
        poller.stop()

    def test_shutdown_does_not_publish_a_false_read_error(self):
        poller = Poller(Path(self.folder.name))
        results = []
        poller.completed.connect(lambda values, error: results.append(error))
        poller.start()
        poller.stop()
        self.app.processEvents()
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
