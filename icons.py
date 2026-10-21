from pathlib import Path
import sys

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
PATHS = {
    "chevrons": '<path d="m11 12 5-5 5 5m-10 8 5 5 5-5"/>',
    "mouse": '<rect x="7" y="3" width="18" height="26" rx="9"/><path d="M16 4v8M8 13h16"/>',
    "keyboard": '<rect x="2" y="7" width="28" height="19" rx="4"/><path d="M7 12h1m4 0h1m4 0h1m4 0h1M7 17h1m4 0h1m4 0h1m4 0h1M10 22h12"/>',
    "headset": '<path d="M5 18v-3a11 11 0 0 1 22 0v3M7 26H6a3 3 0 0 1-3-3v-4a3 3 0 0 1 3-3h1zM25 26h1a3 3 0 0 0 3-3v-4a3 3 0 0 0-3-3h-1z"/>',
    "settings": '<path d="M6 8h20M6 16h20M6 24h20"/><circle cx="12" cy="8" r="3" fill="BACKGROUND"/><circle cx="22" cy="16" r="3" fill="BACKGROUND"/><circle cx="11" cy="24" r="3" fill="BACKGROUND"/>',
    "refresh": '<path d="M26 11A11 11 0 1 0 27 20M26 4v8h-8"/>',
    "close": '<path d="m9 9 14 14M23 9 9 23"/>',
    "back": '<path d="m19 7-9 9 9 9"/>',
    "power": '<path d="M16 3v12M9 7a12 12 0 1 0 14 0"/>',
    "tray": '<path d="M24 9A11 11 0 1 0 27 19"/><path d="M11 18v4m5-9v9m5-13v13"/><circle cx="27" cy="13" r="1.2" fill="COLOR" stroke="none"/>',
}


def symbol(name: str, color: str, size=24, background="none") -> QIcon:
    shapes = PATHS[name].replace("BACKGROUND", background).replace("COLOR", color)
    data = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><g fill="none" stroke="{color}" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">{shapes}</g></svg>'
    renderer = QSvgRenderer(QByteArray(data.encode()))
    icon = QIcon()
    for scale in (1, 2, 3):
        pixmap = QPixmap(size*scale, size*scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        icon.addPixmap(pixmap)
    return icon


def app_icon() -> QIcon:
    return QIcon(str(ROOT / "assets/app.svg"))
