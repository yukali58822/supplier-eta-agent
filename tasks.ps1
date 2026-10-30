param([Parameter(Position = 0)][string]$Task = "help")
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }

switch ($Task) {
    "install"   { & $py -m pip install --upgrade pip; & $py -m pip install -e ".[dev]" }
    "check"     { & $py -m eta_agent.cli check-llm }
    "demo"      { & $py -m eta_agent.cli demo }
    "test"      { & $py -m pytest }
    "eval"      { & $py -m eta_agent.cli eval --data data/emails --out reports }
    "web"       { & $py -m eta_agent.cli web }
    "inbox"     { & $py -m eta_agent.cli inbox }
    "eval-rule" { & $py -m eta_agent.cli eval --data data/emails --out reports --extractor rule }
    "generate"  { & $py -m eta_agent.cli generate --out data/emails }
    "up"        { docker compose up --build }
    "down"      { docker compose down }
    default     { Write-Host "用法：.\tasks.ps1 [install|check|demo|test|eval|eval-rule|web|inbox|generate|up|down]" }
}
