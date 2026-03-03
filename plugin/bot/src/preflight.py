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
    "cdp": "Ensure Chrome is running with --remote-debugging-port=9444 (run /douzonebot:chrome)",
    "claude_cli": "Run: claude /login (Claude Code CLI 인증 필요)",
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


def check_claude_cli(timeout: float = 15.0) -> Tuple[bool, str]:
    """Check Claude Code CLI is available and authenticated."""
    import os

    # Step 1: Binary exists?
    if not shutil.which("claude"):
        return False, "Binary not found in PATH"

    # Step 2: Running inside Claude Code session?
    # When invoked as a plugin skill, we're already inside Claude Code.
    # Nested `claude -p` calls fail, but Claude Code is obviously available.
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return True, "OK (running inside Claude Code session)"

    # Step 3: Auth works? (minimal API round-trip)
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
                # Check if error is about nested sessions
                if "nested" in error_msg.lower() or "already running" in error_msg.lower():
                    return True, "OK (running inside Claude Code session)"
                return False, f"{error_msg}"
        except (json.JSONDecodeError, TypeError):
            pass

        # If JSON parsing failed or no is_error, check return code
        if result.returncode != 0:
            stderr_short = result.stderr.strip()[:200]
            # Check stderr for nested session indicators
            if "nested" in stderr_short.lower() or "already running" in stderr_short.lower():
                return True, "OK (running inside Claude Code session)"
            return False, f"CLI error (exit {result.returncode}): {stderr_short}"

        return True, "OK (authenticated)"

    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s"
    except Exception as e:
        return False, f"Error: {e}"


def _determine_requirements(args, config=None) -> dict:
    """Determine which APIs are required based on mode, flags, and config."""
    needs_cdp = False
    needs_llm = False  # Claude Code CLI check
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

    result = {
        "cdp": needs_cdp,
        "claude_cli": needs_llm,
    }

    return result


def _print_report(report: PreflightReport, config=None) -> None:
    """Print the pre-flight check report."""
    print("\n" + "=" * 60)
    # Build header with mode and provider info
    header_parts = ["Pre-flight API Check"]
    if config:
        mode = "local" if config.is_local() else "server"
        header_parts.append(f"({mode} mode)")
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
    cdp_url = getattr(args, 'cdp_url', 'http://localhost:9444')

    if reqs["cdp"]:
        check_defs.append(("cdp", "CDP connection", True,
                           asyncio.to_thread(check_cdp, cdp_url)))
    if reqs.get("claude_cli"):
        check_defs.append(("claude_cli", "Claude Code CLI", True,
                           asyncio.to_thread(check_claude_cli)))
    # Execute all checks concurrently
    if check_defs:
        tasks = [cd[3] for cd in check_defs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (key, name, required, _), result in zip(check_defs, results):
            if isinstance(result, Exception):
                ok, msg = False, f"Check failed: {result}"
            else:
                ok, msg = result

            report.checks.append(PreflightResult(
                name=name,
                required=required,
                available=ok,
                message=msg,
                fix_hint=FIX_HINTS.get(key, "") if not ok else "",
            ))

    _print_report(report, config=config)
    return report
