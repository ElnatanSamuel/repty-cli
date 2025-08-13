import argparse
import json
import sys
import re
import os
import threading
import time
import subprocess
import shutil
import importlib
from itertools import cycle
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional

from . import db, ai as ai_mod, config as cfg, hooks as hooks_mod


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return (text[: max(0, width - 1)].rstrip() + "…")


def _render_table(rows) -> None:
    # Render a compact table: id | timestamp | exit | tags | command
    # Determine terminal width to size the command column
    try:
        term_width = shutil.get_terminal_size(fallback=(100, 20)).columns
    except Exception:
        term_width = 100

    # Fixed column widths
    id_w = 6
    ts_w = 19  # e.g. 2025-08-13 07:12:34
    ec_w = 4
    # Tags width dynamic but capped
    tags_list = []
    for r in rows:
        tags = r.get("tags") if isinstance(r, dict) else r["tags"]
        tags_list.append(f"[{tags}]" if tags else "")
    tag_w = min(max((len(t) for t in tags_list), default=0), 24)
    # Compute command width
    sep_w = len(" | ") * 4
    cmd_w = max(20, term_width - (id_w + ts_w + ec_w + tag_w + sep_w))

    header = f"{'id':>{id_w}} | {'timestamp':<{ts_w}} | {'exit':>{ec_w}} | {'tags':<{tag_w}} | command"
    # Styled header if TTY
    if sys.stdout.isatty():
        # Bright cyan header
        header_print = f"\033[96m{header}\033[0m"
    else:
        header_print = header
    print(header_print)
    print("-" * min(term_width, len(header)))
    for i, r in enumerate(rows):
        # sqlite3.Row supports mapping access
        rid = r["id"]
        ts = (r["timestamp"] or "")[:ts_w]
        ec = r["exit_code"] if r["exit_code"] is not None else "?"
        tags = tags_list[i]
        cmd = r["command"] or ""
        # Style exit code: green 0, red non-zero, yellow unknown
        if sys.stdout.isatty():
            if ec == 0:
                ec_str = f"\033[92m{ec:>{ec_w}}\033[0m"
            elif ec == "?":
                ec_str = f"\033[93m{ec:>{ec_w}}\033[0m"
            else:
                ec_str = f"\033[91m{ec:>{ec_w}}\033[0m"
            tags_str = f"\033[2m{tags:<{tag_w}}\033[0m" if tags else f"{tags:<{tag_w}}"
        else:
            ec_str = f"{ec:>{ec_w}}"
            tags_str = f"{tags:<{tag_w}}"
        line = f"{rid:>{id_w}} | {ts:<{ts_w}} | {ec_str} | {tags_str} | {_truncate(cmd, cmd_w)}"
        print(line)


def _maybe_prompt_copy(rows) -> None:
    """If interactive, prompt the user to copy a command by id to clipboard."""
    if not rows or not sys.stdout.isatty():
        return
    try:
        resp = input("Copy a command by id (Enter to skip): ").strip()
    except EOFError:
        return
    if not resp:
        return
    try:
        target_id = int(resp)
    except Exception:
        print("Invalid id.")
        return
    # Try to find in current results first
    chosen = None
    for r in rows:
        try:
            if int(r["id"]) == target_id:
                chosen = r
                break
        except Exception:
            continue
    if chosen is None:
        # Fallback to DB lookup
        try:
            with db.connect() as conn:
                chosen = db.get_by_id(conn, target_id)
        except Exception:
            chosen = None
    if not chosen:
        print(f"No command with id={target_id}.")
        return
    text = (chosen["command"] or "")
    if _copy_to_clipboard(text):
        print(f"Copied command #{target_id} to clipboard.")
    else:
        print(text)
        sys.stderr.write("Clipboard not available; printed command instead.\n")


