from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
import winreg
from pathlib import Path

from PySide6.QtCore import (QByteArray, QEvent, QObject, QPoint, QProcess, QRectF, QSize,
                            Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QCursor, QFont, QIcon, QKeySequence, QPainter, QShortcut
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QFrame,
    QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QMenu, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget, QMessageBox)

import asus_control
from core import (AlertPolicy, MODELS, ALL_MODELS, DJI_MODELS, Reading, atomic_json, load_config,
                   read_all, read_poll_file, user_data_dir)
from icons import ROOT, app_icon, symbol
from i18n import LANGUAGES, set_language, tr, translate_message
from polling import RetrySchedule


PALETTES = {
    "light": dict(bg="#f3f5f6", card="#ffffff", text="#20282a", muted="#5e6c70",
                  line="#dce3e5", accent="#087c61", tint="#e6f4ee", track="#e7eeeb",
                  warning="#a34b0a", danger="#c43933", hover="#e5eaec", blue="#1464d8"),
    "dark": dict(bg="#202629", card="#2c3438", text="#f2f5f5", muted="#aab7bc",
                 line="#424e53", accent="#68dcb0", tint="#233d34", track="#424e49",
                 warning="#ffbb70", danger="#ff8c87", hover="#394449", blue="#87b8ff"),
}


def system_theme() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return "light" if winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "light"


class TranslatedLabel(QLabel):
    def __init__(self, text=""):
        self.source_text = text
        super().__init__(translate_message(text))

    def setText(self, text):
        self.source_text = text
        super().setText(translate_message(text))

    def retranslate(self):
        super().setText(translate_message(self.source_text))


def label(text="", role="body"):
    value = TranslatedLabel(text)
    value.setProperty("role", role)
    return value


class Switch(QCheckBox):
    def __init__(self, text, checked=False):
        super().__init__()
        self.source_text = text
        self.setAccessibleName(tr(text))
        self.setToolTip(tr(text))
        self.setChecked(checked)
        self.setFixedSize(44, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.colors = PALETTES["light"]

    def hitButton(self, position):
        # The native checkbox's indicator is only ~14 px wide. Our painted
        # switch is 40 px wide, so use the entire visible control as its target.
        return self.rect().contains(position)

    def bind_label(self, caption):
        caption.setBuddy(self)
        caption.setCursor(Qt.CursorShape.PointingHandCursor)
        caption.installEventFilter(self)

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and watched.rect().contains(event.position().toPoint()) and self.isEnabled()):
            self.click()
            return True
        return super().eventFilter(watched, event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self.colors["accent"] if self.isChecked() else self.colors["line"]))
        p.drawRoundedRect(QRectF(2, 5, 40, 22), 11, 11)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(24 if self.isChecked() else 5, 8, 16, 16))


class MenuChoice(QPushButton):
    """Whole-button menu trigger, with an understated custom chevron pair."""
    currentIndexChanged = Signal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("choiceButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.items, self.index = [], -1
        self.choice_menu = QMenu(self)
        row = QHBoxLayout(self)
        row.setContentsMargins(11, 3, 9, 3)
        row.setSpacing(12)
        self.caption = TranslatedLabel()
        self.indicator = QLabel()
        self.indicator.setFixedSize(13, 16)
        for child in (self.caption, self.indicator):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.caption, 1)
        row.addWidget(self.indicator)
        self.clicked.connect(self.open_menu)

    def addItem(self, text, data):
        self.items.append((text, data))
        action = self.choice_menu.addAction(tr(text))
        action.setCheckable(True)
        index = len(self.items) - 1
        action.triggered.connect(lambda checked=False, i=index: self.setCurrentIndex(i))
        if self.index < 0:
            self.setCurrentIndex(0)

    def findData(self, data):
        return next((i for i, item in enumerate(self.items) if item[1] == data), -1)

    def currentData(self):
        return self.items[self.index][1] if self.index >= 0 else None

    def setCurrentIndex(self, index):
        if index == self.index or not 0 <= index < len(self.items):
            return
        self.index = index
        self.caption.setText(self.items[index][0])
        self.setAccessibleDescription(tr(self.items[index][0]))
        for i, action in enumerate(self.choice_menu.actions()):
            action.setChecked(i == index)
        self.currentIndexChanged.emit(index)

    def retranslate(self):
        for (text, _), action in zip(self.items, self.choice_menu.actions()):
            action.setText(tr(text))
        if self.index >= 0:
            self.caption.setText(self.items[self.index][0])
            self.setAccessibleDescription(tr(self.items[self.index][0]))

    def open_menu(self):
        self.choice_menu.setMinimumWidth(self.width())
        self.choice_menu.popup(self.mapToGlobal(QPoint(0, self.height() + 5)))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.open_menu()
        else:
            super().keyPressEvent(event)

    def apply_theme(self, colors):
        self.indicator.setPixmap(symbol("chevrons", colors["muted"], 13).pixmap(13, 16))
        self.choice_menu.setStyleSheet(f"QMenu{{background:{colors['card']};color:{colors['text']};"
            f"border:1px solid {colors['line']};border-radius:9px;padding:5px;font-size:11pt;}}"
            "QMenu::item{padding:7px 15px 7px 25px;border-radius:5px;}"
            f"QMenu::item:selected{{background:{colors['hover']};}}")


