"""Optional read-only command-line smoke test (does not require Qt)."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from meloshelf.application.scanner import LibraryScanner
from meloshelf.infrastructure.repository import LibraryRepository


def main():
    parser = argparse.ArgumentParser(description='Inspect audio files locally; never modify originals')
    parser.add_argument('root', type=Path, help='Folder with audio files')
    parser.add_argument('--db', type=Path, default=Path.home() / '.meloshelf' / 'library.sqlite3')
    parser.add_argument('--exclude', type=Path, action='append', default=[])
    args = parser.parse_args()
    repo = LibraryRepository(args.db)
    scanner = LibraryScanner()
    batch = []
    total = 0
    try:
        for track in scanner.iter_tracks(args.root, excluded_paths=args.exclude):
            batch.append(track)
            total += 1
            if len(batch) == 250:
                repo.save_many(batch)
                batch.clear()
                if total % 1000 == 0:
                    print(f'Inspected {total:,} audio files')
        if batch:
            repo.save_many(batch)
        print(f'Inspected in this run: {total:,}')
        print('Database totals:', repo.counts())
        if scanner.errors:
            print(f'Access errors: {len(scanner.errors)}', file=sys.stderr)
            return 2
        return 0
    finally:
        repo.close()


if __name__ == '__main__':
    raise SystemExit(main())
