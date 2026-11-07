"""Desktop storage and server behavior without native UI, portable across CI OS."""
from pathlib import Path
import os
import sqlite3
from urllib.request import urlopen

import pytest

from desktop_runtime import (user_data_dir, sqlite_url, prepare_desktop_environment,
                             daily_sqlite_backup, reserve_port, start_local_server)


def test_data_directories_are_outside_program(tmp_path):
    assert user_data_dir('darwin', {}, tmp_path) == tmp_path / 'Library' / 'Application Support' / 'Smech'
    assert user_data_dir('win32', {'LOCALAPPDATA':str(tmp_path / 'local')}, tmp_path) == tmp_path / 'local' / 'Smech'
    assert user_data_dir('linux', {'XDG_DATA_HOME':str(tmp_path / '.data')}, tmp_path) == tmp_path / '.data' / 'Smech'
    assert sqlite_url(tmp_path / 'data.db').startswith('sqlite:///')


def test_desktop_environment_creates_isolated_user_storage(tmp_path, monkeypatch):
    monkeypatch.setenv('SMECH_ALLOWED_HOSTS','bad.example')
    monkeypatch.setenv('SMECH_SETUP_KEY','unused-key')
    prepare_desktop_environment(tmp_path / 'profile')
    assert (tmp_path / 'profile' / 'webview').is_dir()
    assert (tmp_path / 'profile' / 'backups').is_dir()
    assert os.environ['SMECH_ENABLE_WORKSPACE']=='1'
    assert os.environ['SMECH_DATABASE_URL'].endswith('workspace.sqlite3')
    assert 'SMECH_ALLOWED_HOSTS' not in os.environ
    assert 'SMECH_SETUP_KEY' not in os.environ


def test_backup_is_real_sqlite_database_and_retention(tmp_path):
    db=tmp_path / 'workspace.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('create table sample(v text)')
        conn.execute('insert into sample(v) values(?)', ('Пример',))
    target=daily_sqlite_backup(db,tmp_path / 'backups')
    assert target is not None and target.is_file()
    with sqlite3.connect(target) as conn:
        assert conn.execute('select v from sample').fetchone()[0]=='Пример'
    # Existing daily backup is not silently overwritten
    with sqlite3.connect(db) as conn:
        conn.execute('delete from sample')
    assert daily_sqlite_backup(db,tmp_path / 'backups')==target
    with sqlite3.connect(target) as conn:
        assert conn.execute('select count(*) from sample').fetchone()[0]==1


def test_port_is_exclusively_reserved():
    listener=reserve_port(0)
    try:
        port=listener.getsockname()[1]
        with pytest.raises(OSError):
            reserve_port(port)
    finally:
        listener.close()


def test_local_server_serves_real_fastapi_app():
    from app import app
    listener=reserve_port(0)
    server=thread=None
    try:
        server, thread=start_local_server(app,listener, timeout=6)
        port=listener.getsockname()[1]
        with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=5) as res:
            assert res.status==200
            assert b'"status":"ok"' in res.read()
        with urlopen(f'http://127.0.0.1:{port}/static/app.js', timeout=5) as res:
            assert res.status==200
    finally:
        if server: server.should_exit=True
        if thread: thread.join(timeout=5)
        listener.close()


def test_headless_smoke_test_without_native_window(monkeypatch):
    import sys
    import types
    from desktop_launcher import self_test
    monkeypatch.setitem(sys.modules, 'webview', types.ModuleType('webview'))
    assert self_test() == 0
