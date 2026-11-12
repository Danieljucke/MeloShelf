"""HTTP-facing logic for organizing: preview, start, control, the organized library, review, resolve, open folder.

Every refusal carries a registered code. Folder actions are limited to the destination the person configured: a request
cannot ask MeloShelf to open or write anywhere else.
"""
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from meloshelf.application import error_codes as ec
from meloshelf.domain.outcomes import describe

_STATUS = {'organize_running': 409, 'folder_not_found': 400}


def open_in_explorer(path: str) -> None:
    if sys.platform == 'win32':
        os.startfile(path)  # noqa: S606  (the destination the person chose, opened in their own file manager)
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])


_PICKER = """
import tkinter, tkinter.filedialog
root = tkinter.Tk(); root.withdraw(); root.attributes('-topmost', True)
print(tkinter.filedialog.askdirectory(title=%r) or '', end='')
"""


def choose_folder_dialog(title: str) -> str:
    """The native chooser itself; run in a child process (see ``picker_command``). '' when cancelled."""
    import tkinter
    import tkinter.filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    return tkinter.filedialog.askdirectory(title=title) or ''


def picker_command(title: str) -> list:
    """Source: a tiny inline script. Packaged executable: ``sys.executable`` is the app itself, so it is asked to run only the
    chooser (``--pick-folder``) instead of being started again."""
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--pick-folder', title]
    return [sys.executable, '-c', _PICKER % title]


