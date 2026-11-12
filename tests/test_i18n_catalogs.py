"""The eight interface languages: every catalog has exactly the English keys, keeps the same placeholders, has no
blank values, and English matches the text the page ships with, so a missing translation can only fall back to
the same words. The page embeds the catalogs, so the shipped file must equal the files in i18n/."""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from meloshelf.application.ui_preferences import LANGUAGES

WEB = Path(__file__).resolve().parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web'
PAGE = (WEB / 'index.html').read_text(encoding='utf-8')


def catalog(lang):
    return json.loads((WEB / 'i18n' / f'{lang}.json').read_text(encoding='utf-8'))


ENGLISH = catalog('en')
PLACEHOLDER = re.compile(r'\{[a-zA-Z0-9_]+\}')


@pytest.mark.parametrize('lang', LANGUAGES)
def test_each_catalog_has_exactly_the_english_keys(lang):
    assert set(catalog(lang)) == set(ENGLISH)


@pytest.mark.parametrize('lang', LANGUAGES)
def test_no_value_is_blank_or_markup(lang):
    for key, value in catalog(lang).items():
        assert isinstance(value, str) and value.strip(), f'{lang}:{key} is blank'
        bare = value.replace('<b>', '').replace('</b>', '')   # the folder messages may emphasise a path
        assert '<' not in bare and '>' not in bare, f'{lang}:{key} contains markup'


@pytest.mark.parametrize('lang', LANGUAGES)
def test_placeholders_match_english(lang):
    for key, value in catalog(lang).items():
        assert sorted(PLACEHOLDER.findall(value)) == sorted(PLACEHOLDER.findall(ENGLISH[key])), f'{lang}:{key}'


@pytest.mark.parametrize('lang', [l for l in LANGUAGES if l != 'en'])
def test_a_translation_is_not_wildly_longer_than_the_english_it_replaces(lang):
    """Layouts are tested with English; a value many times longer would overflow buttons and the collapsed nav."""
    for key, value in catalog(lang).items():
        english = ENGLISH[key]
        if len(english) < 4:
            continue
        assert len(value) <= len(english) * 3 + 10, f'{lang}:{key} is {len(value)} characters against {len(english)}'


def test_most_values_actually_differ_from_english():
    """Guards against a catalog that is a copy of English."""
    for lang in LANGUAGES:
        if lang == 'en':
            continue
        same = [k for k, v in catalog(lang).items() if v == ENGLISH[k]]
        assert len(same) <= len(ENGLISH) * 0.1, f'{lang} repeats English for {same}'


def test_the_language_names_are_the_eight_required_ones():
    assert LANGUAGES == ('en', 'fr', 'es', 'pt', 'ja', 'zh-Hans', 'ko', 'hi')


class Tagged(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.texts = {}
        self.attrs = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if 'data-i18n-attr' in a:
            self.attrs.append(a['data-i18n-attr'])
        if tag in ('br', 'img', 'input', 'meta', 'link', 'hr', 'use', 'path', 'circle', 'rect', 'line'):
            return
        self.stack.append([a.get('data-i18n'), []])

    def handle_startendtag(self, tag, attrs):
        a = dict(attrs)
        if 'data-i18n-attr' in a:
            self.attrs.append(a['data-i18n-attr'])

    def handle_data(self, data):
        for _, parts in self.stack:
            parts.append(data)

    def handle_endtag(self, tag):
        if not self.stack:
            return
        key, parts = self.stack.pop()
        if key:
            self.texts.setdefault(key, []).append(' '.join(''.join(parts).split()))


tagged = Tagged()
body = PAGE.split('<body', 1)[1]
tagged.feed(re.sub(r'<script.*?</script>', '', body, flags=re.S))


def test_every_tagged_element_names_a_key_in_the_catalog():
    assert tagged.texts, 'the page has no data-i18n elements'
    assert set(tagged.texts) <= set(ENGLISH), sorted(set(tagged.texts) - set(ENGLISH))
    for spec in tagged.attrs:
        for pair in spec.split(';'):
            assert pair.split(':')[1] in ENGLISH, pair


def test_every_catalog_key_is_used_by_the_page_or_its_script():
    used = set(tagged.texts)
    for spec in tagged.attrs:
        used |= {pair.split(':')[1] for pair in spec.split(';')}
    script = PAGE.split('<script id="meloshelf-app">', 1)[1]
    # a plural pair (key.one / key.other) is used through its base key in TN(...)
    used |= {key for key in ENGLISH if f"'{key}'" in script or (key.endswith(('.one', '.other')) and f"'{key.rsplit('.', 1)[0]}'" in script)}
    assert set(ENGLISH) - used == set(), sorted(set(ENGLISH) - used)


def test_english_equals_the_text_the_page_ships_with():
    for key, texts in tagged.texts.items():
        for text in texts:
            assert text == ' '.join(ENGLISH[key].split()), f'{key}: page says {text!r}, catalog says {ENGLISH[key]!r}'


def test_the_page_embeds_the_same_catalogs_as_the_files():
    match = re.search(r'<script type="application/json" id="i18n-data">(.*?)</script>', PAGE, re.S)
    assert match, 'the page has no embedded catalogs; run scripts/build_assets.py --i18n-dir'
    embedded = json.loads(match.group(1).replace('<\\/', '</'))
    assert set(embedded) == set(LANGUAGES)
    for lang in LANGUAGES:
        assert embedded[lang] == catalog(lang), f'{lang} is out of date in the page'


def test_the_catalog_markup_cannot_close_the_script_element():
    match = re.search(r'<script type="application/json" id="i18n-data">(.*?)</script>', PAGE, re.S)
    assert '</' not in match.group(1)


def test_the_notes_say_the_translations_are_unreviewed():
    note = (WEB / 'i18n' / 'README.md').read_text(encoding='utf-8')
    assert 'not been reviewed' in note
    for lang in LANGUAGES:
        assert f'`{lang}`' in note


def test_every_script_built_phrase_names_a_key_and_its_english_matches_the_catalog():
    """Text the page script builds goes through T(key, English): the English in the call is what shows when a
    translation is missing, so it must be the same words as the English catalog."""
    script = PAGE.split('<script id="meloshelf-app">', 1)[1]
    calls = re.findall(r"T\('([a-z0-9.-]+)','((?:[^'\\]|\\.)*)'\)", script)
    assert len(calls) > 100, 'the script-built phrases are expected to be translatable'
    for key, english in calls:
        assert key in ENGLISH, key
        assert english == ENGLISH[key], f'{key}: script says {english!r}, catalog says {ENGLISH[key]!r}'


def test_the_embedded_catalogs_contain_no_raw_angle_bracket_so_no_comment_or_script_sequence_can_form():
    """A raw '<!--' followed by '<script' inside a script element can confuse the HTML parser. Every '<' in the
    embedded JSON is written as \u003c, which JSON reads back as the same character."""
    match = re.search(r'<script type="application/json" id="i18n-data">(.*?)</script>', PAGE, re.S)
    block = match.group(1)
    assert '<' not in block and '<!--' not in block
    assert json.loads(block)['fr'] == catalog('fr')
