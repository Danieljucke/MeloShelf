"""Read file tags without changing content; corrupted files remain inspectable."""
from pathlib import Path


def read_tags(path: Path) -> dict[str, str]:
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path), easy=True)
        if audio is None or not getattr(audio, 'tags', None):
            return {}
        result = {}
        for tag in ('title', 'artist', 'album', 'date'):
            values = audio.tags.get(tag, [])
            if values:
                result[tag] = str(values[0]).strip()
        return result
    except (OSError, ValueError, TypeError, Exception):
        # Read-only inspection must tolerate malformed and unsupported files.
        return {}
