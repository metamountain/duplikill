"""Builds dist\\Duplikill\\Duplikill.exe and removes Qt parts Duplikill never uses.

Run from the project folder: .venv\\Scripts\\python.exe -I tools\\build_exe.py
"""
import glob
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYI = os.path.join(ROOT, ".venv", "Scripts", "pyinstaller.exe")
DIST = os.path.join(ROOT, "dist", "Duplikill")
UNUSED = [  # relative to _internal\PySide6
    "opengl32sw.dll", "translations", "Qt6Quick.dll", "Qt6Qml.dll", "Qt6QmlModels.dll", "Qt6QmlMeta.dll",
    "Qt6QmlWorkerScript.dll", "Qt6Pdf.dll", "Qt6VirtualKeyboard.dll", "Qt6Network.dll", "QtNetwork.pyd",
    "Qt6OpenGL.dll", "plugins/platforminputcontexts", "plugins/networkinformation", "plugins/tls",
    "plugins/imageformats/qpdf.dll", "plugins/generic",
]


def build():
    a = lambda *p: os.path.join(ROOT, *p)
    cmd = [PYI, "--noconfirm", "--onedir", "--windowed", "--name", "Duplikill",
           "--icon", a("assets", "duplikill.ico"),
           "--add-data", a("tools", "windows_czkawka_cli.exe") + ";tools",
           "--add-data", a("tools", "fonts") + ";tools/fonts",
           "--add-data", a("assets", "duplikill.png") + ";assets",
           "--workpath", a("tmp", "build"), "--specpath", a("tmp", "build"), "--distpath", a("dist")]
    for m in ("pytest", "_pytest", "pygments", "torch", "tkinter", "PyInstaller", "PySide6.QtNetwork",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf", "PySide6.QtOpenGL"):
        cmd += ["--exclude-module", m]
    cmd.append(a("run.py"))
    subprocess.run(cmd, check=True, cwd=ROOT)


def strip():
    base = os.path.join(DIST, "_internal", "PySide6")
    for rel in UNUSED:
        for p in glob.glob(os.path.join(base, rel)):
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)


def size_mb(path) -> float:
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs) / 2 ** 20


def smoke_test() -> bool:
    """Starts the exe for a few seconds: it must still be running (no crash on import)."""
    p = subprocess.Popen([os.path.join(DIST, "Duplikill.exe")])
    time.sleep(6)
    alive = p.poll() is None
    if alive:
        p.kill()
    return alive


if __name__ == "__main__":
    build()
    strip()
    print(f"Groesse: {size_mb(DIST):.0f} MB")
    ok = smoke_test()
    print("Probestart:", "OK" if ok else "ABGESTUERZT")
    sys.exit(0 if ok else 1)
