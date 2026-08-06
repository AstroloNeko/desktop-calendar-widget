$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$distDir = Join-Path $projectDir "dist"
$buildDir = Join-Path $projectDir "build"
$releaseDir = Join-Path $projectDir "release"
$appDir = Join-Path $distDir "DesktopCalendar"
$manifestPath = Join-Path $projectDir "windows_per_monitor_v2.manifest"
$pythonCommand = if ($env:CALENDAR_BUILD_PYTHON) { $env:CALENDAR_BUILD_PYTHON } else { "py" }
$pythonPrefix = if ($env:CALENDAR_BUILD_PYTHON) { @() } else { @("-3") }

Set-Location $projectDir
New-Item -ItemType Directory -Path (Join-Path $buildDir "spec") -Force | Out-Null

& $pythonCommand @pythonPrefix -m PyInstaller --noconfirm --clean --windowed --onedir `
    --name DesktopCalendar `
    --icon (Join-Path $projectDir "assets\calendar.ico") `
    --manifest $manifestPath `
    --add-data "$projectDir\assets;assets" `
    --distpath $distDir `
    --workpath (Join-Path $buildDir "app") `
    --specpath (Join-Path $buildDir "spec") `
    app.py
if ($LASTEXITCODE -ne 0) {
    throw "DesktopCalendar build failed with exit code $LASTEXITCODE"
}

& $pythonCommand @pythonPrefix -m PyInstaller --noconfirm --clean --windowed --onefile `
    --name DesktopCalendarUpdater `
    --icon (Join-Path $projectDir "assets\calendar.ico") `
    --manifest $manifestPath `
    --distpath (Join-Path $buildDir "updater-dist") `
    --workpath (Join-Path $buildDir "updater") `
    --specpath (Join-Path $buildDir "spec") `
    updater.py
if ($LASTEXITCODE -ne 0) {
    throw "DesktopCalendarUpdater build failed with exit code $LASTEXITCODE"
}

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
