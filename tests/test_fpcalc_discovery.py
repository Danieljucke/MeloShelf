"""Locating the Chromaprint executable: an explicit setting, then the environment, then PATH."""
from pathlib import Path

from meloshelf.infrastructure.fingerprint import locate_fpcalc


def make_exe(tmp_path, name='fpcalc.exe'):
    tmp_path.mkdir(parents=True, exist_ok=True)
    exe = tmp_path / name
    exe.write_bytes(b'MZ')
    return exe


def no_path(name):
    return None


def test_configured_path_wins_over_environment_and_path(tmp_path):
    configured = make_exe(tmp_path / 'a')
    from_env = make_exe(tmp_path / 'b')
    location = locate_fpcalc(configured=str(configured), env={'MELOSHELF_FPCALC': str(from_env)},
                             which=lambda name: '/on/path/fpcalc')
    assert location.path == str(configured) and location.source == 'setting'


def test_environment_variable_wins_over_path(tmp_path):
    from_env = make_exe(tmp_path)
    location = locate_fpcalc(env={'MELOSHELF_FPCALC': str(from_env)}, which=lambda name: '/on/path/fpcalc')
    assert location.path == str(from_env) and location.source == 'environment'


def test_path_is_used_when_nothing_is_configured():
    location = locate_fpcalc(env={}, which=lambda name: '/usr/bin/fpcalc')
    assert location.path == '/usr/bin/fpcalc' and location.source == 'PATH'


def test_nothing_found_is_reported_without_guessing_locations():
    location = locate_fpcalc(env={}, which=no_path)
    assert location.path is None and location.source == 'none'
    assert 'MELOSHELF_FPCALC' in location.problem


def test_a_configured_path_that_does_not_exist_is_reported_not_ignored(tmp_path):
    missing = tmp_path / 'missing' / 'fpcalc.exe'
    location = locate_fpcalc(configured=str(missing), env={}, which=lambda name: '/usr/bin/fpcalc')
    assert location.path is None
    assert 'does not exist' in location.problem
    assert str(missing) in location.problem


def test_a_relative_or_empty_setting_is_treated_as_not_configured(tmp_path):
    location = locate_fpcalc(configured='  ', env={}, which=lambda name: '/usr/bin/fpcalc')
    assert location.source == 'PATH'


def test_a_conventional_install_folder_is_found_when_path_has_nothing(tmp_path):
    exe = make_exe(tmp_path / 'chromaprint')
    location = locate_fpcalc(env={}, which=no_path, common_dirs=[str(tmp_path / 'nowhere'), str(tmp_path / 'chromaprint')])
    assert location.path == str(exe) and location.source == 'common location'


def test_an_explicit_setting_still_beats_a_conventional_folder(tmp_path):
    configured = make_exe(tmp_path / 'a')
    make_exe(tmp_path / 'b')
    location = locate_fpcalc(configured=str(configured), env={}, which=no_path, common_dirs=[str(tmp_path / 'b')])
    assert location.path == str(configured) and location.source == 'setting'


def test_an_fpcalc_installed_while_the_app_runs_is_used_by_the_next_identification_without_a_restart(tmp_path):
    from meloshelf.application.webapp import create_server
    from meloshelf.infrastructure.secrets import MemorySecretStore
    local = tmp_path / 'lad'
    local.mkdir()
    html = tmp_path / 'index.html'
    html.write_text('x', encoding='utf-8')
    server = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                           environ={'LOCALAPPDATA': str(local), 'MELOSHELF_ACOUSTID_KEY': 'k', 'PATH': ''})
    try:
        check = server.jobs.preflight
        assert check(False) == 'fpcalc_unavailable', 'nothing installed yet'
        make_exe(local / 'chromaprint')                             # the person installs Chromaprint now
        assert check(False) == '', 'found at the next check, no restart'
    finally:
        server.server_close()
