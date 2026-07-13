param(
  [int]$Port = 0,
  [string]$HostAddress = "",
  [string]$WorkDir = "",
  [switch]$Open,
  [switch]$NoCoreAutoInstall
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

$argsList = @()
if ($Port -gt 0) { $argsList += @("--port", "$Port") }
if ($HostAddress) { $argsList += @("--host", $HostAddress) }
if ($WorkDir) { $argsList += @("--workdir", $WorkDir) }
if ($Open) { $argsList += "--open" }
if ($NoCoreAutoInstall) { $argsList += "--no-core-auto-install" }

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
  $python = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $python) {
  throw "Python 3 was not found in PATH. Install Python 3.10+ and retry."
}

& $python.Source app.py @argsList
