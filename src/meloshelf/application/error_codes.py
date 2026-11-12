"""Stable, machine-readable error codes for the local web API.

Every refusal the web layer sends carries ``code`` (stable, never translated), ``params`` (values the server chose)
and ``error`` (the English sentence, kept for logs and older callers). The page translates ``code`` through the
catalogue key ``err.<code>`` in all eight languages.

Nothing sent to the browser is raw exception text, provider text, a local path or a credential. An unrecognised
sentence becomes ``request_refused``; an unexpected exception becomes ``unexpected_error`` with only its class name.
The full detail goes to the server log, where the caller writes it.
"""
import re

# code -> English sentence. Placeholders are the parameters listed in PARAMETERS.
MESSAGES = {
    'invalid_json': 'Invalid JSON',
    'token_invalid': 'Invalid session token',
    'origin_not_allowed': 'Origin not allowed',
    'host_not_allowed': 'Host not allowed',
    'not_found': 'Not found',
    'payload_too_large': 'Payload too large',
    'request_refused': 'The request was refused.',
    'unexpected_error': 'An unexpected error stopped this step ({exception}). The details are in the log.',
    'library_database_error': 'The library database could not be read or written. Restart MeloShelf and try again.',
    'library_busy': 'The library database is busy: another program is holding it. Close other MeloShelf windows or programs and try again.',
    'invalid_track_status': 'Invalid status',
    'job_id_required': 'job_id is required',
    'path_required': 'path is required',
    'credit_required': 'credit is required',
    'key_required': 'key is required',
    'invalid_range': 'Invalid range',
    'file_not_indexed': 'Not an indexed library file',
    'file_missing': 'The file is no longer on disk',
    'file_not_in_library': 'That file is not in the library',
    'no_proposal': 'No proposal for this file',
    'no_embedded_picture': 'No picture is embedded in this file',
    'album_and_artist_required': 'album and artist are required',
    'no_album_cover': 'No cover applies to this album',
    'artist_id_invalid': 'mb must be a MusicBrainz artist ID',
    'no_artist_photo': 'No photograph for this artist',
    'release_id_invalid': 'Invalid release identifier',
    'no_release_cover': 'No cover is available for this release',
    'paging_not_integer': 'offset and limit must be integers',
    'paging_invalid': 'Invalid paging',
    'limit_out_of_range': 'limit must be 1 to {max}',
    'paging_out_of_range': 'limit must be 1 to {max} and offset 0 or more',
    'correction_event_id_invalid': 'event_id must be an integer',
    'correction_action_invalid': 'action must be merge, keep-separate or undo',
    'group_not_found': 'No correction group with this key in the library',
    'folder_not_found': 'Select an existing folder on this computer',
    'folder_scan_running': 'This folder is already being scanned',
    'job_busy': 'Another job is running. Wait for it to finish, or cancel it.',
    'job_not_running': 'This job is not running.',
    'job_finished': 'This job is already finished.',
    'job_not_resumable': 'This job cannot be resumed.',
    'job_retry_later': 'Failed files can be retried once the job is not running.',
    'job_nothing_to_retry': 'No failed file can be retried.',
    'item_changed_while_reading': 'Modified while its tags were being read',
    'item_changed': 'Changed since it was scanned. Scan the folder again.',
    'nothing_to_identify': 'Nothing left to identify: every indexed file already has a result',
    'identify_limit': 'Limit must be 1–{max}',
    'offline_not_boolean': 'offline must be boolean',
    'acoustid_key_required': 'Save your AcoustID key in Settings or set MELOSHELF_ACOUSTID_KEY, or choose offline mode',
    'threshold_out_of_range': 'Auto-approval threshold must be a whole number from {low} to {high}',
    'setting_unknown': 'Unknown setting: {name}',
    'setting_not_boolean': '{name} must be true or false',
    'contact_invalid': 'The MusicBrainz contact must be an email address such as name@example.org.',
    'fpcalc_path_missing': 'The fpcalc path does not exist: {path}',
    'acoustid_key_invalid': 'The AcoustID key has the wrong length or contains characters that are not allowed.',
    'credential_store_unavailable': 'No secure credential store is available, so the key cannot be saved safely. Set MELOSHELF_ACOUSTID_KEY instead.',
    'config_target_invalid': 'target must be acoustid, musicbrainz or fpcalc',
    'lookup_contact_required': 'Save a MusicBrainz contact in Settings before looking up artists',
    'search_contact_required': 'Save a MusicBrainz contact in Settings before text search, or run offline',
    'metadata_action_invalid': 'Choose approve, reject or revert.',
    'metadata_no_single_replacement': 'There is no single replacement to approve. Review the tracks individually.',
    'qa_label_invalid': 'Choose correct, incorrect or unsure.',
    'qa_text_too_long': '{field} must be text of at most {max} characters.',
    'qa_correct_has_correction': 'A correct label cannot carry a correction.',
    'qa_alternative_invalid': 'The chosen alternative is not one of the candidates for this file.',
    'qa_not_analysed': 'This file has not been analysed, so there is no proposal to judge.',
    'qa_mode_invalid': 'mode must be "preview" or "apply".',
    'qa_import_request_invalid': 'Send csv (text) and mode "preview" or "apply".',
    'qa_overwrite_invalid': 'overwrite must be a list of file paths.',
    'invalid_decision': 'Invalid decision',
    'recording_candidate_required': 'Select an available recording candidate explicitly',
    # images and covers
    'image_base64_invalid': 'The image data is not valid base64.',
    'image_type_invalid': 'Use a JPEG or PNG image.',
    'image_not_valid': 'That file is not a real JPEG or PNG image.',
    'image_too_large': 'The image is larger than {kb} KB.',
    'choice_not_undoable': 'That choice cannot be undone.',
    'choice_already_undone': 'That choice has already been undone.',
    'artist_mb_id_invalid': 'That is not a MusicBrainz artist ID.',
    'album_artist_missing': 'An album and an artist are required.',
    # enrichment
    'enrichment_job_running': 'An enrichment job is already running. Pause or cancel it first.',
    'enrichment_other_running': 'Another enrichment job is still running.',
    'enrichment_contact_required': 'Save a MusicBrainz contact in Settings before enriching artists',
    'enrichment_contact_required_resume': 'Save a MusicBrainz contact in Settings before resuming',
    'job_id_not_integer': 'id must be an integer',
    'enrichment_action_invalid': 'action must be pause, resume or cancel',
    'auto_enrich_not_boolean': 'auto_enrich must be true or false',
    'max_names_out_of_range': 'max_names must be between 1 and {max}',
    'batch_out_of_range': 'batch must be between 1 and {max}',
    'enrichment_no_job': 'No enrichment job has this number.',
    'enrichment_not_pausable': 'Only a queued or running job can be paused.',
    'enrichment_not_resumable': 'Only a paused or interrupted job can be resumed.',
    'enrichment_finished': 'This job has already finished or been cancelled.',
    'enrichment_auto_off': 'Automatic artist enrichment is off.',
    'enrichment_auto_contact_required': 'Save a MusicBrainz contact in Settings to enable automatic enrichment.',
    'enrichment_nothing_to_do': 'Every artist name already has a stored answer.',
    # artist corrections
    'source_target_required': 'source and target are required',
    'a_b_required': 'a and b are required',
    'confirmation_required': 'Review the affected recordings, then confirm',
    'correction_names_required': 'Both artist names are needed to merge them.',
    'correction_same_artist': 'These names are already the same artist.',
    'correction_kept_separate': 'These artists were kept separate. Undo that decision before merging them.',
    'correction_undo_unknown': 'An undo refers to a decision that does not exist.',
    'correction_not_undoable': 'That decision cannot be undone.',
    'correction_undone_twice': 'A decision was undone twice.',
    # fallback text search
    'fallback_running': 'A text search is already running.',
    'fallback_nothing_left': 'There is nothing left to search: every file without an acoustic match has an answer.',
    # interface preferences
    'preferences_not_object': 'Expected an object',
    'preference_invalid': '{field} must be one of: {allowed}',
    'preferences_empty': 'Nothing to change: send language and/or theme',
    # artist photographs
    'photo_unverified': 'This artist identity is not verified, so no photograph is fetched.',
    'photo_contact_required': 'Save a MusicBrainz contact in Settings to fetch photographs.',
    'photo_fetch_failed': 'The photograph could not be fetched. Check the connection and try again.',
    # organizing
    'organize_running': 'An organization is already running.',
    'destination_is_source': 'The organized folder cannot be the music folder.',
    'destination_inside_source': 'The organized folder cannot be inside the music folder.',
    'source_inside_destination': 'The music folder cannot be inside the organized folder.',
    'destination_unusable': 'The organized folder cannot be used. Choose a different folder.',
    'not_enough_space': 'There is not enough free space on the organized folder’s drive.',
    'scan_failed': 'Scanning the music folder failed. Check the folder and try again.',
    'organize_failed': 'Organizing stopped unexpectedly. Your original files were not changed. The details are in the log.',
    'disk_full': 'The disk is full. Free some space and run again; nothing was lost.',
    'identity_required': 'A title and an artist are required.',
    'items_required': 'Send at least one edited song.',
    'album_enrichment_running': 'Album covers are already being looked up.',
    'photo_batch_running': 'Artist photographs are already being looked up.',
    'confirmation_required': 'This changes where your files are. Confirm it explicitly to continue.',
    'art_key_invalid': 'The image-source key has the wrong length or contains characters that are not allowed.',
    'art_sources_invalid': 'Image sources must be true or false for fanart and theaudiodb.',
    'consolidation_run_unknown': 'That consolidation is not in the journal.',
    'artist_not_found': 'No artist with this name is in the library.',
    'nothing_organized_yet': 'Nothing has been organized yet.',
    'folder_pick_unavailable': 'The folder chooser is not available. Type the folder path instead.',
    'organize_action_invalid': 'action must be pause or cancel',
    # provider checks (Settings)
    'fpcalc_missing': 'fpcalc was not found.',
    'fpcalc_available': 'fpcalc runs.',
    'fpcalc_failed': 'fpcalc was found but did not run.',
    'acoustid_key_store_unavailable': 'The saved AcoustID key could not be read from the secure credential store. Unlock the store or save the key again.',
    'acoustid_key_missing': 'No AcoustID key is saved or set in the environment.',
    'fingerprint_unavailable': 'Chromaprint could not create a test fingerprint, so the key was not sent. Install Chromaprint or set its location.',
    'acoustid_unexpected': 'The AcoustID check failed unexpectedly ({exception}). Retry; the key was not shown.',
    'acoustid_valid': 'AcoustID accepted the key and answered a real lookup.',
    'acoustid_invalid_key': 'AcoustID rejected the saved key (code {code}). Replace it in Settings with the key from your AcoustID application page.',
    'acoustid_request_rejected': "AcoustID rejected MeloShelf's test request (code {code}). This is a request problem, not a judgement about your key.",
    'acoustid_rate_limited': 'AcoustID is limiting requests from this computer (HTTP 429). Wait a minute, then retry.',
    'acoustid_dns': 'The name api.acoustid.org could not be resolved (DNS). Check the connection, proxy or VPN settings.',
    'acoustid_tls': 'The secure connection to AcoustID failed (TLS or certificate). A security product or proxy may be intercepting it.',
    'acoustid_timeout': 'AcoustID did not answer before the timeout. Retry in a moment.',
    'acoustid_connection': 'The connection to AcoustID was refused or reset. Retry, and check the network.',
    'acoustid_provider_error': 'AcoustID had a server problem (HTTP {status}). Retry later.',
    'acoustid_malformed': 'AcoustID sent an answer MeloShelf cannot read. Retry later.',
    'acoustid_rejected': 'AcoustID rejected the request (HTTP {status}).',
    'musicbrainz_contact_missing': 'No MusicBrainz contact is configured.',
    'musicbrainz_unreachable': 'MusicBrainz could not be reached (HTTP {status}).',
    'musicbrainz_answered': 'MusicBrainz answered.',
    'musicbrainz_unexpected_answer': 'MusicBrainz answered with an unexpected response.',
}

