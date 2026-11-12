"""Metadata-based fallback search for files that AcoustID could not match.

The service sends only text (a parsed title and artist) to MusicBrainz. It never sends audio. Results are
cached by query, so repeating a search does not repeat the request. In offline mode nothing is sent. A
failure is reported with its cause and is not stored as an answer, so a later attempt can succeed.
"""
import hashlib
import json

from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.music_api import APIError


class FallbackService:
    def __init__(self, database, client, *, offline: bool = False):
        self.database = database
        self.client = client
        self.offline = offline
        self.store = MatchStore(database)
        self.last_error = ''

    @staticmethod
    def _key(title: str, artist: str, limit: int) -> str:
        return 'mb-text-search:v1:' + hashlib.sha256(json.dumps([title, artist, limit]).encode('utf-8')).hexdigest()

    def search(self, title: str, artist: str = '', *, limit: int = 5):
        """Candidates for this text, or None when no answer is available (offline, or a provider failure)."""
        self.last_error = ''
        if self.offline:
            self.last_error = 'offline'
            return None
        key = self._key(title, artist, limit)
        cached = self.store.get_cache(key)
        if cached is not None:
            return cached
        try:
            results = self.client.search_recordings(title, artist, limit=limit)
        except APIError as exc:
            self.last_error = exc.kind or 'provider_error'
            return None
        self.store.put_cache(key, results)
        return results
