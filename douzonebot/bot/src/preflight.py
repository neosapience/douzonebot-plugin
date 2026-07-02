"""
Pre-flight API validation for Douzone Expense Automation.

Checks that all required external APIs are reachable before starting processing.
Fails fast with a clear status report if required APIs are unavailable.
"""

import asyncio
import json
import logging
import shutil
import subprocess
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Fix hints shown when a check fails
FIX_HINTS = {
    "cdp": "Ensure Chrome is running with --remote-debugging-port=9222 and SSH tunnel is active",
    "claude_cli": "Run: docker exec -it douzone-bot claude /login",
    "gemini_cli": "Install Gemini CLI: npm install -g @anthropic-ai/gemini-cli (or check PATH)",
    "openrouter": "Set openrouter.api_key in config.yaml or OPENROUTER_API_KEY env var",
    "qwen25vl": "Check OCR API container on sapience-rtx-11: cd /nas2a/yeonghyeon/douzone-bot/ocr_api && docker compose up -d",
}


@dataclass
class PreflightResult:
    name: str
    required: bool
    available: bool
    message: str
    fix_hint: str = ""


@dataclass
class PreflightReport:
    checks: List[PreflightResult] = field(default_factory=list)

    @property
    def all_required_passed(self) -> bool:
        return all(c.available for c in self.checks if c.required)

    @property
    def failed_required(self) -> List[PreflightResult]:
        return [c for c in self.checks if c.required and not c.available]


def check_cdp(cdp_url: str, timeout: float = 3.0) -> Tuple[bool, str]:
    """Check Chrome DevTools Protocol is reachable."""
    url = f"{cdp_url}/json/version"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            browser = data.get("Browser", "unknown")
            return True, f"Connected ({browser})"
    except urllib.error.URLError as e:
        reason = getattr(e, 'reason', str(e))
        return False, f"Cannot connect to {cdp_url} ({reason})"
    except Exception as e:
        return False, f"Error: {e}"


def check_qwen25vl(host: str = "200.168.0.41", port: int = 8810, timeout: float = 5.0) -> Tuple[bool, str]:
    """Check Qwen2.5-VL OCR API is reachable."""
    url = f"http://{host}:{port}/health"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            model = data.get("model", "unknown")
            return True, f"OK (model: {model})"
    except urllib.error.URLError as e:
        reason = getattr(e, 'reason', str(e))
        return False, f"Cannot connect to http://{host}:{port} ({reason})"
    except Exception as e:
        return False, f"Error: {e}"


