"""MeloShelf local-first desktop GUI (browser or optional Qt WebEngine shell)."""
import argparse
import logging
from pathlib import Path
import sqlite3
import sys
import threading
import webbrowser

from meloshelf.application.webapp import create_server

STARTUP_NOTICE = 'Files are moved only after you confirm; each one is verified at its destination before its original is removed.'


def launch_native(url: str):
    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView
    app=QApplication(sys.argv)
    app.setApplicationName('MeloShelf')
    window=QWebEngineView()
    window.setWindowTitle('MeloShelf — Music library studio')
    window.resize(1400,880)
    window.load(QUrl(url))
    window.show()
    return app.exec()


def main(argv=None):
    parser=argparse.ArgumentParser(description='MeloShelf — local-first music organizer')
    parser.add_argument('--desktop', action='store_true',help='Use optional Qt WebEngine native desktop window')
    parser.add_argument('--no-browser',action='store_true',help='Do not automatically open a browser')
    parser.add_argument('--port',type=int,default=0,help='Loopback port; default random')
    parser.add_argument('--db',type=Path,default=None,help='Alternative local SQLite library path')
    args=parser.parse_args(argv)
    if args.port<0 or args.port>65535:parser.error('Invalid port')
    logging.basicConfig(level=logging.INFO,stream=sys.stderr,
                        format='time=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s')
    try:server=create_server(db_path=args.db,port=args.port)
    except sqlite3.Error as exc:
        print(f'MeloShelf cannot open its library database: {exc}',file=sys.stderr)
        return 2
    url=f'http://127.0.0.1:{server.server_address[1]}/'
    print(f'MeloShelf 0.2.1 · local interface: {url}',flush=True)
    print(STARTUP_NOTICE,flush=True)
    from meloshelf.application.config import startup_diagnostics
    for line in startup_diagnostics(server.config):
        print(line,flush=True)
    if args.desktop:
        t=threading.Thread(target=server.serve_forever,daemon=True)
        t.start()
        try:
            return launch_native(url)
        finally:
            server.shutdown();server.server_close();t.join(timeout=3)
    if not args.no_browser:webbrowser.open(url)
    try:server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:pass
    finally:server.server_close()
    return 0

if __name__=='__main__':sys.exit(main())
