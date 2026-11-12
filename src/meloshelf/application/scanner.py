"""Streaming, read-only filesystem scanner; symlinks are intentionally excluded."""
import os
from pathlib import Path
from collections.abc import Callable, Iterator, Iterable
from meloshelf.domain.models import Track, classify_track
from meloshelf.domain.track_numbers import parse_position
from meloshelf.infrastructure.tags import has_picture_frame, read_tags

SUPPORTED_EXTENSIONS = frozenset({'.mp3', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.aiff', '.aif'})


class LibraryScanner:
    def __init__(self, tag_reader: Callable[[Path], dict[str,str]] = read_tags):
        self._tag_reader = tag_reader
        self.errors: list[str] = []

    def iter_files(self, root: Path, *, excluded_paths: Iterable[Path] = (),
                   should_stop: Callable[[], bool] = lambda: False) -> Iterator[tuple[Path, os.stat_result]]:
        """Discover supported audio files without opening them."""
        root = Path(root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        excludes = [Path(p).expanduser().resolve() for p in excluded_paths]
        stack = [root]
        while stack:
            if should_stop():
                return
            directory = stack.pop()
            if any(directory == excluded or excluded in directory.parents for excluded in excludes):
                continue
            try:
                with os.scandir(directory) as entries:
                    children = sorted(entries, key=lambda e: e.name.casefold())
            except OSError as exc:
                self.errors.append(f'{directory}: {exc}')
                continue
            for entry in children:
                if should_stop():
                    return
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if os.path.splitext(entry.name)[1].lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    # The root is resolved and links are skipped, so entry.path is already canonical.
                    yield Path(entry.path), entry.stat(follow_symlinks=False)
                except OSError as exc:
                    self.errors.append(f'{entry.path}: {exc}')

    def read_track(self, path: Path, info: os.stat_result) -> Track:
        """Read one file's tags. Errors from the tag reader propagate to the caller."""
        tags = self._tag_reader(path)
        title = tags.get('title', '')
        artist = tags.get('artist', '')
        album = tags.get('album', '')
        track_no, track_total = parse_position(tags.get('tracknumber'))
        disc_no, disc_total = parse_position(tags.get('discnumber'))
        return Track(path=str(path), size=info.st_size, extension=path.suffix.lower(),
                     mtime_ns=info.st_mtime_ns, title=title, artist=artist, album=album,
                     year=tags.get('date', ''),
                     status=classify_track(title=title, artist=artist, album=album),
                     track_no=track_no, track_total=track_total, disc_no=disc_no, disc_total=disc_total,
                     numbers_indexed=True, has_picture=has_picture_frame(path))

    def iter_tracks(self, root: Path, *, excluded_paths: Iterable[Path] = (),
                    should_stop: Callable[[], bool] = lambda: False) -> Iterator[Track]:
        for path, info in self.iter_files(root, excluded_paths=excluded_paths, should_stop=should_stop):
            try:
                yield self.read_track(path, info)
            except OSError as exc:
                self.errors.append(f'{path}: {exc}')
