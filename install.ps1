# autopara — встановлення одним рухом (Windows). Запускається з install.bat.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Say($text) { Write-Host ""; Write-Host $text -ForegroundColor Cyan }

function Test-Python($exe, [string[]]$prefix) {
    try {
        & $exe @prefix -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($version in "-3.13", "-3.12", "-3.11") {
            if (Test-Python "py" @($version)) { return @("py", $version) }
        }
    }
    if ((Get-Command python -ErrorAction SilentlyContinue) -and (Test-Python "python" @())) { return @("python") }
    return $null
}

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

$python = Find-Python
if (-not $python) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "Потрібен Python 3.11 або новіший: https://www.python.org/downloads/ (постав галочку «Add python.exe to PATH»), потім запусти install.bat ще раз." -ForegroundColor Yellow
        exit 1
    }
    Say "Встановлюю Python 3.12"
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $python = Find-Python
    if (-not $python) {
        Write-Host "Python встановлено, але Windows ще не бачить його. Закрий це вікно й запусти install.bat ще раз." -ForegroundColor Yellow
        exit 1
    }
}

$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ }
if (-not $chrome -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    Say "Встановлюю Google Chrome (у ньому програма заходить на пари)"
    winget install -e --id Google.Chrome --accept-package-agreements --accept-source-agreements
}

Say "1/4 Окреме середовище Python (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    $exe, $prefix = $python
    & $exe @prefix -m venv .venv
}
$venv = (Resolve-Path ".venv\Scripts\python.exe").Path
& $venv -m pip install -q --upgrade pip

Say "2/4 autopara з розпізнаванням мови (кілька хвилин)"
& $venv -m pip install -q -e ".[ears]"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Розпізнавання мови не встановилось — ставлю без нього (доставити можна кнопкою у вікні програми)." -ForegroundColor Yellow
    & $venv -m pip install -q -e .
    if ($LASTEXITCODE -ne 0) { throw "не вдалося встановити autopara" }
}

Say "3/4 Запасний браузер"
& $venv -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Write-Host "Не вийшло — не страшно, якщо встановлений Google Chrome." -ForegroundColor Yellow }

Say "4/4 Ярлики на робочому столі й у меню «Пуск»"
$target = (Resolve-Path ".venv\Scripts\autopara-window.exe").Path
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $link = $shell.CreateShortcut((Join-Path $folder "autopara.lnk"))
    $link.TargetPath = $target
    $link.WorkingDirectory = (Get-Location).Path
    $link.Description = "Автоматичний вхід на пари"
    $link.Save()
}

Say "Готово! Відкриваю вікно програми — далі все там."
Start-Process $target
