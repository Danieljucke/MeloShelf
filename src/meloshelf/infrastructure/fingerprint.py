"""Generate acoustic fingerprints from locally decoded audio. Never modifies a file."""
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class FingerprintError(RuntimeError):
    pass


@dataclass(frozen=True)
class Fingerprint:
    value: str
    duration: int


class Fingerprinter:
    def __init__(self, *, executable: str = 'fpcalc', runner: Callable = subprocess.run, timeout: int = 240):
        self.executable = executable
        self.runner = runner
        self.timeout = timeout

    def compute(self, path: Path) -> Fingerprint:
        path = Path(path)
        if not path.is_file():
            raise FingerprintError('Audio file does not exist')
        try:
            result = self.runner([self.executable, '-json', str(path)], capture_output=True, text=True,
                                 timeout=self.timeout, check=False)
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
