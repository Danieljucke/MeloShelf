"""Streaming, read-only filesystem scanner; symlinks are intentionally excluded."""
import os
from pathlib import Path
from collections.abc import Callable, Iterator, Iterable
from meloshelf.domain.models import Track, classify_track
from meloshelf.infrastructure.tags import read_tags

SUPPORTED_EXTENSIONS = frozenset({'.mp3', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.aiff', '.aif'})


class LibraryScanner:
    def __init__(self, tag_reader: Callable[[Path], dict[str,str]] = read_tags):
        self._tag_reader = tag_reader
        self.errors: list[str] = []

    def iter_tracks(self, root: Path, *, excluded_paths: Iterable[Path] = (),
                    should_stop: Callable[[], bool] = lambda: False) -> Iterator[Track]:
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
                    path = Path(entry.path)
                    extension = path.suffix.lower()
                    if extension not in SUPPORTED_EXTENSIONS:
                        continue
                    info = entry.stat(follow_symlinks=False)
                    tags = self._tag_reader(path)
                    title = tags.get('title', '')
                    artist = tags.get('artist', '')
                    album = tags.get('album', '')
                    yield Track(path=str(path.resolve()), size=info.st_size,
                                extension=extension, mtime_ns=info.st_mtime_ns,
                                title=title, artist=artist, album=album,
                                year=tags.get('date', ''),
                                status=classify_track(title=title, artist=artist, album=album))
                except OSError as exc:
                    self.errors.append(f'{entry.path}: {exc}')
