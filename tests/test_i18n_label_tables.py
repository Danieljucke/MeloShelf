"""Label tables built by the page script (kinds of file, version verdicts, chips, search states) carry a key and the
English text; the English must equal the catalog so a missing translation shows the same words."""
import json
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web'
PAGE = (WEB / 'index.html').read_text(encoding='utf-8')
SCRIPT = PAGE.split('<script id="meloshelf-app">', 1)[1]
ENGLISH = json.loads((WEB / 'i18n' / 'en.json').read_text(encoding='utf-8'))
PAIR = re.compile(r"\['((?:cat|v|cls|vl|fb|tg|id)\.[a-z_]+)','((?:[^'\\]|\\.)*)'\]")
CHIP = re.compile(r"\['(?:ok|rv|un)','((?:[^'\\]|\\.)*)','((?:tg|id)\.[a-z_]+)'\]")


def test_every_label_table_entry_is_in_the_catalog_with_the_same_english():
    pairs = [(k, v) for k, v in PAIR.findall(SCRIPT) if v] + [(k, v) for v, k in CHIP.findall(SCRIPT)]
    assert len(pairs) >= 30, 'the label tables are expected to be translatable'
    for key, english in pairs:
        assert key in ENGLISH, key
        assert english == ENGLISH[key], f'{key}: script says {english!r}, catalog says {ENGLISH[key]!r}'


def test_the_server_supplied_category_names_are_looked_up_in_the_current_language():
    assert 'CATEGORY_NAMES[c.key]||c.label' in PAGE
