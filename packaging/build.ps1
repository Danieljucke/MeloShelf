# Builds the standalone Windows application and its installer in an isolated environment outside the repository.
# Usage (from the repository root):  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# Any failing step stops the script with a non-zero exit code.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$build = if ($env:MELOSHELF_BUILD_DIR) { $env:MELOSHELF_BUILD_DIR } else { 'F:\_meloshelf_build' }
$venv = Join-Path $build 'venv'

function Invoke-Step([string]$name, [scriptblock]$step) {
  & $step
  if ($LASTEXITCODE -ne 0) { throw "$name failed with exit code $LASTEXITCODE" }
}

try {
  if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) { Invoke-Step 'Creating the build environment' { python -m venv $venv } }
  Invoke-Step 'Installing build dependencies' { & "$venv\Scripts\python.exe" -m pip install --quiet pyinstaller "mutagen>=1.47,<2" "keyring>=25,<26" }
  Push-Location $root
  try {
    Invoke-Step 'PyInstaller' { & "$venv\Scripts\python.exe" -m PyInstaller packaging\meloshelf.spec --noconfirm --distpath "$build\dist" --workpath "$build\work" }
    if (-not (Test-Path "$build\dist\MeloShelf\MeloShelf.exe")) { throw 'PyInstaller did not produce MeloShelf.exe' }
    $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (Test-Path $iscc) {
      Invoke-Step 'Inno Setup' { & $iscc "/DDistDir=$build\dist\MeloShelf" "/DOutDir=$build\installer" packaging\installer.iss }
    } else { Write-Warning 'Inno Setup 6 not found: only the portable folder was built.' }
  } finally { Pop-Location }
} catch {
  Write-Error "Build failed: $($_.Exception.Message)"
  exit 1
}
Write-Host "Portable folder: $build\dist\MeloShelf"
Write-Host "Installer:       $build\installer"
