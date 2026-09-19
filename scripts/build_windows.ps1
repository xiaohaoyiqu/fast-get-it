$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$archive = Join-Path $projectRoot 'aria2-1.37.0-win-64bit-build1.zip'
$expectedHash = '67D015301EEF0B612191212D564C5BB0A14B5B9C4796B76454276A4D28D9B288'

if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    throw "Missing the required official aria2 archive: $archive"
}
$actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
if ($actualHash -ne $expectedHash) {
    throw "aria2 archive SHA-256 mismatch; build stopped"
}

$specFiles = @(Get-ChildItem -LiteralPath $projectRoot -Filter '*.spec' -File)
if ($specFiles.Count -ne 1) {
    throw "Expected exactly one .spec file in the project root; found $($specFiles.Count)"
}
$specFile = $specFiles[0].FullName

Push-Location $projectRoot
try {
    python '.\scripts\build_windows_icon.py'
    if ($LASTEXITCODE -ne 0) {
        throw "Windows icon generation failed with exit code $LASTEXITCODE"
    }
    python -m PyInstaller --noconfirm --clean $specFile
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
    Write-Host "Build completed. See the application folder under: $projectRoot\dist"
} finally {
    Pop-Location
}
