"""HTTP-facing logic for identity suggestions. Every write is a human decision; the server validates each one."""
from meloshelf.infrastructure.artist_suggestions import SuggestionDecisions, SuggestionError


def list_open(database) -> dict:
    decisions = SuggestionDecisions(database)
    return {'suggestions': decisions.open_suggestions(), 'decisions': decisions.events()}


def decide(database, payload: dict) -> tuple[int, dict]:
    decisions = SuggestionDecisions(database)
    action = payload.get('action')
    try:
        if action == 'undo':
            event_id = payload.get('event_id')
            if type(event_id) is not int:
                return 400, {'error': 'event_id must be an integer'}
            return 200, {'saved': True, 'event_id': decisions.undo(event_id)}
        credit = payload.get('credit')
        if not isinstance(credit, str) or not credit.strip():
            return 400, {'error': 'credit is required'}
        mb_artist_id = payload.get('mb_artist_id')
        if mb_artist_id is not None and not isinstance(mb_artist_id, str):
            return 400, {'error': 'mb_artist_id must be text'}
        albums = payload.get('albums')
        tracks = payload.get('tracks')
        return 200, {'saved': True,
                     'event_id': decisions.decide(credit, action, mb_artist_id, albums=albums, tracks=tracks)}
    except SuggestionError as exc:
        return 400, {'error': str(exc)}
