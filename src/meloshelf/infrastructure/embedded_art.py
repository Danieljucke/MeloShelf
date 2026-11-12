"""Read the picture embedded in an audio file's own tags. Read-only: the file is never opened for writing.

Supports ID3 (MP3), FLAC/Ogg pictures and MP4 'covr'. A front-cover picture is preferred. The result is accepted only if
the bytes really are a JPEG or PNG within the size limit; the real type, not the declared one, is returned. This art
comes from the file's tags: it is not a verified release cover, and callers must not present it as one.
"""
import logging
from pathlib import Path

from meloshelf.domain.artwork import sniff_image

log = logging.getLogger(__name__)
MAX_EMBEDDED_BYTES = 2_000_000
_FRONT_COVER = 3


def _candidates(path: Path):
    """Yield (picture type, bytes) for every embedded picture, best guess first handled by the caller."""
    from mutagen import File as MutagenFile
    audio = MutagenFile(str(path))
    if audio is None and path.suffix.lower() == '.mp3':
        from mutagen.id3 import ID3
        tags = ID3(str(path))
        for frame in tags.getall('APIC'):
            yield frame.type, bytes(frame.data)
        return
    if audio is None:
        return
    tags = getattr(audio, 'tags', None)
    if tags is not None and hasattr(tags, 'getall'):                     # ID3
        for frame in tags.getall('APIC'):
            yield frame.type, bytes(frame.data)
    for picture in getattr(audio, 'pictures', None) or []:               # FLAC / Ogg FLAC
        yield picture.type, bytes(picture.data)
    if tags is not None and hasattr(tags, 'get'):                        # MP4
        try:
            for cover in tags.get('covr') or []:
                yield _FRONT_COVER, bytes(cover)
        except (TypeError, KeyError):
            pass


_JPEG_END = b'\xff\xd9'                     # end-of-image marker
_PNG_END = b'IEND\xaeB`\x82'                  # the IEND chunk, with its CRC, closes every PNG


def _complete(kind: str, data: bytes) -> bool:
    """Structural check beyond the signature: a truncated or damaged picture has no end marker. (Full decoding would
    need an image library; this refuses the common corruptions without one.)"""
    if kind == 'image/jpeg':
        return data.rstrip(b'\x00').endswith(_JPEG_END)
    if kind == 'image/png':
        return data.endswith(_PNG_END)
    return False


def read_embedded_art(path, *, max_bytes: int = MAX_EMBEDDED_BYTES):
    """(content_type, bytes) for the file's front-cover picture, or None."""
    path = Path(path)
    try:
        pictures = list(_candidates(path))
    except Exception as exc:   # a damaged or unsupported file simply has no usable picture
        log.debug('embedded art unreadable path=%s detail=%s', path, type(exc).__name__)
        return None
    pictures.sort(key=lambda p: p[0] != _FRONT_COVER)   # front cover first, otherwise the file's own order
    for _, data in pictures:
        kind = sniff_image(data)
        if kind is not None and len(data) <= max_bytes and _complete(kind, data):
            return kind, data
    return None
