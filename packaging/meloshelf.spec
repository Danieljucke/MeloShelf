# PyInstaller spec: a one-folder, windowed (no console) Windows application.
# Build with packaging/build.ps1. Paths are relative to this file.
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

web = '../src/meloshelf/presentation/web'
datas = [(web + '/index.html', 'meloshelf/presentation/web'),
         (web + '/brand', 'meloshelf/presentation/web/brand'),
         (web + '/i18n', 'meloshelf/presentation/web/i18n')]
datas += [('../THIRD_PARTY_NOTICES.md', 'licenses'), ('../LICENSE', 'licenses')]
datas += copy_metadata('keyring') + copy_metadata('mutagen')
hidden = collect_submodules('keyring.backends') + ['win32ctypes.pywin32.pywintypes', 'win32ctypes.pywin32.win32cred']

a = Analysis(['../src/meloshelf/launcher.py'], pathex=['../src'], datas=datas, hiddenimports=hidden,
             excludes=['pytest', 'PySide6'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='MeloShelf', console=False, icon='MeloShelf.ico', version='version_info.txt')
coll = COLLECT(exe, a.binaries, a.datas, name='MeloShelf')
