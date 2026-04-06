"""
Configuration loader for douzone-bot.

Loads settings from config.yaml (~/douzone-bot/, CWD, or ~/.config/douzone-bot/).
CLI flags override config file values.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


@dataclass
class AppConfig:
    """Application configuration."""
    # Mode
    mode: str = "server"  # "local" or "server"

    # User
    user_name: str = ""

    # Provider selection
    llm_provider: str = "claude_code"  # claude_code | gemini_cli | openrouter
    receipt_provider: str = "auto"  # claude_code | gemini_cli | openrouter | qwen25vl | auto

    # Chrome debug port (for CDP connection)
    chrome_debug_port: int = 9444

    # OpenRouter settings
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-sonnet-4"
    openrouter_vision_model: str = "anthropic/claude-sonnet-4"

    def is_local(self) -> bool:
        return self.mode == "local"


# Default config search paths
_CONFIG_FILENAMES = ["config.yaml", "config.yml"]
_CONFIG_DIRS = [
    ".",  # CWD (for development)
    str(Path.home() / "douzone-bot"),  # User data directory
    str(Path.home() / ".config" / "douzone-bot"),  # XDG fallback
]


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load config from YAML file. Falls back to defaults if not found.

    Search order:
        1. Explicit path (if provided)
        2. config.yaml in current directory
        3. ~/.config/douzone-bot/config.yaml
    """
    config = AppConfig()

    if not YAML_AVAILABLE:
        logger.debug("pyyaml not installed, using default config")
        return config

    # Find config file
    paths_to_try = []
    if config_path:
        paths_to_try.append(config_path)
    else:
        for dir_path in _CONFIG_DIRS:
            for filename in _CONFIG_FILENAMES:
                paths_to_try.append(os.path.join(dir_path, filename))

    found_path = None
    for p in paths_to_try:
        if os.path.isfile(p):
            found_path = p
            break

    if not found_path:
        logger.debug("No config file found, using defaults")
        return config

    # Load YAML
    try:
        with open(found_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        logger.info(f"Loaded config from {found_path}")
    except Exception as e:
        logger.warning(f"Failed to load config from {found_path}: {e}")
        return config

    # Map YAML fields to AppConfig
    config.mode = data.get("mode", config.mode)
    config.user_name = data.get("user_name", config.user_name)

    providers = data.get("providers", {})
    config.llm_provider = providers.get("llm", config.llm_provider)
    config.receipt_provider = providers.get("receipt_ocr", config.receipt_provider)

    config.chrome_debug_port = data.get("chrome_debug_port", config.chrome_debug_port)

    openrouter = data.get("openrouter", {})
    config.openrouter_api_key = openrouter.get("api_key", config.openrouter_api_key)
    config.openrouter_model = openrouter.get("model", config.openrouter_model)
    config.openrouter_vision_model = openrouter.get("vision_model", config.openrouter_vision_model)

    # Also check environment variable for OpenRouter API key
    if not config.openrouter_api_key:
        config.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "")

    return config


def apply_cli_overrides(config: AppConfig, args) -> AppConfig:
    """Apply CLI argument overrides to config.

    CLI flags take precedence over config file values.
    """
    # --local flag forces local mode
    if getattr(args, "local", False):
        config.mode = "local"

    # --user overrides config user_name
    if getattr(args, "user", None):
        config.user_name = args.user

    # --receipt-provider overrides config (only if explicitly set, not default "auto")
    receipt_provider = getattr(args, "receipt_provider", None)
    if receipt_provider and receipt_provider != "auto":
        config.receipt_provider = receipt_provider

    # In local mode, if receipt_provider is still "auto", default to llm_provider
    if config.is_local() and config.receipt_provider == "auto":
        config.receipt_provider = config.llm_provider

    # Set environment hint for modules that check it
    if config.is_local():
        os.environ["DOUZONE_LOCAL_MODE"] = "1"

    return config