def _copy_to_clipboard(text: str) -> bool:
    """Copy text to clipboard on Windows/macOS/Linux without extra deps.
    Returns True on success, False otherwise.
    """
    try:
        data = text or ""
        # Windows or WSL: try clip.exe first
        if os.name == "nt" or os.environ.get("WSL_DISTRO_NAME"):
            exe = shutil.which("clip.exe") or shutil.which("clip")
            if exe:
                subprocess.run([exe], input=data, text=True, check=True)
                return True
            # PowerShell fallback
            pwsh = shutil.which("pwsh") or shutil.which("powershell") or shutil.which("powershell.exe")
            if pwsh:
                cmd = [pwsh, "-NoProfile", "-Command", "Set-Clipboard -Value $input"]
                subprocess.run(cmd, input=data, text=True, check=True)
                return True
        # macOS
        if sys.platform == "darwin":
            exe = shutil.which("pbcopy")
            if exe:
                subprocess.run([exe], input=data, text=True, check=True)
                return True
        # Linux/BSD: try Wayland first, then X11 tools
        for candidate in ("wl-copy", "xclip", "xsel"):
            exe = shutil.which(candidate)
            if not exe:
                continue
            if candidate == "xclip":
                subprocess.run([exe, "-selection", "clipboard"], input=data, text=True, check=True)
            elif candidate == "xsel":
                subprocess.run([exe, "--clipboard", "--input"], input=data, text=True, check=True)
            else:  # wl-copy
                subprocess.run([exe], input=data, text=True, check=True)
            return True
    except Exception:
        return False
    return False


class _Spinner:
    def __init__(self, message: str = "Working...") -> None:
        self.message = message
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self):
        # Only show spinner on a TTY to avoid polluting piped output
        if sys.stderr.isatty():
            self._start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop_spin()
        return False

    def _start(self) -> None:
        frames = cycle("|/-\\")

        def run():
            while not self._stop.is_set():
                try:
                    frame = next(frames)
                    sys.stderr.write(f"\r{self.message} {frame}")
                    sys.stderr.flush()
                    time.sleep(0.1)
                except Exception:
                    break

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def _stop_spin(self) -> None:
        if self._thread:
            self._stop.set()
            try:
                self._thread.join(timeout=0.2)
            except Exception:
                pass
        # Clear the spinner line
        if sys.stderr.isatty():
            try:
                sys.stderr.write("\r" + " " * (len(self.message) + 2) + "\r")
                sys.stderr.flush()
            except Exception:
                pass


def _desktop_dir() -> Optional[Path]:
    """Best-effort Desktop directory across Windows/macOS/Linux/WSL."""
    try:
        home = Path.home()
    except Exception:
        return None
    candidates = [home / "Desktop"]
    # Handle OneDrive variants on Windows
    try:
        for p in home.iterdir():
            if p.is_dir() and p.name.startswith("OneDrive"):
                desk = p / "Desktop"
                if desk.exists():
                    candidates.append(desk)
    except Exception:
        pass
    for c in candidates:
        try:
            if c.exists():
                return c
        except Exception:
            continue
    return None


def _default_export_path(fmt: str = "md") -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"repty-export-{ts}.{fmt}"
    desk = _desktop_dir()
    base = desk if desk else Path.cwd()
    return str(base / name)


# Simple ASCII banner for onboarding/setup
def _banner() -> None:
    art = (
        "\n"
        "  ██████  ███████ ██████  ████████ ██   ██\n"
        "  ██   ██ ██      ██   ██    ██    ██   ██\n"
        "  ██████  █████   ██████     ██    ███████\n"
        "  ██   ██ ██      ██         ██       ██\n"
        "  ██   ██ ███████ ██         ██       ██\n"
        "\n    Repty — Search your shell history (FTS + AI)\n"
    )
    try:
        print(art)
    except Exception:
        pass


# State file to persist the last logged command id (legacy for prior tag logic)
STATE_FILE = Path.home() / ".repty_state.json"


def _read_state_last_id() -> Optional[int]:
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            val = int(data.get("last_command_id")) if data and "last_command_id" in data else None
            return val
    except Exception:
        return None
    return None


def _write_state_last_id(cmd_id: int, cwd: str = "") -> None:
    try:
        payload = {
            "last_command_id": int(cmd_id),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cwd": cwd or "",
        }
        STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


STOPWORDS = {
    "a", "an", "the", "of", "for", "to", "in", "on", "with", "and", "or",
    "command", "commands", "please", "find", "show", "me", "file", "files",
}


def _clean_query(raw: str) -> str:
    terms = re.findall(r"\w+", (raw or "").lower())
    base = []
    for t in terms:
        if len(t) < 3 or t in STOPWORDS:
            continue
        variants = {t}
        if len(t) >= 5 and t.endswith("ing"):
            stem = t[:-3]
            variants.add(stem)
            variants.add(stem + "e")
        if len(t) >= 4 and t.endswith("s"):
            variants.add(t[:-1])
        for v in sorted(variants):
            if v not in base:
                base.append(v)
    return " ".join(base)


