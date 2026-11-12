"""Accuracy from human labels only.

A label is 'correct', 'incorrect' or 'unsure'. Unsure is neither right nor wrong and is excluded from precision.
Intervals are Wilson 95% intervals, so small samples show how little they establish.
"""
from collections import Counter
from math import sqrt

MIN_SAMPLE = 30  # below this, no production-grade claim is made from the figures
Z95 = 1.959963984540054


def wilson_interval(successes: int, trials: int) -> tuple[float, float]:
    if trials <= 0:
        return (0.0, 1.0)
    p = successes / trials
    denominator = 1 + Z95 ** 2 / trials
    centre = (p + Z95 ** 2 / (2 * trials)) / denominator
    margin = Z95 * sqrt(p * (1 - p) / trials + Z95 ** 2 / (4 * trials ** 2)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _group(rows, status_names):
    selected = [r for r in rows if r['status'] in status_names]
    judged = [r for r in selected if r['label'] in ('correct', 'incorrect')]
    correct = sum(1 for r in judged if r['label'] == 'correct')
    incorrect = len(judged) - correct
    precision = correct / len(judged) if judged else None
    return {
        'labelled': len(selected), 'judged': len(judged), 'correct': correct, 'incorrect': incorrect,
        'unsure': sum(1 for r in selected if r['label'] == 'unsure'),
        'precision': precision,
        'interval95': wilson_interval(correct, len(judged)),
        'sufficient_sample': len(judged) >= MIN_SAMPLE,
        'false_positive_causes': dict(Counter(r.get('cause') or 'unspecified' for r in judged if r['label'] == 'incorrect')),
    }


def summarize(rows, *, proposals: int, technical_failures: int = 0, submitted: int | None = None) -> dict:
    """Figures from labelled rows only. Each row: status, label, has_correction, cause."""
    auto = _group(rows, {'AUTO_APPROVED'})
    review = _group(rows, {'NEEDS_REVIEW'})
    identifiable = sum(1 for r in rows if r['label'] == 'correct' or (r['label'] == 'incorrect' and r.get('has_correction')))
    auto_identifiable = sum(1 for r in rows if r['status'] == 'AUTO_APPROVED' and r['label'] == 'correct')
    total = submitted if submitted is not None else max(1, proposals)
    no_match = sum(1 for r in rows if r['status'] == 'UNIDENTIFIED')
    return {
        'auto': auto,
        'review': review,
        'unsure': sum(1 for r in rows if r['label'] == 'unsure'),
        'identifiable': identifiable,
        'recall_automatic': (auto_identifiable / identifiable) if identifiable else None,
        'auto_coverage': (auto['labelled'] / proposals) if proposals else None,
        'no_match_rate': (no_match / len(rows)) if rows else None,
        'technical_failure_rate': (technical_failures / total) if total else None,
    }


def by_category(rows) -> dict:
    """Precision per sample category from labelled rows (each with 'category', 'label'). Every category is present,
    so an empty one reads as "nothing judged yet" rather than disappearing."""
    from meloshelf.domain.sample_category import KEYS
    out = {}
    for key in KEYS:
        chosen = [r for r in rows if r.get('category') == key]
        judged = [r for r in chosen if r['label'] in ('correct', 'incorrect')]
        correct = sum(1 for r in judged if r['label'] == 'correct')
        out[key] = {'labelled': len(chosen), 'judged': len(judged), 'correct': correct,
                    'incorrect': len(judged) - correct, 'unsure': sum(1 for r in chosen if r['label'] == 'unsure'),
                    'precision': correct / len(judged) if judged else None,
                    'interval95': wilson_interval(correct, len(judged)),
                    'sufficient_sample': len(judged) >= MIN_SAMPLE}
    return out
