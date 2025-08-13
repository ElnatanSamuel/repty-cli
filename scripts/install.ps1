# Requires: PowerShell 5+ and Python 3.9+
$ErrorActionPreference = 'Stop'

function Say($msg){ Write-Host "[repty] $msg" -ForegroundColor Green }
function Warn($msg){ Write-Host "[repty] $msg" -ForegroundColor Yellow }
function Err($msg){ Write-Host "[repty] $msg" -ForegroundColor Red }

$repoUrl = 'https://github.com/ElnatanSamuel/repty-cli.git'
$binName = 'repty'

function Ensure-Pipx {
  if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    Warn 'pipx not found; installing with pip --user...'
    if (Get-Command python -ErrorAction SilentlyContinue) {
      python -m pip install --user pipx | Out-Null
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
      py -m pip install --user pipx | Out-Null
    } else {
      Err 'Python not found. Please install Python 3.9+ and re-run.'
      exit 1
    }
  }
}

function Ensure-Path {
  try { pipx ensurepath | Out-Null } catch {}
  # Update current session PATH immediately
  $userLocalBin = Join-Path $HOME '.local\bin'
  if (Test-Path $userLocalBin) {
    if (-not ($env:Path -split ';' | Where-Object { $_ -eq $userLocalBin })) {
      $env:Path = "$userLocalBin;" + $env:Path
    }
  }
  # pipx on Windows typically installs to %USERPROFILE%\.local\bin
}

function Install-App {
  Say "Installing Repty via pipx from $repoUrl ..."
  pipx install --force "git+$repoUrl" | Out-Null
  Say 'Installed.'
}

function Run-App {
  $candidate = Join-Path $HOME '.local\bin\repty.exe'
  if (Test-Path $candidate) {
    & $candidate
    if ($LASTEXITCODE -ne 0) {
      & $candidate setup
    }
    return
  }
  $reptyCmd = Get-Command repty -ErrorAction SilentlyContinue
  if ($reptyCmd) {
    & $reptyCmd.Path
    if ($LASTEXITCODE -ne 0) {
      & $reptyCmd.Path setup
    }
    return
  }
  Warn "Could not locate 'repty' on PATH. Try opening a new terminal or run: $candidate"
}

Say 'Preparing to install Repty (FTS + AI history search)'
Ensure-Pipx
Ensure-Path
Install-App
Say 'Launching Repty onboarding wizard...'
Run-App
Say "Tip: open a new PowerShell or run '. $PROFILE' after hook installation."