def cmd_log(args: argparse.Namespace) -> int:
    command = args.command
    cwd = args.cwd
    exit_code = args.exit
    with db.connect() as conn:
        # parse inline #tag labels
        inline = db.parse_inline_tags(command)
        tags = db.ensure_tags_string(args.tags, inline)
        new_id = db.add_command(conn, command=command, cwd=cwd, exit_code=exit_code, tags=tags)
        _write_state_last_id(new_id, cwd)
    return 0


def _ensure_fts5_available() -> None:
    """Ensure SQLite FTS5 is available; try to auto-install pysqlite3-binary if missing.

    Raises RuntimeError if FTS5 cannot be ensured.
    """
    try:
        # Attempt to open DB and create schema (will raise if FTS5 missing)
        with db.connect():
            return
    except Exception as e:
        msg = str(e).lower()
        if "fts5" not in msg:
            # Not an FTS5 error; re-raise
            raise
    # Try to install pysqlite3-binary
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "pysqlite3-binary"], check=True)
        # Make db use pysqlite3 and reload module
        os.environ["REPTY_SQLITE_BACKEND"] = "pysqlite3"
        importlib.reload(db)
        # Try again
        with db.connect():
            return
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Failed to ensure FTS5 support: {e}")


def cmd_setup(args: argparse.Namespace) -> int:
    """Set up Repty for immediate use: ensure deps, install hooks, configure AI key."""
    _banner()
    # 1) Ensure FTS5 is available (auto-install pysqlite3-binary if needed)
    try:
        with _Spinner("Checking SQLite/FTS5..."):
            _ensure_fts5_available()
    except Exception as e:
        sys.stderr.write(f"Failed to set up SQLite/FTS5: {e}\n")
        return 1

    # 2) Install shell hooks unless skipped
    if not getattr(args, "no_hooks", False):
        try:
            msg = hooks_mod.install()
            print(msg)
        except Exception as e:
            sys.stderr.write(f"Hook installation failed: {e}\n")
            if not getattr(args, "yes", False):
                try:
                    resp = input("Continue without hooks? [y/N]: ").strip().lower()
                except EOFError:
                    resp = "n"
                if resp not in ("y", "yes"):
                    return 1

    # 3) Configure AI key if provided (optional)
    if getattr(args, "key", None):
        try:
            c = cfg.load_config()
            c["gemini_api_key"] = args.key
            cfg.save_config(c)
            print("Gemini API key saved to ~/.repty_config.json")
        except Exception as e:
            sys.stderr.write(f"Failed to save config: {e}\n")

    print("Setup complete. Open a new shell or reload your profile to start using Repty.")
    return 0


def _suggest_source_instructions() -> None:
    try:
        shell = os.environ.get("SHELL", "")
        if "zsh" in shell:
            print("Next: run 'source ~/.zshrc' or open a new terminal.")
        elif "bash" in shell:
            print("Next: run 'source ~/.bashrc' or open a new terminal.")
        else:
            print("Next: open a new terminal or source your shell profile (~/.bashrc or ~/.zshrc).")
    except Exception:
        print("Next: open a new terminal or source your shell profile (~/.bashrc or ~/.zshrc).")


def onboarding_wizard() -> int:
    """Interactive first-run experience shown when 'repty' is run with no args."""
    _banner()
    print("Let's get you set up. Press Enter to accept defaults.")

    # 1) Install shell hooks
    try:
        resp = input("Install shell logging hooks now? [Y/n]: ").strip().lower()
    except EOFError:
        resp = "y"
    if resp in ("", "y", "yes"):  # default yes
        try:
            msg = hooks_mod.install()
            print(msg)
            _suggest_source_instructions()
        except Exception as e:
            sys.stderr.write(f"Hook installation failed: {e}\n")
    else:
        print("Skipping hook installation. You can run 'repty install-hooks' later.")

    # 2) Optionally set AI key
    try:
        resp = input("Configure Gemini AI now? [y/N]: ").strip().lower()
    except EOFError:
        resp = "n"
    if resp in ("y", "yes"):
        try:
            api_key = input("Enter your Gemini API key (will be saved to ~/.repty_config.json): ").strip()
        except EOFError:
            api_key = ""
        if api_key:
            try:
                c = cfg.load_config()
                c["gemini_api_key"] = api_key
                cfg.save_config(c)
                print("Gemini API key saved to ~/.repty_config.json")
            except Exception as e:
                sys.stderr.write(f"Failed to save config: {e}\n")
        else:
            print("No key entered. You can set it later via: repty config set-key YOUR_KEY")
    else:
        print("You can enable AI later via: repty config set-key YOUR_KEY")

    print("\nAll set! Try running some commands, then:")
    print("  repty search git log")
    return 0

