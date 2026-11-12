"""The asset build treats its sources as untrusted: it refuses entity declarations, scripts, external references and
non-drawing elements, and it prefixes the brand ids so they cannot collide with other markup."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'build_assets.py'
spec = importlib.util.spec_from_file_location('build_assets', SCRIPT)
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M1 1h2"/><circle cx="5" cy="5" r="2"/></svg>'
BRAND = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><title>MeloShelf Soundwave</title>'
         '<defs><linearGradient id="tile"><stop offset="0" stop-color="#fff"/></linearGradient></defs>'
         '<path id="shape" d="M0 0h5" fill="url(#tile)"/></svg>')


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding='utf-8')
    return path


def test_an_entity_declaration_is_refused_before_parsing(tmp_path):
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg"><path d="&a;"/></svg>'
    with pytest.raises(SystemExit):
        build.safe_parse(evil, 'x')


def test_a_non_drawing_element_in_an_icon_is_refused(tmp_path):
    (tmp_path / 'bad.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                                      encoding='utf-8')
    with pytest.raises(SystemExit):
        build.lucide_symbol(tmp_path, 'bad')


def test_a_brand_svg_with_a_script_or_external_reference_is_refused(tmp_path):
    for hostile in ('<svg xmlns="http://www.w3.org/2000/svg"><script>x()</script></svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://evil.example/a.png"/></svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg" onload="x()"></svg>'):
        with pytest.raises(SystemExit):
            build.soundwave_parts(write(tmp_path, 'brand.svg', hostile))


def test_icons_are_copied_path_for_path(tmp_path):
    (tmp_path / 'house.svg').write_text(ICON, encoding='utf-8')
    symbol = build.lucide_symbol(tmp_path, 'house')
    assert symbol == '<symbol id="i-house" viewBox="0 0 24 24"><path d="M1 1h2"/><circle cx="5" cy="5" r="2"/></symbol>'


def test_brand_ids_are_prefixed_and_references_follow(tmp_path):
    defs, shapes, _ = build.soundwave_parts(write(tmp_path, 'brand.svg', BRAND))
    assert 'id="sw-tile"' in defs and 'url(#sw-tile)' in shapes and 'id="shape"' not in shapes


def test_the_block_is_replaced_between_its_markers_and_the_build_is_repeatable():
    page = '<head></head><body><!-- icons:start -->old<!-- icons:end --><p>x</p>'
    once = build.replace_block(page, '<!-- icons:start -->', '<!-- icons:end -->', 'NEW', '<body>')
    twice = build.replace_block(once, '<!-- icons:start -->', '<!-- icons:end -->', 'NEW', '<body>')
    assert once == twice and 'old' not in once and 'NEW' in once