class DeviceCard(QFrame):
    SUBTITLES = {"mouse": "CHAKRAM X · 2.4G", "keyboard": "AZOTH · OMNI",
                 "headset": "Nova Pro Wireless · 2.4G", "mic1": "DJI Mic Mini 2 · TX 1",
                 "mic2": "DJI Mic Mini 2 · TX 2"}

    def __init__(self, key):
        super().__init__()
        self.key = key
        self.setObjectName("deviceCard")
        self.setMinimumHeight(104)
        self.reading = Reading(key, *ALL_MODELS[key])
        self.colors = PALETTES["light"]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 13, 16, 13)
        outer.setSpacing(10)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.glyph = QLabel()
        self.glyph.setObjectName("deviceGlyph")
        self.glyph.setFixedSize(44, 44)
        self.glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.glyph)
        text = QVBoxLayout()
        text.setSpacing(3)
        self.title = label(self.reading.title, "deviceName")
        self.subtitle = label(self.SUBTITLES[key], "caption")
        text.addWidget(self.title)
        text.addWidget(self.subtitle)
        row.addLayout(text, 1)
        self.percentage = label("—", "percentage")
        self.percentage.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if key in DJI_MODELS:
            self.percentage.setWordWrap(True)
            self.percentage.setMaximumWidth(150)
        row.addWidget(self.percentage)
        outer.addLayout(row)
        lower = QHBoxLayout()
        lower.setSpacing(12)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(5)
        lower.addWidget(self.bar, 1)
        self.state_text = label("正在读取", "caption")
        self.state_text.setAlignment(Qt.AlignmentFlag.AlignRight)
        lower.addWidget(self.state_text)
        if key in DJI_MODELS:
            self.details_button = QPushButton(tr("详情"))
            self.details_button.setProperty("sourceText", "详情")
            self.details_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.details_button.setMinimumSize(44, 28)
            lower.insertWidget(0, self.details_button)
        outer.addLayout(lower)

    def render(self, reading, config, stale=False):
        self.reading = reading
        self.title.setText(reading.title)
        c = self.colors
        self.glyph.setPixmap(symbol("microphone" if self.key in DJI_MODELS else self.key, c["text"], 29).pixmap(QSize(29, 29)))
        level = reading.level
        low = (self.key in ("mouse", "keyboard") and level is not None
               and config["alerts"][self.key]["enabled"]
               and level <= config["alerts"][self.key]["threshold"] and not reading.charging)
        color = c["danger"] if low else c["accent"]
        self.percentage.setText(("约 " if reading.approximate else "") + f"{level}%" if level is not None else "—")
        self.percentage.setStyleSheet(f"color:{c['text']};")
        self.bar.setValue(level if level is not None else 0)
        self.bar.setStyleSheet(f"QProgressBar{{background:{c['track']};border:0;border-radius:2px;}}"
                              f"QProgressBar::chunk{{background:{color};border-radius:2px;}}")
        waiting = any(word in reading.error for word in ("未回复", "休眠", "没有回复"))
        state = ("上次读取" if stale else "正在充电" if reading.charging else
                 "电量偏低" if low else "已连接" if level is not None else
                 "正在读取" if not reading.error else "等待唤醒" if waiting else "未连接")
        self.state_text.setText(state)
        self.state_text.setStyleSheet(f"color:{color if low else c['muted']};")
        self.setToolTip(translate_message(reading.error) if reading.error else tr(
            "耳机按 0–8 档换算，显示近似电量；耳机不发送低电量提醒。") if reading.approximate else "")
        self.setAccessibleName(f"{tr(reading.title)} {self.percentage.text()} {tr(state)}")
        if self.key in DJI_MODELS:
            from dji_protocol import GAUGE_TEXT, GAUGE_BAR, STATE_TEXT
            gauge = reading.gauge
            text = GAUGE_TEXT.get(gauge, "电量未知")
            self.percentage.setText(text if reading.state in ("online", "unknown", "stale") else "—")
            self.percentage.setStyleSheet(f"color:{c['text']};font-size:15pt;")
            self.bar.setValue(GAUGE_BAR.get(gauge, 0))
            self.bar.setVisible(gauge is not None and reading.state in ("online", "stale"))
            state = ("上次读取" if stale or reading.state == "stale" else "正在充电"
                     if reading.charging and reading.state in ("online", "unknown") else STATE_TEXT.get(reading.state, "正在读取"))
            self.state_text.setText(state)
            self.state_text.setWordWrap(True)
            self.state_text.setMaximumWidth(240)
            self.setToolTip(tr("电量条仅示意档位，并非精确百分比；麦克风不发送低电量提醒。") + "\n" + tr(state)
                            + ("\n" + reading.detail if reading.detail else ""))
            self.setAccessibleName(f"{tr(reading.title)} {tr(text)} {tr(state)}")


class Poller(QObject):
    completed = Signal(list, str)

    def __init__(self, data_dir):
        super().__init__()
        self.data_dir = data_dir
        self.process = QProcess(self)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.failed)
        self.watchdog = QTimer(self)
        self.watchdog.setSingleShot(True)
        self.watchdog.timeout.connect(self.timeout)
        self.output = None
        self.timed_out = False
        self.active = False
        self.requested_keys = list(MODELS)

    def start(self, key=None):
        if self.active:
            return False
        keys = list(MODELS) if key is None else [key] if isinstance(key, str) else list(key)
        if not keys or any(k not in MODELS for k in keys) or len(set(keys)) != len(keys):
            raise ValueError("Unknown or duplicate device keys")
        self.active = True
        self.timed_out = False
        self.requested_keys = keys
        self.output = self.data_dir / f"poll-{uuid.uuid4().hex}.json"
        args = ["--poll-output", str(self.output)]
        if keys != list(MODELS):
            for selected in keys:
                args += ["--poll-key", selected]
        if not getattr(sys, "frozen", False):
            args.insert(0, str(Path(__file__).with_name("chargedock.py")))
        self.process.start(sys.executable, args)
        self.watchdog.start(9000)
        return True

    def timeout(self):
        self.timed_out = True
        self.process.kill()

    def failed(self, error):
        if error == QProcess.ProcessError.FailedToStart and self.active:
            self.watchdog.stop()
            self.active = False
            self.completed.emit([], "读取进程无法启动，请稍后重试")

    def finished(self, *args):
        if not self.active:
            return
        self.watchdog.stop()
        self.active = False
        try:
            if self.timed_out:
                raise TimeoutError("设备响应超时，已停止本次读取；请唤醒设备后刷新")
            readings = read_poll_file(self.output, self.requested_keys)
            self.completed.emit(readings, "")
        except (OSError, ValueError, TypeError) as exc:
            self.completed.emit([], str(exc))
        finally:
            if self.output:
                self.output.unlink(missing_ok=True)

    def stop(self):
        self.watchdog.stop()
        self.active = False
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(1000)
        if self.output:
            self.output.unlink(missing_ok=True)


