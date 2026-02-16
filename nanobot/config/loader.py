"""Configuration loading utilities."""

import json
from pathlib import Path
from typing import Any

from nanobot.config.schema import Config


def get_config_path() -> Path:
    """Get the default configuration file path."""
    return Path.home() / ".nanobot" / "config.json"


def get_data_dir() -> Path:
    """Get the nanobot data directory."""
    from nanobot.utils.helpers import get_data_path
    return get_data_path()


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.
    
    Args:
        config_path: Optional path to config file. Uses default if not provided.
    
    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()
    
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            data = _migrate_config(data)
            return Config.model_validate(convert_keys(data))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")
    
    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.
    
    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # Convert to camelCase format
    data = config.model_dump()
    data = convert_to_camel(data)
    
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data


def _preserve_dict_keys(path: tuple[str, ...]) -> bool:
    """Return True when dict keys under this path must remain untouched.

    Some config fields are free-form maps (for example MCP env vars and
    custom HTTP headers). Their keys are case-sensitive and must not be
    transformed between snake_case and camelCase.
    """
    if not path:
        return False
    return path[-1] in {"env", "extraHeaders", "extra_headers"}


def convert_keys(data: Any, path: tuple[str, ...] = ()) -> Any:
    """Convert camelCase keys to snake_case for Pydantic."""
    if isinstance(data, dict):
        if _preserve_dict_keys(path):
            return {k: convert_keys(v, path + (k,)) for k, v in data.items()}
        return {
            camel_to_snake(k): convert_keys(v, path + (k,))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [convert_keys(item, path) for item in data]
    return data


def convert_to_camel(data: Any, path: tuple[str, ...] = ()) -> Any:
    """Convert snake_case keys to camelCase."""
    if isinstance(data, dict):
        if _preserve_dict_keys(path):
            return {k: convert_to_camel(v, path + (k,)) for k, v in data.items()}
        return {
            snake_to_camel(k): convert_to_camel(v, path + (k,))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [convert_to_camel(item, path) for item in data]
    return data


def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    result = []
    for i, char in enumerate(name):
        if char.isupper() and i > 0:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])
