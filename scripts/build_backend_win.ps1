# Build the FastAPI sidecar with PyInstaller on Windows.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\build_backend_win.ps1
#   powershell -File scripts\build_backend_win.ps1 -Profile lite -Mode onefile
param(
    [ValidateSet("full", "lite")]
    [string]$Profile = "full",
    [ValidateSet("onedir", "onefile")]
    [string]$Mode = "onedir"
)

$ErrorActionPreference = "Stop"
$BackendDir = Join-Path $PSScriptRoot "..\backend"

Write-Host "[build_backend_win] mode=$Mode profile=$Profile"

Push-Location $BackendDir
try {
    # Use the venv python if present, else the system one.
    $py = "python"
    if (Test-Path ".venv\Scripts\python.exe") { $py = ".venv\Scripts\python.exe" }

    # Full profile bundles torch: use the CPU wheel to keep size sane.
    if ($Profile -eq "full") {
        & $py -m pip install torch --index-url https://download.pytorch.org/whl/cpu
    }
    & $py -m pip install -r requirements.txt pyinstaller

    $env:DEHKHODA_BUNDLE_MODE = $Mode
    $env:DEHKHODA_BUNDLE_PROFILE = $Profile
    & $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build dehkhoda-server.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with $LASTEXITCODE" }

    Write-Host "[build_backend_win] bundle ready: backend\dist\dehkhoda-server"
} finally {
    Pop-Location
}
