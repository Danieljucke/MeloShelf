"""Round 5: Soundwave branding and Lucide icons.

* The logo is the approved Soundwave icon (the project's own asset), inlined as a symbol, with its accessible name.
* Icons are Lucide (ISC) paths copied from the published package into one local sprite; nothing is loaded from a CDN.
* Every icon reference resolves to a symbol; decorative icons are hidden from assistive technology; every navigation
  link keeps a visible text label as its accessible name.
* The notices for both assets are in the repository.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
REQUIRED_ICONS = {'house', 'users', 'disc-3', 'music', 'tags', 'scan-search', 'list-checks', 'badge-check', 'shield-check',
                  'settings', 'sun', 'moon', 'panel-left-close', 'panel-left-open', 'languages', 'check', 'chevron-down',
                  'folder-plus'}


def sprite():
    match = re.search(r'<!-- icons:start -->(.*?)<!-- icons:end -->', PAGE, re.S)
    assert match, 'the icon sprite markers are missing'
    return match.group(1)


def test_the_sprite_defines_every_icon_the_page_needs():
    defined = set(re.findall(r'<symbol id="i-([a-z0-9-]+)"', sprite()))
    assert REQUIRED_ICONS <= defined, REQUIRED_ICONS - defined


def test_every_icon_reference_resolves_and_nothing_is_loaded_from_outside():
    defined = set(re.findall(r'<symbol id="([a-z0-9-]+)"', sprite()))
    used = set(re.findall(r'<use href="#([a-z0-9-]+)"', PAGE))
    assert used and used <= defined, used - defined
    assert 'unpkg.com' not in PAGE and 'cdn.jsdelivr' not in PAGE and 'lucide.dev' not in PAGE
    assert not re.search(r'<use [^>]*href="https?:', PAGE)


def test_the_sprite_contains_only_plain_drawing_elements():
    body = sprite()
    assert '<script' not in body.lower() and 'foreignObject' not in body and 'onload' not in body.lower()
    assert 'xlink:href="http' not in body and 'href="http' not in body


def test_the_lucide_licence_marker_is_recorded_in_the_sprite():
    assert 'lucide-static v1.71.0' in sprite() and 'ISC' in sprite()


def test_the_soundwave_logo_is_inlined_with_its_accessible_name_and_unique_ids():
    body = sprite()
    assert '<symbol id="brand-soundwave"' in body
    assert 'MeloShelf Soundwave' in body
    ids = re.findall(r'\bid="(sw-[a-zA-Z]+)"', body)
    assert len(ids) == len(set(ids)) and ids, 'gradient and filter ids are prefixed and unique'
    assert 'url(#sw-' in body and 'url(#tile)' not in body
    assert re.search(r'<div class="logo">.*?<use href="#brand-soundwave"', PAGE, re.S)


def test_the_favicon_is_the_soundwave_icon_as_a_local_data_uri():
    match = re.search(r'<link rel="icon"[^>]*href="(data:image/svg\+xml[^"]+)"', PAGE)
    assert match, 'a local SVG favicon is required'
    assert 'MeloShelf%20Soundwave' in match.group(1) or 'MeloShelf+Soundwave' in match.group(1)


def test_every_navigation_link_has_a_decorative_icon_and_a_text_label():
    nav = re.search(r'<nav id="mainNav" aria-label="Main">(.*?)</nav>', PAGE, re.S).group(1)
    links = re.findall(r'<a href="#([a-z]+)"[^>]*>(.*?)</a>', nav, re.S)
    assert len(links) == 12
    for name, inner in links:
        assert 'aria-hidden="true"' in inner and '<use href="#i-' in inner, name
        assert re.search(r'<span class="lbl"[^>]*>[^<]+</span>', inner), name


def test_the_icon_buttons_have_accessible_names():
    for button_id in ('themeBtn', 'langBtn', 'navToggle', 'scanLaunch'):
        match = re.search(rf'<button[^>]*id="{button_id}"[^>]*>(.*?)</button>', PAGE, re.S)
        assert match, button_id
        assert re.search(r'[A-Za-z]{3,}', re.sub(r'<svg.*?</svg>', '', match.group(1), flags=re.S) + match.group(0)[:200])


def test_the_asset_notices_and_the_brand_file_are_in_the_repository():
    notices = (ROOT / 'THIRD_PARTY_NOTICES.md').read_text(encoding='utf-8')
    assert 'Lucide' in notices and 'ISC License' in notices and 'lucide-static' in notices
    assert 'Soundwave' in notices
    brand = ROOT / 'src/meloshelf/presentation/web/brand/MeloShelf-Soundwave.svg'
    svg = brand.read_text(encoding='utf-8')
    assert 'MeloShelf Soundwave' in svg and '<script' not in svg.lower()
