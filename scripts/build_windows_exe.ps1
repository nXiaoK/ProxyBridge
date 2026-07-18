param(
  [string]$Python = "",
  [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

function Test-PythonCommand {
  param(
    [string]$Executable,
    [string[]]$Arguments = @()
  )
  try {
    $probeArgs = @()
    if ($Arguments) {
      $probeArgs += $Arguments
    }
    $probeArgs += @(
      "-c",
      "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)"
    )
    & $Executable @probeArgs 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function New-PythonCommand {
  param(
    [string]$Executable,
    [string[]]$Arguments = @()
  )
  $display = $Executable
  if ($Arguments) {
    $display = "$Executable $($Arguments -join ' ')"
  }
  [pscustomobject]@{
    Executable = $Executable
    Arguments = $Arguments
    Display = $display
  }
}

function Resolve-Python {
  param([string]$Preferred)
  if ($Preferred) {
    if (-not (Test-Path -LiteralPath $Preferred -PathType Leaf)) {
      throw "Python executable not found: $Preferred"
    }
    $preferredPath = (Resolve-Path -LiteralPath $Preferred).Path
    if (-not (Test-PythonCommand -Executable $preferredPath)) {
      throw "Python executable is not usable or is older than Python 3.10: $preferredPath"
    }
    return New-PythonCommand -Executable $preferredPath
  }

  foreach ($name in @("python", "python3", "py")) {
    $commands = @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)
    foreach ($cmd in $commands) {
      $candidatePath = $cmd.Path
      if (-not $candidatePath) {
        $candidatePath = $cmd.Source
      }
      if (-not $candidatePath -or -not (Test-Path -LiteralPath $candidatePath -PathType Leaf)) {
        continue
      }
      $candidatePath = (Resolve-Path -LiteralPath $candidatePath).Path
      if ($name -ne "py" -and $candidatePath -match '(?i)[\\/]WindowsApps[\\/]') {
        continue
      }

      $candidateArgs = @()
      if ($name -eq "py") {
        $candidateArgs = @("-3")
      }
      if (Test-PythonCommand -Executable $candidatePath -Arguments $candidateArgs) {
        return New-PythonCommand -Executable $candidatePath -Arguments $candidateArgs
      }
    }
  }

  throw "Python 3.10+ was not found in PATH. If WindowsApps\python.exe is an App Execution Alias, install Python or pass -Python C:\path\to\python.exe."
}

$pythonCommand = Resolve-Python -Preferred $Python
Write-Host "Using Python: $($pythonCommand.Display)"

function Invoke-Python {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CommandArgs)
  $invokeArgs = @()
  if ($pythonCommand.Arguments) {
    $invokeArgs += $pythonCommand.Arguments
  }
  if ($CommandArgs) {
    $invokeArgs += $CommandArgs
  }
  & $pythonCommand.Executable @invokeArgs
  if ($LASTEXITCODE -ne 0) {
    throw "Python command failed: $($CommandArgs -join ' ')"
  }
}

if (-not $SkipInstall) {
  Write-Host "Installing packaging + tray dependencies..."
  Invoke-Python -m pip install --upgrade pip
  Invoke-Python -m pip install -r desktop/requirements.txt
  Invoke-Python -m pip install "pyinstaller>=6.0"
}

$OutDir = Join-Path $RootDir "outputs\windows"
$WorkDir = Join-Path $OutDir "build"
$DistDir = Join-Path $OutDir "dist"
New-Item -ItemType Directory -Force -Path $OutDir, $WorkDir, $DistDir | Out-Null

$IconPng = Join-Path $RootDir "static\assets\app-icon.png"
$IconIco = Join-Path $OutDir "ProxyBridge.ico"
if (Test-Path $IconPng) {
  Write-Host "Generating Windows .ico from app-icon.png..."
  $iconScript = @'
from pathlib import Path
from PIL import Image
src = Path(r"__PNG__")
dst = Path(r"__ICO__")
img = Image.open(src).convert("RGBA")
sizes = [(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)]
img.save(dst, format="ICO", sizes=sizes)
print(dst)
'@
  $iconScript = $iconScript.Replace("__PNG__", $IconPng).Replace("__ICO__", $IconIco)
  $tmpPy = Join-Path $OutDir "_make_ico.py"
  Set-Content -Path $tmpPy -Value $iconScript -Encoding UTF8
  Invoke-Python $tmpPy
}

Write-Host "Building ProxyBridge.exe (onefile, windowed)..."
$addData = @(
  "app.py;.",
  "webdav_sync.py;.",
  "static;static",
  "desktop;desktop"
)
$pyiArgs = @(
  "-m", "PyInstaller",
  "--noconfirm",
  "--clean",
  "--onefile",
  "--windowed",
  "--name", "ProxyBridge",
  "--distpath", $DistDir,
  "--workpath", $WorkDir,
  "--specpath", $OutDir
)
if (Test-Path $IconIco) {
  $pyiArgs += @("--icon", $IconIco)
}
foreach ($item in $addData) {
  $pyiArgs += @("--add-data", $item)
}
$pyiArgs += @(
  "--hidden-import", "app",
  "--hidden-import", "webdav_sync",
  "--hidden-import", "desktop",
  "--hidden-import", "desktop.tray_host",
  "--hidden-import", "desktop.autostart",
  "--hidden-import", "pystray",
  "--hidden-import", "pystray._win32",
  "--hidden-import", "PIL",
  "--hidden-import", "PIL.Image",
  "--hidden-import", "PIL.ImageDraw",
  "desktop\tray_host.py"
)

Invoke-Python @pyiArgs

$exePath = Join-Path $DistDir "ProxyBridge.exe"
if (-not (Test-Path $exePath)) {
  throw "Build finished but ProxyBridge.exe was not found at $exePath"
}

# Convenience copy for double-click distribution.
$releaseDir = Join-Path $OutDir "release"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
Copy-Item -Force $exePath (Join-Path $releaseDir "ProxyBridge.exe")

# Optional portable launcher note
$readme = @"
ProxyBridge Windows portable build
=================================

1. Double-click ProxyBridge.exe
2. A tray icon appears; the web console opens in your browser
3. Data directory: %APPDATA%\ProxyBridge
4. Logs:
   - %APPDATA%\ProxyBridge\tray-host.log
   - %APPDATA%\ProxyBridge\tray-backend.log

First launch may download sing-box / xray from GitHub.
Windows SmartScreen may warn because the binary is unsigned.
"@
Set-Content -Path (Join-Path $releaseDir "README.txt") -Value $readme -Encoding UTF8

Write-Host ""
Write-Host "Build succeeded:"
Write-Host "  $exePath"
Write-Host "  $(Join-Path $releaseDir 'ProxyBridge.exe')"
Write-Host "Double-click the release exe to start the tray host."
Write-Host "Optional shortcuts:"
Write-Host "  .\scripts\create_windows_shortcuts.ps1 -ExePath `"$(Join-Path $releaseDir 'ProxyBridge.exe')`""
Write-Host "Tray menu also supports 开机自启 / --enable-autostart."
