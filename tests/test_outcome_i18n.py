"""Job outcome labels and hints (shown on the job card and in its error list) are translated in all eight languages.

The server keeps sending the English label and hint, which stay the fallback; the page looks the outcome code up
in the catalogue keys oc.<code>.label and oc.<code>.hint.
"""
import json
import re
from pathlib import Path

import pytest

from meloshelf.domain.outcomes import OUTCOMES

WEB = Path(__file__).resolve().parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web'
LANGUAGES = ('en', 'es', 'fr', 'hi', 'ja', 'ko', 'pt', 'zh-Hans')
PLACEHOLDER = re.compile(r'\{[a-zA-Z0-9_]+\}')


def catalog(lang):
    return json.loads((WEB / 'i18n' / f'{lang}.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('lang', LANGUAGES)
def test_every_outcome_has_a_label_and_a_hint_in_each_language(lang):
    entries = catalog(lang)
    for code, outcome in OUTCOMES.items():
        for part, english in (('label', outcome.label), ('hint', outcome.hint)):
            value = entries.get(f'oc.{code}.{part}')
            assert value and value.strip(), f'{lang} has no oc.{code}.{part}'
            if lang == 'en':
                assert value == english, f'oc.{code}.{part} differs from the domain text'
            assert sorted(PLACEHOLDER.findall(value)) == sorted(PLACEHOLDER.findall(english))


def test_the_job_card_and_error_list_use_the_outcome_keys():
    script = (WEB / 'index.html').read_text(encoding='utf-8').split('<script id="meloshelf-app">', 1)[1]
    for code in OUTCOMES:
        assert f"'oc.{code}.label'" in script and f"'oc.{code}.hint'" in script, code
    assert 'outcomeLabel(job.error)' in script and 'outcomeLabel(e)' in script
