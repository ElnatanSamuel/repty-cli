from __future__ import annotations
import os
from pathlib import Path
from typing import Optional

BASH_MARK_START = "# >>> cmdvault bash hook >>>"
BASH_MARK_END = "# <<< cmdvault bash hook <<<"
ZSH_MARK_START = "# >>> cmdvault zsh hook >>>"
ZSH_MARK_END = "# <<< cmdvault zsh hook <<<"
PS_MARK_START = "# >>> cmdvault powershell hook >>>"
PS_MARK_END = "# <<< cmdvault powershell hook <<<"


def bash_snippet() -> str:
    body = (
        r"""
# CmdVault: capture commands and log after execution
# Only run in interactive shells
case $- in
  *i*) ;;
  *) return ;;
esac

_cmdvault_preexec() {
  CMDVAULT_LAST_CMD="$BASH_COMMAND"
  CMDVAULT_LAST_CWD="$PWD"
}
trap '_cmdvault_preexec' DEBUG

_cmdvault_postexec() {
  local exit_code=$?
  # Retrieve the exact typed line (including comments) from history
  local cmd=""
  # Prefer fc: prints last command without numbers
  if command -v fc >/dev/null 2>&1; then
    cmd="$(fc -ln -1 2>/dev/null)"
  fi
  if [[ -z "$cmd" ]]; then
    # Fallback to history 1 and strip leading number/time if present
    local hist="$(history 1 2>/dev/null)"
    if [[ -n "$hist" ]]; then
      cmd="$(printf '%s' "$hist" | sed -E 's/^\\s*[0-9]+\\s*//')"
    else
      cmd="$CMDVAULT_LAST_CMD"
    fi
  fi
  # Do not log empty commands or cmdvault itself
  if [[ -n "$cmd" && "$cmd" != cmdvault\ * && "$cmd" != source\ * && "$cmd" != .\ * ]]; then
    cmdvault log --command "$cmd" --cwd "$CMDVAULT_LAST_CWD" --exit "$exit_code" >/dev/null 2>&1
  fi
}
if [[ -n "$PROMPT_COMMAND" ]]; then
  case "$PROMPT_COMMAND" in
    *_cmdvault_postexec*) ;;
    *) PROMPT_COMMAND="_cmdvault_postexec; $PROMPT_COMMAND" ;;
  esac
else
  PROMPT_COMMAND="_cmdvault_postexec"
fi
"""
    ).strip() + "\n"
    return BASH_MARK_START + "\n" + body + BASH_MARK_END + "\n"


def zsh_snippet() -> str:
    body = (
        r"""
# CmdVault: capture commands via zsh hooks
# Only run in interactive shells
[[ -o interactive ]] || return

autoload -Uz add-zsh-hook

_cmdvault_preexec() {
  CMDVAULT_LAST_CMD="$1"
  CMDVAULT_LAST_CWD="$PWD"
}

_cmdvault_precmd() {
  local exit_code=$?
  local cmd
  # Use history to capture the literal typed line including comments
  if command -v fc >/dev/null 2>&1; then
    cmd=$(fc -ln -1 2>/dev/null)
  fi
  if [[ -z "$cmd" ]]; then
    cmd="$CMDVAULT_LAST_CMD"
  fi
  if [[ -n "$cmd" && "$cmd" != cmdvault\ * && "$cmd" != source\ * && "$cmd" != .\ * ]]; then
    cmdvault log --command "$cmd" --cwd "$CMDVAULT_LAST_CWD" --exit "$exit_code" >/dev/null 2>&1
  fi
}

# Avoid duplicate hooks if the file is sourced multiple times
typeset -ga preexec_functions precmd_functions
if [[ -z "${preexec_functions[(r)_cmdvault_preexec]}" ]]; then
  add-zsh-hook preexec _cmdvault_preexec
fi
if [[ -z "${precmd_functions[(r)_cmdvault_precmd]}" ]]; then
  add-zsh-hook precmd _cmdvault_precmd
fi
"""
    ).strip() + "\n"
    return ZSH_MARK_START + "\n" + body + ZSH_MARK_END + "\n"


def _add_snippet(file_path: Path, snippet: str, mark_start: str, mark_end: str) -> bool:
    content = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    if mark_start in content and mark_end in content:
        return False  # already installed
    with open(file_path, "a", encoding="utf-8") as f:
        if content and not content.endswith("\n"):
            f.write("\n")
        f.write("\n" + snippet + "\n")
    return True