def cmd_search(args: argparse.Namespace) -> int:
    # Accept multi-word natural language queries
    raw = " ".join(args.query) if isinstance(args.query, list) else str(args.query)
    match_q = db.build_fts_query(raw, mode="and", prefix=True)
    with _Spinner("Searching..."):
        with db.connect() as conn:
            tokens = _clean_query(raw).split()
            tag_rows = db.search_by_tags(conn, tokens, limit=args.limit or 50)
            fts_rows = db.search_fts(conn, match_q or raw, limit=max((args.limit or 20) * 3, 50))
    # De-duplicate by id, prioritize tag matches first
    by_id = {}
    for r in tag_rows:
        by_id[r["id"]] = r
    for r in fts_rows:
        if r["id"] not in by_id:
            by_id[r["id"]] = r
    merged = list(by_id.values())
    # Exclude internal commands from results
    def _cmd_text(row) -> str:
        try:
            return ((row.get("command") if isinstance(row, dict) else row["command"]) or "").strip()
        except Exception:
            return ""
    merged = [r for r in merged if not (_cmd_text(r).startswith("repty ") or _cmd_text(r).startswith(". ") or _cmd_text(r).startswith("source "))]
    # Re-rank within merged: tag similarity first, then favorites (only if also matching)
    def tag_score(r) -> tuple:
        tagstr = (r["tags"] or "").lower()
        parts = []
        for p in tagstr.split(","):
            p = p.strip()
            if not p:
                continue
            parts.append(p)
            if p.startswith("desc:"):
                parts.append(p[5:])
        has_match = any(
            any(t in tp or tp in t for tp in parts)
            for t in tokens
        ) if tokens else False
        fav = any(tp == "favorite" for tp in parts)
        return (1 if has_match else 0, 1 if (has_match and fav) else 0)
    merged.sort(key=tag_score, reverse=True)
    rows = merged[: args.limit] if args.limit else merged
    _render_table(rows)
    if getattr(args, "copy_first", False) and rows:
        if _copy_to_clipboard(rows[0]["command"] or ""):
            print("Copied top result to clipboard.")
        else:
            print("Could not copy to clipboard on this system.")
    _maybe_prompt_copy(rows)
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        rows = db.recent(conn, limit=args.limit)
        _render_table(rows)
        if getattr(args, "copy_first", False) and rows:
            if _copy_to_clipboard(rows[0]["command"] or ""):
                print("Copied top result to clipboard.")
            else:
                print("Could not copy to clipboard on this system.")
        _maybe_prompt_copy(rows)
    return 0


def cmd_save(args: argparse.Namespace) -> int:
    description = args.description.strip()
    command = args.command
    cwd = args.cwd
    # Save as a favorite with tags: favorite, desc:<description>
    tags = db.ensure_tags_string(None, ["favorite", f"desc:{description}"])
    with db.connect() as conn:
        new_id = db.add_command(conn, command=command, cwd=cwd, exit_code=None, tags=tags)
    print(f"Saved favorite #{new_id}: {description}")
    return 0


