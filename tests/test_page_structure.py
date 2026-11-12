"""The page's views must each contain their own content: a view that closes early leaks cards into every other view."""
from html.parser import HTMLParser
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web' / 'index.html'


class Placement(HTMLParser):
    """Records, for every element with a given class, which data-v view (if any) it sits in."""

    def __init__(self):
        super().__init__()
        self.stack, self.found = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('br', 'img', 'input', 'link', 'meta', 'hr', 'source'):
            view = next((v for t, v in reversed(self.stack) if v), '')
            if 'settings-card' in (a.get('class') or ''):
                self.found.append(view)
            return
        view = a.get('data-v') or ''
        self.stack.append((tag, view))
        if 'settings-card' in (a.get('class') or ''):
            self.found.append(next((v for t, v in reversed(self.stack) if v), ''))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def test_every_settings_card_belongs_to_the_settings_view():
    body = PAGE.read_text(encoding='utf-8').split('<body>', 1)[1].split('<script id="meloshelf-app">', 1)[0]
    parser = Placement()
    parser.feed(body)
    assert parser.found and set(parser.found) == {'settings'}, parser.found


def test_the_page_no_longer_claims_originals_are_never_changed():
    page = PAGE.read_text(encoding='utf-8')
    assert 'ORIGINALS ARE NEVER CHANGED' not in page.upper()


def test_spacing_rhythm_tokens_and_scoped_rules():
    """Section ≈24px, card ≈16px, control ≈12px; rules stay scoped to the views that needed them."""
    page = PAGE.read_text(encoding='utf-8')
    assert '--space-section:24px' in page and '--space-card:16px' in page and '--space-control:12px' in page
    for scoped in ('section[data-v=qa]>.intro', 'section[data-v=library] .lib-artist', '#consBox', '#profileBody>h2'):
        assert scoped in page, scoped
    assert 'nav{overflow-x:auto' in page.replace(' ', '')


def test_image_source_selector_is_collapsed_and_keys_are_never_prefilled():
    page = PAGE.read_text(encoding='utf-8')
    assert '<details class="card settings-card adv" id="imgSources">' in page
    for field in ('cfgFanartProject', 'cfgFanartClient', 'cfgAudiodb'):
        assert f'id="{field}"' in page and 'type="password"' in page
    assert 'fanart.tv/api-docs' in page and 'theaudiodb.com/free_music_api' in page


def test_artist_counts_use_singular_and_plural_forms_in_every_language():
    import json
    web = PAGE.parent
    page = PAGE.read_text(encoding='utf-8')
    for key in ('prof.stat.albums', 'prof.stat.singles', 'prof.stat.collabs'):
        assert f"TN('{key}'" in page, f'{key} must be pluralised (a profile with one album must not say "1 albums")'
        for catalog in sorted((web / 'i18n').glob('*.json')):
            data = json.loads(catalog.read_text(encoding='utf-8'))
            assert key + '.one' in data and key + '.other' in data and key not in data, (catalog.name, key)
    english = json.loads((web / 'i18n' / 'en.json').read_text(encoding='utf-8'))
    assert english['prof.stat.collabs.one'] == '{n} collaboration' and english['prof.stat.albums.one'] == '{n} album'


def test_the_confirmation_says_so_and_offers_no_move_when_there_is_nothing_to_move():
    import json
    page = PAGE.read_text(encoding='utf-8')
    assert "T('org.confirm.none'" in page and '$(\'orgStart\').disabled=' in page
    for catalog in sorted((PAGE.parent / 'i18n').glob('*.json')):
        assert json.loads(catalog.read_text(encoding='utf-8')).get('org.confirm.none'), catalog.name
    block = page[page.index("T('org.confirm.none'"):][:400]
    assert 'p.total' in block


def test_a_missing_fpcalc_is_explained_on_the_first_screen_with_the_official_link_and_a_recheck_without_restart():
    import json
    page = PAGE.read_text(encoding='utf-8')
    assert 'id="orgSetupLink" href="https://acoustid.org/chromaprint"' in page and 'rel="noopener noreferrer"' in page.split('id="orgSetupLink"')[1][:200]
    assert 'id="orgSetupRecheck"' in page and "$('orgSetupRecheck').onclick" in page
    assert "api('/api/config')" in page.split("$('orgSetupRecheck').onclick")[1][:200], 'rechecks the computer, not a cached answer'
    for catalog in sorted((PAGE.parent / 'i18n').glob('*.json')):
        data = json.loads(catalog.read_text(encoding='utf-8'))
        for key in ('org.setup.recheck', 'org.setup.download', 'org.setup.fpcalc_help'):
            assert data.get(key), (catalog.name, key)
    english = json.loads((PAGE.parent / 'i18n' / 'en.json').read_text(encoding='utf-8'))
    assert 'still organizes' in english['org.setup.fpcalc_help'] and 'restart' in english['org.setup.fpcalc_help']
