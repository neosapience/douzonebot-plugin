"""
Unified LLM Provider abstraction for douzone-bot.

Provides a common interface for text and vision AI calls across:
- Claude Code CLI (claude -p)
- Gemini CLI (gemini)
- OpenRouter API (HTTP, OpenAI-compatible)

Usage:
    from src.config import load_config
    from src.llm_provider import create_provider

    config = load_config()
    provider = create_provider(config)
    result = await provider.complete("Parse this memo: 1/6 <이름> 홍길동")
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Check CLI availability
CLAUDE_CLI_AVAILABLE = shutil.which("claude") is not None
GEMINI_CLI_AVAILABLE = shutil.which("gemini") is not None


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    async def complete(self, prompt: str, model_hint: str = "default",
                       json_output: bool = True, timeout: int = 60) -> str:
        """Text completion. Returns raw response text (not parsed JSON)."""

    @abstractmethod
    async def vision(self, prompt: str, image_path: str, model_hint: str = "default",
                     json_output: bool = True, timeout: int = 60) -> str:
        """Vision completion with image. Returns raw response text."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for logging."""


class ClaudeCliProvider(LLMProvider):
    """LLM provider using Claude Code CLI (claude -p)."""

    MODEL_MAP = {
        "default": None,  # Use CLI default
        "fast": "sonnet",
    }

    @staticmethod
    def _clean_env() -> dict:
        """Return env dict with all Claude Code vars stripped.

        When running as a Claude Code plugin skill, several CLAUDE* env vars
        are set (CLAUDECODE, CLAUDE_CODE_ENTRYPOINT, CLAUDE_CODE_SSE_PORT, etc.).
        These cause nested `claude -p` calls to silently fail (empty stdout).
        Strip ALL Claude-related vars so the subprocess starts a fresh CLI session.
        """
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CLAUDE")}
        return env

    @property
    def name(self) -> str:
        return "claude_code"

    async def complete(self, prompt: str, model_hint: str = "default",
                       json_output: bool = True, timeout: int = 60) -> str:
        if not CLAUDE_CLI_AVAILABLE:
            raise RuntimeError("Claude Code CLI not found. Install from https://claude.ai/code")

        cmd = ["claude", "-p", prompt]
        if json_output:
            cmd.extend(["--output-format", "json"])
        cmd.append("--no-session-persistence")

        model = self.MODEL_MAP.get(model_hint)
        if model:
            cmd.extend(["--model", model])

        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, env=self._clean_env()
        )

        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI error (rc={result.returncode}): {result.stderr[:200]}")

        return self._extract_response(result.stdout, json_output)

    async def vision(self, prompt: str, image_path: str, model_hint: str = "default",
                     json_output: bool = True, timeout: int = 60) -> str:
        if not CLAUDE_CLI_AVAILABLE:
            raise RuntimeError("Claude Code CLI not found. Install from https://claude.ai/code")

        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        # Claude CLI reads files referenced in prompt when run from their directory
        filename = path.name
        full_prompt = f"Read the file {filename}. {prompt}"

        cmd = ["claude", "-p", full_prompt]
        if json_output:
            cmd.extend(["--output-format", "json"])
        cmd.append("--no-session-persistence")

        model = self.MODEL_MAP.get(model_hint)
        if model:
            cmd.extend(["--model", model])

        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(path.parent), env=self._clean_env()
        )

        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI error (rc={result.returncode}): {result.stderr[:200]}")

        if not result.stdout:
            raise RuntimeError(f"Claude CLI returned empty output (stdout=None). stderr: {(result.stderr or '')[:200]}")

        return self._extract_response(result.stdout, json_output)

    def _extract_response(self, stdout: str, json_output: bool) -> str:
        """Extract response text from Claude CLI output."""
        if not stdout:
            return ""
        if not json_output:
            return stdout.strip()
        try:
            cli_output = json.loads(stdout)
            if cli_output.get("is_error"):
                raise RuntimeError(f"Claude error: {cli_output.get('result', '')[:200]}")
            return cli_output.get("result") or ""
        except (json.JSONDecodeError, TypeError):
            # If JSON parsing fails, return raw output
            return (stdout or "").strip()