# code -> the parameters its English sentence and every translation may use.
PARAMETERS = {code: tuple(re.findall(r'\{([a-z_]+)\}', text)) for code, text in MESSAGES.items()}

# Fixed sentences the server builds from its own state, mapped to codes. An exact match needs no parameters.
_EXACT = {
    'Invalid JSON': 'invalid_json',
    'Invalid session token': 'token_invalid',
    'Origin not allowed': 'origin_not_allowed',
    'Host not allowed': 'host_not_allowed',
    'Not found': 'not_found',
    'Payload too large': 'payload_too_large',
    'Invalid status': 'invalid_track_status',
    'job_id is required': 'job_id_required',
    'path is required': 'path_required',
    'credit is required': 'credit_required',
    'key is required': 'key_required',
    'Invalid range': 'invalid_range',
    'Not an indexed library file': 'file_not_indexed',
    'The file is no longer on disk': 'file_missing',
    'That file is not in the library': 'file_not_in_library',
    'No proposal for this file': 'no_proposal',
    'No picture is embedded in this file': 'no_embedded_picture',
    'album and artist are required': 'album_and_artist_required',
    'No cover applies to this album': 'no_album_cover',
    'mb must be a MusicBrainz artist ID': 'artist_id_invalid',
    'No photograph for this artist': 'no_artist_photo',
    'Invalid release identifier': 'release_id_invalid',
    'No cover is available for this release': 'no_release_cover',
    'offset and limit must be integers': 'paging_not_integer',
    'Invalid paging': 'paging_invalid',
    'event_id must be an integer': 'correction_event_id_invalid',
    'action must be merge, keep-separate or undo': 'correction_action_invalid',
    'No correction group with this key in the library': 'group_not_found',
    'Select an existing folder on this computer': 'folder_not_found',
    'This folder is already being scanned': 'folder_scan_running',
    'Another job is running. Wait for it to finish, or cancel it.': 'job_busy',
    'This job is not running.': 'job_not_running',
    'This job is already finished.': 'job_finished',
    'This job cannot be resumed.': 'job_not_resumable',
    'Failed files can be retried once the job is not running.': 'job_retry_later',
    'No failed file can be retried.': 'job_nothing_to_retry',
    'Modified while its tags were being read': 'item_changed_while_reading',
    'Changed since it was scanned. Scan the folder again.': 'item_changed',
    'Identification is already running.': 'job_busy',
    'offline must be boolean': 'offline_not_boolean',
    'Save your AcoustID key in Settings or set MELOSHELF_ACOUSTID_KEY, or choose offline mode': 'acoustid_key_required',
    'The MusicBrainz contact must be an email address such as name@example.org.': 'contact_invalid',
    'The AcoustID key has the wrong length or contains characters that are not allowed.': 'acoustid_key_invalid',
    'target must be acoustid, musicbrainz or fpcalc': 'config_target_invalid',
    'Save a MusicBrainz contact in Settings before looking up artists': 'lookup_contact_required',
    'Save a MusicBrainz contact in Settings before text search, or run offline': 'search_contact_required',
    'Choose approve, reject or revert.': 'metadata_action_invalid',
    'There is no single replacement to approve. Review the tracks individually.': 'metadata_no_single_replacement',
    'Choose correct, incorrect or unsure.': 'qa_label_invalid',
    'A correct label cannot carry a correction.': 'qa_correct_has_correction',
    'The chosen alternative is not one of the candidates for this file.': 'qa_alternative_invalid',
    'This file has not been analysed, so there is no proposal to judge.': 'qa_not_analysed',
    'mode must be "preview" or "apply".': 'qa_mode_invalid',
    'Send csv (text) and mode "preview" or "apply".': 'qa_import_request_invalid',
    'overwrite must be a list of file paths.': 'qa_overwrite_invalid',
    'Invalid decision': 'invalid_decision',
    'Invalid review decision': 'invalid_decision',
    'Select an available recording candidate explicitly': 'recording_candidate_required',
    'No secure credential store is available, so the key cannot be saved safely. Set MELOSHELF_ACOUSTID_KEY instead.': 'credential_store_unavailable',
    'The image data is not valid base64.': 'image_base64_invalid',
    'Use a JPEG or PNG image.': 'image_type_invalid',
    'That file is not a real JPEG or PNG image.': 'image_not_valid',
    'That choice cannot be undone.': 'choice_not_undoable',
    'That choice has already been undone.': 'choice_already_undone',
    'That is not a MusicBrainz artist ID.': 'artist_mb_id_invalid',
    'An album and an artist are required.': 'album_artist_missing',
    'An enrichment job is already running. Pause or cancel it first.': 'enrichment_job_running',
    'Another enrichment job is still running.': 'enrichment_other_running',
    'Save a MusicBrainz contact in Settings before enriching artists': 'enrichment_contact_required',
    'Save a MusicBrainz contact in Settings before resuming': 'enrichment_contact_required_resume',
    'id must be an integer': 'job_id_not_integer',
    'action must be pause, resume or cancel': 'enrichment_action_invalid',
    'auto_enrich must be true or false': 'auto_enrich_not_boolean',
    'No enrichment job has this number.': 'enrichment_no_job',
    'Only a queued or running job can be paused.': 'enrichment_not_pausable',
    'Only a paused or interrupted job can be resumed.': 'enrichment_not_resumable',
    'This job has already finished or been cancelled.': 'enrichment_finished',
    'Automatic artist enrichment is off.': 'enrichment_auto_off',
    'Save a MusicBrainz contact in Settings to enable automatic enrichment.': 'enrichment_auto_contact_required',
    'Every artist name already has a stored answer.': 'enrichment_nothing_to_do',
    'source and target are required': 'source_target_required',
    'a and b are required': 'a_b_required',
    'Review the affected recordings, then confirm': 'confirmation_required',
    'Both artist names are needed to merge them.': 'correction_names_required',
    'Both artist names are needed.': 'correction_names_required',
    'These names are already the same artist.': 'correction_same_artist',
    'These artists were kept separate. Undo that decision before merging them.': 'correction_kept_separate',
    'An undo refers to a decision that does not exist.': 'correction_undo_unknown',
    'That decision cannot be undone.': 'correction_not_undoable',
    'A decision was undone twice.': 'correction_undone_twice',
    'A text search is already running.': 'fallback_running',
    'There is nothing left to search: every file without an acoustic match has an answer.': 'fallback_nothing_left',
    'Expected an object': 'preferences_not_object',
    'A title and an artist are required.': 'identity_required',
    'The image-source key has the wrong length or contains characters that are not allowed.': 'art_key_invalid',
    'Image sources must be true or false for fanart and theaudiodb.': 'art_sources_invalid',
    'Nothing to change: send language and/or theme': 'preferences_empty',
    'fpcalc was not found.': 'fpcalc_missing',
    'fpcalc runs.': 'fpcalc_available',
    'fpcalc was found but did not run.': 'fpcalc_failed',
    'No AcoustID key is saved or set in the environment.': 'acoustid_key_missing',
    'AcoustID accepted the key and answered a real lookup.': 'acoustid_valid',
    'No MusicBrainz contact is configured.': 'musicbrainz_contact_missing',
    'MusicBrainz answered.': 'musicbrainz_answered',
    'MusicBrainz answered with an unexpected response.': 'musicbrainz_unexpected_answer',
}

