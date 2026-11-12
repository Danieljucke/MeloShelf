"""Looks up each distinct artist name once against MusicBrainz and stores the result.

Only names that have no stored result are sent (unless forced). A provider failure stops the run and stores
nothing for the names that failed; the Artists view keeps working on the heuristic grouping.
"""
import time

from meloshelf.domain.artist_identity import ArtistCandidate, decide_link
from meloshelf.domain.artists import canonical_key
from meloshelf.domain.primary_artist import analyse_credit, known_artists
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.music_api import APIError


BATCH = 100


def distinct_names(database) -> list[str]:
    """Primary and featured names, one display form per normalised key, in library order."""
    with database.read() as db:
        credits = [r[0] for r in db.execute("SELECT DISTINCT artist FROM tracks WHERE artist<>'' ORDER BY artist").fetchall()]
    known = known_artists(credits)
    seen, names = set(), []
    for credit in credits:
        parsed = analyse_credit(credit, known=known)
        for name in (parsed.primary, *parsed.collaborators):
            key = canonical_key(name)
            if key and key not in seen:
                seen.add(key)
                names.append(name)
    return names


class ArtistLookup:
    def __init__(self, database, client, *, force: bool = False, clock=time.time):
        self.database = database
        self.client = client
        self.force = force
        self.clock = clock

    def run(self, limit: int | None = None) -> dict:
        links = ArtistLinks(self.database)
        stored = links.all()  # read once; each answer is written in batches so progress survives an interruption
        summary = {'looked_up': 0, 'linked': 0, 'suggested': 0, 'ambiguous': 0, 'no_match': 0, 'failed': 0, 'skipped': 0}
        pending = []
        for name in distinct_names(self.database):
            if limit is not None and summary['looked_up'] >= limit:
                break
            if not self.force and canonical_key(name) in stored:
                summary['skipped'] += 1
                continue
            try:
                hits = self.client.search_artists(name)
            except APIError:
                summary['failed'] += 1
                break  # the provider is unavailable; do not keep calling it
            candidates = [ArtistCandidate(mb_id=h['mb_artist_id'], name=h['name'], score=int(h.get('score') or 0),
                                          aliases=tuple(h.get('aliases') or ()),
                                          disambiguation=h.get('disambiguation') or '') for h in hits]
            decision = decide_link(name, candidates)
            pending.append((name, decision))
            summary['looked_up'] += 1
            summary[decision.status] += 1
            if len(pending) >= BATCH:
                links.record_many(pending, looked_up_at=self.clock())
                pending = []
        if pending:
            links.record_many(pending, looked_up_at=self.clock())
        return summary
