"""Offline desktop runtime primitives for Смеч (no GUI imports).

The HTTP origin is intentionally stable so WebKit/WebView2 localStorage survives
updates and restarts. The listener is reserved before the UI is opened.
"""
from __future__ import annotations

import os
import socket
import sqlite3
from contextlib import closing
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

APP_PORT = 18765


def user_data_dir(platform: str | None = None, environment: dict | None = None,
                  home: Path | None = None) -> Path:
    platform = platform or sys.platform
    env = environment if environment is not None else os.environ
    home = home or Path.home()
    if platform == 'win32':
        base = Path(env.get('LOCALAPPDATA') or home / 'AppData' / 'Local') / 'Smech'
    elif platform == 'darwin':
        base = home / 'Library' / 'Application Support' / 'Smech'
    else:
        base = Path(env.get('XDG_DATA_HOME') or home / '.local' / 'share') / 'Smech'
    return base


def sqlite_url(db_path: Path) -> str:
    # SQLAlchemy uses forward-slash paths on Windows as well.
    return 'sqlite:///' + db_path.resolve().as_posix()


def prepare_desktop_environment(data_dir: Path) -> None:
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (data_dir / 'webview').mkdir(mode=0o700, exist_ok=True)
    (data_dir / 'backups').mkdir(mode=0o700, exist_ok=True)
    os.environ['SMECH_ENABLE_WORKSPACE'] = '1'
    os.environ['SMECH_DATABASE_URL'] = sqlite_url(data_dir / 'workspace.sqlite3')
    # Do not trust development environment variables in a packaged application.
    os.environ.pop('SMECH_SETUP_KEY', None)
    os.environ.pop('SMECH_ALLOWED_HOSTS', None)


def daily_sqlite_backup(db_path: Path, backup_dir: Path, keep: int = 14) -> Path | None:
    """SQLite online backup, not an unsafe bytewise copy of an active database."""
    if not db_path.is_file():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f'workspace-{date.today().isoformat()}.sqlite3'
    if target.exists():
        return target
    with closing(sqlite3.connect(str(db_path))) as src:
        with closing(sqlite3.connect(str(target))) as dest:
            src.backup(dest)
    for old in sorted(backup_dir.glob('workspace-????-??-??.sqlite3'), reverse=True)[keep:]:
        old.unlink()
    return target


def reserve_port(port: int = APP_PORT) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == 'win32' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(('127.0.0.1', port))
        sock.listen(128)
        sock.set_inheritable(False)
        return sock
    except Exception:
        sock.close()
        raise


def start_local_server(app: Any, listener: socket.socket, timeout: float = 15.0):
    import uvicorn
    config = uvicorn.Config(app, host='127.0.0.1', port=listener.getsockname()[1],
                            access_log=False, log_level='warning', server_header=False,
                            proxy_headers=False, lifespan='on')
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]},
                              name='SmechOfflineServer', daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        raise RuntimeError('Локальный сервер «Смеч» не запустился')
    return server, thread
