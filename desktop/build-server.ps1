param(
    [string]$Target = "x86_64-pc-windows-msvc"
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$binaryDir = Join-Path $root "src-tauri\binaries"
New-Item -ItemType Directory -Force -Path $binaryDir | Out-Null

Push-Location $root
try {
    python -m PyInstaller --noconfirm --clean --onefile --name project-sm-server `
        --add-data "data;data" --add-data "templates;templates" --add-data "static;static" `
        desktop\run_server.py

    $targetFile = Join-Path $binaryDir "project-sm-server-$Target.exe"
    Copy-Item "dist\project-sm-server.exe" $targetFile -Force
}
finally {
    Pop-Location
}
