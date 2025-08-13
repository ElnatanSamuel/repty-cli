import json
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_FILENAME = ".repty_config.json"
LEGACY_CONFIG_FILENAME = ".cmdvault_config.json"


def get_config_path() -> Path:
    return Path.home() / CONFIG_FILENAME


def load_config() -> Dict[str, Any]:
    p = get_config_path()
    # Default config
    default_cfg: Dict[str, Any] = {
        "gemini_api_key": None,
        "ai_model": "gemini-1.5-flash",
        "ai_context_limit": 500,
        "default_search_limit": 50,
    }
    # If new config exists, load it
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return dict(default_cfg)
    # Try legacy config and migrate
    legacy = Path.home() / LEGACY_CONFIG_FILENAME
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            # Write to new path for future use
            try:
                p.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception:
                pass
            return data if isinstance(data, dict) else dict(default_cfg)
        except Exception:
            return dict(default_cfg)
    return dict(default_cfg)


def save_config(cfg: Dict[str, Any]) -> None:
    p = get_config_path()
    sanitized = dict(cfg)
    p.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")


def require_api_key(cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg or load_config()
    key = cfg.get("gemini_api_key")
    if not key:
        raise RuntimeError(
            "Gemini API key not set. Run: echo '{\n  \"gemini_api_key\": \"YOUR_KEY\"\n}' > ~/.repty_config.json"
        )
    return key
