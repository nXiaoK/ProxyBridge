param(
  [int]$Port = 18787,
  [string]$HostAddress = "127.0.0.1",
  [string]$WorkDir = "",
  [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
  $python = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $python) {
  throw "Python 3 was not found in PATH. Install Python 3.10+ and retry."
}

& $python.Source -c "import pystray, PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Installing tray dependencies into the current Python environment..."
  & $python.Source -m pip install -r desktop/requirements.txt
}

$argsList = @("--port", "$Port", "--host", $HostAddress)
if ($WorkDir) { $argsList += @("--workdir", $WorkDir) }
if ($NoOpen) { $argsList += "--no-open" }

& $python.Source -m desktop.tray_host @argsList
