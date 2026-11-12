"""Stored provider links from a credit to a MusicBrainz artist ID.

One row per normalised credit. A new lookup replaces the provider row; it never touches a user decision, which
lives in the correction log and takes precedence when identities are grouped.
"""
import json

from meloshelf.domain.artists import canonical_key


class ArtistLinks:
    def __init__(self, database):
        self.database = database

    def record(self, credit: str, decision, *, looked_up_at: float) -> None:
        self.record_many([(credit, decision)], looked_up_at=looked_up_at)

    def record_many(self, items, *, looked_up_at: float) -> None:
        """Store provider decisions in one transaction.

        A stored link is never silently switched. If a later lookup names a different artist, or none, the row
        becomes a conflict: the stored ID is kept, the new answer is recorded in the reason, and the credit is not
        grouped by ID until a person decides.
        """
        with self.database.write() as db:
            for credit, decision in items:
                self._apply(db, credit, decision, looked_up_at)

    def _apply(self, db, credit, decision, looked_up_at):
        key = canonical_key(credit)
        if not key:
            return
        row = db.execute('SELECT * FROM artist_links WHERE credit_key=?', (key,)).fetchone()
        status, mb_id, mb_name, reason = decision.status, decision.mb_id, decision.name, decision.reason
        stored_id = row['mb_artist_id'] if row and row['status'] in ('linked', 'conflict') else None
        if stored_id and not (decision.status == 'linked' and decision.mb_id == stored_id):
            new_answer = decision.mb_id or decision.status
            status = 'conflict'
            mb_id, mb_name = stored_id, row['mb_name']
            reason = f'a later lookup answered {new_answer}; the stored link is kept until a person decides'
        db.execute('''INSERT INTO artist_links(credit_key,credit,mb_artist_id,mb_name,status,provenance,reason,looked_up_at,candidates)
                      VALUES(?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(credit_key) DO UPDATE SET credit=excluded.credit, mb_artist_id=excluded.mb_artist_id,
                        mb_name=excluded.mb_name, status=excluded.status, provenance=excluded.provenance,
                        reason=excluded.reason, looked_up_at=excluded.looked_up_at, candidates=excluded.candidates''',
                   (key, ' '.join(credit.split()), mb_id, mb_name, status,
                    decision.provenance, reason, looked_up_at, json.dumps(list(decision.candidates))))

    def get(self, credit: str) -> dict | None:
        with self.database.read() as db:
            row = db.execute('SELECT * FROM artist_links WHERE credit_key=?', (canonical_key(credit),)).fetchone()
        if row is None:
            return None
        stored = dict(row)
        stored['candidates'] = json.loads(stored.get('candidates') or '[]')
        return stored

    def all(self) -> dict:
        """Every stored link, keyed by normalised credit."""
        with self.database.read() as db:
            rows = db.execute('SELECT * FROM artist_links').fetchall()
        return {r['credit_key']: dict(r) for r in rows}
