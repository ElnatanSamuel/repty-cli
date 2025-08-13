import json
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_FILENAME = ".cmdvault_config.json"


def get_config_path() -> Path:
    return Path.home() / CONFIG_FILENAME


def load_config() -> Dict[str, Any]:
    p = get_config_path()
    if not p.exists():
        return {
            "gemini_api_key": None,
            "ai_model": "gemini-1.5-flash",
            "ai_context_limit": 500,
            "default_search_limit": 50,
        }
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {
            "gemini_api_key": None,
            "ai_model": "gemini-1.5-flash",
            "ai_context_limit": 500,
            "default_search_limit": 50,
        }


def save_config(cfg: Dict[str, Any]) -> None:
    p = get_config_path()
    sanitized = dict(cfg)
    p.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")


def require_api_key(cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg or load_config()
    key = cfg.get("gemini_api_key")
    if not key:
        raise RuntimeError(
            "Gemini API key not set. Run: echo '{\n  \"gemini_api_key\": \"YOUR_KEY\"\n}' > ~/.cmdvault_config.json"
        )
    return key
