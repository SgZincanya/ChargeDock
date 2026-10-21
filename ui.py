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
    QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

import asus_control
from core import (AlertPolicy, MODELS, Reading, atomic_json, load_config,
                   read_all, read_poll_file, user_data_dir)
from icons import ROOT, app_icon, symbol


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


def label(text="", role="body"):
    value = QLabel(text)
    value.setProperty("role", role)
    return value


class Switch(QCheckBox):
    def __init__(self, text, checked=False):
        super().__init__()
        self.setAccessibleName(text)
        self.setToolTip(text)
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
        self.caption = QLabel()
        self.indicator = QLabel()
        self.indicator.setFixedSize(13, 16)
        for child in (self.caption, self.indicator):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.caption, 1)
        row.addWidget(self.indicator)
        self.clicked.connect(self.open_menu)

    def addItem(self, text, data):
        self.items.append((text, data))
        action = self.choice_menu.addAction(text)
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
        self.setAccessibleDescription(self.items[index][0])
        for i, action in enumerate(self.choice_menu.actions()):
            action.setChecked(i == index)
        self.currentIndexChanged.emit(index)

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
                 "headset": "Nova Pro Wireless · 2.4G"}

    def __init__(self, key):
        super().__init__()
        self.key = key
        self.setObjectName("deviceCard")
        self.setMinimumHeight(104)
        self.reading = Reading(key, *MODELS[key])
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
        outer.addLayout(lower)

    def render(self, reading, config, stale=False):
        self.reading = reading
        c = self.colors
        self.glyph.setPixmap(symbol(self.key, c["text"], 29).pixmap(QSize(29, 29)))
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
        self.setToolTip(reading.error or ("耳机按 0–8 档换算，显示近似电量；耳机不发送低电量提醒。"
                                        if reading.approximate else ""))
        self.setAccessibleName(f"{reading.title} {self.percentage.text()} {state}")


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
        self.requested_key = None

    def start(self, key=None):
        if self.active:
            return False
        self.active = True
        self.timed_out = False
        self.requested_key = key
        self.output = self.data_dir / f"poll-{uuid.uuid4().hex}.json"
        args = ["--poll-output", str(self.output)]
        if key:
            args += ["--poll-key", key]
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
            readings = read_poll_file(self.output, [self.requested_key] if self.requested_key else None)
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


