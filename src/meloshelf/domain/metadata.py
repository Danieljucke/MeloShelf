"""Metadata reliability: field-level classification and folder-level contamination patterns.

Pure rules, no I/O. The analysis never declares a tag wrong on its own:

* Suspicion needs independent evidence: acoustically matched recordings whose identity contradicts the tag.
  Filename evidence is recorded as a source but cannot decide anything.
* A tag shared across a folder is a pattern only when the recordings contradict it consistently. A shared
  credit that the recordings confirm, or a compilation ("Various Artists", album "Compilation"), is never
  flagged for being shared.
* A replacement is proposed only when the evidence agrees on it. When the recordings disagree among
  themselves, no replacement is proposed and the group is marked for individual review.
* Original tags are not modified. Classification and proposals are advisory.

Reliability is separate from recording identification confidence.
"""
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

RELIABLE = 'RELIABLE'
PARTIALLY_SUPPORTED = 'PARTIALLY_SUPPORTED'
CONFLICTING = 'CONFLICTING'
MISSING = 'MISSING'
SUSPECTED_CONTAMINATED = 'SUSPECTED_CONTAMINATED'
UNKNOWN = 'UNKNOWN'
CLASSIFICATIONS = (RELIABLE, PARTIALLY_SUPPORTED, CONFLICTING, MISSING, SUSPECTED_CONTAMINATED, UNKNOWN)

SHARED_MIN = 3            # a tag on fewer files is not treated as a folder pattern
CONTRADICTION_SHARE = 0.6  # share of acoustically matched recordings that must contradict a shared tag
ACOUSTIC = 'ACOUSTICALLY_MATCHED'
_LEADING_NUMBER = re.compile(r'^\s*(\d{1,3})(?=[\s._-])')
_GENERIC_TITLE = re.compile(r'^(track\s*\d*|\d+|untitled|unknown|audio|title)$')
_SPLIT = re.compile(r',|&|/|\b(?:feat|ft|with|and|x|vs)\b\.?', re.I)


@dataclass(frozen=True)
class MetaRecord:
    path: str
    title: str = ''
    artist: str = ''
    album: str = ''
    candidate_artist: str = ''
    candidate_title: str = ''
    candidate_album: str = ''
    candidate_status: str = ''
    filename: str = ''


@dataclass(frozen=True)
class FieldAssessment:
    path: str
    field: str
    original: str
    proposed: str
    classification: str
    confidence: float
    reasons: tuple = ()
    evidence_sources: tuple = ()
    needs_review: bool = False  # a shared tag with too little acoustic evidence to decide in batch


@dataclass(frozen=True)
class CorrectionGroup:
    field: str
    original: str
    proposed: str
    classification: str
    paths: tuple
    confidence: float
    individual_review: bool
    reasons: tuple = ()


@dataclass
class FolderReport:
    assessments: list = field(default_factory=list)
    groups: list = field(default_factory=list)


def canonical_text(value: str) -> str:
    """Matching form: compatibility-normalised, accents removed, case folded, spacing collapsed."""
    decomposed = unicodedata.normalize('NFKD', value or '')
    stripped = ''.join(ch for ch in decomposed if not unicodedata.combining(ch))
    return ' '.join(stripped.casefold().split())


def _parts(value: str) -> frozenset:
    return frozenset(p for p in (canonical_text(x) for x in _SPLIT.split(value or '')) if p)


def credits_agree(tag: str, candidate: str) -> bool:
    """Collaboration-aware: 'Canon, MainMain' agrees with 'Canon feat. MainMain'; unrelated names do not."""
    a, b = _parts(tag), _parts(candidate)
    return bool(a and b and a & b)


def _is_compilation(record: MetaRecord) -> bool:
    album = canonical_text(record.album)
    artist = canonical_text(record.artist)
    return 'compilation' in album or 'various' in artist


