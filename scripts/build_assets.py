"""Build the page's local icon sprite and Soundwave brand assets from their published sources.

Usage (every path is an argument; nothing is fetched here):
  python -I scripts/build_assets.py --lucide <lucide-static icons dir> --soundwave Assets/MeloShelf-logo.svg \
      --html src/meloshelf/presentation/web/index.html --brand-dir src/meloshelf/presentation/web/brand

Sources are treated as untrusted data: only plain drawing elements are copied, anything else aborts the build.
Lucide icons (ISC) are copied path-for-path into <symbol> elements; the Soundwave SVG gets prefixed ids so it cannot
collide with other markup. Output is written between marker comments so the build is repeatable.
"""
import argparse
import html as htmllib
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

ICONS = ['house', 'users', 'disc-3', 'music', 'tags', 'scan-search', 'list-checks', 'badge-check', 'shield-check',
         'settings', 'sun', 'moon', 'panel-left-close', 'panel-left-open', 'languages', 'check', 'chevron-down',
         'folder-plus', 'search', 'x', 'info', 'triangle-alert']
DRAWING = {'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'ellipse'}
LUCIDE_VERSION = 'v1.71.0'
SVG_NS = '{http://www.w3.org/2000/svg}'


def safe_parse(text: str, label: str):
    """Parse an untrusted SVG. A DOCTYPE or entity declaration is the vehicle for entity-expansion attacks, and plain icons
    never need one, so any source that has it is refused before parsing."""
    if re.search(r'<!\s*(DOCTYPE|ENTITY)', text, re.I):
        raise SystemExit(f'{label}: DOCTYPE or ENTITY declarations are not allowed')
    return ET.fromstring(text)


def local(tag: str) -> str:
    return tag.replace(SVG_NS, '')


def serialize(element, strip=('id',)) -> str:
    attrs = ' '.join(f'{k}="{htmllib.escape(v, quote=True)}"' for k, v in element.attrib.items() if k not in strip)
    return f'<{local(element.tag)} {attrs}/>' if attrs else f'<{local(element.tag)}/>'


def lucide_symbol(directory: Path, name: str) -> str:
    root = safe_parse((directory / f'{name}.svg').read_text(encoding='utf-8'), name)
    parts = []
    for child in root:
        if local(child.tag) not in DRAWING:
            raise SystemExit(f'{name}: unexpected element <{local(child.tag)}>')
        parts.append(serialize(child))
    return f'<symbol id="i-{name}" viewBox="0 0 24 24">{"".join(parts)}</symbol>'


def soundwave_parts(path: Path):
    text = path.read_text(encoding='utf-8')
    if re.search(r'<script|foreignObject|\son\w+=|href\s*=\s*["\']https?:', text, re.I):
        raise SystemExit('the Soundwave SVG contains active or external content; refusing to use it')
    root = safe_parse(text, 'the Soundwave SVG')
    defs, shapes = [], []
    rename = {}

    def new_id(old):
        return rename.setdefault(old, f'sw-{old}')
    for child in root:
        name = local(child.tag)
        if name == 'defs':
            for item in child:
                if item.get('id'):
                    new_id(item.get('id'))
    def fix(value):
        return re.sub(r'url\(#([\w-]+)\)', lambda m: f'url(#{rename.get(m.group(1), m.group(1))})', value)

    def render(element, strip_ids):
        attrs = {k: fix(v) for k, v in element.attrib.items() if not (k == 'id' and strip_ids)}
        if element.get('id') and not strip_ids:
            attrs['id'] = new_id(element.get('id'))
        inner = ''.join(render(c, strip_ids) for c in element)
        text = htmllib.escape(element.text or '') if element.text and element.text.strip() else ''
        a = ' '.join(f'{k}="{htmllib.escape(v, quote=True)}"' for k, v in attrs.items())
        tag = local(element.tag)
        return f'<{tag} {a}>{text}{inner}</{tag}>' if (inner or text) else f'<{tag} {a}/>'
    for child in root:
        name = local(child.tag)
        if name == 'defs':
            defs.append(''.join(render(c, False) for c in child))
        elif name in ('title', 'desc'):
            continue
        else:
            shapes.append(render(child, True))
    return ''.join(defs), ''.join(shapes), text


def build_sprite(lucide: Path, soundwave: Path) -> str:
    defs, shapes, _ = soundwave_parts(soundwave)
    symbols = ''.join(lucide_symbol(lucide, n) for n in ICONS)
    box = re.search(r'<svg[^>]*\sviewBox="([^"]+)"', soundwave.read_text(encoding='utf-8')).group(1)
    brand = f'<symbol id="brand-soundwave" viewBox="{box}"><title>MeloShelf Soundwave</title>{shapes}</symbol>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0" style="position:absolute" aria-hidden="true" '
            f'focusable="false"><!-- Lucide icons: lucide-static {LUCIDE_VERSION} - ISC; see THIRD_PARTY_NOTICES.md -->'
            f'<defs>{defs}</defs>{symbols}{brand}</svg>')


def replace_block(page: str, start: str, end: str, body: str, anchor: str, before: bool = False) -> str:
    block = f'{start}{body}{end}'
    if start in page:
        return re.sub(re.escape(start) + r'.*?' + re.escape(end), lambda m: block, page, flags=re.S)
    if anchor not in page:
        raise SystemExit(f'anchor {anchor!r} not found')
    return page.replace(anchor, block + anchor if before else anchor + block, 1)


LANGUAGES = ['en', 'fr', 'es', 'pt', 'ja', 'zh-Hans', 'ko', 'hi']


def build_i18n(directory: Path) -> str:
    """One JSON script element holding every language catalog. '</' is escaped so no string can close the element."""
    import json
    catalogs = {}
    for code in LANGUAGES:
        data = json.loads((directory / f'{code}.json').read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
            raise SystemExit(f'{code}.json must map text keys to text values')
        catalogs[code] = data
    # Every '<' becomes <: JSON reads it back unchanged, and no '</', '<!--' or '<script' can reach the parser.
    text = json.dumps(catalogs, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    return f'<script type="application/json" id="i18n-data">{text}</script>'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--lucide', required=True, type=Path)
    parser.add_argument('--soundwave', required=True, type=Path)
    parser.add_argument('--html', required=True, type=Path)
    parser.add_argument('--brand-dir', required=True, type=Path)
    parser.add_argument('--i18n-dir', type=Path, help='folder holding en.json, fr.json, ... for the eight languages')
    args = parser.parse_args(argv)
    sprite = build_sprite(args.lucide, args.soundwave)
    page = args.html.read_text(encoding='utf-8')
    page = replace_block(page, '<!-- icons:start -->', '<!-- icons:end -->', sprite, '<body>')
    svg_text = args.soundwave.read_text(encoding='utf-8')
    favicon = f'<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,{quote(svg_text, safe="")}">'
    page = replace_block(page, '<!-- favicon:start -->', '<!-- favicon:end -->', favicon, '</head>', before=True)
    if args.i18n_dir is not None:
        page = replace_block(page, '<!-- i18n:start -->', '<!-- i18n:end -->', build_i18n(args.i18n_dir),
                             '<script id="meloshelf-app">', before=True)
    args.html.write_text(page, encoding='utf-8')
    args.brand_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.soundwave, args.brand_dir / 'MeloShelf-Soundwave.svg')
    print(f'sprite {len(sprite):,} bytes, favicon {len(favicon):,} bytes, {len(ICONS)} Lucide icons')
    return 0


if __name__ == '__main__':
    sys.exit(main())