class GeminiCliProvider(LLMProvider):
    """LLM provider using Gemini CLI (gemini)."""

    MODEL_MAP = {
        "default": "gemini-2.5-flash",
        "fast": "gemini-2.5-flash",
    }

    def __init__(self, model: Optional[str] = None):
        self._model_override = model

    @property
    def name(self) -> str:
        return "gemini_cli"

    def _get_model(self, model_hint: str) -> str:
        if self._model_override:
            return self._model_override
        return self.MODEL_MAP.get(model_hint, self.MODEL_MAP["default"])

    async def complete(self, prompt: str, model_hint: str = "default",
                       json_output: bool = True, timeout: int = 60) -> str:
        if not GEMINI_CLI_AVAILABLE:
            raise RuntimeError("Gemini CLI not found. Install from https://ai.google.dev/gemini-api/docs/quickstart")

        cmd = ["gemini"]
        model = self._get_model(model_hint)
        if model:
            cmd.extend(["-m", model])
        if json_output:
            cmd.extend(["-o", "json"])
        cmd.append(prompt)

        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout
        )

        if result.returncode != 0:
            raise RuntimeError(f"Gemini CLI error (rc={result.returncode}): {result.stderr[:200]}")

        return self._extract_response(result.stdout, json_output)

    async def vision(self, prompt: str, image_path: str, model_hint: str = "default",
                     json_output: bool = True, timeout: int = 60) -> str:
        if not GEMINI_CLI_AVAILABLE:
            raise RuntimeError("Gemini CLI not found.")

        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        # Gemini CLI reads files referenced in prompt when run from their directory
        filename = path.name
        full_prompt = f"Read the file {filename}. {prompt}"

        cmd = ["gemini"]
        model = self._get_model(model_hint)
        if model:
            cmd.extend(["-m", model])
        if json_output:
            cmd.extend(["-o", "json"])
        cmd.append(full_prompt)

        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(path.parent)
        )

        if result.returncode != 0:
            raise RuntimeError(f"Gemini CLI error (rc={result.returncode}): {result.stderr[:200]}")

        return self._extract_response(result.stdout, json_output)

    def _extract_response(self, stdout: str, json_output: bool) -> str:
        """Extract response from Gemini CLI output."""
        if not json_output:
            return stdout.strip()
        try:
            # Gemini CLI may prefix non-JSON text before the JSON object
            json_start = stdout.find("{")
            if json_start == -1:
                return stdout.strip()
            cli_output = json.loads(stdout[json_start:])
            return cli_output.get("response", stdout.strip())
        except json.JSONDecodeError:
            return stdout.strip()


class OpenRouterProvider(LLMProvider):
    """LLM provider using OpenRouter API (OpenAI-compatible)."""

    API_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "anthropic/claude-sonnet-4",
                 vision_model: Optional[str] = None):
        if not api_key:
            raise ValueError("OpenRouter API key is required. Set openrouter.api_key in config.yaml or OPENROUTER_API_KEY env var.")
        self.api_key = api_key
        self.model = model
        self.vision_model = vision_model or model

    @property
    def name(self) -> str:
        return "openrouter"

    async def complete(self, prompt: str, model_hint: str = "default",
                       json_output: bool = True, timeout: int = 60) -> str:
        import httpx

        messages = [{"role": "user", "content": prompt}]

        if json_output:
            messages[0]["content"] += "\n\nRespond with valid JSON only, no markdown formatting."

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                self.API_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                },
            )
            response.raise_for_status()

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return content

    async def vision(self, prompt: str, image_path: str, model_hint: str = "default",
                     json_output: bool = True, timeout: int = 60) -> str:
        import httpx

        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        # Encode image as base64
        with open(path, "rb") as f:
            image_bytes = f.read()
        b64_image = base64.b64encode(image_bytes).decode("utf-8")

        # Determine MIME type
        suffix = path.suffix.lower()
        mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                    ".gif": "image/gif", ".webp": "image/webp"}
        mime_type = mime_map.get(suffix, "image/jpeg")

        text_content = prompt
        if json_output:
            text_content += "\n\nRespond with valid JSON only, no markdown formatting."

        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": text_content},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime_type};base64,{b64_image}"
                }},
            ],
        }]

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                self.API_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.vision_model,
                    "messages": messages,
                },
            )
            response.raise_for_status()

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return content


def create_provider(config, provider_type: str = "llm") -> LLMProvider:
    """Create an LLM provider from config.

    Args:
        config: AppConfig instance
        provider_type: "llm" for text tasks, "receipt_ocr" for vision/OCR tasks

    Returns:
        LLMProvider instance
    """
    if provider_type == "receipt_ocr":
        provider_name = config.receipt_provider
    else:
        provider_name = config.llm_provider

    # For "auto", default to llm_provider
    if provider_name == "auto":
        provider_name = config.llm_provider

    if provider_name == "claude_code":
        if not CLAUDE_CLI_AVAILABLE:
            logger.warning("Claude CLI not available, check your PATH")
        return ClaudeCliProvider()

    elif provider_name == "gemini_cli":
        if not GEMINI_CLI_AVAILABLE:
            logger.warning("Gemini CLI not available, check your PATH")
        return GeminiCliProvider()

    elif provider_name == "openrouter":
        return OpenRouterProvider(
            api_key=config.openrouter_api_key,
            model=config.openrouter_model,
            vision_model=config.openrouter_vision_model,
        )

    else:
        raise ValueError(
            f"Unknown provider: {provider_name}. "
            f"Choose from: claude_code, gemini_cli, openrouter"
        )