def cmd_copy(args: argparse.Namespace) -> int:
    cmd_id = int(args.id)
    with db.connect() as conn:
        row = db.get_by_id(conn, cmd_id)
    if not row:
        sys.stderr.write(f"No command found with id={cmd_id}\n")
        return 1
    text = row["command"] or ""
    if not text.strip():
        sys.stderr.write("Command is empty.\n")
        return 1
    if _copy_to_clipboard(text):
        print(f"Copied command #{cmd_id} to clipboard.")
        return 0
    # Fallback: print to stdout so the user can copy manually
    print(text)
    sys.stderr.write("Clipboard not available; printed command instead.\n")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    # Determine output path: if not provided or --prompt set, ask user.
    out_path = args.file if getattr(args, "file", None) else None
    fmt = "csv" if (out_path or "").lower().endswith(".csv") else "md"
    if args.prompt or not out_path:
        default_path = _default_export_path(fmt)
        try:
            user_in = input(f"Save export to file [{default_path}]: ").strip()
        except EOFError:
            user_in = ""
        out_path = user_in or default_path
    with db.connect() as conn:
        out = db.export_to(conn, out_path, limit=args.limit)
    print(f"Exported to {out}")
    return 0


def cmd_install_hooks(args: argparse.Namespace) -> int:
    msg = hooks_mod.install()
    print(msg)
    return 0


