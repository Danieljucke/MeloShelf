"""Read file tags without changing content; corrupted files remain inspectable."""
from pathlib import Path


def read_tags(path: Path) -> dict[str, str]:
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path), easy=True)
        if audio is None or not getattr(audio, 'tags', None):
            return {}
        result = {}
        for tag in ('title', 'artist', 'album', 'date', 'tracknumber', 'discnumber'):
            values = audio.tags.get(tag, [])
            if values:
                result[tag] = str(values[0]).strip()
        return result
    except (OSError, ValueError, TypeError, Exception):
        # Read-only inspection must tolerate malformed and unsupported files.
        return {}


def has_picture_frame(path: Path) -> bool:
    """True when the file's own tags contain a picture entry (ID3 APIC, FLAC/Ogg picture or MP4 cover).

    Only the tag structure is inspected, never the image bytes, so this is cheap enough to run while scanning. It says
    nothing about whether the picture is usable: a picture that is corrupt or in an unsupported format still counts as
    present here, and the thumbnail endpoint records it as absent the first time it cannot serve it. Read-only."""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path))
    except Exception:
        return False
    if audio is None:
        return False
    tags = getattr(audio, 'tags', None)
    if tags is not None and hasattr(tags, 'getall') and tags.getall('APIC'):
        return True
    if getattr(audio, 'pictures', None):
        return True
    if tags is not None and hasattr(tags, 'get'):
        try:
            return bool(tags.get('covr'))
        except (TypeError, KeyError):
            return False
    return False
