# Start the transcriber and open it in your browser. Windows counterpart to run.sh.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "No virtualenv found. Run:  uv venv --python 3.13 .venv; uv pip install --python .venv\Scripts\python.exe -e ."
    exit 1
}

if (-not $env:PORT) { $env:PORT = "8420" }

# Load secrets from .env if present (ANTHROPIC_API_KEY, OPENROUTER_API_KEY, HF_TOKEN).
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#=\s][^=]*)\s*=\s*(.*)\s*$') {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim().Trim('"').Trim("'")
            Set-Item -Path "Env:$name" -Value $value
        }
    }
}

Start-Job -ScriptBlock {
    param($port)
    Start-Sleep -Seconds 1.5
    Start-Process "http://127.0.0.1:$port"
} -ArgumentList $env:PORT | Out-Null

& $python -m uvicorn app.main:app --host 127.0.0.1 --port $env:PORT $args
