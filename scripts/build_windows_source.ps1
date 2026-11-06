#!/usr/bin/env pwsh
# 编译源码启动器; Python 环境和前端由项目目录提供.
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

if (-not $IsWindows) {
    throw "源码托盘启动器必须在 Windows 上构建。"
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Out = Join-Path $Root "dist\Amane.Source"
$Version = (& (Join-Path $Root ".venv\Scripts\python.exe") -c "from amane.version import get_version; print(get_version())").Trim()

dotnet publish (Join-Path $Root "winapp\Amane.csproj") `
    -c Release -r win-x64 --self-contained -o $Out `
    /p:PublishAot=false /p:PublishSingleFile=true /p:IncludeNativeLibrariesForSelfExtract=true `
    /p:AssemblyName=Amane.Source /p:Version="$Version"

Set-Content -LiteralPath (Join-Path $Out "source-root.txt") -Value $Root -Encoding utf8
Write-Output "EXE=$(Join-Path $Out 'Amane.Source.exe')"
