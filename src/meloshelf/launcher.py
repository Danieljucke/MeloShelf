"""Entry point of the packaged Windows application (no console window).

* ``--pick-folder TITLE`` runs only the native folder chooser and prints the choice (the server starts it as a child process).
* Otherwise: start the local server, open the interface in the default browser and show a small control window with
  *Open MeloShelf* and *Quit*. A second launch opens the already-running instance instead of starting another.
* Without a console, ``sys.stdout`` and ``sys.stderr`` can be ``None``; they are redirected to a log file first.
"""
import json
import logging
import logging.handlers
import os
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path


def data_dir(environ=None) -> Path:
    """Where the launcher keeps its log and instance file: outside the installation folder, per user."""
    env = os.environ if environ is None else environ
    base = env.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
    return Path(base) / 'MeloShelf'


def running_instance(state_file: Path, probe=None):
    """The URL of an instance that is already serving, or ''. ``probe(url)`` says whether the address answers."""
    try:
        url = json.loads(Path(state_file).read_text(encoding='utf-8')).get('url', '')
    except (OSError, ValueError, AttributeError):
        return ''
    if not isinstance(url, str) or not url.startswith('http://127.0.0.1:'):
        return ''
    probe = probe or _answers
    return url if probe(url) else ''


def _answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            return response.status == 200 and b'MeloShelf' in response.read(65536)
    except (OSError, ValueError):
        return False


def _prepare_streams(folder: Path):
    """A windowed program has no stdout/stderr; give the application's prints and logging a rotating file."""
    folder.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(folder / 'meloshelf.log', maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('time=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s'))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    if sys.stdout is None or sys.stderr is None:
        stream = open(folder / 'meloshelf-console.log', 'a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream


def _control_window(server, url: str) -> None:
    import tkinter
    from tkinter import messagebox
    root = tkinter.Tk()
    root.title('MeloShelf')
    root.resizable(False, False)
    tkinter.Label(root, text='MeloShelf is running on this computer.', padx=18, pady=10).pack()
    tkinter.Label(root, text=url, fg='#555555').pack()

    def quit_app():
        running = False
        try:
            running = bool(server.organizer.status().get('running'))
        except Exception:                                          # noqa: BLE001 - never block quitting on a status error
            pass
        if running and not messagebox.askyesno('MeloShelf', 'Files are being moved right now.\n\nIt is safe to stop: '
                                               'MeloShelf will offer to resume next time.\n\nQuit anyway?'):
            return
        root.destroy()

    row = tkinter.Frame(root, pady=12)
    row.pack()
    tkinter.Button(row, text='Open MeloShelf', width=16, command=lambda: webbrowser.open(url)).pack(side='left', padx=6)
    tkinter.Button(row, text='Quit', width=10, command=quit_app).pack(side='left', padx=6)
    root.protocol('WM_DELETE_WINDOW', quit_app)
    root.mainloop()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['--pick-folder']:
        from meloshelf.application.organize_api import choose_folder_dialog
        print(choose_folder_dialog(argv[1] if len(argv) > 1 else 'Choose a folder'), end='')
        return 0
    folder = data_dir()
    _prepare_streams(folder)
    state = folder / 'instance.json'
    existing = running_instance(state)
    if existing:
        webbrowser.open(existing)
        return 0
    from meloshelf.__main__ import STARTUP_NOTICE
    from meloshelf.application.config import startup_diagnostics
    from meloshelf.application.webapp import create_server
    try:
        server = create_server(port=0)
    except Exception as exc:                                       # noqa: BLE001
        logging.getLogger('meloshelf.launcher').error('cannot open the library database: %s', type(exc).__name__)
        _alert('MeloShelf could not open its library database. See ' + str(folder / 'meloshelf.log'))
        return 2
    url = f'http://127.0.0.1:{server.server_address[1]}/'
    state.write_text(json.dumps({'url': url, 'pid': os.getpid()}), encoding='utf-8')
    print('MeloShelf 0.2.1 - local interface:', url, flush=True)
    print(STARTUP_NOTICE, flush=True)
    for line in startup_diagnostics(server.config):
        print(line, flush=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .25}, daemon=True)
    thread.start()
    webbrowser.open(url)
    try:
        _control_window(server, url)
    except Exception:                                              # noqa: BLE001 - no window available: keep serving until interrupted
        logging.getLogger('meloshelf.launcher').exception('control window unavailable')
        thread.join()
    finally:
        server.shutdown()
        server.server_close()
        try:
            state.unlink()
        except OSError:
            pass
    return 0


def _alert(message: str) -> None:
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror('MeloShelf', message)
        root.destroy()
    except Exception:                                              # noqa: BLE001
        pass


if __name__ == '__main__':
    sys.exit(main())