class DjiDetails(QWidget):
    """Reported values only; selections revert until a fresh report arrives."""
    def __init__(self, panel):
        super().__init__()
        from dji_control import SETTINGS
        self.panel, self.unit, self.controls = panel, 1, {}
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        self.status_label = label("等待设备回报", "caption")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)
        self.capability_note = label("此接收器的降噪写入受固件限制，仅显示设备回报状态。", "caption")
        self.capability_note.setWordWrap(True); self.capability_note.hide()
        outer.addWidget(self.capability_note)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        form = QWidget(); form.setObjectName("settingsForm")
        layout = QVBoxLayout(form); layout.setContentsMargins(0, 0, 5, 0)
        for target, title in (("tx", "此麦克风"), ("broadcast", "发射器设置（所有在线麦克风）"), ("rx", "接收器设置")):
            heading = label(title, "section"); heading.setWordWrap(True); layout.addWidget(heading)
            for key, desc in SETTINGS.items():
                if desc.target != target:
                    continue
                card = QFrame(); card.setObjectName("deviceCard")
                row = QHBoxLayout(card); row.setContentsMargins(12, 8, 12, 8)
                text = label(desc.label); text.setWordWrap(True)
                row.addWidget(text, 1)
                choice = MenuChoice()
                choice.setMinimumSize(110, 34); choice.setMaximumWidth(135)
                choice.addItem("未知／不支持", None)
                for value, caption, _ in desc.options:
                    choice.addItem(caption, value)
                choice.currentIndexChanged.connect(lambda index, k=key: self.change(k))
                row.addWidget(choice)
                self.controls[key] = choice
                layout.addWidget(card)
        note = label("设置会影响实际音频。立体声与安全音轨互斥；免拔插外放会重启接收器。", "caption")
        note.setWordWrap(True); layout.addWidget(note); layout.addStretch()
        scroll.setWidget(form); outer.addWidget(scroll)

    def refresh(self):
        from dji_control import ERRORS, reported, status_from_message
        client = self.panel.controller.dji
        raw = client.state if client else {}
        status = status_from_message(raw) if raw else None
        self.capability_note.setVisible(bool(status and status.receiver_product == "DJI Mic Mini 2"))
        live = bool(status and time.time() - raw.get("heartbeat", 0) < 2
                    and all(r.get("state") in ("online", "unknown", "offline") for r in raw.get("readings", [])))
        pending = bool(client and client.pending_id)
        result = raw.get("command_result") or {}
        message = "等待设备回报"
        if pending:
            message = "正在等待设备确认"
        elif result:
            message = ERRORS.get(result.get("code"), "设置失败，设备未确认更改")
        elif live:
            message = "显示设备回报的真实设置"
        if not live and raw.get("readings"):
            from dji_protocol import STATE_TEXT
            message = STATE_TEXT.get(raw["readings"][0].get("state"), "等待设备回报")
        self.status_label.setText(message)
        for key, choice in self.controls.items():
            value = reported(status, key, self.unit) if live else None
            choice.blockSignals(True)
            choice.setCurrentIndex(choice.findData(value))
            choice.blockSignals(False)
            capability_key = key + (":" + str(self.unit) if key == "voice-tone" else "")
            rejected = capability_key in raw.get("unavailable_settings", [])
            choice.setEnabled(live and not pending and value is not None and not rejected)
            choice.setToolTip(tr("设备返回非成功 ACK，设置未确认；可能受固件能力限制") if rejected else "")

    def change(self, key):
        from dji_control import SETTINGS
        client = self.panel.controller.dji
        value = self.controls[key].currentData()
        if not client or value is None:
            self.refresh(); return
        desc = SETTINGS[key]
        confirmed = False
        if desc.reboot:
            dialog = QMessageBox(self.panel)
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setWindowTitle(tr("重启接收器"))
            dialog.setText(tr("更改免拔插外放会重启接收器并短暂中断音频。是否继续？"))
            proceed = dialog.addButton(tr("继续"), QMessageBox.ButtonRole.AcceptRole)
            cancel = dialog.addButton(tr("取消"), QMessageBox.ButtonRole.RejectRole)
            dialog.setDefaultButton(cancel)
            dialog.exec()
            if dialog.clickedButton() is not proceed:
                self.refresh(); return
            confirmed = True
        try:
            client.submit(key, value, self.unit if desc.target == "tx" else None, confirmed)
        except OSError:
            self.status_label.setText("设置发送失败，请检查连接")
        self.refresh()


