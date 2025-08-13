# cmdvault

Dual-mode terminal command history tool for Bash/Zsh with:
- Local fast search via SQLite FTS5
- Optional Gemini AI search that understands natural language

All data stored locally in `~/.cmdvault.db`. Config stored in `~/.cmdvault_config.json`.

## Features
- Auto-log every command you run (command text, timestamp, cwd, exit code, tags)
- Inline tags using comments like `#tag build` or `#tag deploy`
- Local search: `cmdvault search "keywords"` (FTS5)
- AI search: `cmdvault ai "natural language query"` (Gemini, with fallback to local search)
- Recent: `cmdvault recent --limit N`
- Tag: `cmdvault tag <id> <tag>`
- Save favorites: `cmdvault save <description> "<command>"`
- Export to Markdown/CSV: `cmdvault export <file>`
- One-step hook installer for `.bashrc` and `.zshrc`

## Requirements
- Python 3.9+
- SQLite with FTS5 support
  - Most modern Python builds (3.11+) include FTS5. If you see an error about missing FTS5, install `pysqlite3-binary` and set env var: `CMDVAULT_SQLITE_BACKEND=pysqlite3` before running. Alternatively, use a Python with FTS5-enabled sqlite.
- For AI search: Gemini API key

## Install

Using pip (recommended in a virtualenv):

```bash
pip install .
```

Or for development:

```bash
pip install -e .
```

This installs the `cmdvault` CLI.

### Install shell hooks
Append logging hooks to your shell init files:

```bash
cmdvault install-hooks
# To remove later:
cmdvault uninstall-hooks
```

Hooks are added to `~/.bashrc` and `~/.zshrc` between clearly marked blocks. They capture each executed command and log it via `cmdvault log ...`.

Note for Windows users:
- Hooks target Bash/Zsh. Use WSL or Git Bash/MinGW. PowerShell is not yet supported.

## Configuration
Create or edit `~/.cmdvault_config.json`:

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
cmdvault config set-key YOUR_API_KEY
cmdvault config show
```

## Usage

- Local search (FTS5):
  ```bash
  cmdvault search "docker build"
  cmdvault search "tags:deploy"
  ```

- AI search (Gemini with fallback to local on failure):
  ```bash
  cmdvault ai "how did I start my local postgres last week?"
  ```

- Show recent:
  ```bash
  cmdvault recent --limit 30
  ```

- Tag an entry:
  ```bash
  cmdvault tag 123 deploy
  ```

- Save a favorite:
  ```bash
  cmdvault save "build backend" "docker build -t myapp:latest ."
  ```

- Export:
  ```bash
  cmdvault export ~/cmds.md
  cmdvault export ~/cmds.csv
  ```

## How it works
- A small Bash/Zsh snippet installs preexec/precmd (Zsh) or DEBUG/PROMPT_COMMAND (Bash) hooks.
- After each command runs, the hook calls `cmdvault log --command "..." --cwd "..." --exit <code>` in the background.
- Commands are stored in `~/.cmdvault.db` with the schema:

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
  export CMDVAULT_SQLITE_BACKEND=pysqlite3
  ```
- No results in AI search: Ensure your `~/.cmdvault_config.json` has a valid `gemini_api_key`.
- Hooks not firing: Ensure your shell is interactive and that `cmdvault` is on PATH.

## Security notes
- Only command text, timestamp, cwd, exit code, and optional tags are stored locally.
- AI mode sends only a subset (default last 500) commands + your query to Gemini for ranking. Disable by avoiding `cmdvault ai`.

## License
MIT
