#!/usr/bin/env python3
"""Installe « Whiteboard » dans le menu des applications (Linux, freedesktop).

  python3 install_desktop.py              # icône, entrée de menu, commande ~/.local/bin/whiteboard
  python3 install_desktop.py --uninstall

Rien n'est copié : la commande et l'entrée de menu pointent vers ce dossier.
"""
from __future__ import annotations

import argparse, os, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
DATA = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share"))
APPS, ICON_ROOT, BIN = DATA / "applications", DATA / "icons/hicolor", HOME / ".local/bin"
APP_ID = "com.delata.Whiteboard"
DESKTOP_FILE = APPS / f"{APP_ID}.desktop"
ICON_FILE = ICON_ROOT / "scalable/apps/whiteboard.svg"
LAUNCHER = BIN / "whiteboard"
PYTHON = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable   # celui qui a GTK et WebKit
PNG_SIZES = (48, 64, 128, 256)

DESKTOP = """[Desktop Entry]
Type=Application
Name=Whiteboard
GenericName=Prof vocal au tableau
Comment=Un prof qui t'explique à l'oral, avec un tableau (cahier) et la dictée de tes calculs
Exec="{launcher}"
Icon=whiteboard
Terminal=false
Categories=Education;Science;
Keywords=prof;tableau;cours;cahier;vocal;MVA;
StartupNotify=true
StartupWMClass=Whiteboard
"""

LAUNCHER_SH = """#!/bin/sh
# Lance Whiteboard (fenêtre native GTK + WebKit ; le Prof tourne dans le .venv du projet).
exec "{python}" "{script}" "$@"
"""


def install() -> None:
    for d in (APPS, ICON_FILE.parent, BIN):
        d.mkdir(parents=True, exist_ok=True)
    src = ROOT / "assets/whiteboard.svg"
    ICON_FILE.write_text(src.read_text())
    tool = shutil.which("rsvg-convert")
    for size in PNG_SIZES:
        out = ICON_ROOT / f"{size}x{size}/apps/whiteboard.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        if tool:
            subprocess.run([tool, "-w", str(size), "-h", str(size), str(src), "-o", str(out)], capture_output=True)
    LAUNCHER.write_text(LAUNCHER_SH.format(python=PYTHON, script=ROOT / "whiteboard.py"))
    LAUNCHER.chmod(0o755)
    DESKTOP_FILE.write_text(DESKTOP.format(launcher=LAUNCHER))
    DESKTOP_FILE.chmod(0o755)
    for cmd in (["update-desktop-database", str(APPS)], ["gtk-update-icon-cache", "-f", "-t", "--ignore-theme-index", str(ICON_ROOT)]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, capture_output=True)
    print(f"✔ {DESKTOP_FILE}\n✔ {ICON_FILE}\n✔ {LAUNCHER}\n\nLance « Whiteboard » depuis le menu des applications, ou : whiteboard")


def uninstall() -> None:
    for f in (DESKTOP_FILE, ICON_FILE, LAUNCHER, *(ICON_ROOT / f"{s}x{s}/apps/whiteboard.png" for s in PNG_SIZES)):
        if f.exists():
            f.unlink()
            print(f"✔ supprimé {f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true")
    uninstall() if ap.parse_args().uninstall else install()
