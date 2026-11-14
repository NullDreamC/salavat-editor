param([switch]$Install, [switch]$NoBrowser, [int]$Port = 8000)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonPath)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python.' }
    $Install = $true
}
if ($Install) {
    & $pythonPath -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости Python.' }
    Push-Location web
    try {
        npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости интерфейса.' }
        npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось собрать интерфейс.' }
    } finally { Pop-Location }
}
if (!(Test-Path -LiteralPath 'web\dist\index.html')) {
    throw 'Сначала выполните .\start.ps1 -Install'
}
if (!$NoBrowser) { Start-Process "http://127.0.0.1:$Port" }
& $pythonPath -m uvicorn server.main:app --host 127.0.0.1 --port $Port --log-level warning