class Panel(QWidget):
    def __init__(self, controller):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.controller = controller
        QApplication.instance().installEventFilter(self)
        self.setWindowTitle(tr("电量岛 · ChargeDock"))
        self.setWindowIcon(app_icon())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(432)
        self.colors = PALETTES["light"]
        self.icon_buttons = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        self.shell = QFrame()
        self.shell.setObjectName("shell")
        shadow = QGraphicsDropShadowEffect(self.shell)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(0, 0, 0, 45))
        self.shell.setGraphicsEffect(shadow)
        layout.addWidget(self.shell)
        content = QVBoxLayout(self.shell)
        content.setContentsMargins(20, 18, 20, 17)
        content.setSpacing(14)
        header = QHBoxLayout()
        header.setSpacing(10)
        self.back = self.icon_button("back", "返回电量", self.show_main)
        self.logo = QLabel()
        self.logo.setPixmap(app_icon().pixmap(34, 34))
        header.addWidget(self.back)
        header.addWidget(self.logo)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.heading = label("电量岛", "heading")
        self.tagline = label("所有设备，一眼可见", "caption")
        titles.addWidget(self.heading)
        titles.addWidget(self.tagline)
        header.addLayout(titles, 1)
        self.settings_button = self.icon_button("settings", "偏好设置", self.show_settings)
        header.addWidget(self.settings_button)
        header.addWidget(self.icon_button("close", "收起窗口", self.hide))
        content.addLayout(header)
        self.stack = QStackedWidget()
        content.addWidget(self.stack)
        self.main_page = QWidget()
        self.stack.addWidget(self.main_page)
        main = QVBoxLayout(self.main_page)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(10)
        self.devices_scroll = QScrollArea()
        self.devices_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.devices_scroll.setWidgetResizable(True)
        self.devices_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        devices = QWidget()
        devices.setObjectName("settingsForm")
        self.devices_layout = QVBoxLayout(devices)
        self.devices_layout.setContentsMargins(0, 0, 0, 0)
        self.devices_layout.setSpacing(10)
        self.devices_scroll.setWidget(devices)
        main.addWidget(self.devices_scroll)
        self.cards = {}
        for key in ALL_MODELS:
            card = DeviceCard(key)
            self.cards[key] = card
            self.devices_layout.addWidget(card)
            if key in DJI_MODELS:
                card.details_button.clicked.connect(lambda checked=False, u=int(key[-1]): self.show_dji(u))
        self.notice = label("", "caption")
        self.notice.setWordWrap(True)
        self.notice.hide()
        main.addWidget(self.notice)
        footer = QHBoxLayout()
        self.updated_label = label("正在连接设备…", "caption")
        footer.addWidget(self.updated_label, 1)
        self.refresh_button = self.icon_button("refresh", "刷新电量", controller.refresh)
        footer.addWidget(self.refresh_button)
        main.addLayout(footer)
        self.build_settings()
        self.dji_page = DjiDetails(self)
        self.stack.addWidget(self.dji_page)
        self.dismiss = QTimer(self)
        self.dismiss.setSingleShot(True)
        self.dismiss.timeout.connect(self.auto_hide)
        QShortcut(QKeySequence("Escape"), self, self.hide)
        QShortcut(QKeySequence("Ctrl+R"), self, controller.refresh)
        QShortcut(QKeySequence("Ctrl+,"), self, self.show_settings)
        self.show_main()

    def eventFilter(self, watched, event):
        if isinstance(watched, QWidget) and watched.window() is self:
            if event.type() == QEvent.Type.KeyPress and event.key() in (
                    Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def icon_button(self, name, text, callback):
        button = QPushButton()
        button.setObjectName("iconButton")
        button.setFixedSize(36, 36)
        button.setIconSize(QSize(20, 20))
        button.setAccessibleName(text)
        button.setToolTip(text)
        button.setProperty("sourceTip", text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        self.icon_buttons.append((button, name))
        return button

    def build_settings(self):
        self.settings_page = QScrollArea()
        self.settings_page.setObjectName("settingsScroll")
        self.settings_page.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_page.setWidgetResizable(True)
        self.settings_page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack.addWidget(self.settings_page)
        form = QWidget()
        form.setObjectName("settingsForm")
        self.settings_page.setWidget(form)
        layout = QVBoxLayout(form)
        layout.setContentsMargins(0, 0, 2, 0)
        layout.setSpacing(12)
        layout.addWidget(label("低电量提醒", "section"))
        alarm_box = QFrame()
        alarm_box.setObjectName("settingsGroup")
        alarm_layout = QVBoxLayout(alarm_box)
        alarm_layout.setContentsMargins(13, 8, 13, 8)
        self.thresholds, self.switches = {}, {}
        for key, title in (("mouse", "鼠标"), ("keyboard", "键盘")):
            row = QHBoxLayout()
            caption = label(title)
            caption.setWordWrap(True)
            row.addWidget(caption, 1)
            spin = QSpinBox()
            spin.setRange(1, 99)
            spin.setSingleStep(5)
            spin.setSuffix(" %")
            spin.setValue(self.controller.config["alerts"][key]["threshold"])
            spin.setAccessibleName(f"{title}低电量提醒阈值")
            spin.setProperty("sourceTip", f"{title}低电量提醒阈值")
            spin.setFixedWidth(84)
            spin.setMinimumHeight(32)
            spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
            spin.setToolTip("输入 1–99%，或用上下箭头调整")
            spin.setProperty("sourceHelp", "输入 1–99%，或用上下箭头调整")
            row.addWidget(spin)
            switch = Switch(f"{title}低电量提醒", self.controller.config["alerts"][key]["enabled"])
            switch.bind_label(caption)
            spin.setEnabled(switch.isChecked())
            switch.toggled.connect(spin.setEnabled)
            row.addWidget(switch)
            spin.valueChanged.connect(lambda value, k=key: self.controller.alert_setting(k, "threshold", value))
            switch.toggled.connect(lambda value, k=key: self.controller.alert_setting(k, "enabled", value))
            self.thresholds[key], self.switches[key] = spin, switch
            alarm_layout.addLayout(row)
        layout.addWidget(alarm_box)
        hint = label("每轮低电量只提醒一次。耳机仅显示电量。", "caption")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        for title, name, choices in (
            ("自动刷新", "interval", [("30 秒", 30), ("1 分钟", 60), ("2 分钟", 120), ("5 分钟", 300)]),
            ("自动收起", "dismiss_seconds", [("8 秒", 8), ("12 秒", 12), ("20 秒", 20), ("30 秒", 30)])):
            row = QHBoxLayout()
            row.addWidget(label(title), 1)
            combo = MenuChoice()
            combo.setAccessibleName(title)
            combo.setProperty("sourceTip", title)
            combo.setMinimumHeight(32)
            combo.setMinimumWidth(140)
            for text, value in choices:
                combo.addItem(text, value)
            combo.setCurrentIndex(combo.findData(self.controller.config[name]))
            combo.currentIndexChanged.connect(lambda _, c=combo, n=name: self.controller.setting(n, c.currentData()))
            row.addWidget(combo)
            layout.addLayout(row)
        language_row = QHBoxLayout()
        language_row.addWidget(label("语言"), 1)
        self.language_choice = MenuChoice()
        self.language_choice.setProperty("sourceTip", "语言")
        self.language_choice.setMinimumHeight(32)
        self.language_choice.setMinimumWidth(155)
        for code, title in LANGUAGES.items():
            self.language_choice.addItem(title, code)
        self.language_choice.setCurrentIndex(self.language_choice.findData(self.controller.config["language"]))
        self.language_choice.currentIndexChanged.connect(
            lambda _: self.controller.setting("language", self.language_choice.currentData()))
        language_row.addWidget(self.language_choice)
        layout.addLayout(language_row)
        for title, name, checked in (("提醒时播放提示音", "sound", self.controller.config["sound"]),
                                      ("登录时自动启动", "startup", asus_control.startup_enabled())):
            row = QHBoxLayout()
            caption = label(title)
            caption.setWordWrap(True)
            row.addWidget(caption, 1)
            switch = Switch(title, checked)
            switch.bind_label(caption)
            switch.toggled.connect(lambda value, n=name: self.controller.setting(n, value))
            if name == "startup":
                self.startup_switch = switch
            row.addWidget(switch)
            layout.addLayout(row)
        layout.addWidget(label("奥创电量浮窗", "section"))
        asus_box = QFrame()
        asus_box.setObjectName("settingsGroup")
        group = QVBoxLayout(asus_box)
        group.setContentsMargins(13, 12, 13, 12)
        group.setSpacing(9)
        self.asus_state = label("", "body")
        self.asus_hint = label("只控制电量浮窗，更改时需要管理员授权。", "caption")
        self.asus_hint.setWordWrap(True)
        group.addWidget(self.asus_state)
        group.addWidget(self.asus_hint)
        buttons = QHBoxLayout()
        self.asus_disable = QPushButton("关闭显示")
        self.asus_enable = QPushButton("打开显示")
        self.asus_disable.setProperty("sourceText", "关闭显示")
        self.asus_enable.setProperty("sourceText", "打开显示")
        for button in (self.asus_disable, self.asus_enable):
            button.setMinimumHeight(34)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.asus_disable.clicked.connect(lambda: self.controller.change_asus("disable"))
        self.asus_enable.clicked.connect(lambda: self.controller.change_asus("enable"))
        buttons.addWidget(self.asus_disable)
        buttons.addWidget(self.asus_enable)
        group.addLayout(buttons)
        layout.addWidget(asus_box)
        self.settings_status = label("设置即时保存", "caption")
        self.settings_status.setWordWrap(True)
        layout.addWidget(self.settings_status)
        layout.addStretch()

    def retranslate(self):
        self.setWindowTitle(tr("电量岛 · ChargeDock"))
        for widget in self.findChildren(QWidget):
            if isinstance(widget, TranslatedLabel):
                widget.retranslate()
            if isinstance(widget, MenuChoice):
                widget.retranslate()
            if isinstance(widget, Switch):
                widget.setAccessibleName(tr(widget.source_text))
                widget.setToolTip(tr(widget.source_text))
            if widget.property("sourceText"):
                widget.setText(tr(widget.property("sourceText")))
            if widget.property("sourceTip"):
                widget.setAccessibleName(tr(widget.property("sourceTip")))
                if isinstance(widget, QPushButton) and not isinstance(widget, MenuChoice):
                    widget.setToolTip(tr(widget.property("sourceTip")))
            if widget.property("sourceHelp"):
                widget.setToolTip(tr(widget.property("sourceHelp")))
        self.update_asus_state()
        self.resize_to_content()

    def apply_theme(self, theme=None):
        self.colors = PALETTES[theme or system_theme()]
        c = self.colors
        self.setStyleSheet(f"""
            QWidget {{ color:{c['text']}; font-family:'Segoe UI','Microsoft YaHei UI'; font-size:11pt; }}
            QFrame#shell {{ background:{c['bg']}; border:1px solid {c['line']}; border-radius:22px; }}
            QFrame#deviceCard, QFrame#settingsGroup {{ background:{c['card']}; border-radius:15px; }}
            QLabel {{ background:transparent; border:0; }}
            QLabel[role='heading'] {{ font-size:18pt; font-weight:650; }}
            QLabel[role='deviceName'] {{ font-size:12pt; font-weight:600; }}
            QLabel[role='percentage'] {{ font-size:23pt; font-weight:600; }}
            QLabel[role='caption'] {{ font-size:10pt; color:{c['muted']}; }}
            QLabel[role='section'] {{ font-size:10pt; font-weight:600; color:{c['muted']}; padding-top:3px; }}
            QLabel#deviceGlyph {{ background:{c['bg']}; border-radius:12px; }}
            QPushButton {{ background:{c['card']}; border:1px solid {c['line']}; border-radius:9px; padding:3px 8px; outline:0; }}
            QPushButton:hover {{ background:{c['hover']}; }}
            QPushButton:disabled {{ color:{c['muted']}; background:{c['bg']}; }}
            QPushButton#iconButton {{ background:transparent; border:1px solid transparent; padding:4px; border-radius:9px; }}
            QPushButton#iconButton:hover {{ background:{c['hover']}; }}
            QSpinBox, QComboBox {{ background:{c['card']}; border:1px solid {c['line']}; border-radius:8px; padding:3px 6px; outline:0; }}
            QSpinBox::up-button {{ subcontrol-origin:border; subcontrol-position:top right; width:20px; height:16px; border:0; }}
            QSpinBox::down-button {{ subcontrol-origin:border; subcontrol-position:bottom right; width:20px; height:16px; border:0; }}
            QSpinBox::up-arrow {{ image:url('{(ROOT / 'assets/chevron-up.svg').as_posix()}'); width:8px; height:5px; }}
            QSpinBox::down-arrow {{ image:url('{(ROOT / 'assets/chevron-down.svg').as_posix()}'); width:8px; height:5px; }}
            QComboBox QAbstractItemView {{ background:{c['card']}; selection-background-color:{c['hover']}; }}
            QScrollArea, QWidget#settingsForm, QScrollArea > QWidget > QWidget {{ background:transparent; }}
            QScrollBar:vertical {{ background:transparent; width:5px; }}
            QScrollBar::handle:vertical {{ background:{c['line']}; border-radius:2px; min-height:20px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
            QToolTip {{ background:{c['card']}; color:{c['text']}; border:1px solid {c['line']}; padding:5px; }}
        """)
        for button, name in self.icon_buttons:
            button.setIcon(symbol(name, c["text"], 20, c["bg"]))
        for switch in self.findChildren(Switch):
            switch.colors = c
            switch.update()
        for choice in self.findChildren(MenuChoice):
            choice.apply_theme(c)
        for key, card in self.cards.items():
            card.colors = c
            card.render(self.controller.readings[key], self.controller.config, self.controller.stale)

    def show_main(self):
        self.stack.setCurrentWidget(self.main_page)
        self.back.hide()
        self.logo.show()
        self.settings_button.show()
        self.heading.setText("电量岛")
        self.tagline.setText("所有设备，一眼可见")
        self.resize_to_content()
        if hasattr(self, "dismiss"):
            self.restart_dismiss()

    def show_settings(self):
        self.stack.setCurrentWidget(self.settings_page)
        self.back.show()
        self.logo.hide()
        self.settings_button.hide()
        self.heading.setText("偏好设置")
        self.tagline.setText("按你的习惯，安静地工作")
        self.dismiss.stop()
        self.update_asus_state()
        self.resize_to_content()

    def show_dji(self, unit):
        self.stack.setCurrentWidget(self.dji_page)
        self.dji_page.unit = unit
        self.back.show(); self.logo.hide(); self.settings_button.hide()
        self.heading.setText("麦克风 1" if unit == 1 else "麦克风 2")
        self.tagline.setText("大疆设备控制")
        self.dismiss.stop()
        self.dji_page.refresh()
        self.resize_to_content()

    def update_asus_state(self):
        state = asus_control.status()
        self.asus_state.setText({"enabled": "奥创浮窗组件：已启用", "disabled": "奥创浮窗组件：已关闭",
                                "missing": "未安装奥创电量浮窗组件", "conflict": "奥创更新后存在两份组件，暂不更改"}[state])
        pending = self.controller.admin_result is not None
        self.asus_disable.setEnabled(not pending and state in ("enabled", "disabled"))
        self.asus_enable.setEnabled(not pending and state in ("enabled", "disabled"))

    def resize_to_content(self):
        screen = self.screen() or QApplication.primaryScreen()
        room = screen.availableGeometry()
        self.devices_scroll.setMinimumHeight(min(self.devices_layout.sizeHint().height(), max(180, room.height() - 240)))
        height = (min(self.main_page.layout().sizeHint().height(), room.height() - 150) if self.stack.currentWidget() == self.main_page
                  else min(535, room.height() - 150))
        self.stack.setFixedHeight(max(180, height))
        self.shell.layout().activate()
        self.layout().activate()
        self.setFixedHeight(self.layout().sizeHint().height())
        if self.isVisible():
            self.place()

    def place(self):
        tray_rect = self.controller.tray.geometry()
        screen = QApplication.screenAt(tray_rect.center()) if tray_rect.isValid() else QApplication.primaryScreen()
        room = (screen or QApplication.primaryScreen()).availableGeometry()
        self.move(room.right() - self.width() - 1, room.bottom() - self.height() - 1)

    def reveal(self, settings=False):
        self.apply_theme()
        self.show_settings() if settings else self.show_main()
        self.place()
        self.show()
        self.raise_()
        self.activateWindow()
        if time.time() - self.controller.last_updated > 10:
            self.controller.refresh()
        if not settings:
            self.restart_dismiss()

    def restart_dismiss(self):
        self.dismiss.start(self.controller.config["dismiss_seconds"] * 1000)

    def auto_hide(self):
        if self.underMouse():
            self.dismiss.start(1500)
        elif self.stack.currentWidget() == self.main_page:
            self.hide()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.ActivationChange and self.isVisible() and not self.isActiveWindow():
            QTimer.singleShot(150, self.hide_if_inactive)
        super().changeEvent(event)

    def hide_if_inactive(self):
        if (not self.isActiveWindow() and QApplication.activePopupWidget() is None
                and QApplication.activeModalWidget() is None):
            self.hide()

    def closeEvent(self, event):
        event.ignore()
        self.hide()


class Controller(QObject):
    def __init__(self, app, args, server):
        super().__init__()
        self.app, self.args, self.server = app, args, server
        self.closing = False
        self.data_dir = args.data_dir or user_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "settings.json"
        self.config = load_config(self.config_path)
        set_language(self.config["language"])
        self.app.setApplicationDisplayName(tr("电量岛"))
        self.policy = AlertPolicy()
        self.readings = {key: Reading(key, *value) for key, value in ALL_MODELS.items()}
        from dji_protocol import readings as dji_readings, Status
        self.readings.update({r.key: r for r in dji_readings(Status(), "connecting")})
        self.stale = False
        self.stale_keys = set()
        self.last_updated = 0
        self.last_error = ""
        self.admin_result = None
        self.admin_started = 0
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.panel = Panel(self)
        self.panel.apply_theme(args.snapshot_theme if args.snapshot else None)
        self.panel.retranslate()
        self.tray.setToolTip(tr("电量岛 · 正在读取设备"))
        self.tray.activated.connect(self.activated)
        self.tray.messageClicked.connect(lambda: self.panel.reveal())
        self.menu = QMenu()
        self.menu_actions = []
        for source, callback in (("显示电量", lambda: self.panel.reveal()),
                ("偏好设置", lambda: self.panel.reveal(settings=True)),
                ("立即刷新", self.refresh), ("暂停提醒 30 分钟", self.snooze)):
            self.menu_actions.append((self.menu.addAction(tr(source), callback), source))
        self.menu.addSeparator()
        self.menu_actions.append((self.menu.addAction(tr("退出电量岛"), app.quit), "退出电量岛"))
        self.tray.setContextMenu(self.menu)
        self.poller = Poller(self.data_dir)
        self.poller.completed.connect(self.receive)
        self.dji = None
        if not args.snapshot:
            from dji_client import DjiClient
            self.dji = DjiClient(self.data_dir)
            self.dji.updated.connect(self.receive_dji)
            self.dji.start()
        self.wake_timer = QTimer(self)
        self.pending_wake_keys = set()
        self.wake_timer.setSingleShot(True)
        self.wake_timer.timeout.connect(self.refresh_after_wake)
        self.retry_schedule = RetrySchedule(MODELS)
        self.wake_monitor = None
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.timeout.connect(self.refresh_due)
        self.admin_timer = QTimer(self)
        self.admin_timer.timeout.connect(self.check_admin)
        self.app.aboutToQuit.connect(self.cleanup)
        self.server.newConnection.connect(self.connection)
        if not args.snapshot:
            from wake_monitor import WakeMonitor
            self.wake_monitor = WakeMonitor(app, int(self.panel.winId()), self.request_wake_refresh,
                                            lambda: self.readings, lambda: self.poller.active)
            self.tray.show()
            QTimer.singleShot(0, self.refresh)
        if not args.background and not args.snapshot and not args.smoke_test:
            QTimer.singleShot(100, self.panel.reveal)

    def activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.panel.isVisible():
                self.panel.hide()
            else:
                self.panel.reveal()

    def connection(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            socket.readyRead.connect(lambda s=socket: self.handle_message(s))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self.handle_message(socket)

    def handle_message(self, socket):
        command = bytes(socket.readAll()).decode(errors="ignore")
        if command.strip() == "quit":
            self.app.quit()
        else:
            self.panel.reveal()
        socket.disconnectFromServer()

    def refresh(self):
        if self.closing or self.poller.active:
            return
        if self.dji:
            self.dji.refresh()
        self.retry_schedule.reset()
        self.start_reading(list(MODELS))

    def schedule_next(self):
        if not self.closing and not self.args.snapshot:
            self.refresh_timer.start(self.retry_schedule.delay_ms())

    def refresh_due(self):
        if self.closing:
            return
        if self.poller.active:
            self.refresh_timer.start(250)
            return
        keys = self.retry_schedule.due()
        if keys:
            self.start_reading(keys)
        else:
            self.schedule_next()

    def start_reading(self, keys):
        self.refresh_timer.stop()
        if self.poller.start(keys):
            self.panel.refresh_button.setEnabled(False)
            self.panel.updated_label.setText("正在更新…")

    def request_wake_refresh(self, key=None):
        if self.closing:
            return
        if key in MODELS:
            self.pending_wake_keys.add(key)
        else:
            self.pending_wake_keys.update(MODELS)
        if not self.wake_timer.isActive():
            self.wake_timer.start(650)

    def refresh_after_wake(self):
        if self.poller.active:
            self.wake_timer.start(250)
        else:
            keys = [k for k in MODELS if k in self.pending_wake_keys]
            self.pending_wake_keys.clear()
            if keys == list(MODELS) or not keys:
                self.refresh()
            else:
                self.retry_schedule.reset(keys)
                self.start_reading(keys)

    def receive(self, readings, error):
        if self.closing:
            return
        self.panel.refresh_button.setEnabled(True)
        if error:
            self.stale_keys.update(self.poller.requested_keys)
        for reading in readings:
            self.stale_keys.discard(reading.key)
        self.stale = bool(self.stale_keys)
        if readings:
            self.readings.update({r.key: r for r in readings})
            self.last_updated = time.time()
            warnings = self.policy.evaluate(readings, self.config)
            if warnings and not self.args.snapshot:
                message = "\n".join(tr("{device}剩余 {level}%", device=tr(r.title), level=r.level) for r in warnings)
                self.tray.showMessage(tr("设备电量偏低"), message + tr("，可以准备充电了。"),
                                      QSystemTrayIcon.MessageIcon.Warning, 7000)
                if self.config["sound"]:
                    import winsound
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        attempted = [r.key for r in readings] if readings else self.poller.requested_keys
        self.retry_schedule.record(readings, attempted, self.config["interval"])
        self.schedule_next()
        self.update_presentation(error)
        if self.args.smoke_test:
            import ctypes
            user32 = ctypes.WinDLL("user32")
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.GetWindowLongW.restype = ctypes.c_long
            ex_style = user32.GetWindowLongW(int(self.panel.winId()), -20)
            atomic_json(self.args.smoke_test, {"error": error, "devices": [r.__dict__ for r in self.readings.values()],
                        "tray_count": len(self.findChildren(QSystemTrayIcon)),
                        "wake_registered": bool(self.wake_monitor and self.wake_monitor.registration_ok),
                        "tool_window": bool(ex_style & 0x80), "app_window": bool(ex_style & 0x40000)})
            QTimer.singleShot(200, self.app.quit)

    def receive_dji(self, readings, state):
        if self.closing:
            return
        self.readings.update({r.key: r for r in readings})
        self.update_presentation(self.last_error)
        self.panel.dji_page.refresh()

    def update_presentation(self, error=""):
        self.last_error = error
        for key, card in self.panel.cards.items():
            card.render(self.readings[key], self.config, key in self.stale_keys)
        self.panel.updated_label.setText("上次更新 " + time.strftime("%H:%M", time.localtime(self.last_updated))
                                        if self.last_updated else "尚未读取到电量")
        self.panel.notice.setText(error or ("提醒已暂停 30 分钟" if time.time() < self.policy.snoozed_until else ""))
        self.panel.notice.setVisible(bool(self.panel.notice.text()))
        self.panel.resize_to_content()
        tooltip = [tr("电量岛 · ChargeDock")]
        low = False
        for r in self.readings.values():
            if r.key in DJI_MODELS:
                from dji_protocol import GAUGE_TEXT, STATE_TEXT
                text = (GAUGE_TEXT.get(r.gauge, "电量未知") if r.state in ("online", "unknown", "stale")
                        else STATE_TEXT.get(r.state, "电量未知"))
                if r.state == "stale":
                    text = tr("上次读取") + " · " + tr(text)
                tooltip.append(f"{tr(r.title)}: {tr(text)}")
            else:
                tooltip.append(f"{tr(r.title)}: " + ((tr("约 ") if r.approximate else "") + f"{r.level}%" if r.level is not None else tr("未连接")))
            if r.key in ("mouse", "keyboard") and r.level is not None:
                item = self.config["alerts"][r.key]
                low |= item["enabled"] and r.level <= item["threshold"] and not r.charging
        self.tray.setToolTip("\n".join(tooltip))
        self.tray.setIcon(symbol("tray", self.panel.colors["warning"] if low else self.panel.colors["accent"], 32))

    def save(self):
        try:
            atomic_json(self.config_path, self.config)
            self.panel.settings_status.setText("设置已保存")
        except OSError:
            self.panel.settings_status.setText("设置无法保存，请检查目录权限")

    def alert_setting(self, key, name, value):
        self.config["alerts"][key][name] = value
        self.save()
        for k, card in self.panel.cards.items():
            card.render(self.readings[k], self.config, k in self.stale_keys)

    def setting(self, name, value):
        if name == "language":
            if value not in LANGUAGES:
                return
            self.config[name] = value
            set_language(value)
            self.app.setApplicationDisplayName(tr("电量岛"))
            choice = self.panel.language_choice
            choice.blockSignals(True)
            choice.setCurrentIndex(choice.findData(value))
            choice.blockSignals(False)
            self.save()
            self.panel.retranslate()
            for action, source in self.menu_actions:
                action.setText(tr(source))
            self.update_presentation(self.last_error)
            return
        if name == "startup":
            try:
                asus_control.set_startup(value)
                self.panel.settings_status.setText("开机启动已开启" if value else "开机启动已关闭")
            except (OSError, RuntimeError) as exc:
                self.panel.startup_switch.blockSignals(True)
                self.panel.startup_switch.setChecked(asus_control.startup_enabled())
                self.panel.startup_switch.blockSignals(False)
                self.panel.settings_status.setText(str(exc))
            return
        self.config[name] = value
        if name == "interval":
            self.retry_schedule.change_interval(value)
            self.schedule_next()
        self.save()

    def snooze(self):
        self.policy.snoozed_until = time.time() + 1800
        self.panel.notice.setText("提醒已暂停 30 分钟")
        self.panel.notice.show()
        self.panel.resize_to_content()

    def change_asus(self, action):
        if self.admin_result:
            return
        if action == "disable" and asus_control.status() == "disabled":
            self.panel.settings_status.setText("奥创电量浮窗已经关闭，无需重复操作")
            return
        result = self.data_dir / f"asus-{uuid.uuid4().hex}.json"
        try:
            asus_control.request_admin(action, result)
            self.admin_result = result
            self.admin_started = time.monotonic()
            self.panel.settings_status.setText("等待管理员授权及操作结果…")
            self.panel.update_asus_state()
            self.admin_timer.start(500)
        except OSError as exc:
            self.panel.settings_status.setText(str(exc))

    def check_admin(self):
        if self.admin_result and self.admin_result.exists():
            try:
                value = json.loads(self.admin_result.read_text(encoding="utf-8"))
                self.panel.settings_status.setText(value["message"])
                self.admin_result.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError):
                self.panel.settings_status.setText("无法读取操作结果，请检查浮窗组件状态")
            self.admin_result = None
            self.admin_timer.stop()
            self.panel.update_asus_state()
        elif time.monotonic() - self.admin_started > 120:
            self.admin_result = None
            self.admin_timer.stop()
            self.panel.settings_status.setText("等待超时，可重新打开设置查看状态")
            self.panel.update_asus_state()

    def cleanup(self):
        if self.closing:
            return
        self.closing = True
        self.wake_timer.stop()
        if self.wake_monitor:
            self.wake_monitor.close()
            self.wake_monitor = None
        self.refresh_timer.stop()
        self.admin_timer.stop()
        self.poller.stop()
        if self.dji:
            self.dji.stop()
        self.tray.hide()
        self.panel.hide()
        self.server.close()


def run(args):
    app = QApplication(sys.argv[:1])
    app.setApplicationName("ChargeDock")
    app.setApplicationDisplayName("电量岛")
    app.setStyle("Fusion")
    app.setWindowIcon(app_icon())
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setQuitOnLastWindowClosed(False)
    directory = args.data_dir or user_data_dir()
    identity = hashlib.sha256(str(directory.resolve()).lower().encode()).hexdigest()[:16]
    name = "ChargeDock-" + identity
    if args.snapshot:
        name += "-snapshot-" + uuid.uuid4().hex
    socket = QLocalSocket()
    socket.connectToServer(name)
    if socket.waitForConnected(400):
        socket.write(b"quit" if args.quit else b"show")
        socket.waitForBytesWritten(1000)
        socket.disconnectFromServer()
        return 0
    if args.quit:
        return 0
    server = QLocalServer()
    QLocalServer.removeServer(name)
    if not server.listen(name):
        return 1
    controller = Controller(app, args, server)
    if args.snapshot:
        controller.receive(read_all(), "")
        controller.panel.apply_theme(args.snapshot_theme)
        if args.snapshot_settings:
            controller.panel.show_settings()
        controller.panel.show()
        app.processEvents()
        controller.panel.resize_to_content()
        app.processEvents()
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        controller.panel.grab().save(str(args.snapshot))
        controller.cleanup()
        return 0
    if args.test_exit_ms:
        QTimer.singleShot(args.test_exit_ms, app.quit)
    return app.exec()