def _classify_artist(record, folder_disagreement, folder_group_size):
    tag, candidate = record.artist, record.candidate_artist
    acoustic = record.candidate_status == ACOUSTIC and bool(candidate)
    sources = ['tags']
    if acoustic:
        sources.append('acoustic recording (candidate artist)')
    if record.filename:
        sources.append('filename (never decisive alone)')
    if not canonical_text(tag):
        return MISSING, '', 0.9, ('no artist tag',), tuple(sources)
    if _is_compilation(record):
        return PARTIALLY_SUPPORTED, '', 0.5, ('compilation or various-artists credit: not flagged for being shared',), tuple(sources)
    if acoustic and credits_agree(tag, candidate):
        return RELIABLE, '', 0.9, ('the acoustically matched recording confirms this credit',), tuple(sources)
    if folder_group_size >= SHARED_MIN:
        acoustic_group, disagreeing = folder_disagreement
        if acoustic_group >= 2 and disagreeing / acoustic_group >= CONTRADICTION_SHARE and acoustic and not credits_agree(tag, candidate):
            return (SUSPECTED_CONTAMINATED, '', min(0.95, 0.5 + disagreeing / acoustic_group / 2),
                    (f'{folder_group_size} recordings share this artist tag and {disagreeing} of {acoustic_group} '
                     f'acoustically matched recordings name someone else',), tuple(sources))
        if acoustic_group >= 2 and disagreeing == 0:
            return RELIABLE, '', 0.8, (f'{folder_group_size} recordings share this credit and the matched ones confirm it',), tuple(sources)
    if acoustic:
        return (CONFLICTING, candidate, 0.7,
                ('the acoustically matched recording names a different artist',), tuple(sources))
    return UNKNOWN, '', 0.0, ('no acoustic evidence for this artist',), tuple(sources)


def _artist_key(record):
    return canonical_text(record.artist)


def analyse_folder(records) -> FolderReport:
    records = list(records)
    report = FolderReport()
    groups = defaultdict(list)
    for r in records:
        groups[_artist_key(r)].append(r)

    # Per shared artist: how many acoustically matched recordings contradict it.
    disagreement = {}
    for key, members in groups.items():
        acoustic = [m for m in members if m.candidate_status == ACOUSTIC and m.candidate_artist]
        contradicting = sum(1 for m in acoustic if not credits_agree(m.artist, m.candidate_artist))
        disagreement[key] = (len(acoustic), contradicting)

    # Unicode/case forms inside one canonical key: the most common written form is proposed.
    form_counts = {key: Counter(m.artist for m in members) for key, members in groups.items()}

    for r in records:
        key = _artist_key(r)
        members = groups[key]
        classification, proposed, confidence, reasons, sources = _classify_artist(
            r, disagreement[key], len(members))
        forms = form_counts[key]
        if classification == RELIABLE and len(forms) > 1 and r.artist != _canonical_form(forms):
            classification, proposed = CONFLICTING, _canonical_form(forms)
            confidence, reasons = 0.6, (f'written inconsistently: {len(forms)} forms of this name in the folder',)
        shared, acoustic_count = len(members) >= SHARED_MIN, disagreement[key][0]
        weak = shared and acoustic_count < 2 and classification != RELIABLE
        if weak:
            reasons = list(reasons) + [f'{len(members)} tracks in this folder carry this tag; '
                                       f'{acoustic_count} of them are acoustically matched']
        report.assessments.append(FieldAssessment(r.path, 'artist', r.artist, proposed, classification,
                                                  confidence, tuple(reasons), sources, needs_review=weak))
        report.assessments.append(_classify_title(r))
        report.assessments.append(_classify_album(r, records))
        report.assessments.append(_classify_track(r, records))

    report.groups = _group(report.assessments)
    return report


