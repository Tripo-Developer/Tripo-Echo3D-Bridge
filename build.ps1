[CmdletBinding()]
param([string]$OutputName = 'Tripo-Echo3D-Bridge')
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = (Get-Command python -ErrorAction Stop).Source
& $python -m pip install --disable-pip-version-check -q -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $python -m pip install --disable-pip-version-check -q 'pyinstaller>=6,<7'
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed.' }
& $python -m PyInstaller --noconfirm --clean --windowed --onefile --collect-all customtkinter --icon "$root\assets\bridge.ico" --add-data "$root\assets;assets" --name $OutputName --add-data "$root\web;web" --paths $root --distpath (Join-Path $root 'dist') --workpath (Join-Path $root 'build') --specpath $root (Join-Path $root 'desktop_main.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
Write-Host "Built $(Join-Path $root "dist\$OutputName.exe")"
