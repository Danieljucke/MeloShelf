"""Metadata reliability: field-level classification and folder-level contamination patterns.

Tags are never declared wrong on their own. Suspicion needs independent evidence, and a replacement is
proposed only when the acoustically matched recordings agree on it. Original tags are never changed here.
"""
import unicodedata

from meloshelf.domain.metadata import (CONFLICTING, MISSING, PARTIALLY_SUPPORTED, RELIABLE,
                                       SUSPECTED_CONTAMINATED, UNKNOWN, MetaRecord, analyse_folder)

FOLDER = 'D:/Library/Folder---01'


def rec(name, artist, *, title='', album='SS19', cand_artist='', cand_title='', status='ACOUSTICALLY_MATCHED'):
    return MetaRecord(path=f'{FOLDER}/{name}', title=title or name, artist=artist, album=album,
                      candidate_artist=cand_artist, candidate_title=cand_title or name,
                      candidate_album='', candidate_status=status, filename=name)


def artist_assessments(report):
    return [a for a in report.assessments if a.field == 'artist']


# ── the Folder---01 regression: a shared tag contradicted by the recordings ───────

def ss19_records():
    # Inherited "Tedashii/Steven Malcom" on recordings that are by other performers.
    return [
        rec('CASS_-_Control_feat._Parris_Chariz.mp3', 'Tedashii/Steven Malcom', cand_artist='CASS feat. Parris Chariz'),
        rec('KB_-_Hold_Me_Back.mp3', 'Tedashii/Steven Malcom', cand_artist='KB'),
        rec('DJ_Mykael_V_-_Switches.mp3', 'Tedashii/Steven Malcom', cand_artist='DJ Mykael V'),
        rec('1K_Phew,_Aha_Gazelle_-_x10.mp3', 'Tedashii/Steven Malcom', cand_artist='1K Phew'),
        rec('Aklesso_-_Lights.mp3', 'Tedashii/Steven Malcom', cand_artist='Aklesso'),
    ]


def test_a_tag_repeated_across_recordings_that_the_audio_contradicts_is_suspected_contaminated():
    report = analyse_folder(ss19_records())
    assert {a.classification for a in artist_assessments(report)} == {SUSPECTED_CONTAMINATED}


def test_no_single_replacement_is_proposed_when_the_recordings_disagree_among_themselves():
    report = analyse_folder(ss19_records())
    group = [g for g in report.groups if g.field == 'artist'][0]
    assert group.proposed == ''
    assert group.individual_review is True
    assert len(group.paths) == 5


def test_the_report_states_the_evidence_and_keeps_the_original_value():
    report = analyse_folder(ss19_records())
    first = artist_assessments(report)[0]
    assert first.original == 'Tedashii/Steven Malcom'
    assert any('acoustic' in source for source in first.evidence_sources)
    assert first.reasons


# ── filename evidence alone is never proof ─────────────────────────────────────

def test_a_filename_that_names_another_performer_alone_does_not_make_a_tag_contaminated():
    records = [rec('KB_-_Hold_Me_Back.mp3', 'Tedashii/Steven Malcom', status='')]  # no acoustic evidence
    assessment = artist_assessments(analyse_folder(records))[0]
    assert assessment.classification != SUSPECTED_CONTAMINATED
    assert assessment.classification in (UNKNOWN, PARTIALLY_SUPPORTED)


def test_a_single_disagreeing_recording_is_conflicting_not_a_folder_pattern():
    records = [rec('Song.mp3', 'Wrong Artist', cand_artist='Right Artist')]
    assessment = artist_assessments(analyse_folder(records))[0]
    assert assessment.classification == CONFLICTING
    assert assessment.proposed == 'Right Artist'


# ── legitimate shared credits and compilations are not flagged ─────────────────

def test_a_compilation_whose_recordings_each_confirm_their_own_artist_is_not_contaminated():
    records = [
        rec('Danny Gokey - Fighter.mp3', 'Various Artists', album='Compilation', cand_artist='Danny Gokey'),
        rec('Colton Dixon - Breathe.mp3', 'Various Artists', album='Compilation', cand_artist='Colton Dixon'),
        rec('Byron Juane - Run.mp3', 'Various Artists', album='Compilation', cand_artist='Byron Juane'),
        rec('Aaron Cole - Einstein.mp3', 'Various Artists', album='Compilation', cand_artist='Aaron Cole'),
    ]
    report = analyse_folder(records)
    assert SUSPECTED_CONTAMINATED not in {a.classification for a in artist_assessments(report)}
    assert all(a.classification != SUSPECTED_CONTAMINATED for a in report.assessments)


def test_a_shared_artist_tag_that_all_the_recordings_confirm_is_reliable():
    records = [rec(f'Danny Gokey - Song {i}.mp3', 'Danny Gokey', cand_artist='Danny Gokey') for i in range(4)]
    assert {a.classification for a in artist_assessments(analyse_folder(records))} == {RELIABLE}


# ── collaborations are preserved, not flattened ────────────────────────────────

def test_a_collaboration_credit_matching_the_recording_is_kept_and_not_reported_as_an_error():
    records = [rec('Big Dog Walking.mp3', 'Canon, MainMain', cand_artist='Canon feat. MainMain')]
    assessment = artist_assessments(analyse_folder(records))[0]
    assert assessment.classification == RELIABLE
    assert assessment.proposed == ''


