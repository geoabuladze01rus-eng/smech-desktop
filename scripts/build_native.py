"""Build Смеч on the target OS. Requires requirements-desktop.txt."""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build():
    if sys.platform not in ('win32', 'darwin'):
        raise SystemExit('Для Windows .exe сборка выполняется на Windows; для macOS .app — на macOS.')
    assets = ROOT / 'desktop' / 'assets'
    icon = assets / ('Smech.ico' if sys.platform == 'win32' else 'Smech.icns')
    if not icon.is_file():
        raise SystemExit(f'Нет иконки: {icon}')
    cmd = [sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
           '--name', 'Smech', '--windowed', '--onedir',
           '--icon', str(icon),
           '--add-data', f'{ROOT / "static"}{os.pathsep}static',
           '--collect-all', 'webview',
           '--collect-all', 'pypdfium2',
           '--collect-all', 'pdfplumber',
           '--collect-all', 'argon2',
           '--collect-all', 'pytesseract',
           '--collect-all', 'cv2',
           '--hidden-import', 'uvicorn.loops.asyncio',
           '--hidden-import', 'uvicorn.protocols.http.h11_impl',
           '--hidden-import', 'uvicorn.lifespan.on',
           '--distpath', str(ROOT / 'dist'),
           '--workpath', str(ROOT / 'build'),
           '--specpath', str(ROOT)]
    if sys.platform == 'darwin':
        cmd.extend(['--osx-bundle-identifier', 'ru.smech.desktop'])
    cmd.append(str(ROOT / 'desktop_launcher.py'))
    print('Platform:', platform.platform(), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)
    if sys.platform == 'win32':
        dest = ROOT / 'dist' / 'Smech' / 'Smech.exe'
    else:
        dest = ROOT / 'dist' / 'Smech.app' / 'Contents' / 'MacOS' / 'Smech'
    if not dest.is_file():
        raise SystemExit(f'Ошибка: исполняемый файл не создан: {dest}')
    print(f'Built: {dest}', flush=True)


if __name__ == '__main__':
    build()
