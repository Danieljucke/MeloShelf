; Inno Setup 6 script. Per-user install (no administrator rights); the library and settings live outside the
; installation folder (%USERPROFILE%\.meloshelf and %LOCALAPPDATA%\MeloShelf) and are kept on uninstall.
; Compile with packaging/build.ps1 (expects the PyInstaller output in DistDir).
#ifndef DistDir
  #define DistDir "..\dist\MeloShelf"
#endif
#ifndef OutDir
  #define OutDir "..\installer"
#endif
#define AppVersion "0.2.1"

[Setup]
AppId={{6B1F0B7E-5C0A-4C8B-9F38-6D4E0A7A3E21}
AppName=MeloShelf
AppVersion={#AppVersion}
AppPublisher=MeloShelf
AppCopyright=Copyright (C) 2026 Joy Daniel. GPL-2.0-or-later.
LicenseFile=..\LICENSE
DefaultDirName={autopf}\MeloShelf
DefaultGroupName=MeloShelf
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutDir}
OutputBaseFilename=MeloShelf-{#AppVersion}-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\MeloShelf.exe
SetupIconFile=MeloShelf.ico
VersionInfoVersion=0.2.1.0
VersionInfoProductName=MeloShelf
DisableProgramGroupPage=yes
WizardStyle=modern

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\MeloShelf"; Filename: "{app}\MeloShelf.exe"
Name: "{autodesktop}\MeloShelf"; Filename: "{app}\MeloShelf.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\MeloShelf.exe"; Description: "Start MeloShelf"; Flags: nowait postinstall skipifsilent

[Messages]
FinishedLabel=MeloShelf is installed. Your music library data is kept in your user profile, not in the installation folder, so uninstalling never touches your music or your library.%n%nTo recognise songs from their sound, MeloShelf uses Chromaprint (fpcalc), which is not included. See the README for where to get it.