class Panel(QWidget):
    def __init__(self, controller):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.controller = controller
        QApplication.instance().installEventFilter(self)
        self.setWindowTitle("电量岛 · ChargeDock")
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
        self.cards = {}
        for key in MODELS:
            card = DeviceCard(key)
            self.cards[key] = card
            main.addWidget(card)
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
            row.addWidget(caption, 1)
            spin = QSpinBox()
            spin.setRange(1, 99)
            spin.setSingleStep(5)
            spin.setSuffix(" %")
            spin.setValue(self.controller.config["alerts"][key]["threshold"])
            spin.setAccessibleName(f"{title}低电量提醒阈值")
            spin.setFixedWidth(84)
            spin.setMinimumHeight(32)
            spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
            spin.setToolTip("输入 1–99%，或用上下箭头调整")
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
            combo.setMinimumHeight(32)
            combo.setMinimumWidth(105)
            for text, value in choices:
                combo.addItem(text, value)
            combo.setCurrentIndex(combo.findData(self.controller.config[name]))
            combo.currentIndexChanged.connect(lambda _, c=combo, n=name: self.controller.setting(n, c.currentData()))
            row.addWidget(combo)
            layout.addLayout(row)
        for title, name, checked in (("提醒时播放提示音", "sound", self.controller.config["sound"]),
                                      ("登录时自动启动", "startup", asus_control.startup_enabled())):
            row = QHBoxLayout()
            caption = label(title)
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
        height = (self.main_page.layout().sizeHint().height() if self.stack.currentWidget() == self.main_page
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
        if not self.isActiveWindow() and QApplication.activePopupWidget() is None:
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
        self.policy = AlertPolicy()
        self.readings = {key: Reading(key, *value) for key, value in MODELS.items()}
        self.stale = False
        self.last_updated = 0
        self.admin_result = None
        self.admin_started = 0
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.panel = Panel(self)
        self.panel.apply_theme(args.snapshot_theme if args.snapshot else None)
        self.tray.setToolTip("电量岛 · 正在读取设备")
        self.tray.activated.connect(self.activated)
        self.tray.messageClicked.connect(lambda: self.panel.reveal())
        self.menu = QMenu()
        self.menu.addAction("显示电量", lambda: self.panel.reveal())
        self.menu.addAction("偏好设置", lambda: self.panel.reveal(settings=True))
        self.menu.addAction("立即刷新", self.refresh)
        self.menu.addAction("暂停提醒 30 分钟", self.snooze)
        self.menu.addSeparator()
        self.menu.addAction("退出电量岛", app.quit)
        self.tray.setContextMenu(self.menu)
        self.poller = Poller(self.data_dir)
        self.poller.completed.connect(self.receive)
        self.wake_timer = QTimer(self)
        self.wake_timer.setSingleShot(True)
        self.wake_timer.timeout.connect(self.refresh_after_wake)
        self.headset_retry = QTimer(self)
        self.headset_retry.setInterval(5000)
        self.headset_retry.timeout.connect(self.retry_headset)
        self.wake_monitor = None
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start(self.config["interval"] * 1000)
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
        if self.closing:
            return
        if self.poller.start():
            self.panel.refresh_button.setEnabled(False)
            self.panel.updated_label.setText("正在更新…")

    def request_wake_refresh(self):
        if self.closing:
            return
        if not self.wake_timer.isActive():
            self.wake_timer.start(650)

    def refresh_after_wake(self):
        if self.poller.active:
            self.wake_timer.start(250)
        else:
            self.refresh()

    def retry_headset(self):
        if not self.closing and not self.poller.active:
            self.poller.start("headset")

    def receive(self, readings, error):
        if self.closing:
            return
        self.panel.refresh_button.setEnabled(True)
        self.stale = bool(error) or (self.stale and len(readings) < len(MODELS))
        if readings:
            self.readings.update({r.key: r for r in readings})
            self.last_updated = time.time()
            warnings = self.policy.evaluate(readings, self.config)
            if warnings and not self.args.snapshot:
                message = "；".join(f"{r.title}剩余 {r.level}%" for r in warnings)
                self.tray.showMessage("设备电量偏低", message + "，可以准备充电了。",
                                      QSystemTrayIcon.MessageIcon.Warning, 7000)
                if self.config["sound"]:
                    import winsound
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            headset = self.readings["headset"]
            if not self.args.snapshot and headset.level is None and (
                    "未开机" in headset.error or "未回复" in headset.error):
                if not self.headset_retry.isActive():
                    self.headset_retry.start()
            else:
                self.headset_retry.stop()
        for key, card in self.panel.cards.items():
            card.render(self.readings[key], self.config, self.stale)
        self.panel.updated_label.setText("上次更新 " + time.strftime("%H:%M", time.localtime(self.last_updated))
                                        if self.last_updated else "尚未读取到电量")
        self.panel.notice.setText(error or ("提醒已暂停 30 分钟" if time.time() < self.policy.snoozed_until else ""))
        self.panel.notice.setVisible(bool(self.panel.notice.text()))
        self.panel.resize_to_content()
        tooltip = ["电量岛 · ChargeDock"]
        low = False
        for r in self.readings.values():
            tooltip.append(f"{r.title}：" + (("约 " if r.approximate else "") + f"{r.level}%" if r.level is not None else "未连接"))
            if r.key in ("mouse", "keyboard") and r.level is not None:
                item = self.config["alerts"][r.key]
                low |= item["enabled"] and r.level <= item["threshold"] and not r.charging
        self.tray.setToolTip("\n".join(tooltip))
        self.tray.setIcon(symbol("tray", self.panel.colors["warning"] if low else self.panel.colors["accent"], 32))
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
            card.render(self.readings[k], self.config, self.stale)

    def setting(self, name, value):
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
        self.refresh_timer.setInterval(self.config["interval"] * 1000)
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
        self.headset_retry.stop()
        if self.wake_monitor:
            self.wake_monitor.close()
            self.wake_monitor = None
        self.refresh_timer.stop()
        self.admin_timer.stop()
        self.poller.stop()
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
