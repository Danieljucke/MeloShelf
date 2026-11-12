"""Runs the metadata fallback over files without an acoustic match.

Durable and resumable: each answered file is stored at once, and a later run skips answered files. Files with
an acoustic match are never searched. Offline runs send nothing. A provider failure leaves the file unanswered,
so a later run can try again. Cancellation stops between files and keeps what is already stored.
"""
import dataclasses
import os
import re
from pathlib import Path

from meloshelf.domain.fallback import evaluate_fallback, parse_filename
from meloshelf.infrastructure.fallback_store import FallbackStore


def read_duration(path) -> float | None:
    """The length in seconds, read without writing anything. None when the file cannot be read."""
    try:
        from mutagen import File
        audio = File(Path(path))
        length = getattr(getattr(audio, 'info', None), 'length', None)
        return float(length) if length and length > 0 else None
    except Exception:  # an unreadable file is a finding, not a crash
        return None


class FallbackRunner:
    def __init__(self, database, service, *, should_cancel=None, duration_of=None):
        self.database = database
        self.service = service
        self.should_cancel = should_cancel or (lambda: False)
        self.duration_of = duration_of or read_duration
        self.store = FallbackStore(database)

    def _pending(self, limit: int) -> list[dict]:
        answered = self.store.answered()
        with self.database.read() as db:
            rows = db.execute('''SELECT t.path, t.title, t.artist FROM tracks t
                                 LEFT JOIN match_results m ON m.path = t.path
                                 WHERE (m.path IS NULL OR m.data NOT LIKE '%"recording_id"%')
                                 ORDER BY t.path''').fetchall()
        pending = [dict(r) for r in rows if r['path'] not in answered]
        return pending[:max(0, limit)]

    def run(self, *, limit: int = 25, on_progress=None) -> dict:
        """``on_progress(summary)`` is called after each file has been dealt with, with a copy of the totals."""
        summary = {'examined': 0, 'suggested': 0, 'unidentified': 0, 'provider_failed': 0,
                   'offline_skipped': 0, 'cancelled': False}
        for row in self._pending(limit):
            if self.should_cancel():
                summary['cancelled'] = True
                break
            summary['examined'] += 1
            self._one(row, summary)
            if on_progress:
                on_progress(dict(summary))
        return summary

    def _one(self, row: dict, summary: dict) -> None:
        name = os.path.basename(row['path'].replace('\\', '/'))
        parsed = parse_filename(name)
        # The filename is preferred; the file's own tags fill in what the filename does not say.
        parsed = dataclasses.replace(parsed, title=parsed.title or row['title'] or '',
                                     artist=parsed.artist or row['artist'] or '')
        # Search with the base title; bracketed descriptors are compared when the candidates are scored.
        base = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', parsed.title).strip() or parsed.title
        candidates = self.service.search(base, parsed.artist)
        if candidates is None:
            if self.service.last_error == 'offline':
                summary['offline_skipped'] += 1
            else:
                summary['provider_failed'] += 1
            return
        outcome = evaluate_fallback(parsed, self.duration_of(row['path']), candidates)
        self.store.put(row['path'], outcome.level, {
            'query': {'title': parsed.title, 'artist': parsed.artist, 'featured': list(parsed.featured),
                      'version_terms': list(parsed.version_terms), 'removed': list(parsed.removed)},
            'candidate_count': len(candidates), 'reason': outcome.reason,
            'chosen': outcome.chosen, 'evidence': list(outcome.evidence),
            'version_pending': outcome.version_pending, 'alternatives': list(outcome.alternatives),
            'acoustically_verified': False,
        })
        summary['suggested' if outcome.level == 'METADATA_SUGGESTED' else 'unidentified'] += 1
