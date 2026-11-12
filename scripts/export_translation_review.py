"""Export every interface string as a CSV for native-language reviewers.

Usage: python scripts/export_translation_review.py > translation-review.csv

One row per catalogue key: English and the seven translations side by side, the placeholders that must be kept, an
area to help the reviewer, and empty reviewer columns. ``review_status`` is always ``pending``: the translations
are unreviewed drafts, and this file records that a reviewer still has to confirm or correct each one.
"""
import csv
import json
import re
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web' / 'i18n'
LANGUAGES = ('en', 'es', 'fr', 'hi', 'ja', 'ko', 'pt', 'zh-Hans')
COLUMNS = ['key', 'area', 'placeholders', *LANGUAGES, 'review_status', 'reviewer', 'reviewed_on', 'reviewer_notes']
PLACEHOLDER = re.compile(r'\{[a-zA-Z0-9_]+\}')


def area(key: str) -> str:
    if key.startswith('err.'):
        return 'server error'
    if key.startswith('oc.'):
        return 'job outcome'
    if key.startswith('m.job-'):
        return 'job message'
    if key.startswith(('vd.', 'st.', 'sg.', 'aid.')):
        return 'status label'
    return 'interface'


def write_csv(out) -> None:
    catalogs = {lang: json.loads((WEB / f'{lang}.json').read_text(encoding='utf-8')) for lang in LANGUAGES}
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator='\n')
    writer.writeheader()
    for key in sorted(catalogs['en']):
        row = {'key': key, 'area': area(key),
               'placeholders': ' '.join(sorted(set(PLACEHOLDER.findall(catalogs['en'][key])))),
               'review_status': 'pending', 'reviewer': '', 'reviewed_on': '', 'reviewer_notes': ''}
        for lang in LANGUAGES:
            row[lang] = catalogs[lang][key]
        writer.writerow(row)


if __name__ == '__main__':
    write_csv(sys.stdout)