def check_claude_cli(timeout: float = 15.0) -> Tuple[bool, str]:
    """Check Claude Code CLI is available and authenticated."""
    # Step 1: Binary exists?
    if not shutil.which("claude"):
        return False, "Binary not found in PATH"

    # Step 2: Auth works? (minimal API round-trip)
    try:
        result = subprocess.run(
            ["claude", "-p", "Reply with exactly: ok",
             "--output-format", "json", "--no-session-persistence"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        # Try to parse JSON output first (contains is_error and result fields)
        try:
            cli_output = json.loads(result.stdout)
            if cli_output.get("is_error"):
                error_msg = cli_output.get("result", "unknown error")[:150]
                return False, f"{error_msg}"
        except (json.JSONDecodeError, TypeError):
            pass

        # If JSON parsing failed or no is_error, check return code
        if result.returncode != 0:
            stderr_short = result.stderr.strip()[:200]
            return False, f"CLI error (exit {result.returncode}): {stderr_short}"

        return True, "OK (authenticated)"

    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s"
    except Exception as e:
        return False, f"Error: {e}"


def check_gemini_cli(timeout: float = 10.0) -> Tuple[bool, str]:
    """Check Gemini CLI is available."""
    if not shutil.which("gemini"):
        return False, "Binary not found in PATH"

    try:
        result = subprocess.run(
            ["gemini", "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr_short = result.stderr.strip()[:200]
            return False, f"CLI error (exit {result.returncode}): {stderr_short}"

        version = result.stdout.strip()[:100]
        return True, f"OK ({version})" if version else "OK"

    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s"
    except Exception as e:
        return False, f"Error: {e}"


def check_openrouter(config=None) -> Tuple[bool, str]:
    """Check OpenRouter API key is configured."""
    import os
    api_key = ""
    if config:
        api_key = getattr(config, 'openrouter_api_key', '') or ''
    if not api_key:
        api_key = os.environ.get("OPENROUTER_API_KEY", "")

    if not api_key:
        return False, "No API key configured (set in config.yaml or OPENROUTER_API_KEY env var)"

    # Mask the key for display
    masked = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 8 else "****"
    return True, f"API key configured ({masked})"


def _determine_requirements(args, config=None) -> dict:
    """Determine which APIs are required based on mode, flags, and config.

    If config is provided, the LLM provider check is chosen based on
    config.llm_provider instead of always checking Claude CLI.
    """
    needs_cdp = False
    needs_llm = False  # Generic flag: "we need the configured LLM provider"
    needs_qwen = False  # Always optional (warn only)

    # Determine which LLM provider to check
    llm_provider = "claude_code"  # default
    if config and hasattr(config, 'llm_provider'):
        llm_provider = config.llm_provider or "claude_code"

    if getattr(args, 'simple', False):
        # Simple mode: only CDP
        needs_cdp = True
    elif getattr(args, 'test', None):
        # Legacy test mode: only CDP
        needs_cdp = True
    elif getattr(args, 'user', None):
        # MVP mode
        test_mode = getattr(args, 'test_mode', False)
        stage3_cache_in = getattr(args, 'stage3_cache_in', None)

        if getattr(args, 'receipts_only', False):
            needs_llm = True
        elif stage3_cache_in:
            needs_cdp = not test_mode
        else:
            needs_cdp = not test_mode
            needs_llm = True

        # Qwen is checked but never required (warn only)
        # In local mode, skip Qwen check entirely
        is_local = config.is_local() if config else getattr(args, 'local', False)
        if not is_local and not stage3_cache_in and not getattr(args, 'stage1_cache_in', None):
            needs_qwen = True

    # Map needs_llm to the specific provider check
    result = {
        "cdp": needs_cdp,
        "claude_cli": needs_llm and llm_provider == "claude_code",
        "gemini_cli": needs_llm and llm_provider == "gemini_cli",
        "openrouter": needs_llm and llm_provider == "openrouter",
        "qwen25vl": needs_qwen,
    }

    return result


def _print_report(report: PreflightReport, config=None) -> None:
    """Print the pre-flight check report."""
    print("\n" + "=" * 60)
    # Build header with mode and provider info
    header_parts = ["Pre-flight API Check"]
    if config:
        mode = "local" if config.is_local() else "server"
        provider = getattr(config, 'llm_provider', 'unknown')
        header_parts.append(f"({mode} mode, provider: {provider})")
    print(" ".join(header_parts))
    print("=" * 60)

    for check in report.checks:
        if not check.required and not check.available:
            tag = "WARN"
        elif not check.required:
            tag = "SKIP" if not check.available else "PASS"
        elif check.available:
            tag = "PASS"
        else:
            tag = "FAIL"
        print(f"  [{tag}] {check.name:<24}: {check.message}")

    print("-" * 60)

    if not report.all_required_passed:
        print("\nFATAL: Required API(s) unavailable:")
        for c in report.failed_required:
            print(f"  - {c.name}: {c.message}")
            if c.fix_hint:
                print(f"    -> {c.fix_hint}")
        print("\nAborting. Use --skip-preflight to bypass these checks.\n")


async def run_preflight(args, config=None) -> PreflightReport:
    """Run pre-flight checks based on the current mode/args/config.

    Returns a PreflightReport. Prints status to stdout.
    """
    reqs = _determine_requirements(args, config=config)
    report = PreflightReport()

    # Build check tasks (run in parallel)
    check_defs = []
    cdp_url = getattr(args, 'cdp_url', 'http://localhost:9222')

    if reqs["cdp"]:
        check_defs.append(("cdp", f"CDP connection ({cdp_url})", True,
                           asyncio.to_thread(check_cdp, cdp_url, 8.0)))
    if reqs.get("claude_cli"):
        check_defs.append(("claude_cli", "Claude Code CLI", True,
                           asyncio.to_thread(check_claude_cli)))
    if reqs.get("gemini_cli"):
        check_defs.append(("gemini_cli", "Gemini CLI", True,
                           asyncio.to_thread(check_gemini_cli)))
    if reqs.get("openrouter"):
        check_defs.append(("openrouter", "OpenRouter API", True,
                           asyncio.to_thread(check_openrouter, config)))
    if reqs.get("qwen25vl"):
        # Qwen is never required - only warn
        check_defs.append(("qwen25vl", "Qwen2.5-VL OCR API", False,
                           asyncio.to_thread(check_qwen25vl)))

    # Execute all checks concurrently
    if check_defs:
        tasks = [cd[3] for cd in check_defs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (key, name, required, _), result in zip(check_defs, results):
            if isinstance(result, Exception):
                ok, msg = False, f"Check failed: {result}"
            else:
                ok, msg = result

            # Add context for Qwen warning
            if key == "qwen25vl" and not ok:
                llm_provider = config.llm_provider if config else "claude_code"
                msg += f" (will use {llm_provider} for OCR)"

            report.checks.append(PreflightResult(
                name=name,
                required=required,
                available=ok,
                message=msg,
                fix_hint=FIX_HINTS.get(key, "") if not ok else "",
            ))

    # Determine which provider checks exist
    llm_provider = config.llm_provider if config else "claude_code"
    # All possible provider-related keys and their display names
    all_keys = {
        "cdp": "CDP connection",
        "claude_cli": "Claude Code CLI",
        "gemini_cli": "Gemini CLI",
        "openrouter": "OpenRouter API",
        "qwen25vl": "Qwen2.5-VL OCR API",
    }
    checked_keys = {cd[0] for cd in check_defs}

    # Add skipped checks for completeness
    for key, name in all_keys.items():
        if key not in checked_keys:
            # Determine appropriate skip message
            if key in ("claude_cli", "gemini_cli", "openrouter"):
                msg = f"Not required (using {llm_provider})"
            else:
                msg = "Not required for this mode"
            report.checks.append(PreflightResult(
                name=name,
                required=False,
                available=True,
                message=msg,
            ))

    _print_report(report, config=config)
    return report
