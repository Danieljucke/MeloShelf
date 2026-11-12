"""Writes metadata into an organized COPY. It is given a path in the destination and never sees the source.

Only the fields asked for are set, and only when they have a value; every other tag in the file is kept. A cover is
embedded only when the copy has no picture of its own. Any failure returns False: the engine then keeps the verified
copy and records that its tags were not written, instead of losing the file.
"""
import logging
from pathlib import Path

from meloshelf.infrastructure.tags import has_picture_frame

log = logging.getLogger(__name__)
FIELDS = ('title', 'artist', 'album', 'albumartist', 'tracknumber', 'discnumber', 'date')


def _image_kind(data: bytes) -> str | None:
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    return None


def _embed(path: str, cover_path: str) -> None:
    data = Path(cover_path).read_bytes()
    kind = _image_kind(data)
    if kind is None or has_picture_frame(Path(path)):
        return
    suffix = Path(path).suffix.lower()
    if suffix == '.mp3':
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(APIC(encoding=3, mime=kind, type=3, desc='Cover', data=data))
        tags.save(path)
    elif suffix == '.flac':
        from mutagen.flac import FLAC, Picture
        audio = FLAC(path)
        picture = Picture()
        picture.type, picture.mime, picture.data = 3, kind, data
        audio.add_picture(picture)
        audio.save()
    elif suffix in ('.m4a', '.mp4'):
        from mutagen.mp4 import MP4, MP4Cover
        audio = MP4(path)
        audio['covr'] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG if kind == 'image/jpeg' else MP4Cover.FORMAT_PNG)]
        audio.save()


def write_tags(path: str, tags: dict, cover_path: str = '') -> bool:
    try:
        wanted = {key: str(value).strip() for key, value in (tags or {}).items() if key in FIELDS and str(value or '').strip()}
        if wanted:
            if Path(path).suffix.lower() == '.mp3':
                from mutagen.easyid3 import EasyID3
                from mutagen.id3 import ID3NoHeaderError
                try:
                    audio = EasyID3(path)
                except ID3NoHeaderError:
                    audio = EasyID3()
                for key, value in wanted.items():
                    audio[key] = value
                audio.save(path)
            else:
                from mutagen import File
                audio = File(path, easy=True)
                if audio is None:
                    return False
                if audio.tags is None:
                    audio.add_tags()
                for key, value in wanted.items():
                    audio[key] = value
                audio.save()
        if cover_path:
            _embed(path, cover_path)
        return True
    except Exception as exc:                  # tagging must never lose the copy: report and let the engine record it
        log.info('tags not written (%s)', type(exc).__name__)
        return False


def read_cover(path: str):
    """(content_type, bytes) of the picture a file carries, or None. Reading only; never writes.
    Only real JPEG or PNG bytes within the size limit are returned, with their real type."""
    from meloshelf.infrastructure.embedded_art import read_embedded_art
    return read_embedded_art(path)
