$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$distDir = Join-Path $projectDir "dist"
$buildDir = Join-Path $projectDir "build"
$releaseDir = Join-Path $projectDir "release"
$appDir = Join-Path $distDir "DesktopCalendar"
$pythonCommand = if ($env:CALENDAR_BUILD_PYTHON) { $env:CALENDAR_BUILD_PYTHON } else { "py" }
$pythonPrefix = if ($env:CALENDAR_BUILD_PYTHON) { @() } else { @("-3") }

Set-Location $projectDir
New-Item -ItemType Directory -Path (Join-Path $buildDir "spec") -Force | Out-Null

& $pythonCommand @pythonPrefix -m PyInstaller --noconfirm --clean --windowed --onedir `
    --name DesktopCalendar `
    --distpath $distDir `
    --workpath (Join-Path $buildDir "app") `
    --specpath (Join-Path $buildDir "spec") `
    app.py

& $pythonCommand @pythonPrefix -m PyInstaller --noconfirm --clean --windowed --onefile `
    --name DesktopCalendarUpdater `
    --distpath (Join-Path $buildDir "updater-dist") `
    --workpath (Join-Path $buildDir "updater") `
    --specpath (Join-Path $buildDir "spec") `
    updater.py

Copy-Item (Join-Path $buildDir "updater-dist\DesktopCalendarUpdater.exe") $appDir -Force
Copy-Item (Join-Path $projectDir "README.md") $appDir -Force
Copy-Item (Join-Path $projectDir "LICENSE") $appDir -Force

New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
$archive = Join-Path $releaseDir "DesktopCalendar-win64.zip"
if (Test-Path $archive) {
    Remove-Item -LiteralPath $archive -Force
}
Compress-Archive -Path $appDir -DestinationPath $archive -CompressionLevel Optimal

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
"$hash  DesktopCalendar-win64.zip" | Set-Content `
    -LiteralPath (Join-Path $releaseDir "DesktopCalendar-win64.zip.sha256") `
    -Encoding ascii

Write-Host "Release package: $archive"
Write-Host "SHA-256: $hash"
