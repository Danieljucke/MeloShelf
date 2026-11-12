"""Artist Cleanup shows real persisted identities: provider name, ID link, known aliases, collaborators and photo.

Aliases come from the provider's stored candidates for the artist ID the identity is actually grouped under; an identity
without a verified link shows none. Nothing is guessed.
"""
from meloshelf.domain.artist_identity import ArtistCandidate, LinkDecision, decide_link
from meloshelf.domain.models import Track
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.repository import LibraryRepository

MB = 'cc2c9c3c-b7bc-4b8b-84d8-4fbd8779e493'


def library(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    LibraryRepository(db).save_many([
        Track('/a.mp3', 1, '.mp3', 1, 'T', 'Adele', 'A', '', 'tagged'),
        Track('/b.mp3', 1, '.mp3', 1, 'T', 'Adele Feat. Jenn Johnson', 'B', '', 'tagged'),
        Track('/c.mp3', 1, '.mp3', 1, 'T', 'Local Band', 'C', '', 'tagged')])
    return db


def test_a_linked_identity_carries_the_providers_aliases_for_its_own_id(tmp_path):
    db = library(tmp_path)
    ArtistLinks(db).record('Adele', decide_link('Adele', [
        ArtistCandidate(MB, 'Adele', 100, aliases=('Adèle', 'Adele Adkins')),
        ArtistCandidate('other-id', 'Adele Other', 80, aliases=('Not Mine',))]), looked_up_at=1.0)
    artists, _ = LibraryRepository(db).artist_identities(limit=50)
    adele = next(a for a in artists if a['name'] == 'Adele')
    assert adele['mb_artist_id'] == MB and adele['aliases'] == ['Adèle', 'Adele Adkins']


def test_an_unlinked_identity_has_no_aliases_and_a_suggestion_does_not_borrow_them(tmp_path):
    db = library(tmp_path)
    ArtistLinks(db).record('Local Band', decide_link('Local Band', [
        ArtistCandidate('x', 'Local Band UK', 100, aliases=('LB',))]), looked_up_at=1.0)
    artists, _ = LibraryRepository(db).artist_identities(limit=50)
    band = next(a for a in artists if a['name'] == 'Local Band')
    assert band['aliases'] == [] and band['mb_artist_id'] is None


def test_aliases_are_bounded(tmp_path):
    db = library(tmp_path)
    many = tuple(f'Alias {n}' for n in range(40))
    ArtistLinks(db).record('Adele', LinkDecision('linked', mb_id=MB, name='Adele', reason='fixture'), looked_up_at=1.0)
    with db.write() as connection:
        import json
        connection.execute('UPDATE artist_links SET candidates=? WHERE credit_key=?',
                           (json.dumps([{'mb_artist_id': MB, 'name': 'Adele', 'score': 100, 'aliases': list(many),
                                         'disambiguation': ''}]), 'adele'))
    artists, _ = LibraryRepository(db).artist_identities(limit=50)
    assert len(next(a for a in artists if a['name'] == 'Adele')['aliases']) == 8


def test_collaborators_and_variants_stay_with_the_identity(tmp_path):
    db = library(tmp_path)
    artists, _ = LibraryRepository(db).artist_identities(limit=50)
    adele = next(a for a in artists if a['name'] == 'Adele')
    assert 'Jenn Johnson' in adele['collaborators']
    assert {v['credit'] for v in adele['variants']} == {'Adele', 'Adele Feat. Jenn Johnson'}


def test_the_cleanup_page_shows_photo_link_aliases_and_collaborators():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    for hook in ('a.aliases', 'a.collaborators', 'https://musicbrainz.org/artist/', 'avatar(a,'):
        assert hook in page, hook
