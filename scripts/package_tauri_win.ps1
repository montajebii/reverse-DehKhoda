# Package the Windows desktop app: copy the PyInstaller sidecar into the Tauri
# resource layout, then run `tauri build` (NSIS/MSI).
#
# Prereqs: backend\dist\dehkhoda-server (run scripts\build_backend_win.ps1 first),
#          node >= 18, rust >= 1.77, VS Build Tools (C++ workload).
param()
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$Sidecar = Join-Path $Root "backend\dist\dehkhoda-server"
if (-not (Test-Path (Join-Path $Sidecar "dehkhoda-server.exe"))) {
    throw "sidecar missing - run scripts\build_backend_win.ps1 first"
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root "desktop\src-tauri\resources") | Out-Null
if (Test-Path (Join-Path $Root "desktop\src-tauri\resources\dehkhoda-server")) {
    Remove-Item -Recurse -Force (Join-Path $Root "desktop\src-tauri\resources\dehkhoda-server")
}
Copy-Item -Recurse -Force $Sidecar (Join-Path $Root "desktop\src-tauri\resources\dehkhoda-server")
Write-Host "[package_tauri_win] sidecar copied"

Push-Location (Join-Path $Root "desktop")
try {
    if (-not (Test-Path "node_modules")) { npm install }
    npx tauri build @args
    if ($LASTEXITCODE -ne 0) { throw "tauri build failed" }
    Write-Host "[package_tauri_win] bundles: desktop\src-tauri\target\release\bundle\"
} finally {
    Pop-Location
}