# ── field completeness and text forms ──────────────────────────────────────────

def test_a_missing_artist_is_missing_and_a_generic_title_is_missing():
    records = [MetaRecord(path=f'{FOLDER}/x.mp3', title='Track 01', artist='', album='', candidate_status='', filename='x.mp3')]
    report = analyse_folder(records)
    by_field = {a.field: a.classification for a in report.assessments}
    assert by_field['artist'] == MISSING
    assert by_field['title'] == MISSING


def test_case_and_unicode_variants_of_one_artist_are_conflicting_with_a_canonical_proposal():
    nfc = unicodedata.normalize('NFC', 'Beyoncé')
    nfd = unicodedata.normalize('NFD', 'Beyoncé')
    records = [rec('a.mp3', nfc, cand_artist='Beyoncé'), rec('b.mp3', nfc, cand_artist='Beyoncé'),
               rec('c.mp3', nfd, cand_artist='Beyoncé'), rec('d.mp3', 'BEYONCÉ', cand_artist='Beyoncé')]
    report = analyse_folder(records)
    variant_groups = [g for g in report.groups if g.field == 'artist' and g.classification == CONFLICTING]
    assert variant_groups, 'form variants must be reported'
    assert variant_groups[0].proposed == nfc


def test_the_same_text_in_different_unicode_forms_is_not_treated_as_different_by_the_matcher():
    from meloshelf.domain.metadata import canonical_text
    assert canonical_text(unicodedata.normalize('NFD', 'Café')) == canonical_text('Café')


def test_legitimate_album_tags_shared_by_one_artist_are_not_flagged():
    records = [rec(f'Bethel {i}.mp3', 'Bethel Music', album='Moments: Mighty Sound', cand_artist='Bethel Music')
               for i in range(3)]
    report = analyse_folder(records)
    assert all(a.classification != SUSPECTED_CONTAMINATED for a in report.assessments)


def test_original_tags_are_never_modified_by_the_analysis():
    records = ss19_records()
    before = [(r.artist, r.album, r.title) for r in records]
    analyse_folder(records)
    assert [(r.artist, r.album, r.title) for r in records] == before


# ── found on the isolated validation copy ─────────────────────────────────────

def test_a_featured_credit_with_a_dot_still_agrees_with_the_primary_artist():
    # Real case: tag "Andy Mineo", candidate "Social Club Misfits feat. Andy Mineo". "feat." must split the credit.
    from meloshelf.domain.metadata import credits_agree
    assert credits_agree('Andy Mineo', 'Social Club Misfits feat. Andy Mineo') is True
    assert credits_agree('Canon, MainMain', 'Canon ft. MainMain') is True


def test_one_acoustic_match_against_a_shared_folder_tag_needs_individual_review_not_batch_approval():
    # Real case: six tracks share "Tedashii/Steven Malcom" but only one is acoustically matched.
    records = [rec('CASS_-_Control.mp3', 'Tedashii/Steven Malcom', cand_artist='CASS feat. Parris Chariz')]
    records += [rec(f'Other_{i}.mp3', 'Tedashii/Steven Malcom', status='') for i in range(5)]
    groups = [g for g in analyse_folder(records).groups if g.field == 'artist']
    assert groups and all(g.individual_review for g in groups)


# ── track numbering (filename evidence only: tag track numbers are not indexed yet) ──

def test_duplicate_track_numbers_in_a_folder_are_a_partially_supported_finding_not_a_contamination():
    from meloshelf.domain.metadata import PARTIALLY_SUPPORTED
    records = [rec('03 Whom Shall I Fear.mp3', 'Chris Tomlin', cand_artist='Chris Tomlin', album='Burning Lights'),
               rec('03 Our God.mp3', 'Chris Tomlin', cand_artist='Chris Tomlin', album='Burning Lights'),
               rec('05 Thank You.mp3', 'Chris Tomlin', cand_artist='Chris Tomlin', album='Burning Lights')]
    report = analyse_folder(records)
    numbering = [a for a in report.assessments if a.field == 'track']
    duplicates = [a for a in numbering if a.original == '03']
    assert len(duplicates) == 2 and all(a.classification == PARTIALLY_SUPPORTED for a in duplicates)
    assert [a.classification for a in numbering if a.original == '05'] == ['RELIABLE']
    assert any('03' in reason for a in numbering for reason in a.reasons)
    assert all(a.classification != SUSPECTED_CONTAMINATED for a in report.assessments)
    assert not [g for g in report.groups if g.field == 'track']


def test_sequential_numbering_with_no_duplicates_is_reliable_and_unnumbered_files_are_unknown():
    from meloshelf.domain.metadata import RELIABLE
    records = [rec('01 A.mp3', 'X', cand_artist='X'), rec('02 B.mp3', 'X', cand_artist='X'),
               rec('Plain name.mp3', 'X', cand_artist='X')]
    by_path = {a.path.rsplit('/', 1)[-1]: a.classification for a in analyse_folder(records).assessments if a.field == 'track'}
    assert by_path['01 A.mp3'] == RELIABLE and by_path['02 B.mp3'] == RELIABLE
    assert by_path['Plain name.mp3'] == UNKNOWN
