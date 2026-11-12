"""Runtime configuration: where each requirement comes from, and whether it is satisfied.

Precedence, highest first:
    secret key      secure credential store, then MELOSHELF_ACOUSTID_KEY
    contact         settings saved in the application, then MELOSHELF_MUSICBRAINZ_USER_AGENT
    fpcalc          settings saved in the application, then MELOSHELF_FPCALC, then PATH

Secrets are returned only to code that calls for them; summaries, diagnostics and API
responses report presence and source, never values.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

from meloshelf.infrastructure import log_redaction
from meloshelf.infrastructure.fingerprint import FpcalcLocation, locate_fpcalc, no_console_options
from meloshelf.infrastructure.secrets import SecretStoreError

ACOUSTID_KEY_NAME = 'acoustid-application-key'
ACOUSTID_ENV = 'MELOSHELF_ACOUSTID_KEY'
MUSICBRAINZ_ENV = 'MELOSHELF_MUSICBRAINZ_USER_AGENT'
FPCALC_ENV = 'MELOSHELF_FPCALC'
CONTACT_SETTING = 'musicbrainz_contact'
FPCALC_SETTING = 'fpcalc_path'
ART_SOURCES_SETTING = 'art_sources'
# optional artwork providers: secure-store name and environment variable for each key
ART_KEYS = {'fanart_project_key': ('fanart-project-key', 'MELOSHELF_FANART_PROJECT_KEY'),
            'fanart_client_key': ('fanart-personal-key', 'MELOSHELF_FANART_CLIENT_KEY'),
            'theaudiodb_key': ('theaudiodb-key', 'MELOSHELF_THEAUDIODB_KEY')}
ART_SOURCE_NAMES = ('fanart', 'theaudiodb')
_KEY_PATTERN = re.compile(r'^[A-Za-z0-9_-]{8,128}$')
_CONTACT_PATTERN = re.compile(r'^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$')
APP_VERSION = '0.2.1'


def _is_web_url(value: str) -> bool:
    """A project page is an accepted MusicBrainz contact: http(s) with a real host and no spaces."""
    from urllib.parse import urlparse
    if re.search(r'\s', value):
        return False
    parts = urlparse(value)
    return parts.scheme in ('http', 'https') and '.' in parts.hostname if parts.hostname else False


class ConfigError(ValueError):
    """A value was rejected; the message is safe to show to the user."""


class AppConfig:
    def __init__(self, store, secrets, *, env=None, which=shutil.which):
        import os
        self.store = store
        self.secrets = secrets
        self.env = os.environ if env is None else env
        self._which = which

    # ── AcoustID key ─────────────────────────────────────────────────────────

    def acoustid_key(self) -> tuple[str, str]:
        key, source, _ = self.acoustid_key_detail()
        return key, source

    def acoustid_key_detail(self) -> tuple[str, str, bool]:
        """(key, source, store_error). store_error means a saved key could not be read from the secure store."""
        store_error = False
        try:
            stored = self.secrets.get(ACOUSTID_KEY_NAME)
        except SecretStoreError:
            stored, store_error = None, True
        if stored:
            log_redaction.register(stored)
            return stored, 'secure store', False
        from_env = (self.env.get(ACOUSTID_ENV) or '').strip()
        if from_env:
            log_redaction.register(from_env)
            return from_env, 'environment', store_error
        return '', 'none', store_error

    def set_acoustid_key(self, value: str) -> None:
        cleaned = (value or '').strip()
        if not _KEY_PATTERN.fullmatch(cleaned):
            raise ConfigError('The AcoustID key has the wrong length or contains characters that are not allowed.')
        try:
            self.secrets.set(ACOUSTID_KEY_NAME, cleaned)
        except SecretStoreError as exc:
            raise ConfigError('No secure credential store is available, so the key cannot be saved safely. '
                              f'Set {ACOUSTID_ENV} instead.') from exc

    def clear_acoustid_key(self) -> None:
        self.secrets.delete(ACOUSTID_KEY_NAME)

    # ── optional artwork providers (Fanart.tv, TheAudioDB) ─────────────────────

    def art_key(self, name: str) -> str:
        """A provider key from the secure store, then the environment; '' when none. Never logged."""
        secret_name, env_name = ART_KEYS[name]
        try:
            stored = self.secrets.get(secret_name)
        except SecretStoreError:
            stored = None
        value = stored or (self.env.get(env_name) or '').strip()
        if value:
            log_redaction.register(value)
        return value

    def set_art_key(self, name: str, value: str) -> None:
        if name not in ART_KEYS:
            raise ConfigError('Unknown setting')
        cleaned = (value or '').strip()
        if not _KEY_PATTERN.fullmatch(cleaned):
            raise ConfigError('The image-source key has the wrong length or contains characters that are not allowed.')
        try:
            self.secrets.set(ART_KEYS[name][0], cleaned)
        except SecretStoreError as exc:
            raise ConfigError('No secure credential store is available, so the key cannot be saved safely. '
                              f'Set {ART_KEYS[name][1]} instead.') from exc
        log_redaction.register(cleaned)

    def clear_art_key(self, name: str) -> None:
        self.secrets.delete(ART_KEYS[name][0])

    def art_sources(self) -> dict:
        """Which optional providers the person has switched on. Off by default."""
        saved = self.store.get_setting(ART_SOURCES_SETTING, None)
        saved = saved if isinstance(saved, dict) else {}
        return {name: bool(saved.get(name)) for name in ART_SOURCE_NAMES}

    def set_art_sources(self, value) -> None:
        if not isinstance(value, dict) or set(value) - set(ART_SOURCE_NAMES) or not all(isinstance(v, bool) for v in value.values()):
            raise ConfigError('Image sources must be true or false for fanart and theaudiodb.')
        self.store.set_setting(ART_SOURCES_SETTING, {**self.art_sources(), **value})

    def art_status(self) -> dict:
        """What is switched on and which keys exist. Never the keys themselves."""
        from meloshelf.infrastructure.art_sources import SHARED_TEST_KEYS
        on = self.art_sources()
        audiodb = self.art_key('theaudiodb_key')
        return {'fanart': {'enabled': on['fanart'], 'configured': bool(self.art_key('fanart_project_key')), 'personal_key': bool(self.art_key('fanart_client_key'))},
                'theaudiodb': {'enabled': on['theaudiodb'], 'configured': bool(audiodb), 'test_key_only': audiodb in SHARED_TEST_KEYS}}

    def art_clients(self, **options) -> list:
        """The optional providers that are switched on AND have a usable key, in order. Others are simply absent."""
        from meloshelf.infrastructure.art_sources import AudioDbClient, FanartClient, SourceError
        on, clients = self.art_sources(), []
        if on['fanart']:
            try:
                clients.append(FanartClient(self.art_key('fanart_project_key'), self.art_key('fanart_client_key'), **options))
            except SourceError:
                pass
        if on['theaudiodb']:
            try:
                clients.append(AudioDbClient(self.art_key('theaudiodb_key'), **options))
            except SourceError:
                pass
        return clients

    # ── MusicBrainz contact ─────────────────────────────────────────────────

    def musicbrainz_user_agent(self) -> tuple[str, str]:
        contact = self.store.get_setting(CONTACT_SETTING, '')
        if contact:
            log_redaction.register(contact)
            return f'MeloShelf/{APP_VERSION} ({contact})', 'settings'
        from_env = (self.env.get(MUSICBRAINZ_ENV) or '').strip()
        for contact_part in re.findall(r'\(([^)]+)\)', from_env):  # the contact inside the User-Agent
            log_redaction.register(contact_part)
        return (from_env, 'environment') if from_env else ('', 'none')

    def set_musicbrainz_contact(self, contact: str) -> None:
        cleaned = (contact or '').strip()
        if len(cleaned) > 120 or not (_CONTACT_PATTERN.fullmatch(cleaned) or _is_web_url(cleaned)):
            raise ConfigError('The MusicBrainz contact must be an email address such as name@example.org.')
        log_redaction.register(cleaned)
        self.store.set_setting(CONTACT_SETTING, cleaned)

    # ── fpcalc ───────────────────────────────────────────────────────────────

    def set_fpcalc_path(self, path: str) -> None:
        cleaned = (path or '').strip()
        if cleaned and not Path(cleaned).is_file():
            raise ConfigError(f'The fpcalc path does not exist: {cleaned}')
        self.store.set_setting(FPCALC_SETTING, cleaned)

    def fpcalc_location(self) -> FpcalcLocation:
        return locate_fpcalc(configured=self.store.get_setting(FPCALC_SETTING, ''), env=self.env, which=self._which,
                             common_dirs=conventional_fpcalc_dirs(self.env))

    # ── reporting (no values) ───────────────────────────────────────────────

    def summary(self) -> dict:
        key, key_source = self.acoustid_key()
        agent, agent_source = self.musicbrainz_user_agent()
        location = self.fpcalc_location()
        return {
            'acoustid': {'configured': bool(key), 'source': key_source},
            'musicbrainz': {'configured': bool(agent), 'source': agent_source},
            'fpcalc': {'configured': location.path is not None, 'source': location.source},
            'secure_store': {'available': self.secrets.available(), 'backend': getattr(self.secrets, 'backend', '')},
            'art': self.art_status(),
        }


def conventional_fpcalc_dirs(env=None) -> list[str]:
    """Where people usually unzip Chromaprint on Windows, so a first run works without editing PATH."""
    import os
    env = os.environ if env is None else env
    drive = env.get('SystemDrive')
    roots = [env.get('ProgramFiles'), env.get('ProgramFiles(x86)'), env.get('LOCALAPPDATA'),
             drive and drive + '/Tools', drive and drive + '/']
    folders = []
    for root in roots:
        if root:
            folders += [os.path.join(root, 'chromaprint'), os.path.join(root, 'Chromaprint'), os.path.join(root, 'Programs', 'chromaprint')]
    return folders


def check_fpcalc(path: str, *, runner=subprocess.run, platform: str = sys.platform) -> bool:
    """True when the executable starts and reports a version."""
    try:
        result = runner([path, '-version'], capture_output=True, text=True, timeout=15, check=False,
                        **no_console_options(platform))
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and 'fpcalc' in (result.stdout + result.stderr).lower()


def startup_diagnostics(config: AppConfig, fpcalc_check=check_fpcalc) -> list[str]:
    """One line per requirement, saying what is satisfied and where it came from. Never a value."""
    summary = config.summary()
    location = config.fpcalc_location()
    lines = []
    if location.path and fpcalc_check(location.path):
        lines.append(f'OK       Chromaprint (fpcalc): found via {location.source}.')
    elif location.path:
        lines.append(f'FAILED   Chromaprint (fpcalc): found via {location.source}, but it did not run.')
    else:
        lines.append(f'MISSING  Chromaprint (fpcalc): {location.problem or "not found"}')
    key = summary['acoustid']
    if key['configured']:
        lines.append(f"OK       AcoustID key: configured ({key['source']}).")
    else:
        lines.append('MISSING  AcoustID key: identification needs your own key; use Settings or '
                     f'{ACOUSTID_ENV}. Cached results remain usable.')
    contact = summary['musicbrainz']
    if contact['configured']:
        lines.append(f"OK       MusicBrainz contact: configured ({contact['source']}).")
    else:
        lines.append('MISSING  MusicBrainz contact (optional): release details are skipped until one is set.')
    store = summary['secure_store']
    if store['available']:
        lines.append(f"OK       Secure credential store: {store['backend']}.")
    else:
        lines.append('MISSING  Secure credential store: unavailable; keys can only come from the environment.')
    return lines
