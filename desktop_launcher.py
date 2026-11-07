"""Смеч 0.6 Desktop: one native window on Windows / macOS, no external cloud."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from desktop_runtime import (APP_PORT, daily_sqlite_backup, prepare_desktop_environment,
                             reserve_port, start_local_server, user_data_dir)


def main() -> int:
    folder = user_data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('smech.desktop')
    logger.setLevel(logging.INFO)
    logger.addHandler(RotatingFileHandler(folder / 'desktop.log', maxBytes=500_000,
                                         backupCount=2, encoding='utf-8'))
    listener = None
    server = thread = None
    try:
        prepare_desktop_environment(folder)
        daily_sqlite_backup(folder / 'workspace.sqlite3', folder / 'backups')
        # Import after environment configuration: workspace DB connects at import.
        from app import app
        # Import the platform-specific UI backend only when launching the window.
        import webview
        listener = reserve_port(APP_PORT)
        server, thread = start_local_server(app, listener)
        webview.create_window('Смеч — Ведомости и экономика СМР',
                              f'http://127.0.0.1:{APP_PORT}/',
                              width=1380, height=860, min_size=(900, 600),
                              background_color='#f6f7fa')
        kwargs = {'private_mode': False,
                  'storage_path': str(folder / 'webview'), 'debug': False}
        if sys.platform == 'win32':
            # Avoid the unsupported legacy Internet Explorer rendering engine.
            kwargs['gui'] = 'edgechromium'
        webview.start(**kwargs)
        return 0
    except OSError as exc:
        logger.exception('Cannot reserve local port or access data directory')
        _show_error(f'Невозможно запустить «Смеч». Возможно, программа уже открыта.\n\n{exc}\n\nЖурнал: {folder / "desktop.log"}')
        return 1
    except Exception as exc:
        logger.exception('Desktop launch failed')
        _show_error(f'Не удалось запустить «Смеч».\n{exc}\n\nЖурнал: {folder / "desktop.log"}')
        return 1
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        if listener is not None:
            listener.close()


def self_test() -> int:
    """Headless packaged-runtime smoke test for native CI runners."""
    import tempfile
    from urllib.request import urlopen
    from desktop_runtime import reserve_port, start_local_server
    with tempfile.TemporaryDirectory(prefix='smech-native-test-') as temp:
        prepare_desktop_environment(Path(temp))
        from app import app
        from pdfio import parse_pdf
        from excelio import parse_xlsx
        import webview  # Verify native UI runtime is bundled, but do not open a window.
        listener = reserve_port(0)
        server = thread = None
        try:
            server, thread = start_local_server(app, listener, timeout=12)
            base = f'http://127.0.0.1:{listener.getsockname()[1]}'
            with urlopen(base + '/api/health', timeout=5) as reply:
                assert reply.status == 200 and b'"status":"ok"' in reply.read()
            with urlopen(base, timeout=5) as reply:
                assert reply.status == 200 and b'index.html' not in reply.read(512)
            with urlopen(base + '/static/app.js', timeout=5) as reply:
                assert reply.status == 200 and len(reply.read()) > 10_000
            print('Smech desktop self-test: OK', flush=True)
            return 0
        finally:
            if server is not None:
                server.should_exit = True
            if thread is not None:
                thread.join(timeout=5)
            listener.close()




def _show_error(message: str) -> None:
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, 'Смеч: ошибка', 0x10)
            return
        except Exception:
            pass
    elif sys.platform == 'darwin':
        try:
            import subprocess
            # subprocess args, not a shell command, prevent command injection.
            short = message.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
            subprocess.run(['osascript', '-e', f'display alert "Смеч: ошибка" message "{short}"'],
                           check=False, timeout=8)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


if __name__ == '__main__':
    raise SystemExit(self_test() if '--self-test' in sys.argv else main())