def pick_folder(kind: str) -> str:
    """A native folder chooser, run in its own process so it cannot disturb the server. '' when cancelled."""
    title = 'Choose the folder with your music' if kind == 'source' else 'Choose where the organized music goes'
    result = subprocess.run(picker_command(title), capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise OSError('the folder chooser could not open')
    return result.stdout.strip()


def _folders(payload: dict):
    source, dest = payload.get('source'), payload.get('dest')
    if not isinstance(source, str) or not isinstance(dest, str) or not source.strip() or not dest.strip():
        return None
    return source.strip(), dest.strip()


def get(server, route: str, query: dict) -> tuple[int, dict]:
    service = server.organizer
    if route == '/api/organize/status':
        status = service.status()
        if status.get('recognition_blocker'):
            outcome = describe(status['recognition_blocker'])
            status['blocker'] = {'code': status['recognition_blocker'], 'label': outcome.label, 'hint': outcome.hint}
        return 200, status
    if route == '/api/organize/review':
        return 200, {'items': review_items(server)}
    if route == '/api/organize/library':
        return 200, library(server)
    return 404, ec.body('not_found')


def post(server, path: str, payload: dict) -> tuple[int, dict]:
    service = server.organizer
    if path == '/api/organize/preview':
        folders = _folders(payload)
        if folders is None:
            return 400, ec.body('folder_not_found')
        result = service.preview(*folders, scan=bool(payload.get('scan')))
        return (200, result) if result['ok'] else (400, ec.body(result['code']))
    if path == '/api/organize/start':
        folders = _folders(payload)
        if folders is None:
            return 400, ec.body('folder_not_found')
        result = service.start(*folders, offline=bool(payload.get('offline')))
        if result['started']:
            return 202, {'started': True}
        return _STATUS.get(result['code'], 400), ec.body(result['code'])
    if path == '/api/organize/control':
        action = payload.get('action')
        if action not in ('pause', 'cancel'):
            return 400, ec.body('organize_action_invalid')
        service.pause() if action == 'pause' else service.cancel()
        return 200, {'ok': True}
    if path in ('/api/organize/consolidate/preview', '/api/organize/consolidate/apply', '/api/organize/consolidate/undo'):
        return consolidation(server, path, payload)
    if path == '/api/organize/resolve-many':
        items = payload.get('items')
        if not isinstance(items, list) or not items or len(items) > 500 or not all(isinstance(i, dict) for i in items):
            return 400, ec.body('items_required')
        clean = [{'path': str(i.get('path') or ''), 'title': str(i.get('title') or ''), 'artist': str(i.get('artist') or ''),
                  'album': str(i.get('album') or ''), 'overwrite': bool(i.get('overwrite'))} for i in items]
        if payload.get('dry_run'):
            return 200, service.validate_review(clean)
        result = service.start_review(clean)
        return (202, {'started': True}) if result['started'] else (409, ec.body(result['code']))
    if path == '/api/organize/resolve':
        try:
            service.resolve(str(payload.get('path') or ''), title=str(payload.get('title') or ''),
                            artist=str(payload.get('artist') or ''), album=str(payload.get('album') or ''))
        except ValueError as exc:
            return 400, ec.from_text(str(exc))
        except OSError:
            return 404, ec.body('file_missing')
        return 200, {'saved': True}
    if path == '/api/organize/open':
        dest = service.status().get('dest') or ''
        if not dest or not Path(dest).is_dir():
            return 409, ec.body('nothing_organized_yet')
        server.explorer_opener(dest)           # always the configured destination, never a path from the request
        return 200, {'opened': True}
    if path == '/api/organize/art/accept':
        album, artist = str(payload.get('album') or ''), str(payload.get('artist') or '')
        if not album or not artist or not server.album_art.accept(album, artist):
            return 404, ec.body('no_album_cover')
        return 200, {'accepted': True, 'covers_added': server.organizer.apply_covers()}
    if path == '/api/organize/pick':
        try:
            chosen = server.folder_picker(payload.get('kind') if payload.get('kind') in ('source', 'dest') else 'source')
            return 200, {'path': os.path.normpath(chosen) if chosen else ''}      # the dialog answers with forward slashes
        except (OSError, subprocess.SubprocessError):
            return 503, ec.body('folder_pick_unavailable')
    return 404, ec.body('not_found')


def review_items(server) -> list[dict]:
    dest = server.organizer.status().get('dest') or ''
    review = server.organizer.review_root(dest) if dest else ''
    if not review or not Path(review).is_dir():
        return []
    items = []
    for entry in server.organizer.decisions(review):
        decision = entry['decision']
        if decision.status != 'review':
            continue
        path = entry['row']['path']
        items.append({'path': path, 'name': entry['name'], 'reason_code': decision.reason_code, 'reason': decision.reason,
                      'candidates': [dict(c) for c in decision.candidates], 'size': entry['row']['size'],
                      'audio_url': '/api/audio?path=' + quote(path, safe='')})
    return items


def library(server) -> dict:
    status = server.organizer.status()
    dest = status.get('dest') or ''
    if not dest:
        return {'dest': '', 'artists': [], 'review_count': 0, 'organized_count': 0}
    known = server.organizer._organizer().known_outputs(dest)
    artists: dict[str, dict[str, list]] = {}
    root = Path(dest)
    review = organized = 0
    for entry in sorted(known.values(), key=lambda e: e['relative'].lower()):
        parts = entry['relative'].split('/')
        if entry['kind'] != 'organize' or len(parts) < 3:
            review += 1 if entry['kind'] == 'review' else 0
            continue
        organized += 1
        artists.setdefault(parts[0], {}).setdefault(parts[1], []).append({'file': parts[-1], 'relative': entry['relative']})
    return {'dest': dest, 'review_count': review, 'organized_count': organized,
            'artists': [{'name': name, 'albums': [{'name': album, 'tracks': tracks, 'art': album_art(server, root, name, album, tracks)}
                                                  for album, tracks in albums.items()]}
                        for name, albums in artists.items()]}


def album_art(server, root: Path, artist: str, album: str, tracks: list) -> dict:
    """How sure MeloShelf is about an album's cover. Never asks the catalogue: a listing must stay instant."""
    paths = [str(root / track['relative']) for track in tracks]
    found = server.album_art.lookup(album, artist, paths, network=False)
    out = {'state': found['state'], 'source': found.get('source', ''), 'embeddable': bool(found.get('embeddable', False))}
    if found.get('chosen'):
        out['chosen'] = True
    if found.get('attribution'):
        a = found['attribution']
        out.update(provenance=a['provider'], source_url=a['source_url'], terms_url=a['terms_url'], license=a['license'], author=a['author'])
    return out


def art_image(server, album: str, artist: str):
    """(content type, bytes) of the cover MeloShelf currently has for this album, or None.

    The files consulted are the ones MeloShelf itself recorded under this artist and album in the destination; no part
    of the request becomes a filesystem path."""
    if not album or not artist:
        return None
    dest = server.organizer.status().get('dest') or ''
    if not dest:
        return None
    root = Path(dest)
    prefix = f'{artist}/{album}/'
    known = server.organizer._organizer().known_outputs(dest)
    paths = [str(root / entry['relative']) for entry in known.values()
             if entry['kind'] == 'organize' and entry['relative'].startswith(prefix) and entry['relative'].count('/') == 2]
    found = server.album_art.lookup(album, artist, paths, network=False)
    if found['state'] == 'missing' or found.get('content_type') not in ('image/jpeg', 'image/png'):
        return None
    return found['content_type'], found['data']


def consolidation(server, path: str, payload: dict) -> tuple[int, dict]:
    """Merge collaboration folders into canonical artist folders. Preview changes nothing; apply needs ``confirm: true``."""
    service = server.organizer
    status = service.status()
    dest = status.get('dest') or ''
    if not dest or not Path(dest).is_dir():
        return 409, ec.body('nothing_organized_yet')
    if status.get('running') or status.get('phase') in ('scanning', 'recognizing', 'planning', 'artwork', 'moving'):
        return 409, ec.body('organize_running')
    if path.endswith('/preview'):
        return 200, service.consolidation_plan(dest, scan=bool(payload.get('scan')))
    if path.endswith('/undo'):
        run_id = str(payload.get('run_id') or '')
        try:
            return 200, service.undo_consolidation(run_id)
        except KeyError:
            return 404, ec.body('consolidation_run_unknown')
    if payload.get('confirm') is not True:
        return 400, ec.body('confirmation_required')
    return 200, service.consolidate(dest, confirm=True)
