# One-click launcher: pulls the latest dev (only onto a clean tree), reinstalls
# dependencies if anything changed, and (re)starts the server — detached, so
# closing the window that launched this doesn't kill it. Safe to run repeatedly:
# if nothing changed and the server's already up, it just opens the browser.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $env:PORT) { $env:PORT = "8420" }
$pidFile = "$PSScriptRoot\.server.pid"
$logFile = "$PSScriptRoot\.autopull.log"

function Get-RunningPid {
    if (Test-Path $pidFile) {
        $existing = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($existing -and (Get-Process -Id $existing -ErrorAction SilentlyContinue)) {
            return [int]$existing
        }
    }
    return $null
}

# Pull the latest — but only onto a clean tree, so this never clobbers
# in-progress local edits just because it ran unattended.
$pulled = $false
try {
    $before = git rev-parse HEAD 2>$null
    $status = git status --porcelain 2>$null
    $branch = git rev-parse --abbrev-ref HEAD 2>$null
    if ($before -and (-not $status) -and $branch) {
        git fetch origin $branch *> $logFile
        git merge --ff-only "origin/$branch" *>> $logFile
        $after = git rev-parse HEAD 2>$null
        $pulled = ($after -ne $before)
    } elseif ($status) {
        "Skipped auto-pull: working tree not clean." | Out-File $logFile
    }
} catch { "Auto-pull failed: $_" | Out-File $logFile }

# New commits landed — reinstall in case a dependency changed. Cheap when
# nothing actually did; uv resolves and no-ops almost instantly.
if ($pulled) {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        & uv pip install --python "$PSScriptRoot\.venv\Scripts\python.exe" -e . *>> $logFile
    }
}

$running = Get-RunningPid
if ($running -and $pulled) {
    Stop-Process -Id $running -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    $running = $null
}

if (-not $running) {
    $python = "$PSScriptRoot\.venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        Write-Host "No virtualenv found. Run:  uv venv --python 3.13 .venv; uv pip install --python .venv\Scripts\python.exe -e ."
        exit 1
    }

    # ffmpeg from winget doesn't always land on a fresh process's PATH.
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
        $found = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($found) { $env:Path = "$($found.DirectoryName);$env:Path" }
    }

    # Load secrets from .env if present (ANTHROPIC_API_KEY, OPENROUTER_API_KEY, HF_TOKEN).
    $envFile = Join-Path $PSScriptRoot ".env"
    if (Test-Path $envFile) {
        Get-Content $envFile | ForEach-Object {
            if ($_ -match '^\s*([^#=\s][^=]*)\s*=\s*(.*)\s*$') {
                Set-Item -Path "Env:$($matches[1].Trim())" -Value $matches[2].Trim().Trim('"').Trim("'")
            }
        }
    }

    $proc = Start-Process -FilePath $python `
        -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port",$env:PORT `
        -WorkingDirectory $PSScriptRoot `
        -RedirectStandardOutput "$PSScriptRoot\.server.stdout.log" `
        -RedirectStandardError "$PSScriptRoot\.server.stderr.log" `
        -WindowStyle Hidden -PassThru
    $proc.Id | Out-File -FilePath $pidFile -Encoding ascii
    Start-Sleep -Seconds 2
}

Start-Process "http://127.0.0.1:$($env:PORT)"
