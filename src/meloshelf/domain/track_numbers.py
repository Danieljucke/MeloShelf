"""Track and disc positions from tag text ("3", "03/12", "2 of 3"). Strict: anything that is not a plain position is
ignored rather than guessed, so a bad tag can never put a track in the wrong place on an album."""
import re

_POSITION = re.compile(r'\s*(\d{1,3})?\s*(?:(?:/|of)\s*(\d{1,3})?)?\s*', re.ASCII | re.IGNORECASE)
LIMIT = 999


def parse_position(text) -> tuple[int | None, int | None]:
    """(number, total) from the text, or (None, None). A total smaller than the number is dropped; a zero is unusable."""
    if not isinstance(text, str) or len(text) > 20:
        return None, None
    match = _POSITION.fullmatch(text)
    if match is None or not any(g is not None for g in match.groups()):
        return None, None
    number, total = (int(g) if g is not None else None for g in match.groups())
    if number == 0 or total == 0:
        return None, None
    if number is not None and total is not None and total < number:
        total = None
    return number, total