# Sentences with values in them: a pattern, its code, and how to read the parameters from the match.
_PATTERNS = [
    (re.compile(r'Limit must be 1–(\d+)'), 'identify_limit', lambda m: {'max': int(m.group(1))}),
    (re.compile(r'limit must be 1 to (\d+) and offset 0 or more'), 'paging_out_of_range', lambda m: {'max': int(m.group(1))}),
    (re.compile(r'limit must be 1 to (\d+)'), 'limit_out_of_range', lambda m: {'max': int(m.group(1))}),
    (re.compile(r'Auto-approval threshold must be a whole number from (\d+) to (\d+)'), 'threshold_out_of_range',
     lambda m: {'low': int(m.group(1)), 'high': int(m.group(2))}),
    (re.compile(r'Unknown setting: ([A-Za-z0-9_]+)'), 'setting_unknown', lambda m: {'name': m.group(1)}),
    (re.compile(r'([A-Za-z0-9_]+) must be true or false'), 'setting_not_boolean', lambda m: {'name': m.group(1)}),
    (re.compile(r'The fpcalc path does not exist: (.+)'), 'fpcalc_path_missing', lambda m: {'path': m.group(1)}),
    (re.compile(r'(\w+) must be text of at most (\d+) characters\.'), 'qa_text_too_long',
     lambda m: {'field': m.group(1), 'max': int(m.group(2))}),
    (re.compile(r'The image is larger than (\d+) KB\.'), 'image_too_large', lambda m: {'kb': int(m.group(1))}),
    (re.compile(r'max_names must be between 1 and ([\d,]+)'), 'max_names_out_of_range',
     lambda m: {'max': int(m.group(1).replace(',', ''))}),
    (re.compile(r'batch must be between 1 and (\d+)'), 'batch_out_of_range', lambda m: {'max': int(m.group(1))}),
    (re.compile(r'(\w+) must be one of: (.+)'), 'preference_invalid', lambda m: {'field': m.group(1), 'allowed': m.group(2)}),
    (re.compile(r'Another (\w+) job is running\.'), 'job_busy', lambda m: {}),
]


def body(code: str, /, **params) -> dict:
    """The response body for a registered code. Unknown codes are a programming error, not a user error."""
    if code not in MESSAGES:
        raise KeyError(f'unregistered error code: {code}')
    missing = set(PARAMETERS[code]) - set(params)
    if missing:
        raise KeyError(f'{code} needs parameters {sorted(missing)}')
    return {'error': MESSAGES[code].format(**params), 'code': code, 'params': {k: params[k] for k in PARAMETERS[code]}}


def from_text(text: str) -> dict:
    """A body for a sentence the server built. Anything not recognised is refused without its text."""
    text = str(text)
    if text in _EXACT:
        return body(_EXACT[text])
    for pattern, code, read in _PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            return body(code, **read(match))
    return body('request_refused')


def unexpected(exc: BaseException) -> dict:
    """An unexpected exception: the class name only. The caller logs the full detail on the server."""
    return body('unexpected_error', exception=type(exc).__name__)


def provider_number(value):
    """A provider's numeric code as an integer, or None: a provider's text is never passed to the browser."""
    text = str(value or '')
    return int(text) if text.isdigit() and len(text) <= 10 else None


def http_status(value) -> str:
    return str(value) if value else 'no status'
