"""HTTP-facing logic for artist corrections.

The server computes every preview itself. A write is applied only when the request says `confirm: true`, and
a request without it returns the affected recordings instead, so nothing changes before the person has seen them.
"""
from meloshelf.domain.artist_corrections import ArtistCorrectionError, check_merge
from meloshelf.domain.artists import canonical_key
from meloshelf.infrastructure.artist_corrections import ArtistCorrections
from meloshelf.infrastructure.repository import LibraryRepository

PREVIEW_LIMIT = 200


from meloshelf.application import error_codes as ec
def _coded_reason(text):
    answer = ec.from_text(text)
    return {'reason': answer['error'], 'code': answer['code'], 'params': answer['params']}


def _public_event(event: dict) -> dict:
    return {k: event[k] for k in ('id', 'action', 'source_key', 'source_name', 'target_key', 'target_name', 'ref', 'at')}


def list_events(database) -> dict:
    return {'events': [_public_event(e) for e in ArtistCorrections(database).events()]}


def _recordings(database, name: str) -> dict:
    rows, total = LibraryRepository(database).artist_recordings(name, limit=PREVIEW_LIMIT)
    return {'name': name, 'total': total, 'recordings': rows, 'shown': len(rows)}


def preview_merge(database, source: str, target: str) -> dict:
    try:
        check_merge(source, target, ArtistCorrections(database).state())
    except ArtistCorrectionError as exc:
        return {'allowed': False, 'total': 0, 'recordings': [], 'shown': 0, **_coded_reason(str(exc))}
    affected = _recordings(database, source)
    return {'allowed': True, 'reason': '', **affected}


def preview_keep_separate(database, a: str, b: str) -> dict:
    if not canonical_key(a) or not canonical_key(b):
        return {'allowed': False, 'total': 0, 'recordings': [], 'shown': 0, **_coded_reason('Both artist names are needed.')}
    if canonical_key(a) == canonical_key(b):
        return {'allowed': False, 'total': 0, 'recordings': [], 'shown': 0, **_coded_reason('These names are already the same artist.')}
    first = _recordings(database, a)
    second = _recordings(database, b)
    return {'allowed': True, 'reason': '', 'total': first['total'] + second['total'],
            'recordings': (first['recordings'] + second['recordings'])[:PREVIEW_LIMIT],
            'shown': min(PREVIEW_LIMIT, first['shown'] + second['shown'])}


def preview_undo(database, event_id: int) -> dict:
    event = next((e for e in ArtistCorrections(database).events() if e['id'] == event_id), None)
    if event is None or event['action'] == 'undo':
        return {'allowed': False, 'total': 0, 'recordings': [], 'shown': 0, **_coded_reason('That decision cannot be undone.')}
    if event['action'] == 'split':
        # A keep-separate decision set two names apart; undoing it regroups both, so both are listed.
        first = _recordings(database, event['source_key'])
        second = _recordings(database, event['target_key'])
        return {'allowed': True, 'reason': '', 'total': first['total'] + second['total'],
                'recordings': (first['recordings'] + second['recordings'])[:PREVIEW_LIMIT],
                'shown': min(PREVIEW_LIMIT, first['shown'] + second['shown'])}
    return {'allowed': True, 'reason': '', **_recordings(database, event['source_key'] or event['target_key'])}


def write(database, path: str, payload: dict) -> tuple[int, dict]:
    """Apply a correction. Returns (status, body). Unknown paths return (404, {})."""
    corrections = ArtistCorrections(database)
    confirmed = payload.get('confirm') is True
    try:
        if path == '/api/artist-corrections/merge':
            source, target = payload.get('source'), payload.get('target')
            if not isinstance(source, str) or not isinstance(target, str):
                return 400, ec.body('source_target_required')
            if not confirmed:
                return 409, {**ec.body('confirmation_required'), 'preview': preview_merge(database, source, target)}
            return 200, {'saved': True, 'event_id': corrections.merge(source, target)}
        if path == '/api/artist-corrections/keep-separate':
            a, b = payload.get('a'), payload.get('b')
            if not isinstance(a, str) or not isinstance(b, str):
                return 400, ec.body('a_b_required')
            if not confirmed:
                return 409, {**ec.body('confirmation_required'), 'preview': preview_keep_separate(database, a, b)}
            return 200, {'saved': True, 'event_id': corrections.keep_separate(a, b)}
        if path == '/api/artist-corrections/undo':
            event_id = payload.get('event_id')
            if type(event_id) is not int:
                return 400, ec.body('correction_event_id_invalid')
            if not confirmed:
                return 409, {**ec.body('confirmation_required'), 'preview': preview_undo(database, event_id)}
            return 200, {'saved': True, 'event_id': corrections.undo(event_id)}
    except ArtistCorrectionError as exc:
        return 400, ec.from_text(exc)
    return 404, {}
