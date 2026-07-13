param(
  [string]$ExePath = "",
  [switch]$Startup,
  [switch]$Desktop
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot

if (-not $ExePath) {
  $candidate = Join-Path $RootDir "outputs\windows\release\ProxyBridge.exe"
  if (Test-Path $candidate) {
    $ExePath = $candidate
  } else {
    throw "ProxyBridge.exe not found. Pass -ExePath or build with scripts/build_windows_exe.ps1 first."
  }
}

$ExePath = (Resolve-Path $ExePath).Path
if (-not (Test-Path $ExePath)) {
  throw "Executable not found: $ExePath"
}

function New-Shortcut {
  param(
    [string]$LinkPath,
    [string]$Target,
    [string]$Arguments = ""
  )
  $dir = Split-Path -Parent $LinkPath
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $ws = New-Object -ComObject WScript.Shell
  $sc = $ws.CreateShortcut($LinkPath)
  $sc.TargetPath = $Target
  $sc.Arguments = $Arguments
  $sc.WorkingDirectory = Split-Path -Parent $Target
  $sc.WindowStyle = 7
  $sc.Description = "ProxyBridge tray host"
  $sc.Save()
  Write-Host "Created: $LinkPath"
}

if (-not $Startup -and -not $Desktop) {
  $Startup = $true
  $Desktop = $true
}

if ($Desktop) {
  $desktop = [Environment]::GetFolderPath("Desktop")
  New-Shortcut -LinkPath (Join-Path $desktop "ProxyBridge.lnk") -Target $ExePath
}

if ($Startup) {
  $startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\ProxyBridge.lnk"
  New-Shortcut -LinkPath $startup -Target $ExePath -Arguments "--no-open"
  Write-Host "Login autostart enabled."
}
