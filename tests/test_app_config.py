"""Application configuration: precedence, secure storage, validation and diagnostics.

Secrets must never appear in summaries, diagnostics or error messages.
"""
import pytest

from meloshelf.application.config import AppConfig, ConfigError, startup_diagnostics
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.secrets import MemorySecretStore, UnavailableSecretStore

KEY = 'AbCdEf1234_xyz-SECRETVALUE'


def config(tmp_path, *, env=None, secrets=None, which=lambda name: None):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    return AppConfig(MatchStore(database), secrets if secrets is not None else MemorySecretStore(),
                     env=env or {}, which=which)


# ── precedence ───────────────────────────────────────────────────────────────

def test_stored_key_takes_precedence_over_environment(tmp_path):
    secrets = MemorySecretStore()
    settings = config(tmp_path, env={'MELOSHELF_ACOUSTID_KEY': 'EnvValueForTests1'}, secrets=secrets)
    settings.set_acoustid_key(KEY)
    assert settings.acoustid_key() == (KEY, 'secure store')


def test_environment_is_the_fallback_when_nothing_is_stored(tmp_path):
    settings = config(tmp_path, env={'MELOSHELF_ACOUSTID_KEY': 'EnvValueForTests1'})
    assert settings.acoustid_key() == ('EnvValueForTests1', 'environment')


def test_missing_key_is_reported_as_none(tmp_path):
    assert config(tmp_path).acoustid_key() == ('', 'none')


def test_contact_precedence_is_stored_then_environment(tmp_path):
    settings = config(tmp_path, env={'MELOSHELF_MUSICBRAINZ_USER_AGENT': 'MeloShelf/0.2.1 (env@example.org)'})
    assert settings.musicbrainz_user_agent() == ('MeloShelf/0.2.1 (env@example.org)', 'environment')
    settings.set_musicbrainz_contact('ana@example.org')
    assert settings.musicbrainz_user_agent() == ('MeloShelf/0.2.1 (ana@example.org)', 'settings')


def test_fpcalc_setting_then_environment_then_path(tmp_path):
    exe = tmp_path / 'fpcalc.exe'
    exe.write_bytes(b'MZ')
    settings = config(tmp_path, env={'MELOSHELF_FPCALC': str(tmp_path / 'other.exe')}, which=lambda n: '/usr/bin/fpcalc')
    settings.set_fpcalc_path(str(exe))
    location = settings.fpcalc_location()
    assert location.path == str(exe) and location.source == 'setting'
    settings.set_fpcalc_path('')
    assert settings.fpcalc_location().source in ('environment', 'PATH', 'none')


# ── secure storage ───────────────────────────────────────────────────────────

def test_key_is_never_written_to_the_database_or_the_settings_table(tmp_path):
    settings = config(tmp_path)
    settings.set_acoustid_key(KEY)
    store = settings.store
    assert KEY not in repr(store.automation_settings())
    import sqlite3
    raw = sqlite3.connect(tmp_path / 'lib.sqlite3')
    dump = '\n'.join(str(row) for table in ('app_settings', 'remote_cache') for row in raw.execute(f'SELECT * FROM {table}'))
    raw.close()
    assert KEY not in dump


def test_without_a_secure_store_the_key_is_refused_not_saved_in_plain_text(tmp_path):
    settings = config(tmp_path, secrets=UnavailableSecretStore('no credential service'))
    with pytest.raises(ConfigError, match='secure credential store'):
        settings.set_acoustid_key(KEY)
    assert settings.acoustid_key() == ('', 'none')


def test_clearing_the_stored_key_falls_back_to_the_environment(tmp_path):
    settings = config(tmp_path, env={'MELOSHELF_ACOUSTID_KEY': 'EnvValueForTests1'})
    settings.set_acoustid_key(KEY)
    settings.clear_acoustid_key()
    assert settings.acoustid_key() == ('EnvValueForTests1', 'environment')


# ── validation ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('bad', ['', '   ', 'short', 'has spaces in it', 'semi;colon-value-here', 'x' * 129])
def test_invalid_keys_are_rejected(tmp_path, bad):
    with pytest.raises(ConfigError):
        config(tmp_path).set_acoustid_key(bad)


@pytest.mark.parametrize('bad', ['no-at-sign', 'a@b', ' ', 'x' * 200])
def test_invalid_contacts_are_rejected(tmp_path, bad):
    with pytest.raises(ConfigError):
        config(tmp_path).set_musicbrainz_contact(bad)


def test_fpcalc_path_must_exist(tmp_path):
    with pytest.raises(ConfigError, match='does not exist'):
        config(tmp_path).set_fpcalc_path(str(tmp_path / 'missing.exe'))


# ── summary and diagnostics never expose secrets ─────────────────────────────

def test_summary_reports_status_and_sources_without_values(tmp_path):
    settings = config(tmp_path, env={'MELOSHELF_MUSICBRAINZ_USER_AGENT': 'MeloShelf/0.2.1 (ops@example.org)'})
    settings.set_acoustid_key(KEY)
    summary = settings.summary()
    assert summary['acoustid'] == {'configured': True, 'source': 'secure store'}
    assert summary['musicbrainz'] == {'configured': True, 'source': 'environment'}
    assert summary['secure_store']['available'] is True
    assert KEY not in repr(summary) and 'ops@example.org' not in repr(summary)


def test_startup_diagnostics_name_each_requirement_and_hide_values(tmp_path):
    exe = tmp_path / 'fpcalc.exe'
    exe.write_bytes(b'MZ')
    settings = config(tmp_path, which=lambda n: str(exe))
    settings.set_acoustid_key(KEY)
    lines = startup_diagnostics(settings, fpcalc_check=lambda path: True)
    text = '\n'.join(lines)
    assert 'Chromaprint' in text and 'AcoustID' in text and 'MusicBrainz' in text
    assert 'missing' in text.lower(), 'an unset requirement must be named'
    assert KEY not in text


@pytest.mark.parametrize('good', ['https://github.com/Danieljucke/MeloShelf', 'http://example.org/contact'])
def test_a_project_url_is_an_accepted_musicbrainz_contact(tmp_path, good):
    # MusicBrainz accepts a contact URL or an email address; a project page is a valid, non-personal contact.
    settings = config(tmp_path)
    settings.set_musicbrainz_contact(good)
    assert settings.musicbrainz_user_agent() == (f'MeloShelf/0.2.1 ({good})', 'settings')


@pytest.mark.parametrize('bad', ['ftp://example.org', 'https://', 'javascript:alert(1)', 'https://exa mple.org'])
def test_a_non_web_contact_is_still_rejected(tmp_path, bad):
    with pytest.raises(ConfigError):
        config(tmp_path).set_musicbrainz_contact(bad)
