$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$distDir = Join-Path $projectDir "dist"
$buildDir = Join-Path $projectDir "build"
$releaseDir = Join-Path $projectDir "release"
$appDir = Join-Path $distDir "DesktopCalendar"
$manifestPath = Join-Path $projectDir "windows_per_monitor_v2.manifest"
$pythonSelector = if ($env:CALENDAR_BUILD_PYTHON) { $env:CALENDAR_BUILD_PYTHON } else { "py" }
$pythonSelectorArguments = if ($env:CALENDAR_BUILD_PYTHON) { @() } else { @("-3") }
$resolveArguments = @($pythonSelectorArguments) + @("-c", "import sys; print(sys.executable)")
$pythonOutput = @(& $pythonSelector @resolveArguments)
if ($LASTEXITCODE -ne 0 -or $pythonOutput.Count -eq 0) {
    throw "Unable to resolve the selected Python interpreter"
}
$pythonExe = $pythonOutput[-1].Trim()
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Resolved Python interpreter does not exist: $pythonExe"
}

function Invoke-PythonBuild {
    param(
        [Parameter(Mandatory = $true)]
        [string[]] $Arguments,
        [Parameter(Mandatory = $true)]
        [string] $FailureMessage
    )

    & $pythonExe @Arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "$FailureMessage with exit code $exitCode"
    }
}

Set-Location $projectDir
New-Item -ItemType Directory -Path (Join-Path $buildDir "spec") -Force | Out-Null

Write-Host "Python interpreter: $pythonExe"
$appBuildArguments = @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
    "--name", "DesktopCalendar",
    "--icon", (Join-Path $projectDir "assets\calendar.ico"),
    "--manifest", $manifestPath,
    "--add-data", "$projectDir\assets;assets",
    "--distpath", $distDir,
    "--workpath", (Join-Path $buildDir "app"),
    "--specpath", (Join-Path $buildDir "spec"),
    "app.py"
)
Invoke-PythonBuild -Arguments $appBuildArguments -FailureMessage "DesktopCalendar build failed"

$updaterBuildArguments = @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onefile",
    "--name", "DesktopCalendarUpdater",
    "--icon", (Join-Path $projectDir "assets\calendar.ico"),
    "--manifest", $manifestPath,
    "--distpath", (Join-Path $buildDir "updater-dist"),
    "--workpath", (Join-Path $buildDir "updater"),
    "--specpath", (Join-Path $buildDir "spec"),
    "updater.py"
)
Invoke-PythonBuild -Arguments $updaterBuildArguments -FailureMessage "DesktopCalendarUpdater build failed"

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
