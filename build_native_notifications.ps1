param([string] $OutputDirectory = "build\native-notifications")
$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $vswhere)) { throw "Existing MSVC Build Tools and Windows SDK are required; nothing was installed." }
$visualStudio = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $visualStudio) { throw "No existing MSVC x64 toolchain found" }
$developerCommand = Join-Path $visualStudio "Common7\Tools\VsDevCmd.bat"
$outputPath = [IO.Path]::GetFullPath((Join-Path $projectDir $OutputDirectory))
if (-not $outputPath.StartsWith(($projectDir + "\build\"), [StringComparison]::OrdinalIgnoreCase)) {
    throw "Native build output must stay in this project's build directory"
}
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null
$source = Join-Path $projectDir "native\windows_notifications.cpp"
$dll = Join-Path $outputPath "DesktopCalendarNotifications.dll"
Push-Location $outputPath
try {
    $command = '"' + $developerCommand + '" -arch=x64 -host_arch=x64 >nul && cl.exe /nologo /std:c++20 /EHsc /MT /O2 /LD /utf-8 "' + $source + '" /link /OUT:"' + $dll + '" windowsapp.lib ole32.lib propsys.lib shell32.lib'
    & $env:ComSpec /d /s /c $command
    if ($LASTEXITCODE -ne 0) { throw "Native notification bridge build failed ($LASTEXITCODE)" }
} finally { Pop-Location }
Write-Host "Native notification bridge: $dll"