def cmd_uninstall_hooks(args: argparse.Namespace) -> int:
    msg = hooks_mod.uninstall()
    print(msg)
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Completely remove Repty hooks and local data files.

    This will:
      - Remove Bash/Zsh/PowerShell hook blocks from shell profiles
      - Delete the local SQLite DB (~/.repty.db) and its -wal/-shm files (and legacy ~/.cmdvault.db*)
      - Delete the config file (~/.repty_config.json) (and legacy ~/.cmdvault_config.json)
      - Delete the state file (~/.repty_state.json) (and legacy ~/.cmdvault_state.json)
    """
    if not getattr(args, "yes", False):
        try:
            resp = input(
                "This will remove Repty hooks and DELETE your local data files. Proceed? [y/N]: "
            ).strip().lower()
        except EOFError:
            resp = "n"
        if resp not in ("y", "yes"):
            print("Cancelled.")
            return 1

    msgs = []
    # 1) Remove hooks from all supported shells
    try:
        rm_msg = hooks_mod.uninstall()
        if rm_msg:
            msgs.append(rm_msg)
    except Exception as e:
        msgs.append(f"Hook removal error: {e}")

    # 2) Delete data/config/state files (including legacy paths)
    paths = []
    try:
        db_path = Path(db.get_db_path())
        paths.append(db_path)
        # Also remove WAL/SHM if present
        wal = Path(str(db_path) + "-wal")
        shm = Path(str(db_path) + "-shm")
        paths.extend([wal, shm])
    except Exception:
        pass
    # Legacy files
    try:
        legacy_db = Path.home() / ".cmdvault.db"
        paths.append(legacy_db)
        paths.append(Path(str(legacy_db) + "-wal"))
        paths.append(Path(str(legacy_db) + "-shm"))
    except Exception:
        pass
    try:
        paths.append(Path.home() / ".cmdvault_config.json")
    except Exception:
        pass
    try:
        paths.append(Path.home() / ".cmdvault_state.json")
    except Exception:
        pass
    try:
        paths.append(cfg.get_config_path())
    except Exception:
        pass
    try:
        paths.append(STATE_FILE)
    except Exception:
        pass

    for p in paths:
        try:
            if isinstance(p, str):
                p = Path(p)
            if p.exists():
                p.unlink()
                msgs.append(f"Deleted {p}")
        except Exception as e:
            msgs.append(f"Failed to delete {p}: {e}")

    # 3) Final message and guidance to uninstall package
    if msgs:
        print("\n".join(msgs))
    print("Repty local data removed.")
    print("Optional: to remove the Python package itself, run: pip uninstall repty")
    return 0


def cmd_ai(args: argparse.Namespace) -> int:
    raw = " ".join(args.query) if isinstance(args.query, list) else str(args.query)
    try:
        with _Spinner("AI searching..."):
            results = ai_mod.ai_search(raw, limit=args.limit)
        # If AI returns no results, fall back to local search so the user sees something
        if not results:
            sys.stderr.write("No AI-ranked matches. Searching locally...\n")
            cleaned = _clean_query(raw)
            match_q = db.build_fts_query(cleaned or raw, mode="and", prefix=True)
            with _Spinner("Searching locally..."):
                with db.connect() as conn:
                    tokens = _clean_query(raw).split()
                    tag_rows = db.search_by_tags(conn, tokens, limit=args.limit or 50)
                    rows_fts = db.search_fts(conn, match_q or cleaned or raw, limit=args.limit or 50)
            # Merge with tag priority and re-rank
            by_id = {r["id"]: r for r in tag_rows}
            for r in rows_fts:
                if r["id"] not in by_id:
                    by_id[r["id"]] = r
            merged = list(by_id.values())
            # Exclude internal commands from results
            merged = [r for r in merged if not (_cmd_text(r).startswith("repty ") or _cmd_text(r).startswith(". ") or _cmd_text(r).startswith("source "))]
            def tag_score(r) -> tuple:
                tagstr = (r["tags"] or "").lower()
                parts = []
                for p in tagstr.split(","):
                    p = p.strip()
                    if not p:
                        continue
                    parts.append(p)
                    if p.startswith("desc:"):
                        parts.append(p[5:])
                has_match = any(
                    any(t in tp or tp in t for tp in parts)
                    for t in tokens
                ) if tokens else False
                fav = any(tp == "favorite" for tp in parts)
                return (1 if has_match else 0, 1 if (has_match and fav) else 0)
            merged.sort(key=tag_score, reverse=True)
            # Apply action-token filter to local results to avoid irrelevant commands
            _GENERIC = {
                "git", "bash", "zsh", "fish", "python", "pip", "pipx", "node", "npm", "pnpm",
                "yarn", "docker", "kubectl", "kube", "ssh", "curl", "wget",
            }
            action_tokens = [t for t in tokens if t not in _GENERIC and len(t) >= 4]
            if action_tokens:
                filtered = [r for r in merged if any(t in (_cmd_text(r).lower()) for t in action_tokens)]
                if not filtered:
                    sys.stderr.write("No local commands matched those keywords.\n")
                    _render_table([])
                    return 0
                rows = filtered
            else:
                rows = merged
            rows = rows[: args.limit] if args.limit else rows
            _render_table(rows)
            if getattr(args, "copy_first", False) and rows:
                if _copy_to_clipboard(rows[0]["command"] or ""):
                    print("Copied top result to clipboard.")
                else:
                    print("Could not copy to clipboard on this system.")
            _maybe_prompt_copy(rows)
            return 0
        # Show a concise table first
        rows = [item["row"] for item in results]
        _render_table(rows)
        if getattr(args, "copy_first", False) and rows:
            if _copy_to_clipboard(rows[0]["command"] or ""):
                print("Copied top result to clipboard.")
            else:
                print("Could not copy to clipboard on this system.")
        _maybe_prompt_copy(rows)
        # Then, if any reasoning present, print under the table
        for item in results:
            reason = item.get("reason")
            score = item.get("score")
            if reason:
                print(f"- id {item['row']['id']}: {reason}")
    except Exception as e:
        # Fallback to local search
        sys.stderr.write(f"AI search failed ({e}). Falling back to local search...\n")
        cleaned = _clean_query(raw)
        match_q = db.build_fts_query(cleaned or raw, mode="and", prefix=True)
        with _Spinner("Searching locally..."):
            with db.connect() as conn:
                tokens = _clean_query(raw).split()
                tag_rows = db.search_by_tags(conn, tokens, limit=args.limit or 50)
                rows_fts = db.search_fts(conn, match_q or cleaned or raw, limit=args.limit or 50)
        # Merge with tag priority and re-rank
        by_id = {r["id"]: r for r in tag_rows}
        for r in rows_fts:
            if r["id"] not in by_id:
                by_id[r["id"]] = r
        merged = list(by_id.values())
        def tag_score(r) -> tuple:
            tagstr = (r["tags"] or "").lower()
            parts = []
            for p in tagstr.split(","):
                p = p.strip()
                if not p:
                    continue
                parts.append(p)
                if p.startswith("desc:"):
                    parts.append(p[5:])
            has_match = any(
                any(t in tp or tp in t for tp in parts)
                for t in tokens
            ) if tokens else False
            fav = any(tp == "favorite" for tp in parts)
            return (1 if has_match else 0, 1 if fav else 0)
        merged.sort(key=tag_score, reverse=True)
        rows = merged[: args.limit] if args.limit else merged
        _render_table(rows)
        return 0
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    if args.action == "show":
        c = cfg.load_config()
        # Avoid printing the full key accidentally
        c_safe = dict(c)
        if c_safe.get("gemini_api_key"):
            c_safe["gemini_api_key"] = "***SET***"
        print(json.dumps(c_safe, indent=2))
        return 0
    elif args.action == "set-key":
        c = cfg.load_config()
        c["gemini_api_key"] = args.key
        cfg.save_config(c)
        print("Gemini API key saved to ~/.repty_config.json")
        return 0
    else:
        print("Unknown config action")
        return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="repty",
        description="Repty: Command history search with local FTS and optional Gemini AI",
        epilog=(
            "Examples:\n"
            "  repty search git reset\n"
            "  repty search docker build --limit 10\n"
            "  repty recent --limit 5\n"
            "  repty config show\n"
            "  repty config set-key YOUR_GEMINI_API_KEY\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=False)

    # Hidden/internal: log (used by shell hooks)
    sp = sub.add_parser("log", help=argparse.SUPPRESS)
    sp.add_argument("--command", required=True)
    sp.add_argument("--cwd", required=True)
    sp.add_argument("--exit", type=int, required=True)
    sp.add_argument("--tags", default=None)
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("search", help="Local FTS search (multi-word OK)")
    sp.add_argument("query", nargs="+")
    sp.add_argument("--limit", type=int, default=cfg.load_config().get("default_search_limit", 5))
    sp.add_argument("--copy-first", action="store_true", help="Copy the top result to clipboard")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser(
        "ai",
        help="Gemini AI search (natural language)",
        description=(
            "Gemini AI search with local fallback.\n"
            "Requires an API key. Set it via:\n"
            "  repty config set-key YOUR_GEMINI_API_KEY\n"
        ),
    )
    sp.add_argument("query", nargs="+")
    sp.add_argument("--limit", type=int, default=cfg.load_config().get("default_search_limit", 5))
    sp.add_argument("--copy-first", action="store_true", help="Copy the top result to clipboard")
    sp.set_defaults(func=cmd_ai)

    sp = sub.add_parser("recent", help="Show most recent commands")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--copy-first", action="store_true", help="Copy the top result to clipboard")
    sp.set_defaults(func=cmd_recent)

    sp = sub.add_parser("save", help="Save a favorite manually")
    sp.add_argument("description")
    sp.add_argument("command")
    sp.add_argument("--cwd", default="")
    sp.set_defaults(func=cmd_save)

    # Copy command by id
    sp = sub.add_parser("copy", help="Copy command text to clipboard by id")
    sp.add_argument("id", type=int)
    sp.set_defaults(func=cmd_copy)

    sp = sub.add_parser("export", help="Export history to Markdown or CSV")
    sp.add_argument("file", nargs="?", help="Destination file path (.md or .csv). If omitted, you will be prompted.")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--prompt", action="store_true", help="Prompt for destination path (default if file is omitted)")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("install-hooks", help="Install Bash/Zsh logging hooks")
    sp.set_defaults(func=cmd_install_hooks)

    sp = sub.add_parser("uninstall-hooks", help="Remove Bash/Zsh logging hooks")
    sp.set_defaults(func=cmd_uninstall_hooks)

    sp = sub.add_parser("uninstall", help="Completely remove hooks and local data files")
    sp.add_argument("-y", "--yes", action="store_true", help="Do not prompt for confirmation")
    sp.set_defaults(func=cmd_uninstall)

    sp = sub.add_parser("setup", help="Ensure deps (FTS5), install hooks, and optionally set AI key")
    sp.add_argument("-y", "--yes", action="store_true", help="Answer yes to prompts when possible")
    sp.add_argument("--no-hooks", action="store_true", help="Skip installing shell hooks")
    sp.add_argument("--key", help="Set Gemini API key during setup")
    sp.set_defaults(func=cmd_setup)

    sp = sub.add_parser(
        "config",
        help="Config (show, set-key)",
        description=(
            "Manage configuration.\n"
            "Examples:\n"
            "  repty config show\n"
            "  repty config set-key YOUR_GEMINI_API_KEY\n"
        ),
    )
    sp.add_argument("action", choices=["show", "set-key"])
    sp.add_argument("key", nargs="?")
    sp.set_defaults(func=cmd_config)

    return p


def main(argv: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # If no subcommand provided, show onboarding wizard in interactive terminals
    if getattr(args, "cmd", None) is None:
        if sys.stdout.isatty():
            return onboarding_wizard()
        # Non-interactive: print help
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
