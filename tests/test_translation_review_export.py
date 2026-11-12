"""The translation-review export lists every catalogue entry for native-language reviewers and never claims a review."""
import csv
import importlib.util
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('export_translation_review', ROOT / 'scripts' / 'export_translation_review.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def rows():
    buffer = io.StringIO()
    exporter.write_csv(buffer)
    return list(csv.DictReader(io.StringIO(buffer.getvalue())))


def test_every_catalogue_key_has_a_row_with_all_eight_languages():
    english = json.loads((ROOT / 'src' / 'meloshelf' / 'presentation' / 'web' / 'i18n' / 'en.json').read_text(encoding='utf-8'))
    data = rows()
    assert {r['key'] for r in data} == set(english)
    for row in data:
        for lang in ('en', 'es', 'fr', 'hi', 'ja', 'ko', 'pt', 'zh-Hans'):
            assert row[lang].strip(), (row['key'], lang)


def test_no_row_claims_a_review_and_placeholders_are_listed():
    data = rows()
    assert {r['review_status'] for r in data} == {'pending'}
    assert all(r['reviewer'] == '' and r['reviewed_on'] == '' for r in data)
    row = next(r for r in data if r['key'] == 'err.threshold_out_of_range')
    assert row['placeholders'] == '{high} {low}' and row['area'] == 'server error'


def test_areas_distinguish_server_errors_job_outcomes_and_interface_text():
    areas = {r['area'] for r in rows()}
    assert {'server error', 'job outcome', 'job message', 'interface'} <= areas
