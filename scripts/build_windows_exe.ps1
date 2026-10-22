# Version 是写入产物的完整 SemVer；BuildNumber 是 Windows 四段文件版本的末段，均可由对应环境变量注入。
param(
  [string]$Python = "",
  [switch]$SkipInstall,
  [string]$Version = "",
  [string]$BuildNumber = ""
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

# 仓库 VERSION 是人工维护的基础版本；GitHub Actions 会追加 build 序号后传入完整版本。
$RepositoryVersionPath = Join-Path $RootDir "VERSION"
if (-not (Test-Path -LiteralPath $RepositoryVersionPath -PathType Leaf)) {
  throw "VERSION file not found: $RepositoryVersionPath"
}
$BaseVersion = (Get-Content -LiteralPath $RepositoryVersionPath -Raw).Trim()
if ($BaseVersion -notmatch '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') {
  throw "VERSION must contain a three-part numeric SemVer: $BaseVersion"
}
if (-not $Version) {
  $Version = $env:PROXY_BRIDGE_VERSION
}
if (-not $Version) {
  $Version = $BaseVersion
}
if (-not $BuildNumber) {
  $BuildNumber = $env:PROXY_BRIDGE_BUILD_NUMBER
}
if (-not $BuildNumber) {
  $BuildNumber = "1"
}

$VersionMatch = [regex]::Match(
  $Version,
  '^(?<major>0|[1-9]\d*)\.(?<minor>0|[1-9]\d*)\.(?<patch>0|[1-9]\d*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$'
)
if (-not $VersionMatch.Success) {
  throw "Version must be SemVer, for example 0.3.0 or 0.3.0-build.12: $Version"
}

$MajorVersion = [int]$VersionMatch.Groups["major"].Value
$MinorVersion = [int]$VersionMatch.Groups["minor"].Value
$PatchVersion = [int]$VersionMatch.Groups["patch"].Value
[int]$BuildNumberValue = 0
if ((-not [int]::TryParse($BuildNumber, [ref]$BuildNumberValue)) -or $BuildNumberValue -lt 0 -or $BuildNumberValue -gt 65535) {
  throw "BuildNumber must be an integer in 0..65535: $BuildNumber"
}
foreach ($part in @($MajorVersion, $MinorVersion, $PatchVersion)) {
  if ($part -gt 65535) {
    throw "Each numeric version part must be in 0..65535: $Version"
  }
}
Write-Host "Building ProxyBridge version $Version (build $BuildNumberValue)"

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
$MetadataDir = Join-Path $OutDir "metadata"
New-Item -ItemType Directory -Force -Path $OutDir, $WorkDir, $DistDir, $MetadataDir | Out-Null

# PyInstaller 使用独立文件写入 Windows 资源；包内 VERSION 供后端和控制台在运行时显示同一完整版本。
$BundledVersionPath = Join-Path $MetadataDir "VERSION"
Set-Content -LiteralPath $BundledVersionPath -Value $Version -Encoding ASCII
# 外壳修订号：发布流水线通过 PROXY_BRIDGE_SHELL_REVISION 注入（与更新清单使用同一个值），本地构建时按外壳源文件计算。
# 后端据此判断新版本能否只热更新后端载荷，还是需要替换整个 exe。
$ShellRevision = "$env:PROXY_BRIDGE_SHELL_REVISION".Trim()
if (-not $ShellRevision) {
  $revisionArgs = @((Join-Path $RootDir "scripts\build_update_release.py"), "shell-revision", "--shell", "windows-exe")
  $ShellRevision = ((Invoke-Python @revisionArgs) | Out-String).Trim()
}
if ($ShellRevision -notmatch '^[0-9a-f]{64}$') {
  throw "Invalid shell revision: $ShellRevision"
}
$ShellRevisionPath = Join-Path $MetadataDir "SHELL_REVISION"
Set-Content -LiteralPath $ShellRevisionPath -Value $ShellRevision -Encoding ASCII
$VersionResourcePath = Join-Path $MetadataDir "ProxyBridge.version.txt"
$VersionResource = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($MajorVersion, $MinorVersion, $PatchVersion, $BuildNumberValue),
    prodvers=($MajorVersion, $MinorVersion, $PatchVersion, $BuildNumberValue),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'040904B0',
        [
          StringStruct(u'CompanyName', u'ProxyBridge'),
          StringStruct(u'FileDescription', u'ProxyBridge desktop client'),
          StringStruct(u'FileVersion', u'$Version'),
          StringStruct(u'InternalName', u'ProxyBridge'),
          StringStruct(u'OriginalFilename', u'ProxyBridge.exe'),
          StringStruct(u'ProductName', u'ProxyBridge'),
          StringStruct(u'ProductVersion', u'$Version')
        ]
      )
    ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"@
