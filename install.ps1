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

Say "3/4 Браузер програми (у ньому вона заходить на пари; ~150 МБ)"
& $venv -m playwright install chromium --no-shell
if ($LASTEXITCODE -ne 0) { Write-Host "Не вийшло — не страшно: програма завантажить браузер сама при першому запуску." -ForegroundColor Yellow }

Say "4/4 Ярлики на робочому столі й у меню «Пуск»"
$target = (Resolve-Path ".venv\Scripts\autopara-window.exe").Path
$icon = Join-Path (Get-Location).Path "autopara.ico"
& $venv -c "import sys; from PySide6.QtGui import QGuiApplication, QIcon; app = QGuiApplication(['x', '-platform', 'offscreen']); QIcon(sys.argv[1]).pixmap(256, 256).save(sys.argv[2])" "autopara\gui\icon.svg" $icon
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $link = $shell.CreateShortcut((Join-Path $folder "autopara.lnk"))
    $link.TargetPath = $target
    $link.WorkingDirectory = (Get-Location).Path
    $link.Description = "Автоматичний вхід на пари"
    if (Test-Path $icon) { $link.IconLocation = $icon }
    $link.Save()
}

Say "Готово! Відкриваю програму — далі простий майстер."
Start-Process $target