def _canonical_form(forms: Counter) -> str:
    """Most common written form; ties prefer the NFC form, then the lexically first, for stable results."""
    best = max(forms.values())
    tied = sorted(f for f, n in forms.items() if n == best)
    nfc = [f for f in tied if unicodedata.is_normalized('NFC', f)]
    return (nfc or tied)[0]


def _classify_title(r) -> FieldAssessment:
    title = (r.title or '').strip()
    if not title or _GENERIC_TITLE.match(canonical_text(title)):
        return FieldAssessment(r.path, 'title', r.title, '', MISSING, 0.9, ('the title is missing or generic',), ('tags',))
    return FieldAssessment(r.path, 'title', r.title, '', RELIABLE, 0.7, (), ('tags',))


def _classify_track(r, records) -> FieldAssessment:
    """Numbering from the filename only: tag track numbers are not indexed yet. A duplicate is a finding to review,
    never proof of anything else, so the most it can say is partially supported."""
    name = r.filename or r.path.replace('\\', '/').rsplit('/', 1)[-1]
    match = _LEADING_NUMBER.match(name)
    if not match:
        return FieldAssessment(r.path, 'track', '', '', UNKNOWN, 0.0, ('no track number in the filename',), ('filename',))
    number = match.group(1)
    same = sum(1 for x in records if _track_number(x) == number)
    if same > 1:
        return FieldAssessment(r.path, 'track', number, '', PARTIALLY_SUPPORTED, 0.5,
                               (f'track number {number} appears on {same} files in this folder',), ('filename',))
    return FieldAssessment(r.path, 'track', number, '', RELIABLE, 0.6, ('the filename number is unique in this folder',), ('filename',))


def _track_number(record):
    match = _LEADING_NUMBER.match(record.filename or record.path.replace('\\', '/').rsplit('/', 1)[-1])
    return match.group(1) if match else None


def _classify_album(r, records) -> FieldAssessment:
    album = (r.album or '').strip()
    if not album:
        return FieldAssessment(r.path, 'album', r.album, '', MISSING, 0.5, ('no album tag',), ('tags',))
    if 'compilation' in canonical_text(album):
        return FieldAssessment(r.path, 'album', r.album, '', PARTIALLY_SUPPORTED, 0.5,
                               ('compilation album: recordings may legitimately come from several artists',), ('tags',))
    same = [x for x in records if canonical_text(x.album) == canonical_text(album)]
    if len(same) >= SHARED_MIN:
        artists = {canonical_text(x.candidate_artist) for x in same if x.candidate_status == ACOUSTIC and x.candidate_artist}
        if len(artists) >= 2:
            return FieldAssessment(r.path, 'album', r.album, '', PARTIALLY_SUPPORTED, 0.4,
                                   (f'{len(same)} recordings share this album across {len(artists)} recording artists: '
                                    'it may be a compilation or a download folder name',),
                                   ('tags', 'acoustic recordings (candidate artists)'))
    return FieldAssessment(r.path, 'album', r.album, '', RELIABLE, 0.6, (), ('tags',))


def _group(assessments) -> list:
    buckets = defaultdict(list)
    for a in assessments:
        if a.classification in (SUSPECTED_CONTAMINATED, CONFLICTING) and a.original != a.proposed:
            buckets[(a.field, a.original, a.classification)].append(a)
    groups = []
    for (fld, original, classification), members in buckets.items():
        proposals = Counter(m.proposed for m in members)
        proposed = proposals.most_common(1)[0][0] if len(proposals) == 1 and members[0].proposed else ''
        confidence = round(sum(m.confidence for m in members) / len(members), 3)
        individual = (proposed == '' or len(proposals) > 1 or classification == SUSPECTED_CONTAMINATED
                      or any(m.needs_review for m in members))
        reasons = tuple(dict.fromkeys(reason for m in members for reason in m.reasons))
        groups.append(CorrectionGroup(fld, original, proposed, classification,
                                      tuple(m.path for m in members), confidence, individual, reasons))
    return groups
