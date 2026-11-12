"""Filters for the validation queue, the bulk list and the report. Everything except the category is applied in SQL
with bound parameters; the category is derived in Python from the same row (see domain.sample_category)."""
import json
from dataclasses import dataclass

from meloshelf.domain.sample_category import KEYS, categorize

MAX_TEXT = 200
VERSIONS = ('', 'conflict', 'clean')
SCORE = "CAST(json_extract(m.data, '$.candidates[0].evidence.score') AS REAL)"
PROVIDER = "CAST(json_extract(m.data, '$.candidates[0].acoustid_score') AS REAL)"
CONFLICT = "COALESCE(json_extract(m.data, '$.candidates[0].evidence.version_conflict'), 0)"


def _like(text: str) -> str:
    return '%' + text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def _one(query, name: str) -> str:
    value = query.get(name, '')
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ''
    return str(value or '').strip()


def _number(query, name: str, kind, low, high):
    raw = _one(query, name)
    if raw == '':
        return None
    try:
        value = kind(raw)
    except ValueError:
        raise ValueError(f'{name} must be a number') from None
    if not low <= value <= high:
        raise ValueError(f'{name} must be between {low} and {high}')
    return value


@dataclass(frozen=True)
class QaFilter:
    status: str = ''
    category: str = ''
    score_min: int | None = None
    score_max: int | None = None
    provider_min: float | None = None
    artist: str = ''
    album: str = ''
    version: str = ''

    def is_empty(self) -> bool:
        return self == QaFilter()

    def sql(self) -> tuple[list[str], list]:
        """Clauses over m (match_results) and t (tracks, may be missing)."""
        clauses: list[str] = []
        values: list = []
        if self.status:
            clauses.append('m.recording_status = ?')
            values.append(self.status)
        if self.score_min is not None:
            clauses.append(f'{SCORE} >= ?')
            values.append(self.score_min)
        if self.score_max is not None:
            clauses.append(f'{SCORE} <= ?')
            values.append(self.score_max)
        if self.provider_min is not None:
            clauses.append(f'{PROVIDER} >= ?')
            values.append(self.provider_min)
        if self.artist:
            clauses.append("t.artist LIKE ? ESCAPE '\\'")
            values.append(_like(self.artist))
        if self.album:
            clauses.append("t.album LIKE ? ESCAPE '\\'")
            values.append(_like(self.album))
        if self.version == 'conflict':
            clauses.append(f'{CONFLICT} = 1')
        elif self.version == 'clean':
            clauses.append(f'{CONFLICT} = 0')
        return clauses, values

    def accepts(self, row) -> bool:
        """The part SQL cannot do. Rows carry path, title, artist, album and the stored proposal data."""
        return not self.category or row_category(row) == self.category


def row_category(row) -> str:
    top = (json.loads(row['data']).get('candidates') or [{}])[0]
    return categorize(path=row['path'], title=row['title'] or '', artist=row['artist'] or '', album=row['album'] or '',
                      proposed_title=top.get('title', ''), proposed_artist=top.get('artist', ''),
                      version_conflict=bool((top.get('evidence') or {}).get('version_conflict')))


def parse_filter(query) -> QaFilter:
    """From a parsed query string (name -> list) or a plain dict. Raises ValueError with a message safe to show."""
    category = _one(query, 'category')
    if category and category not in KEYS:
        raise ValueError('Unknown category')
    version = _one(query, 'version')
    if version not in VERSIONS:
        raise ValueError('version must be conflict or clean')
    text = {name: _one(query, name) for name in ('artist', 'album', 'status')}
    for name, value in text.items():
        if len(value) > MAX_TEXT:
            raise ValueError(f'{name} is too long')
    low, high = _number(query, 'score_min', int, 0, 100), _number(query, 'score_max', int, 0, 100)
    if low is not None and high is not None and low > high:
        raise ValueError('score_min cannot be above score_max')
    return QaFilter(status=text['status'], category=category, score_min=low, score_max=high,
                    provider_min=_number(query, 'provider_min', float, 0.0, 1.0), artist=text['artist'],
                    album=text['album'], version=version)
