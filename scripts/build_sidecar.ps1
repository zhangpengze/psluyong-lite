# 打包 Python sidecar 为独立 exe
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_sidecar.ps1
#
# 产物：client\src-tauri\sidecar-dist\psluyong-sidecar.exe
# 说明：用 --onefile 换部署简单，代价是每次启动多 1-2 秒解包。

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$outDir = Join-Path $root 'client\src-tauri\sidecar-dist'
$entry = Join-Path $root 'scripts\sidecar_entry.py'

Write-Host "仓库根目录: $root"

# 优先用仓库里的 venv，避免打进去一堆无关包
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = 'python'
    Write-Host "未找到 .venv，使用 PATH 中的 python" -ForegroundColor Yellow
}

Write-Host "检查 pyinstaller..."
& $python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "未安装 pyinstaller，正在安装..." -ForegroundColor Yellow
    & $python -m pip install pyinstaller
}

if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
$workDir = Join-Path $root 'build\_sidecar'
if (Test-Path $workDir) { Remove-Item -Recurse -Force $workDir }

Write-Host "开始打包..."
& $python -m PyInstaller `
    --onefile `
    --name psluyong-sidecar `
    --distpath $outDir `
    --workpath $workDir `
    --specpath $workDir `
    --noconfirm `
    --clean `
    --hidden-import win32com.client `
    --hidden-import pythoncom `
    --hidden-import pywintypes `
    --collect-submodules core `
    --collect-submodules client.sidecar `
    --paths $root `
    $entry

if ($LASTEXITCODE -ne 0) { throw "打包失败" }

$exe = Join-Path $outDir 'psluyong-sidecar.exe'
if (Test-Path $exe) {
    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "完成: $exe ($size MB)" -ForegroundColor Green
} else {
    throw "未生成 exe"
}
