# Build with: python -m PyInstaller ChargeDock.spec --distpath ../ --workpath ../../work/chargedock-build
from pathlib import Path
import PySide6

root = Path(SPECPATH)
qt = Path(PySide6.__file__).parent
# Keep the C++ runtime next to the frozen Python DLL, as well as Qt's copies.
# This avoids mixing Python's older runtime with the runtime shipped by Qt.
runtime_names = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
                 "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_codecvt_ids.dll", "concrt140.dll")
runtime = [(str(qt / name), ".") for name in runtime_names if (qt / name).exists()]
a = Analysis([str(root / "chargedock.py")], pathex=[str(root)], binaries=runtime,
             datas=[(str(root / "assets"), "assets"), (str(root / "locales"), "locales")], hiddenimports=[],
             hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "tkinter"],
             noarchive=False, optimize=0)
# Qt uses the Windows system ICU/UCRT. Never collect same-named DLLs from
# unrelated command-line tools on the build host's PATH (e.g. Poppler).
system_dlls = {"icuuc.dll", "icuin.dll", "icudt78.dll", "ucrtbase.dll"}
a.binaries = [item for item in a.binaries
              if Path(item[0]).name.lower() not in system_dlls
              and not Path(item[0]).name.lower().startswith("api-ms-win-")
              and "codex-runtimes" not in item[1].lower()]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="ChargeDock",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False,
          icon=str(root / "assets/app.ico"))
