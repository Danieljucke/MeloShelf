"""Album artwork, looked up for the album itself and labelled by how sure MeloShelf is.

confirmed  – the album's own embedded art, a cover the person chose, or the cover of a release the recognised
             recordings agree on. Only a confirmed cover is ever embedded in an organized copy.
plausible  – a catalogue search found an album with the same title and artist. Shown for the person to accept or
             ignore; never written to a file on its own.
missing    – nothing found (or the catalogue could not be asked).
"""
import hashlib
import json
import time

from meloshelf.domain.artists import canonical_key
from meloshelf.domain.metadata import credits_agree
from meloshelf.infrastructure.tag_writer import read_cover

RETRY_AFTER = 7 * 24 * 3600          # a catalogue that found nothing is not asked again for a week
MIN_SCORE = 90
RELEASE_TYPES = ('Album', 'EP', 'Single')
EXCLUDED_TYPES = {'Live', 'Compilation', 'Remix', 'DJ-mix', 'Mixtape/Street', 'Demo', 'Interview', 'Audiobook', 'Spokenword'}
_EXTENSION = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}


def _key(album: str, artist: str) -> str:
    return f'{canonical_key(album)}|{canonical_key(artist)}'


class AlbumArtService:
    def __init__(self, store, covers, cache, directory, *, search=None, clock=time.time, fallbacks=None, max_provider_bytes=100_000_000):
        self.store, self.covers, self.cache = store, covers, cache
        self.directory, self.search, self.clock = directory, search, clock
        self._fallbacks = fallbacks                  # optional providers (Fanart.tv, TheAudioDB): fallbacks, display-only
        self.max_provider_bytes = max_provider_bytes

    def fallbacks(self) -> list:
        return list((self._fallbacks() if callable(self._fallbacks) else self._fallbacks) or [])

    # ── looking up ───────────────────────────────────────────────────────

    def lookup(self, album: str, artist: str, paths=(), *, network: bool = True, year: str = '', check_held: bool = True) -> dict:
        album, artist = (album or '').strip(), (artist or '').strip()
        if not album:
            return {'state': 'missing', 'source': ''}
        held = self.covers.cover(album, artist) if check_held else None
        if held:                                   # a cover the person chose, or a verified release's cover
            out = {'state': 'confirmed', 'source': 'coverartarchive', 'content_type': held[0], 'data': held[1], 'embeddable': True}
            accepted = self.store.get_setting(self._accepted_key(album, artist), None)
            if isinstance(accepted, dict) and accepted.get('sha256') == hashlib.sha256(held[1]).hexdigest():
                # The person chose a provider's image. The choice is theirs; the image is still the provider's, and a
                # choice is not proof of reuse rights, so the source stays attached and the image is never embedded.
                out.update(source=accepted['provider'], chosen=True, embeddable=False,
                           attribution={k: accepted.get(k, '') for k in ('provider', 'source_url', 'terms_url', 'license', 'author')})
            return out
        for path in paths:
            embedded = read_cover(path)
            if embedded:
                return {'state': 'confirmed', 'source': 'embedded', 'content_type': embedded[0], 'data': embedded[1], 'embeddable': True}
        return self._catalogue(album, artist, network, str(year or '')[:4])

    @staticmethod
    def _year_of(date: str) -> str:
        """The four-digit year a date starts with, or '' when it does not start with one."""
        head = str(date or '')[:4]
        return head if len(head) == 4 and head.isdigit() else ''

    def _choose(self, hits: list, album: str, artist: str, year: str) -> dict:
        """The catalogue's answer for this album, judged only on evidence.

        confirmed - exactly one release group has the album's exact title, agrees with the artist, is an Album, EP or
                    Single with no live, compilation, remix or similar flag, does not contradict the year in the tags,
                    and neither year is impossible (before recorded music or later than next year).
        plausible - the artist and title match but the group or edition is uncertain (several equals, no release type,
                    or a year that cannot be right).
        Anything live, a compilation, a remix or from another year is ruled out: it is another release, not this album."""
        latest = time.gmtime(self.clock()).tm_year + 1
        possible = lambda y: bool(y) and 1877 <= int(y) <= latest      # noqa: E731 - the first recorded sound is from 1877
        tag_year = self._year_of(year)
        doubtful_tag = bool(year) and not possible(tag_year)           # a tag year that cannot be right is not evidence either way
        fits = []
        for h in hits:
            if h.get('score', 0) < MIN_SCORE or canonical_key(h.get('title', '')) != canonical_key(album) \
                    or not credits_agree(artist, h.get('artist', '')) or not h.get('group_id'):
                continue
            if set(h.get('secondary_types') or ()) & EXCLUDED_TYPES:
                continue
            when = self._year_of(h.get('first_release_date', ''))
            if not doubtful_tag and tag_year and when and tag_year != when:
                continue
            fits.append(h)
        if not fits:
            return {'group_id': '', 'state': 'missing'}
        doubtful = lambda h: bool(self._year_of(h.get('first_release_date', ''))) and not possible(self._year_of(h.get('first_release_date', '')))  # noqa: E731
        typed = [h for h in fits if h.get('primary_type') in RELEASE_TYPES]
        if len(fits) == 1 and typed and not doubtful_tag and not doubtful(fits[0]):
            return {'group_id': fits[0]['group_id'], 'state': 'confirmed', 'primary_type': fits[0]['primary_type']}
        best = sorted(fits, key=lambda h: (doubtful(h), -h.get('score', 0), h.get('first_release_date') or '9999'))[0]
        return {'group_id': best['group_id'], 'state': 'plausible', 'primary_type': best.get('primary_type', '')}

    def _catalogue(self, album: str, artist: str, network: bool = True, year: str = '') -> dict:
        if self.search is None:
            return {'state': 'missing', 'source': ''}
        key = 'album_art_search:' + _key(album, artist)
        remembered = self.store.get_setting(key, None)
        if isinstance(remembered, dict) and self.clock() - remembered.get('at', 0) < RETRY_AFTER \
                and (not year or remembered.get('year', year) == year):
            choice = remembered
        elif not network:
            return {'state': 'missing', 'source': ''}
        else:
            try:
                hits = self.search(album, artist) or []
            except Exception:                      # offline, rate limited, malformed: the cover stays missing, nothing is remembered
                return {'state': 'missing', 'source': ''}
            choice = {**self._choose(hits, album, artist, year), 'at': self.clock(), 'year': year}
            self.store.set_setting(key, choice)
        group = choice.get('group_id') or ''
        if not group:
            return {'state': 'missing', 'source': ''}
        state = 'confirmed' if choice.get('state') == 'confirmed' else 'plausible'
        image = self.cache.cover(group, group=True)
        if image:
            return {'state': state, 'source': 'musicbrainz_release_group' if state == 'confirmed' else 'musicbrainz_search',
                    'group_id': group, 'content_type': image[0], 'data': image[1], 'primary_type': choice.get('primary_type', ''), 'embeddable': True}
        provided = self._provider_cover(group, network)
        if provided is None:
            return {'state': 'missing', 'source': ''}
        return {'state': state, 'source': provided['provider'], 'group_id': group, 'content_type': provided['content_type'], 'data': provided['data'],
                'primary_type': choice.get('primary_type', ''), 'embeddable': False,
                'attribution': {k: provided[k] for k in ('provider', 'source_url', 'terms_url', 'license', 'author')}}

    # ── optional providers: only for a release group MusicBrainz identified, only when Cover Art Archive has no image ──

    def _provider_file(self, name: str):
        from pathlib import Path
        return Path(self.directory) / 'provider-covers' / name

    def _provider_cover(self, group: str, network: bool):
        extras = self.fallbacks()
        signature = ','.join(sorted(c.name for c in extras))
        key = 'album_art_provider:' + group
        record = self.store.get_setting(key, None)
        if isinstance(record, dict) and (record.get('sources') == signature or not network):
            return self._read_provider(record)
        if not extras or not network:
            return None
        from meloshelf.domain.artwork import sniff_image
        from meloshelf.infrastructure.art_sources import SourceError
        from meloshelf.infrastructure.music_api import APIError
        for client in extras:
            try:
                found = client.album_cover(group)
                if not found:
                    continue
                declared, data = client.download(found['url'])
            except APIError:
                return None                          # a rate limit or outage is not an answer: nothing is remembered
            except SourceError:
                continue
            real = sniff_image(data)
            if real is None:
                continue
            import hashlib
            name = hashlib.sha256(data).hexdigest()[:32] + ('.png' if real == 'image/png' else '.jpg')
            target = self._provider_file(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self._evict_provider_files(keep=name)
            record = {'sources': signature, 'file': name, 'content_type': real, 'provider': client.name, 'source_url': found.get('source_url', ''),
                      'terms_url': found.get('terms_url', ''), 'license': found.get('license', ''), 'author': found.get('author', ''), 'at': self.clock()}
            self.store.set_setting(key, record)
            return self._read_provider(record)
        self.store.set_setting(key, {'sources': signature, 'file': '', 'at': self.clock()})      # nobody had one, for these providers
        return None

    def _read_provider(self, record: dict):
        if not record.get('file'):
            return None
        try:
            data = self._provider_file(record['file']).read_bytes()
        except OSError:
            return None
        return {'content_type': record['content_type'], 'data': data, 'provider': record['provider'], 'source_url': record.get('source_url', ''),
                'terms_url': record.get('terms_url', ''), 'license': record.get('license', ''), 'author': record.get('author', '')}

    def _evict_provider_files(self, keep: str) -> None:
        """Bounded storage: the oldest provider images go first."""
        folder = self._provider_file('').parent
        files = sorted((p for p in folder.glob('*') if p.is_file()), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for p in files:
            if total <= self.max_provider_bytes:
                return
            if p.name != keep:
                total -= p.stat().st_size
                p.unlink()

    def describe(self, album: str, artist: str, *, check_held: bool = True) -> dict:
        """State and attribution for display; reads only what is already cached. ``check_held=False`` skips the verified-release
        and chosen-cover lookup for callers that already know it."""
        found = self.lookup(album, artist, (), network=False, check_held=check_held)
        info = {'state': found['state'], 'source': found.get('source', '')}
        if found.get('group_id'):
            info.update(group_id=found['group_id'], provenance='coverartarchive',
                        source_url='https://musicbrainz.org/release-group/' + found['group_id'], primary_type=found.get('primary_type', ''))
        if found.get('chosen'):
            info['chosen'] = True
        if found.get('attribution'):
            a = found['attribution']
            info.update(provenance=a['provider'], source_url=a['source_url'], terms_url=a['terms_url'], license=a['license'], author=a['author'])
        info['embeddable'] = bool(found.get('embeddable', False))
        return info

    def enrich(self, pairs, *, should_cancel=lambda: False, on_progress=lambda done, total, counts: None) -> dict:
        """Look up many (album, artist, year) pairs one at a time. Each answer is cached as it arrives, so cancelling or a
        restart loses nothing and a second run only asks about what is still unknown."""
        counts = {'confirmed': 0, 'plausible': 0, 'missing': 0}
        total, done = len(pairs), 0
        for album, artist, year in pairs:
            if should_cancel():
                return {'done': done, 'total': total, 'counts': {**counts, 'suggested': counts['plausible']}, 'cancelled': True}
            state = self.lookup(album, artist, (), year=year)['state']
            counts[state] = counts.get(state, 0) + 1
            done += 1
            on_progress(done, total, dict(counts))
        return {'done': done, 'total': total, 'counts': {**counts, 'suggested': counts['plausible']}, 'cancelled': False}

    # ── what happens to a found cover ───────────────────────────────────────

    def accept(self, album: str, artist: str) -> bool:
        """The person accepts the suggested cover: it becomes their own confirmed choice."""
        found = self._catalogue(album, artist, False)
        if found['state'] == 'missing' or (found['state'] == 'confirmed' and found.get('embeddable', True)):
            return False                         # nothing to accept, or already the person's / a verified cover
        digest = hashlib.sha256(found['data']).hexdigest()
        held = self.covers.cover(album, artist)
        provided = found.get('attribution')
        if provided and held and hashlib.sha256(held[1]).hexdigest() == digest:
            return True                          # already chosen: nothing new to record
        self.covers.set_manual(album, artist, found['content_type'], found['data'], note='accepted suggestion')
        if provided:                             # keep where it came from and on what terms, next to the person's choice
            self.store.set_setting(self._accepted_key(album, artist), {'sha256': digest, **provided})
        return True

    @staticmethod
    def _accepted_key(album: str, artist: str) -> str:
        return 'album_art_accepted:' + _key(album, artist)

    def cover_for(self, decision, item) -> str:
        """A file path for the cover of a CONFIRMED album, to embed in a copy; '' otherwise."""
        if not decision.album:
            return ''
        paths = list((item or {}).get('paths') or [])
        row = (item or {}).get('row') or {}
        if row.get('path'):
            paths.append(row['path'])
        found = self.lookup(decision.album, decision.artist, paths)
        if found['state'] != 'confirmed' or not found.get('embeddable', True):
            return ''                            # a provider's cover is display-only until the person accepts it
        extension = _EXTENSION.get(found['content_type'], 'jpg')
        target = self.directory / f"{hashlib.sha256(found['data']).hexdigest()[:32]}.{extension}"
        if not target.exists():
            self.directory.mkdir(parents=True, exist_ok=True)
            target.write_bytes(found['data'])
        return str(target)
