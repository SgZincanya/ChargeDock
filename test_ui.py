"""Interaction tests run on an isolated, offscreen Qt surface."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QTimer
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
        (Path(self.folder.name) / "settings.json").write_text('{"language":"zh-CN"}', encoding="utf-8")
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
        self.controller.deleteLater()
        self.server.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def dji_fixture(self):
        import time
        from dataclasses import asdict
        from unittest.mock import Mock
        from dji_protocol import Status, readings
        from test_dji import status_frame
        status=Status(); status.decode(status_frame(((1,1,False),(2,5,True))))
        for tx in status.tx.values(): tx['product']='DJI Mic Mini 2'
        raw=json.loads(json.dumps({'heartbeat':time.time(),'protocol':2,'settings':status.settings,
            'tx':status.tx,'readings':[asdict(r) for r in readings(status)]}))
        client=SimpleNamespace(state=raw,pending_id=None,submit=Mock(return_value=True),stop=Mock(),refresh=Mock())
        self.controller.dji=client
        self.controller.receive_dji(readings(status),raw)
        return client

    def test_dji_card_uses_category_not_percent_and_details_keep_unit(self):
        self.dji_fixture()
        card=self.panel.cards['mic2']
        self.assertEqual(card.percentage.text(),'电量偏低')
        self.assertNotIn('%',card.percentage.text())
        self.panel.show(); self.app.processEvents()
        QTest.mouseClick(card.details_button,Qt.MouseButton.LeftButton)
        self.assertEqual(self.panel.dji_page.unit,2)
        self.assertIs(self.panel.stack.currentWidget(),self.panel.dji_page)

    def test_dji_control_is_not_optimistic_and_voice_tone_targets_actual_unit(self):
        client=self.dji_fixture()
        self.panel.show_dji(2)
        choice=self.panel.dji_page.controls['voice-tone']
        choice.setCurrentIndex(choice.findData('rich'))
        client.submit.assert_called_once_with('voice-tone','rich',2,False)
        self.assertEqual(choice.currentData(),'standard')

    def test_dji_pending_confirmation_preserves_scroll_position(self):
        from PySide6.QtWidgets import QScrollArea
        client=self.dji_fixture()
        self.panel.show_dji(2); self.panel.show(); self.panel.activateWindow(); self.app.processEvents()
        page=self.panel.dji_page
        scroll=page.findChild(QScrollArea)
        choice=page.controls['voice-tone']
        QTest.mouseClick(choice,Qt.MouseButton.LeftButton)
        choice.choice_menu.hide(); self.app.processEvents()
        bar=scroll.verticalScrollBar(); bar.setValue(0)
        self.assertGreater(bar.maximum(),0)
        client.pending_id='pending'
        page.refresh(); self.app.processEvents()
        self.assertEqual(bar.value(),0)
        client.pending_id=None
        page.refresh(); self.app.processEvents()
        self.assertEqual(bar.value(),0)

    def test_unchanged_dji_push_does_not_reset_tray_or_resize_details(self):
        client=self.dji_fixture()
        self.panel.show_dji(2); self.panel.show(); self.app.processEvents()
        self.controller.update_presentation()
        with patch.object(self.controller.tray,'setIcon') as icon, \
                patch.object(self.panel,'resize_to_content') as resize:
            self.controller.receive_dji(list(self.controller.readings.values())[3:],client.state)
        icon.assert_not_called()
        resize.assert_not_called()

    def test_dji_heartbeat_is_not_a_visual_change_but_setting_changes_are(self):
        import time
        from PySide6.QtCore import QProcess
        from dji_client import DjiClient
        from core import atomic_json
        fixture=self.dji_fixture()
        client=DjiClient(Path(self.folder.name))
        received=[]; client.updated.connect(lambda values,state:received.append(state))
        client.started=time.time()-1
        raw=json.loads(json.dumps(fixture.state))
        try:
            with patch.object(client.process,'state',return_value=QProcess.ProcessState.Running):
                atomic_json(client.folder/'state.json',raw); client.tick()
                raw['heartbeat']=time.time()
                for reading in raw['readings']: reading['updated']+=1
                atomic_json(client.folder/'state.json',raw); client.tick()
                self.assertEqual(len(received),1)
                self.assertEqual(client.state['heartbeat'],raw['heartbeat'])
                raw['tx']['2']['tone']='rich'
                atomic_json(client.folder/'state.json',raw); client.tick()
                self.assertEqual(len(received),2)
        finally:
            client.stop()

    def test_dji_stale_and_rejected_controls_disabled(self):
        client=self.dji_fixture()
        client.state['unavailable_settings']=['noise-cancel']
        self.panel.show_dji(1)
        self.assertFalse(self.panel.dji_page.controls['noise-cancel'].isEnabled())
        client.state['heartbeat']=0
        self.panel.dji_page.refresh()
        self.assertTrue(all(not choice.isEnabled() for choice in self.panel.dji_page.controls.values()))

    def test_dji_reboot_cancel_does_not_send_or_optimistically_change(self):
        from PySide6.QtWidgets import QMessageBox
        client=self.dji_fixture()
        self.panel.show_dji(2)
        choice=self.panel.dji_page.controls['plug-free']
        with patch.object(QMessageBox,'exec',return_value=0):
            choice.setCurrentIndex(choice.findData(True))
        client.submit.assert_not_called()
        self.assertIs(choice.currentData(),False)

    def test_dji_partial_update_preserves_existing_device_readings(self):
        self.controller.receive([Reading('mouse','魔刃 X','',74)],'')
        self.dji_fixture()
        self.assertEqual(self.controller.readings['mouse'].level,74)
        self.assertEqual(self.controller.readings['mic2'].gauge,5)

    def test_dji_unknown_gauge_still_shows_confirmed_charging(self):
        from dji_protocol import Status,readings
        from test_dji import status_frame
        status=Status(); status.decode(status_frame(((2,0,True),)))
        self.controller.receive_dji(readings(status),{})
        card=self.panel.cards['mic2']
        self.assertEqual(card.percentage.text(),'电量未知')
        self.assertEqual(card.state_text.text(),'正在充电')

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

    def test_periodic_read_skips_device_in_backoff(self):
        schedule = self.controller.retry_schedule
        schedule.clock = lambda: 10
        schedule.deadlines.update(mouse=0, keyboard=0, headset=300)
        with patch.object(self.controller.poller, "start", return_value=True) as start:
            self.controller.refresh_due()
            start.assert_called_once_with(["mouse", "keyboard"])

    def test_manual_refresh_bypasses_backoff(self):
        schedule = self.controller.retry_schedule
        schedule.record([], list(schedule.keys), 60)
        with patch.object(self.controller.poller, "start", return_value=True) as start:
            self.controller.refresh()
            start.assert_called_once_with(["mouse", "keyboard", "headset"])
        self.assertEqual(schedule.failures["headset"], 0)

    def test_mouse_wake_preserves_headset_backoff(self):
        schedule = self.controller.retry_schedule
        schedule.record([], ["headset"], 60)
        deadline = schedule.deadlines["headset"]
        self.controller.request_wake_refresh("mouse")
        with patch.object(self.controller.poller, "start", return_value=True) as start:
            self.controller.refresh_after_wake()
            start.assert_called_once_with(["mouse"])
        self.assertEqual(schedule.deadlines["headset"], deadline)

    def test_partial_recovery_clears_only_its_stale_marker(self):
        self.controller.receive([], "timeout")
        self.controller.receive([Reading("mouse", "鼠标", "", 74)], "")
        self.assertEqual(self.panel.cards["mouse"].state_text.text(), "已连接")
        self.assertEqual(self.panel.cards["keyboard"].state_text.text(), "上次读取")
        self.controller.receive([Reading("keyboard", "键盘", "", 50), Reading("headset", "耳机", "", 75)], "")
        self.assertFalse(self.controller.stale)

    def test_event_loop_keeps_ticking_while_worker_waits(self):
        import sys
        timer = QTimer()
        beats = []
        timer.timeout.connect(lambda: beats.append(1))
        timer.start(10)
        poller = Poller(Path(self.folder.name))
        poller.process.start(sys.executable, ["-c", "import time; time.sleep(0.4)"])
        self.assertTrue(poller.process.waitForStarted(1000))
        QTest.qWait(250)
        timer.stop()
        self.assertGreaterEqual(len(beats), 5)
        self.assertNotEqual(poller.process.state(), poller.process.ProcessState.NotRunning)
        poller.stop()

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
