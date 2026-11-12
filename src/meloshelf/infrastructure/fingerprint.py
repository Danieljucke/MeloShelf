"""Generate acoustic fingerprints from locally decoded audio. Never modifies a file."""
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_CREATE_NO_WINDOW = 0x08000000


def no_console_options(platform: str = sys.platform) -> dict:
    """Extra ``subprocess`` keywords that stop a console program from opening a black window on Windows.

    ``CREATE_NO_WINDOW`` gives the child no console at all, so nothing flashes and focus stays where it was; its output
    pipes are unaffected. Other platforms get nothing, so their behaviour is unchanged.
    """
    if platform == 'win32':
        return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', _CREATE_NO_WINDOW)}
    return {}


class FingerprintError(RuntimeError):
    pass


@dataclass(frozen=True)
class FpcalcLocation:
    path: str | None
    source: str      # setting | environment | PATH | none
    problem: str = ''


def locate_fpcalc(*, configured: str | None = None, env=None, which: Callable = shutil.which,
                  common_dirs=()) -> FpcalcLocation:
    """Find the Chromaprint executable: explicit setting, then MELOSHELF_FPCALC, then PATH, then the folders
    ``common_dirs`` names (conventional install places the caller chooses; none by default).

    A configured location that does not exist is reported, never silently replaced.
    """
    import os
    env = os.environ if env is None else env
    for source, candidate in (('setting', (configured or '').strip()),
                              ('environment', (env.get('MELOSHELF_FPCALC') or '').strip())):
        if not candidate:
            continue
        if Path(candidate).is_file():
            return FpcalcLocation(str(Path(candidate)), source)
        return FpcalcLocation(None, 'none',
                              f'Configured Chromaprint path does not exist: {candidate}')
    found = which('fpcalc')
    if found:
        return FpcalcLocation(found, 'PATH')
    for folder in common_dirs:
        for name in ('fpcalc.exe', 'fpcalc'):
            candidate = Path(folder) / name
            if candidate.is_file():
                return FpcalcLocation(str(candidate), 'common location')
    return FpcalcLocation(None, 'none',
                          'fpcalc was not found. Set MELOSHELF_FPCALC to the full path of fpcalc.exe '
                          'or add its folder to PATH.')


class FingerprintUnavailable(FingerprintError):
    """The fpcalc executable itself cannot be started."""


@dataclass(frozen=True)
class Fingerprint:
    value: str
    duration: int


class Fingerprinter:
    def __init__(self, *, executable: str = 'fpcalc', runner: Callable = subprocess.run, timeout: int = 240,
                 which: Callable = shutil.which, platform: str = sys.platform):
        self.executable = executable
        self._platform = platform
        self.runner = runner
        self.timeout = timeout
        self._which = which

    def available(self) -> bool:
        return self._which(self.executable) is not None

    def compute(self, path: Path) -> Fingerprint:
        path = Path(path)
        if not path.is_file():
            raise FingerprintError('Audio file does not exist')
        try:
            result = self.runner([self.executable, '-json', str(path)], capture_output=True, text=True,
                                 timeout=self.timeout, check=False, **no_console_options(self._platform))
        except (FileNotFoundError, PermissionError) as exc:
            raise FingerprintUnavailable(f'fpcalc failed: {type(exc).__name__}: "{self.executable}" cannot be started') from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FingerprintError(f'fpcalc failed: {type(exc).__name__}') from exc
        if result.returncode != 0:
            raise FingerprintError(f'fpcalc returned {result.returncode}: {result.stderr[:300]}')
        try:
            data = json.loads(result.stdout)
            duration = int(round(float(data['duration'])))
            fingerprint = data['fingerprint']
            if not isinstance(fingerprint, str) or not fingerprint or duration <= 0:
                raise ValueError('Missing fingerprint or duration')
            return Fingerprint(value=fingerprint, duration=duration)
        except (KeyError, ValueError, TypeError) as exc:
            raise FingerprintError('Invalid fpcalc JSON output') from exc
