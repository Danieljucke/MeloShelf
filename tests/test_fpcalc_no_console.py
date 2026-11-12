"""Chromaprint must never open a console window on Windows, and must behave exactly as before elsewhere."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from meloshelf.application.config import check_fpcalc
from meloshelf.infrastructure.fingerprint import Fingerprinter, FingerprintError, FingerprintUnavailable

CREATE_NO_WINDOW = 0x08000000


def _ok(duration=182, fingerprint='ABCDEF'):
    return subprocess.CompletedProcess(['fpcalc'], 0, stdout=json.dumps({'duration': duration, 'fingerprint': fingerprint}), stderr='')


def _audio(tmp_path, name='song.mp3'):
    path = tmp_path / name
    path.write_bytes(b'x')
    return path


def test_windows_launch_suppresses_the_console_window(tmp_path):
    run = Mock(return_value=_ok())
    Fingerprinter(runner=run, platform='win32').compute(_audio(tmp_path))
    kwargs = run.call_args.kwargs
    assert kwargs['creationflags'] & CREATE_NO_WINDOW
    assert not kwargs.get('shell')
    assert kwargs['capture_output'] is True and kwargs['text'] is True


def test_other_platforms_launch_exactly_as_before(tmp_path):
    for platform in ('linux', 'darwin'):
        run = Mock(return_value=_ok())
        Fingerprinter(runner=run, platform=platform).compute(_audio(tmp_path))
        assert 'creationflags' not in run.call_args.kwargs
        assert 'startupinfo' not in run.call_args.kwargs


def test_the_audio_path_is_passed_as_one_argument_without_a_shell(tmp_path):
    path = _audio(tmp_path, 'Café été - 日本語 (live) 01.mp3')
    run = Mock(return_value=_ok())
    Fingerprinter(runner=run, executable='C:/Program Files/chromaprint/fpcalc.exe', platform='win32').compute(path)
    assert run.call_args.args[0] == ['C:/Program Files/chromaprint/fpcalc.exe', '-json', str(path)]


def test_parsing_is_unchanged(tmp_path):
    fp = Fingerprinter(runner=Mock(return_value=_ok(181.6, 'XYZ')), platform='win32').compute(_audio(tmp_path))
    assert (fp.duration, fp.value) == (182, 'XYZ')


@pytest.mark.parametrize('stdout', ['not json', '{}', '{"duration": 0, "fingerprint": "A"}', '{"duration": 5, "fingerprint": ""}'])
def test_invalid_responses_are_reported(tmp_path, stdout):
    run = Mock(return_value=subprocess.CompletedProcess(['fpcalc'], 0, stdout=stdout, stderr=''))
    with pytest.raises(FingerprintError):
        Fingerprinter(runner=run, platform='win32').compute(_audio(tmp_path))


def test_timeout_and_start_failures_keep_their_meaning(tmp_path):
    path = _audio(tmp_path)
    with pytest.raises(FingerprintError, match='TimeoutExpired'):
        Fingerprinter(runner=Mock(side_effect=subprocess.TimeoutExpired('fpcalc', 1)), platform='win32').compute(path)
    with pytest.raises(FingerprintUnavailable):
        Fingerprinter(runner=Mock(side_effect=FileNotFoundError()), platform='win32').compute(path)
    with pytest.raises(FingerprintUnavailable):
        Fingerprinter(runner=Mock(side_effect=PermissionError()), platform='win32').compute(path)
    with pytest.raises(FingerprintError):
        Fingerprinter(runner=Mock(side_effect=OSError('boom')), platform='win32').compute(path)


def test_nonzero_exit_reports_the_captured_stderr(tmp_path):
    run = Mock(return_value=subprocess.CompletedProcess(['fpcalc'], 2, stdout='', stderr='ERROR: could not decode'))
    with pytest.raises(FingerprintError, match='returned 2: ERROR: could not decode'):
        Fingerprinter(runner=run, platform='win32').compute(_audio(tmp_path))


def test_many_consecutive_runs_each_hide_the_window(tmp_path):
    run = Mock(return_value=_ok())
    finger = Fingerprinter(runner=run, platform='win32')
    path = _audio(tmp_path)
    for _ in range(25):
        finger.compute(path)
    assert run.call_count == 25
    assert all(call.kwargs['creationflags'] & CREATE_NO_WINDOW for call in run.call_args_list)


def test_the_version_check_also_hides_the_window():
    run = Mock(return_value=subprocess.CompletedProcess(['fpcalc'], 0, stdout='fpcalc version 1.6.0', stderr=''))
    assert check_fpcalc('C:/x/fpcalc.exe', runner=run, platform='win32') is True
    assert run.call_args.kwargs['creationflags'] & CREATE_NO_WINDOW
    run = Mock(return_value=subprocess.CompletedProcess(['fpcalc'], 0, stdout='fpcalc version 1.6.0', stderr=''))
    assert check_fpcalc('/usr/bin/fpcalc', runner=run, platform='linux') is True
    assert 'creationflags' not in run.call_args.kwargs


@pytest.mark.skipif(sys.platform != 'win32', reason='runs a real Windows child process')
def test_a_real_child_process_gets_the_path_with_spaces_and_unicode(tmp_path):
    script = tmp_path / 'fake fpcalc.cmd'
    script.write_text('@echo off\r\nif not exist "%~2" exit /b 3\r\necho {"duration": 12.4, "fingerprint": "REAL"}\r\n')
    audio = _audio(tmp_path, 'Café été 日本語.mp3')
    finger = Fingerprinter(executable=str(script))
    for _ in range(3):
        fp = finger.compute(audio)
        assert (fp.duration, fp.value) == (12, 'REAL')
