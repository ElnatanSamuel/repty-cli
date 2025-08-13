# repty

Dual-mode terminal command history tool for Bash/Zsh with:

- Local fast search via SQLite FTS5
- Optional Gemini AI search that understands natural language

All data stored locally in `~/.repty.db`. Config stored in `~/.repty_config.json`.

## Quick Start

0) One-line install (handles PATH and launches the wizard)

- macOS/Linux/WSL (Bash/Zsh):
  ```bash
  curl -fsSL https://raw.githubusercontent.com/ElnatanSamuel/repty-cli/main/scripts/install.sh | bash
  ```

- Windows PowerShell:
  ```powershell
  irm https://raw.githubusercontent.com/ElnatanSamuel/repty-cli/main/scripts/install.ps1 | iex
  ```

1) Install and run `repty` (opens onboarding wizard)

- pipx (recommended):
  ```bash
  pipx install git+https://github.com/ElnatanSamuel/repty-cli.git
  repty  # launches setup wizard (installs hooks, optional AI key)
  ```

- pip in a virtualenv:
  ```bash
  python -m venv .venv
  # macOS/Linux
  source .venv/bin/activate
  # Windows PowerShell
  . .venv/Scripts/Activate.ps1

  pip install "repty-cli @ git+https://github.com/ElnatanSamuel/repty-cli.git"
  repty  # launches setup wizard (installs hooks, optional AI key)
  ```

2) Reload your shell profile or open a new terminal
- Bash: `source ~/.bashrc`
- Zsh: `source ~/.zshrc`
- PowerShell: `. $PROFILE`

3) Try it
```bash
repty search "git log"
```

## Features

- Auto-log every command you run (command text, timestamp, cwd, exit code, tags)
- Inline tags using comments like `#tag build` or `#tag deploy`
- Local search: `repty search "keywords"` (FTS5)
- AI search: `repty ai "natural language query"` (Gemini, with fallback to local search)
- Recent: `repty recent --limit N`
- Save favorites: `repty save <description> "<command>"`
- Export to Markdown/CSV: `repty export <file>`
- One-step hook installer for `.bashrc` and `.zshrc`

## Requirements

- Python 3.9+
- SQLite with FTS5 support
  - Most modern Python builds (3.11+) include FTS5. If you see an error about missing FTS5, install `pysqlite3-binary` and set env var: `REPTY_SQLITE_BACKEND=pysqlite3` before running. Alternatively, use a Python with FTS5-enabled sqlite.
- For AI search: Gemini API key

## Install

From GitHub with pipx (recommended):

```bash
pipx install git+https://github.com/ElnatanSamuel/repty-cli.git
repty
```

With pip in a virtualenv:

```bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell
. .venv/Scripts/Activate.ps1

pip install "repty-cli @ git+https://github.com/ElnatanSamuel/repty-cli.git"
repty
```

From source (development):

```bash
git clone https://github.com/ElnatanSamuel/repty-cli.git
cd repty-cli
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell
. .venv/Scripts/Activate.ps1
pip install -e .
repty
```

### Install shell hooks

If you ran the `repty` onboarding wizard, hooks are already installed. This section is optional.

Append logging hooks to your shell init files:

```bash
repty install-hooks
# To remove later:
repty uninstall-hooks
```

Hooks are added to `~/.bashrc` and `~/.zshrc` between clearly marked blocks. They capture each executed command and log it via `repty log ...`.

Windows/PowerShell:

- Native PowerShell auto-logging is supported. The installer will add a hook block to your PowerShell profile.

## Configuration

Create or edit `~/.repty_config.json`:

```json
{
  "gemini_api_key": "YOUR_API_KEY",
  "ai_model": "gemini-1.5-flash",
  "ai_context_limit": 500,
  "default_search_limit": 50
}
```

You can also set the key via CLI:

```bash
repty config set-key YOUR_API_KEY
repty config show
```

## Usage

- Local search (FTS5):

  ```bash
  repty search "docker build"
  repty search "tags:deploy"
  ```

- AI search (Gemini with fallback to local on failure):

  ```bash
  repty ai "how did I start my local postgres last week?"
  ```

- Show recent:

  ```bash
  repty recent --limit 30
  ```

- Save a favorite:

  ```bash
  repty save "build backend" "docker build -t myapp:latest ."
  ```

- Export:
  ```bash
  repty export ~/cmds.md
  repty export ~/cmds.csv
  ```

## How it works

- A small Bash/Zsh snippet installs preexec/precmd (Zsh) or DEBUG/PROMPT_COMMAND (Bash) hooks.
- After each command runs, the hook calls `repty log --command "..." --cwd "..." --exit <code>` in the background.
- Commands are stored in `~/.repty.db` with the schema:

```sql
CREATE TABLE commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  command TEXT NOT NULL,
  cwd TEXT,
  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
  exit_code INTEGER,
  tags TEXT
);
CREATE VIRTUAL TABLE commands_fts USING fts5(
  command, tags, content='commands', content_rowid='id'
);
```

- FTS stays in sync via triggers.

## Troubleshooting

- FTS5 not available: Use Python with FTS5-enabled sqlite (3.11+ often OK) or
  ```bash
  pip install pysqlite3-binary
  export REPTY_SQLITE_BACKEND=pysqlite3
  ```
- No results in AI search: Ensure your `~/.repty_config.json` has a valid `gemini_api_key`.
- Hooks not firing: Ensure your shell is interactive and that `repty` is on PATH.

## Security notes

- Only command text, timestamp, cwd, exit code, and optional tags are stored locally.
- AI mode sends only a subset (default last 500) commands + your query to Gemini for ranking. Disable by avoiding `repty ai`.

## License

MIT
