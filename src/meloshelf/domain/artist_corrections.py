"""Artist correction rules: a pure replay of an append-only log.

* merge(source -> target): the variant resolves to the target name. The latest active merge wins.
* keep_separate(a, b): neither name may be merged into the other. It blocks merges; it does not move tags.
* undo(ref): reverses one earlier merge or keep_separate. An undo cannot itself be undone, and an event can be
  undone once. Nothing is deleted from the log.

Keys come from canonical_key(), so case, accents and punctuation never create a separate correction.
"""
from dataclasses import dataclass, field

from meloshelf.domain.artists import canonical_key

MERGE, SPLIT, UNDO = 'merge', 'split', 'undo'
ACTIONS = (MERGE, SPLIT, UNDO)


class ArtistCorrectionError(ValueError):
    """A correction was refused; the message is safe to show to the user."""


@dataclass
class ArtistState:
    merges: dict = field(default_factory=dict)      # source key -> (target key, target name, event id)
    separated: set = field(default_factory=set)     # frozenset({key_a, key_b})

    def resolve(self, name: str) -> str | None:
        """The corrected display name for this variant, or None when no correction applies."""
        entry = self.merges.get(canonical_key(name))
        return entry[1] if entry else None

    def is_separated(self, a: str, b: str) -> bool:
        return frozenset({canonical_key(a), canonical_key(b)}) in self.separated


def check_merge(source: str, target: str, state: ArtistState) -> tuple[str, str]:
    """Validate a merge request and return the cleaned (source, target) names."""
    source = ' '.join((source or '').split())
    target = ' '.join((target or '').split())
    if not canonical_key(source) or not canonical_key(target):
        raise ArtistCorrectionError('Both artist names are needed to merge them.')
    if canonical_key(source) == canonical_key(target):
        raise ArtistCorrectionError('These names are already the same artist.')
    if state.is_separated(source, target):
        raise ArtistCorrectionError('These artists were kept separate. Undo that decision before merging them.')
    return source, target


def replay(events: list[dict]) -> ArtistState:
    """Build the current state from the full log. Refuses a log whose undo points at nothing valid."""
    by_id = {e['id']: e for e in events}
    undone: set[int] = set()
    for event in events:
        if event['action'] != UNDO:
            continue
        ref = by_id.get(event.get('ref'))
        if ref is None or ref['action'] == UNDO or event['ref'] >= event['id']:
            raise ArtistCorrectionError('An undo refers to a decision that does not exist.')
        if event['ref'] in undone:
            raise ArtistCorrectionError('A decision was undone twice.')
        undone.add(event['ref'])
    state = ArtistState()
    for event in events:
        if event['action'] == UNDO or event['id'] in undone:
            continue
        if event['action'] == MERGE:
            state.merges[event['source_key']] = (event['target_key'], event['target_name'], event['id'])
        elif event['action'] == SPLIT:
            state.separated.add(frozenset({event['source_key'], event['target_key']}))
    return state
