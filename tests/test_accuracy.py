"""Accuracy from human labels only: precision, recall, coverage and uncertainty, with sample sizes reported."""
import pytest

from meloshelf.domain.accuracy import summarize, wilson_interval


def row(status, label, corrected=False, cause=''):
    return {'status': status, 'label': label, 'has_correction': corrected, 'cause': cause}


def test_wilson_interval_for_a_perfect_small_sample_is_wide():
    low, high = wilson_interval(13, 13)
    assert low < 0.80 and high == pytest.approx(1.0)


def test_wilson_interval_with_no_observations_is_the_full_range():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_unsure_is_neither_correct_nor_incorrect():
    report = summarize([row('AUTO_APPROVED', 'correct'), row('AUTO_APPROVED', 'unsure')], proposals=2)
    assert report['auto']['judged'] == 1 and report['auto']['precision'] == 1.0
    assert report['unsure'] == 1


def test_precision_counts_only_incorrect_automatic_approvals():
    rows = [row('AUTO_APPROVED', 'correct')] * 9 + [row('AUTO_APPROVED', 'incorrect', cause='version')]
    report = summarize(rows, proposals=10)
    assert report['auto']['precision'] == pytest.approx(0.9)
    assert report['auto']['incorrect'] == 1
    assert report['auto']['false_positive_causes'] == {'version': 1}


def test_recall_measures_identifiable_tracks_found_automatically():
    rows = [row('AUTO_APPROVED', 'correct'),
            row('NEEDS_REVIEW', 'incorrect', corrected=True),  # identifiable: the right answer was offered
            row('UNIDENTIFIED', 'incorrect', corrected=True)]
    report = summarize(rows, proposals=2)
    # Three identifiable tracks: one correct automatic approval, two incorrect ones whose right answer was offered.
    assert report['identifiable'] == 3
    assert report['recall_automatic'] == pytest.approx(1 / 3)


def test_coverage_is_the_share_of_proposals_approved_automatically():
    rows = [row('AUTO_APPROVED', 'correct'), row('NEEDS_REVIEW', 'correct')]
    assert summarize(rows, proposals=4)['auto_coverage'] == pytest.approx(0.25)


def test_small_samples_are_marked_as_insufficient_for_production_claims():
    report = summarize([row('AUTO_APPROVED', 'correct')] * 13, proposals=13)
    assert report['auto']['sufficient_sample'] is False
    assert report['auto']['interval95'][0] < 1.0


def test_no_match_and_technical_failure_rates_are_reported_separately():
    report = summarize([row('UNIDENTIFIED', 'correct')], proposals=1, technical_failures=1, submitted=3)
    assert report['no_match_rate'] == pytest.approx(1.0)
    assert report['technical_failure_rate'] == pytest.approx(1 / 3)


def test_accuracy_by_status_is_available_for_review_items_too():
    rows = [row('NEEDS_REVIEW', 'correct'), row('NEEDS_REVIEW', 'incorrect')]
    assert summarize(rows, proposals=2)['review']['precision'] == pytest.approx(0.5)
