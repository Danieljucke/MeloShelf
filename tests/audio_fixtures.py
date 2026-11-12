"""Test helpers: real, parseable MP3 files and stand-ins for fpcalc and the providers.

Files are written only into pytest's temporary folders. The application under
test never writes audio; these helpers do, to build the fixtures.
"""
import hashlib
import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3

FRAME_HEADER = bytes.fromhex('fffb9064')  # MPEG-1 Layer III, 128 kbps, 44.1 kHz
FRAME_SIZE = 417
SECONDS_PER_FRAME = 1152 / 44100


def write_mp3(path: Path, *, seconds: float = 4, seed: int = 0, tags: dict | None = None) -> Path:
    """A valid silent MP3 whose audio bytes depend on ``seed`` (so does its fake fingerprint)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = FRAME_HEADER + bytes([seed % 251]) * (FRAME_SIZE - len(FRAME_HEADER))
    path.write_bytes(frame * max(4, round(seconds / SECONDS_PER_FRAME)))
    if tags:
        ID3().save(path)
        audio = EasyID3(path)
        audio.update(tags)
        audio.save()
    return path


def audio_fingerprint(path: Path) -> str:
    """Depends on the audio only: the tail of the file is unaffected by the tag header."""
    return 'FP' + hashlib.sha256(Path(path).read_bytes()[-FRAME_SIZE * 3:]).hexdigest()[:24]


class FakeFpcalc:
    """Replaces subprocess.run for Fingerprinter: decodes nothing, never writes."""
    def __init__(self):
        self.calls = []

    def __call__(self, command, **_options):
        path = Path(command[-1])
        self.calls.append(path.name)
        data = path.read_bytes()
        start = data.find(FRAME_HEADER)
        if start < 0:
            return subprocess.CompletedProcess(command, 2, stdout='', stderr='ERROR: Error decoding audio frame')
        frames = (len(data) - start) // FRAME_SIZE
        return subprocess.CompletedProcess(command, 0, stderr='', stdout=json.dumps(
            {'duration': frames * SECONDS_PER_FRAME, 'fingerprint': audio_fingerprint(path)}))


class FakeProviders:
    """One transport for both clients, answering in the providers' real JSON shapes."""
    def __init__(self):
        self.acoustid: dict[str, list[dict]] = {}     # fingerprint → AcoustID "results"
        self.recordings: dict[str, dict] = {}         # MBID → MusicBrainz recording
        self.acoustid_calls = []
        self.musicbrainz_calls = []
        self.fail_next: list[Exception] = []

    def add(self, path: Path, results: list[dict]) -> None:
        self.acoustid[audio_fingerprint(path)] = results

    def __call__(self, url, *, data=None, headers=None, timeout=None):
        if self.fail_next:
            raise self.fail_next.pop(0)
        if 'acoustid.org' in url:
            fingerprint = parse_qs(data.decode())['fingerprint'][0]
            self.acoustid_calls.append(fingerprint)
            return {'status': 'ok', 'results': self.acoustid.get(fingerprint, [])}
        recording_id = url.split('/recording/')[1].split('?')[0]
        self.musicbrainz_calls.append(recording_id)
        return self.recordings[recording_id]


def mbid(n: int) -> str:
    return f'{n:08d}-0000-4000-8000-000000000000'


def acoustid_result(recording: int, title: str, artist: str, *, score: float, duration: float) -> dict:
    return {'id': f'track-{recording}', 'score': score, 'recordings': [
        {'id': mbid(recording), 'title': title, 'duration': duration, 'artists': [{'name': artist}]}]}


def musicbrainz_recording(recording: int, title: str, artist: str, *, seconds: float, releases: list[tuple]) -> dict:
    return {'id': mbid(recording), 'title': title, 'length': int(seconds * 1000),
            'artist-credit': [{'name': artist}],
            'releases': [{'id': f'rel-{recording}-{i}', 'title': name, 'date': date,
                          'release-group': {'primary-type': kind, 'secondary-types': secondary}}
                         for i, (name, date, kind, secondary) in enumerate(releases)]}