Set-Content -LiteralPath $VersionResourcePath -Value $VersionResource -Encoding UTF8

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
# --specpath 位于 outputs/windows；源文件必须使用绝对路径，否则 PyInstaller 会错误地从 spec 目录查找。
$AppSource = Join-Path $RootDir "app.py"
$WebDavSource = Join-Path $RootDir "webdav_sync.py"
$RemoteBrowserSource = Join-Path $RootDir "remote_browser.py"
$BrowserFingerprintSource = Join-Path $RootDir "browser_fingerprint.py"
$VersionModuleSource = Join-Path $RootDir "proxybridge_version.py"
$AppPayloadSource = Join-Path $RootDir "app_payload.py"
$AppUpdateSource = Join-Path $RootDir "app_update.py"
$StaticSource = Join-Path $RootDir "static"
$DesktopSource = Join-Path $RootDir "desktop"
$TrayEntry = Join-Path $RootDir "desktop\tray_host.py"
$addData = @(
  "$AppSource;.",
  "$WebDavSource;.",
  # 主模块会导入远程浏览器 Broker 客户端；Windows 包需收集该同级模块。
  "$RemoteBrowserSource;.",
  # 指纹浏览器配置与 CDP 客户端模块同样由主模块静态导入。
  "$BrowserFingerprintSource;.",
  # 版本解析模块和构建生成的 VERSION 必须同时进入 onefile 临时目录。
  "$VersionModuleSource;.",
  "$BundledVersionPath;.",
  # 软件更新：载荷启动器、更新模块与外壳修订号。
  "$AppPayloadSource;.",
  "$AppUpdateSource;.",
  "$ShellRevisionPath;.",
  "$StaticSource;static",
  "$DesktopSource;desktop"
)
$pyiArgs = @(
  "-m", "PyInstaller",
  "--noconfirm",
  "--clean",
  "--onefile",
  "--windowed",
  "--name", "ProxyBridge",
  "--version-file", $VersionResourcePath,
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
  # 明确声明隐藏导入，避免动态加载 app 时遗漏 Broker 客户端模块。
  "--hidden-import", "remote_browser",
  "--hidden-import", "browser_fingerprint",
  "--hidden-import", "proxybridge_version",
  "--hidden-import", "app_payload",
  "--hidden-import", "app_update",
  "--hidden-import", "desktop",
  "--hidden-import", "desktop.tray_host",
  "--hidden-import", "desktop.autostart",
  "--hidden-import", "pystray",
  "--hidden-import", "pystray._win32",
  "--hidden-import", "PIL",
  "--hidden-import", "PIL.Image",
  "--hidden-import", "PIL.ImageDraw",
  $TrayEntry
)

Invoke-Python @pyiArgs

$exePath = Join-Path $DistDir "ProxyBridge.exe"
if (-not (Test-Path $exePath)) {
  throw "Build finished but ProxyBridge.exe was not found at $exePath"
}
$EmbeddedProductVersion = (Get-Item -LiteralPath $exePath).VersionInfo.ProductVersion
if ($EmbeddedProductVersion -ne $Version) {
  throw "Built exe ProductVersion mismatch: expected $Version, got $EmbeddedProductVersion"
}

# Convenience copy for double-click distribution.
$releaseDir = Join-Path $OutDir "release"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
Copy-Item -Force $exePath (Join-Path $releaseDir "ProxyBridge.exe")

# Optional portable launcher note
$readme = @"
ProxyBridge Windows portable build
=================================

Version: $Version

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
