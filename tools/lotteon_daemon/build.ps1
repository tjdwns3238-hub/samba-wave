# Autotune daemon single .exe build - PyInstaller --onefile + Playwright Chromium bundle.
# Usage: .\build.ps1
# Output: dist\daemon.exe (single file, ~342MB)
#
# This .exe self-installs to %APPDATA%\samba-autotune-daemon\ + registers HKCU\Run.
# User clicks once, daemon runs forever (auto-update included).

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

function Write-Step($msg) {
  Write-Host ""
  Write-Host ("=== " + $msg + " ===") -ForegroundColor Cyan
}

Write-Step "Python check"
& python --version

Write-Step "venv create/activate"
if (-not (Test-Path '.venv')) { & python -m venv .venv }
& (Join-Path $here '.venv\Scripts\Activate.ps1')

Write-Step "Install build deps"
& python -m pip install --upgrade pip
& python -m pip install playwright httpx pyinstaller pystray Pillow

# chromium 번들 제거(2026-05-23): 350MB→~25MB. 데몬은 시스템 크롬/Edge 헤드리스 사용
# (_launch_browser channel="chrome"→"msedge"). Edge 는 Windows 기본 설치라 거의 항상 존재.
Write-Step "PyInstaller build (onefile + noconsole + tray, no chromium bundle)"
if (Test-Path 'dist') { Remove-Item -Recurse -Force 'dist' }
if (Test-Path 'build') { Remove-Item -Recurse -Force 'build' }
& pyinstaller `
  --name daemon `
  --onefile `
  --noconfirm `
  --noconsole `
  --collect-all playwright `
  --collect-all httpx `
  --collect-all pystray `
  --collect-all PIL `
  --hidden-import asyncio `
  --hidden-import pystray._win32 `
  --hidden-import site_handlers `
  daemon.py

Write-Step "Build done"
Write-Host "Output: dist\daemon.exe"
Write-Host ""
Write-Host "Deploy: .\upload.ps1 (GitHub Release upload)"
