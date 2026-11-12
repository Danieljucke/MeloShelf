"""Catalogue of per-file outcomes, so failures are explained instead of counted.

``is_error`` separates technical failures from valid results such as "no match".
``retryable`` marks failures worth trying again later.
``blocks_job`` marks configuration problems: repeating them for every file is
pointless, so a job stops at the first one.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Outcome:
    label: str
    hint: str
    is_error: bool = True
    retryable: bool = False
    blocks_job: bool = False


OUTCOMES: dict[str, Outcome] = {
    'no_match': Outcome(
        'No acoustic match found',
        'The fingerprint is not in the AcoustID database. The track stays unidentified.', is_error=False),
    'insufficient_evidence': Outcome(
        'Evidence too weak',
        'A faint match exists but is not reliable enough to propose. The track stays unidentified.', is_error=False),
    'offline_no_cache': Outcome(
        'Not in the offline cache',
        'Cached-results-only mode is on and this track was never looked up. Run again with it off.', is_error=False),
    'fpcalc_unavailable': Outcome(
        'Chromaprint (fpcalc) is not installed',
        'Install Chromaprint, or set where fpcalc is in Settings. "fpcalc -version" must work in a terminal. No restart is needed.',
        blocks_job=True),
    'acoustid_key_missing': Outcome(
        'AcoustID API key missing',
        'Set the MELOSHELF_ACOUSTID_KEY environment variable to your own AcoustID application key and restart.',
        blocks_job=True),
    'provider_auth': Outcome(
        'Provider rejected the credentials',
        'Check that MELOSHELF_ACOUSTID_KEY holds a valid AcoustID application key.', blocks_job=True),
    'network_error': Outcome(
        'Network failure or timeout',
        'The provider could not be reached. Check the internet connection, then retry the failed tracks.',
        retryable=True),
    'rate_limited': Outcome(
        'Rate limit reached',
        'The provider asked MeloShelf to slow down. Wait a few minutes, then retry the failed tracks.',
        retryable=True),
    'provider_unavailable': Outcome(
        'Provider temporarily unavailable',
        'The provider answered with a server error. Retry the failed tracks later.', retryable=True),
    'provider_error': Outcome(
        'Unexpected provider response',
        'The provider returned something MeloShelf could not use. Details are shown with the track.'),
    'metadata_lookup_failed': Outcome(
        'Metadata lookup failed',
        'The recording was matched but its MusicBrainz details could not be loaded. Retry to complete them.',
        retryable=True),
    'invalid_audio': Outcome(
        'Invalid or unsupported audio',
        'The file could not be decoded. It may be corrupted or in a format fpcalc cannot read.'),
    'file_missing': Outcome(
        'File no longer exists',
        'The file was moved or deleted after the scan. Scan the folder again to update the index.'),
    'file_changed': Outcome(
        'File changed during processing',
        'The file was modified while being analysed. Scan the folder again, then retry.', retryable=True),
    'file_unreadable': Outcome(
        'File could not be read',
        'Check that the drive is connected and that you have permission to read the file.', retryable=True),
    'internal_error': Outcome(
        'Unexpected internal error',
        'MeloShelf hit a bug while processing this file. The details are in the application log.', retryable=True),
}


def describe(code: str) -> Outcome:
    return OUTCOMES.get(code, OUTCOMES['internal_error'])
