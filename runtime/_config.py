# aicp/config.py
"""Config loader — reads aicp.yaml, resolves environment variables."""

import os
import re
from pathlib import Path
from typing import Any

import yaml

ENV_VAR_PATTERN = re.compile(r'\$\{(\w+)\}')

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 9000,
    "plugins_dir": "plugins",
    "data_dir": "data",
    "static_dir": None,
    "models": {
        "default": "gpt-3.5-turbo",
        "max_retries": 3,
        "request_timeout": 60,
        "stream_timeout": 300,
        "max_concurrent": 10,
        "providers": {},
    },
    "robots": [],
    "groups": [],
}


def _resolve_env_vars(value: Any) -> Any:
    """Replace ${VAR} with environment variable value."""
    if not isinstance(value, str):
        return value

    def replacer(match):
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            print(f"  ⚠️  Environment variable '{var_name}' not set, using empty string")
            return ""
        return env_value

    return ENV_VAR_PATTERN.sub(replacer, value)


def _walk_and_resolve(obj: Any) -> Any:
    """Recursively walk a dict/list and resolve ${VAR} in all strings."""
    if isinstance(obj, dict):
        return {k: _walk_and_resolve(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_walk_and_resolve(v) for v in obj]
    elif isinstance(obj, str):
        return _resolve_env_vars(obj)
    return obj


def _deep_merge(base: dict, override: dict):
    """Recursively merge override into base. Override wins."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def load_config(config_path: str = None) -> dict:
    """
    Load configuration.
    
    Priority:
    1. Explicit config_path argument
    2. aicp.yaml in current directory
    3. Default config
    
    Environment variables in ${VAR} format are resolved automatically.
    """
    config = _deep_copy(DEFAULT_CONFIG)

    # Determine config file path
    if config_path:
        path = Path(config_path)
    else:
        path = Path("aicp.yaml")

    if path.exists():
        print(f"   Loading: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw:
            # Resolve environment variables
            resolved = _walk_and_resolve(raw)
            # Merge into defaults
            _deep_merge(config, resolved)
    else:
        print(f"   No config file found, using defaults")
        _create_default_config(path)

    return config


def _create_default_config(path: Path):
    """Create a default config file if none exists."""
    default_yaml = """# AICP Configuration
# https://github.com/your-org/aicp

host: 0.0.0.0
port: 9000

models:
  default: gpt-3.5-turbo
  max_retries: 3
  request_timeout: 60
  stream_timeout: 300
  max_concurrent: 10
  providers:
    openai:
      base_url: https://api.openai.com/v1
      api_key: ${OPENAI_API_KEY}
      models:
        - id: gpt-3.5-turbo
          max_tokens: 4096
          temperature: 0.7
          default: true
"""
    try:
        path.write_text(default_yaml, encoding="utf-8")
        print(f"   Created default config: {path}")
    except Exception:
        pass


def _deep_copy(d: dict) -> dict:
    """Deep copy a dict (simple version, sufficient for config)."""
    import copy
    return copy.deepcopy(d)