def _remove_snippet(file_path: Path, mark_start: str, mark_end: str) -> bool:
    if not file_path.exists():
        return False
    content = file_path.read_text(encoding="utf-8")
    start = content.find(mark_start)
    end = content.find(mark_end)
    if start == -1 or end == -1:
        return False
    end += len(mark_end)
    # remove the block plus surrounding newlines
    new_content = (content[:start].rstrip("\n") + "\n" + content[end:].lstrip("\n")).strip("\n") + "\n"
    file_path.write_text(new_content, encoding="utf-8")
    return True


def _ps_profile_paths() -> list[Path]:
    # Support both classic Documents and OneDrive\Documents locations
    home = Path.home()
    paths: list[Path] = [
        home / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
        home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
    ]
    one = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer") or os.environ.get("OneDriveCommercial")
    if one:
        one_docs = Path(one) / "Documents"
        paths.extend([
            one_docs / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
            one_docs / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
        ])
    return paths


def powershell_snippet() -> str:
    body = (
        r"""
if (-not (Get-Command cmdvault -ErrorAction SilentlyContinue)) { return }
if (-not (Get-Variable -Name CmdVaultLastHistoryId -Scope Global -ErrorAction SilentlyContinue)) { $global:CmdVaultLastHistoryId = 0 }

function global:__CmdVault_LogLast {
  try {
    $hist = Get-History -Count 1 -ErrorAction SilentlyContinue
    if ($null -ne $hist -and $hist.Id -ne $global:CmdVaultLastHistoryId) {
      $cmd = $hist.CommandLine
      $cwd = (Get-Location).Path
      if ($null -ne $LASTEXITCODE) { $exit = $LASTEXITCODE } elseif ($?) { $exit = 0 } else { $exit = 1 }
      if ($cmd -and -not $cmd.StartsWith('cmdvault ')) {
        cmdvault log --command $cmd --cwd $cwd --exit $exit | Out-Null
      }
      $global:CmdVaultLastHistoryId = $hist.Id
    }
  } catch { }
}

if (Test-Path Function:\Prompt) {
  $oldPrompt = (Get-Command Prompt -CommandType Function).ScriptBlock
  function global:Prompt {
    & $oldPrompt
    __CmdVault_LogLast
  }
} else {
  function global:Prompt {
    __CmdVault_LogLast
    'PS ' + (Get-Location) + '> '
  }
}
"""
    ).strip() + "\n"
    return PS_MARK_START + "\n" + body + PS_MARK_END + "\n"


def install(bashrc: Optional[Path] = None, zshrc: Optional[Path] = None) -> str:
    home = Path.home()
    bashrc = bashrc or home / ".bashrc"
    zshrc = zshrc or home / ".zshrc"
    msgs = []
    if _add_snippet(bashrc, bash_snippet(), BASH_MARK_START, BASH_MARK_END):
        msgs.append(f"Installed Bash hook in {bashrc}")
    else:
        msgs.append(f"Bash hook already present in {bashrc}")
    if _add_snippet(zshrc, zsh_snippet(), ZSH_MARK_START, ZSH_MARK_END):
        msgs.append(f"Installed Zsh hook in {zshrc}")
    else:
        msgs.append(f"Zsh hook already present in {zshrc}")
    # PowerShell profiles (Windows PowerShell and PowerShell 7+)
    for p in _ps_profile_paths():
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if _add_snippet(p, powershell_snippet(), PS_MARK_START, PS_MARK_END):
                msgs.append(f"Installed PowerShell hook in {p}")
            else:
                msgs.append(f"PowerShell hook already present in {p}")
        except Exception as e:
            msgs.append(f"PowerShell hook install failed for {p}: {e}")
    return "\n".join(msgs)


def uninstall(bashrc: Optional[Path] = None, zshrc: Optional[Path] = None) -> str:
    home = Path.home()
    bashrc = bashrc or home / ".bashrc"
    zshrc = zshrc or home / ".zshrc"
    msgs = []
    if _remove_snippet(bashrc, BASH_MARK_START, BASH_MARK_END):
        msgs.append(f"Removed Bash hook from {bashrc}")
    if _remove_snippet(zshrc, ZSH_MARK_START, ZSH_MARK_END):
        msgs.append(f"Removed Zsh hook from {zshrc}")
    for p in _ps_profile_paths():
        try:
            if _remove_snippet(p, PS_MARK_START, PS_MARK_END):
                msgs.append(f"Removed PowerShell hook from {p}")
        except Exception:
            pass
    return "\n".join(msgs) if msgs else "No hooks found to remove."
