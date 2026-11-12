"""The job card's message line is translated, and an internal error never shows the exception's text.

The server's job messages are a closed set of sentences built in jobs.py and job_store.py; the page maps each to
a catalogue key (with plurals for counts) and falls back to the server's sentence for anything it does not know.
"""
import json
import re
from pathlib import Path

import pytest

from test_jobs import FakeRecognition, database, job_of, make_service, run_scan  # noqa: F401

WEB = Path(__file__).resolve().parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web'
SRC = Path(__file__).resolve().parents[1] / 'src' / 'meloshelf'
LANGUAGES = ('en', 'es', 'fr', 'hi', 'ja', 'ko', 'pt', 'zh-Hans')
FIXED = ['Starting…', 'Reading tags…', 'Looking for audio files…', 'Analysing…', 'Pausing after the current file…',
         'Paused. Resume to continue where it stopped.', 'Cancelled. Files already processed were kept.',
         'Interrupted when MeloShelf closed. Resume to continue.', 'Ready. Your music has not been modified.']
KEYS = ['m.job-starting', 'm.job-reading-tags', 'm.job-discovering', 'm.job-analysing', 'm.job-pausing',
        'm.job-paused', 'm.job-cancelled', 'm.job-interrupted', 'm.job-ready', 'm.job-paused-network',
        'm.job-discovered.one', 'm.job-discovered.other', 'm.job-files-indexed.one', 'm.job-files-indexed.other',
        'm.job-files-analysed.one', 'm.job-files-analysed.other', 'm.job-with-problems.one', 'm.job-with-problems.other']


def test_every_fixed_job_sentence_in_the_server_is_mapped_by_the_page():
    source = (SRC / 'application' / 'jobs.py').read_text(encoding='utf-8') + \
        (SRC / 'infrastructure' / 'job_store.py').read_text(encoding='utf-8')
    script = (WEB / 'index.html').read_text(encoding='utf-8').split('<script id="meloshelf-app">', 1)[1]
    for sentence in FIXED:
        assert sentence in source, f'the server no longer says {sentence!r}: update the page and this test'
        assert f"'{sentence}':" in script, f'the page does not translate {sentence!r}'
    assert 'jobMessage(job)' in script


@pytest.mark.parametrize('lang', LANGUAGES)
def test_every_job_message_key_is_translated_with_its_placeholders(lang):
    entries = json.loads((WEB / 'i18n' / f'{lang}.json').read_text(encoding='utf-8'))
    english = json.loads((WEB / 'i18n' / 'en.json').read_text(encoding='utf-8'))
    for key in KEYS:
        assert entries.get(key, '').strip(), f'{lang} has no {key}'
        assert sorted(re.findall(r'\{\w+\}', entries[key])) == sorted(re.findall(r'\{\w+\}', english[key])), key


def test_an_unexpected_job_failure_shows_the_exception_class_not_its_text(tmp_path, database):
    root = tmp_path / 'music'
    root.mkdir()
    (root / 'a.mp3').write_bytes(b'x' * 64)
    service = make_service(database)

    def broken(job, control):
        raise ValueError('cannot open C:/Users/someone/Private/library.db')

    service._run_scan = broken
    result = service.start_scan(str(root))
    assert service.wait(result['job_id'], 20)
    job = job_of(service, 'scan')
    assert job['error']['code'] == 'internal_error'
    assert job['message'] == 'ValueError'
    assert 'Private' not in json.dumps(job) and 'someone' not in json.dumps(job)


def test_switching_language_redraws_the_job_cards_from_the_last_activity():
    script = (WEB / 'index.html').read_text(encoding='utf-8').split('<script id="meloshelf-app">', 1)[1]
    handler = script.split("bindMenu('langBtn','langMenu',", 1)[1].split('\n  });', 1)[0]
    assert 'renderActivity(lastActivity)' in handler, 'job cards keep the previous language until the next poll'


@pytest.mark.parametrize('lang', LANGUAGES)
def test_the_scan_summary_uses_plural_forms_for_both_counts(lang):
    script = (WEB / 'index.html').read_text(encoding='utf-8').split('<script id="meloshelf-app">', 1)[1]
    assert "TN('m.last-scan-read'" in script and "TN('m.library-holds'" in script
    entries = json.loads((WEB / 'i18n' / f'{lang}.json').read_text(encoding='utf-8'))
    for key in ('m.last-scan-read.one', 'm.last-scan-read.other', 'm.library-holds.one', 'm.library-holds.other'):
        assert '{n}' in entries.get(key, ''), f'{lang} {key}'


def test_the_error_list_is_redrawn_when_the_language_changes():
    script = (WEB / 'index.html').read_text(encoding='utf-8').split('<script id="meloshelf-app">', 1)[1]
    assert 'const signature=LANG+JSON.stringify(job.errors)' in script


def test_the_running_file_label_is_translated():
    script = (WEB / 'index.html').read_text(encoding='utf-8').split('<script id="meloshelf-app">', 1)[1]
    assert '<p class="now">Now: ' not in script
    assert "T('m.now-file'" in script
    for lang in LANGUAGES:
        entries = json.loads((WEB / 'i18n' / f'{lang}.json').read_text(encoding='utf-8'))
        assert '{file}' in entries.get('m.now-file', ''), lang


def test_japanese_and_chinese_join_sentences_and_lists_without_latin_separators():
    script = (WEB / 'index.html').read_text(encoding='utf-8').split('<script id="meloshelf-app">', 1)[1]
    assert 'joinSentences(' in script and 'listSep()' in script
