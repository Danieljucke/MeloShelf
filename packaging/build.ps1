# Builds the standalone Windows application and its installer in an isolated environment outside the repository.
# Usage (from the repository root):  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$build = if ($env:MELOSHELF_BUILD_DIR) { $env:MELOSHELF_BUILD_DIR } else { 'F:\_meloshelf_build' }
$venv = Join-Path $build 'venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) { python -m venv $venv }
& "$venv\Scripts\python.exe" -m pip install --quiet pyinstaller "mutagen>=1.47,<2" "keyring>=25,<26"
Push-Location $root
try {
  & "$venv\Scripts\python.exe" -m PyInstaller packaging\meloshelf.spec --noconfirm --distpath "$build\dist" --workpath "$build\work"
  $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
  if (Test-Path $iscc) {
    & $iscc "/DDistDir=$build\dist\MeloShelf" "/DOutDir=$build\installer" packaging\installer.iss
  } else { Write-Warning 'Inno Setup 6 not found: only the portable folder was built.' }
} finally { Pop-Location }
Write-Host "Portable folder: $build\dist\MeloShelf"
Write-Host "Installer:       $build\installer"
