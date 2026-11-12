"""Logs keep their diagnostic value but never carry credentials, the session token or private paths.

Redaction is applied when a log record is created, so it covers every logger, every handler (including pytest's)
and the text of tracebacks, not only the call sites that exist today.
"""
import json
import logging
import sqlite3
import threading
from pathlib import Path

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.infrastructure import log_redaction
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request

KEY = 'AbCdEf1234_xyz-SECRETVALUE'
CONTACT = 'person.private@example.org'


@pytest.fixture(autouse=True)
def redaction():
    log_redaction.install()
    yield
    log_redaction.forget_all()


def test_a_registered_secret_is_removed_from_messages_and_tracebacks(caplog):
    log_redaction.register(KEY)
    log = logging.getLogger('meloshelf.test')
    with caplog.at_level(logging.DEBUG):
        log.warning('lookup used client key %s', KEY)
        try:
            raise ValueError(f'request failed https://api.acoustid.org/v2/lookup?client={KEY}&meta=recordings')
        except ValueError:
            log.exception('identification failed')
    assert KEY not in caplog.text
    assert 'identification failed' in caplog.text and 'ValueError' in caplog.text, 'the diagnosis stays'


def test_an_unregistered_client_parameter_is_redacted_by_its_shape(caplog):
    with caplog.at_level(logging.DEBUG):
        logging.getLogger('meloshelf.test').warning('url was https://api.acoustid.org/v2/lookup?client=Zz99Yy88&x=1')
    assert 'Zz99Yy88' not in caplog.text and 'client=[redacted]' in caplog.text


def test_the_home_directory_is_shortened_so_the_account_name_is_not_logged(caplog):
    home = str(Path.home())
    with caplog.at_level(logging.DEBUG):
        logging.getLogger('meloshelf.test').info('reading %s', str(Path.home() / 'Music' / 'song.mp3'))
    assert home not in caplog.text and 'song.mp3' in caplog.text


def test_the_contact_and_the_session_token_are_registered_by_the_application(tmp_path, caplog):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={'MELOSHELF_MUSICBRAINZ_USER_AGENT': f'MeloShelf/test ({CONTACT})'})
    try:
        running.config.musicbrainz_user_agent()
        with caplog.at_level(logging.DEBUG):
            logging.getLogger('meloshelf.test').warning('token=%s agent contact=%s', running.token, CONTACT)
        assert running.token not in caplog.text and CONTACT not in caplog.text
    finally:
        running.server_close()


def test_a_saved_acoustid_key_is_registered_when_it_is_read(tmp_path, caplog):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    store = MemorySecretStore()
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=store, environ={})
    try:
        running.config.set_acoustid_key(KEY)
        running.config.acoustid_key()
        with caplog.at_level(logging.DEBUG):
            logging.getLogger('meloshelf.test').error('failed with %s', KEY)
        assert KEY not in caplog.text
    finally:
        running.server_close()


def test_a_request_error_is_logged_with_its_route_and_not_its_query(tmp_path, caplog, monkeypatch):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(), environ={})
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        def broken(*_args, **_kwargs):
            raise sqlite3.OperationalError('disk I/O error')
        monkeypatch.setattr(running.repo, 'counts', broken)
        base = f'http://127.0.0.1:{running.server_address[1]}'
        with caplog.at_level(logging.DEBUG):
            status, _, _ = request(base, 'GET', '/api/stats?folder=D:/Private/Diary')
        assert status == 500
        assert '/api/stats' in caplog.text and 'Private' not in caplog.text and 'Diary' not in caplog.text
    finally:
        running.shutdown()
        running.server_close()


def test_an_unreadable_library_file_is_logged_by_error_class_not_by_its_path(tmp_path, caplog):
    from test_jobs import FakeRecognition, make_service, run_scan  # noqa: F401
    from meloshelf.infrastructure.database import Database
    root = tmp_path / 'Secret Album'
    root.mkdir()
    (root / 'a.mp3').write_bytes(b'x' * 64)
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()

    def reader(path):
        raise PermissionError(13, 'Access is denied', str(path))

    service = make_service(db, reader=reader)
    with caplog.at_level(logging.DEBUG):
        run_scan(service, root)
    assert 'Secret Album' not in caplog.text
    assert 'PermissionError' in caplog.text or 'errno=13' in caplog.text
    assert json.dumps([])  # keeps the import used
