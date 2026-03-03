"""
Douzone Expense Automation - MVP v6.2

MVP Mode (Default):
    python main.py --user "<이름>" --memo ./test_memo/memo.txt --receipts ./test_receipts
    python main.py --user "<이름>" --memo ./test_memo/memo.txt --receipts ./test_receipts --test-mode

Simple Mode (attendee only):
    python main.py --user "<이름>" --simple

Debug Mode (verbose logging):
    python main.py --user "<이름>" --memo ./test_memo/memo.txt --receipts ./test_receipts --debug
"""
import asyncio
import argparse
import logging
import sys
import os
from datetime import datetime

# Force UTF-8 on Windows (prevents cp949 crashes with emoji/unicode in logs)
if sys.platform == "win32" and sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Suppress spurious _readerthread exceptions during subprocess cleanup.
# These occur when nested asyncio loops (asyncio.to_thread → asyncio.run)
# spawn subprocesses whose pipe reader threads outlive the event loop.
import threading
_orig_excepthook = threading.excepthook
def _suppress_readerthread(args):
    if args.thread and "_readerthread" in (args.thread.name or ""):
        return  # Silently ignore
    _orig_excepthook(args)
threading.excepthook = _suppress_readerthread

from src.automation import DouzoneAutomation, debug as debug_logger
from src.config import load_config, apply_cli_overrides, AppConfig
from src.llm_provider import create_provider

# Configure logging
def setup_logging(debug_mode: bool = False, quiet_mode: bool = False, log_file: str = None):
    """Configure logging with optional debug/quiet mode and file output."""
    import warnings

    if quiet_mode:
        level = logging.ERROR
        # Suppress Python warnings (like FutureWarning from google.generativeai)
        warnings.filterwarnings("ignore")
    elif debug_mode:
        level = logging.DEBUG
    else:
        level = logging.INFO

    # Create formatter
    if debug_mode:
        formatter = logging.Formatter(
            '%(asctime)s.%(msecs)03d %(levelname)-8s [%(name)s] %(message)s',
            datefmt='%H:%M:%S'
        )
    else:
        formatter = logging.Formatter('%(levelname)-8s [%(name)s] %(message)s')

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)

    # File handler (if specified)
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)  # Always debug level for file
        root_logger.addHandler(file_handler)
        if not quiet_mode:
            print(f"📝 Logging to: {log_file}")

    return root_logger

logger = logging.getLogger(__name__)


# ============================================================================
# MVP MODE
# ============================================================================

async def run_mvp_mode(args, config: AppConfig = None):
    """Run the MVP automation flow."""
    from src.orchestrator import run_mvp

    # Configure debug logger
    debug_mode = getattr(args, 'debug', False)
    debug_logger.enabled = debug_mode

    if args.stage1_only and not args.stage1_cache_in and not args.stage1_cache_out:
        args.stage1_cache_out = os.path.join("cache", "stage1_cache.json")
    if args.stage1_only and not args.stage1_report_out:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.stage1_report_out = os.path.join("reports", f"stage1_report_{timestamp}.md")
    if args.receipts_only and not args.stage1_report_out:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.stage1_report_out = os.path.join("reports", f"receipt_ocr_report_{timestamp}.md")

    # Use provided config or load fresh
    if config is None:
        config = load_config(getattr(args, 'config', None))
        config = apply_cli_overrides(config, args)

    try:
        provider = create_provider(config, provider_type="llm")
    except Exception as e:
        print(f"\n❌ Failed to create LLM provider: {e}")
        return False

    print("\n" + "="*60)
    print("🚀 Douzone Expense Automation - MVP v6.2")
    print("="*60)
    print(f"\nUser: {args.user}")
    print(f"Mode: {'Local' if config.is_local() else 'Server'}")
    print(f"LLM Provider: {config.llm_provider}")
    print(f"Memo: {args.memo or '(none)'}")
    print(f"Receipts: {args.receipts or '(none)'}")
    print(f"CDP URL: {args.cdp_url}")
    print(f"Test Mode: {args.test_mode}")
    print(f"Debug Mode: {debug_mode}")
    print(f"Stage 1 Cache In: {args.stage1_cache_in or '(none)'}")
    print(f"Stage 1 Cache Out: {args.stage1_cache_out or '(none)'}")
    print(f"Stage 1 Report Out: {args.stage1_report_out or '(none)'}")
    print(f"Stage 1 Only: {args.stage1_only}")
    print(f"Receipts Only: {args.receipts_only}")
    print(f"Receipt Provider: {args.receipt_provider}")
    print(f"Receipt Cache: {getattr(args, 'receipt_cache', None) or '(none)'}")
    print(f"Stage 2 Cache Out: {getattr(args, 'stage2_cache_out', None) or '(none)'}")
    print(f"Stage 2 Only: {getattr(args, 'stage2_only', False)}")
    print(f"Stage 3 Cache In: {getattr(args, 'stage3_cache_in', None) or '(none)'}")
    print(f"Stage 3 Cache Out: {getattr(args, 'stage3_cache_out', None) or '(none)'}")
    print(f"Stage 3 Only: {getattr(args, 'stage3_only', False)}")
    print(f"Review Only: {getattr(args, 'review_only', False)}")
    if args.stage1_cache_in or args.stage1_cache_out or args.stage1_report_out or args.stage1_only or args.receipts_only or getattr(args, 'stage2_cache_out', None) or getattr(args, 'stage2_only', False) or getattr(args, 'stage3_cache_in', None) or getattr(args, 'stage3_cache_out', None) or getattr(args, 'stage3_only', False):
        print("NOTE: Stage caching is debug-only and should not be used in production.")
    
    if debug_mode:
        print(f"\n{'─'*60}")
        print("🔍 DEBUG MODE ENABLED")
        print("   - Detailed action logging for every step")
        print("   - Screenshots saved to: screenshots/debug/")
        print("   - Log file: logs/mvp_debug_<timestamp>.log")
        print(f"{'─'*60}")
    
    max_rows = getattr(args, 'max_rows', 0)
    if max_rows > 0:
        print(f"\n⚠️  LIMITED MODE: Processing only first {max_rows} row(s)")
    
    result = await run_mvp(
        user_name=args.user,
        memo_path=args.memo,
        receipts_path=args.receipts,
        cdp_url=args.cdp_url,
        test_mode=args.test_mode,
        auto_approve=getattr(args, 'auto_approve', False),
        stage1_cache_in=args.stage1_cache_in,
        stage1_cache_out=args.stage1_cache_out,
        stage1_report_out=args.stage1_report_out,
        stage1_only=args.stage1_only,
        receipts_only=args.receipts_only,
        receipt_provider=config.receipt_provider,
        receipt_cache=getattr(args, 'receipt_cache', None),
        max_rows=max_rows,
        stage2_cache_in=getattr(args, 'stage2_cache_in', None),
        stage2_cache_out=getattr(args, 'stage2_cache_out', None),
        stage2_only=getattr(args, 'stage2_only', False),
        stage3_cache_in=getattr(args, 'stage3_cache_in', None),
        stage3_cache_out=getattr(args, 'stage3_cache_out', None),
        stage3_only=getattr(args, 'stage3_only', False),
        review_only=getattr(args, 'review_only', False),
        provider=provider,
    )
    
    if "error" in result:
        print(f"\n❌ Error: {result['error']}")
        return 1

    if args.test_mode or args.receipts_only:
        print("\n✅ Test mode completed successfully")
        return 0

    success = result.get("success", 0)
    total = result.get("total", 0)
    if success == total:
        return 0  # All succeeded
    elif success == 0:
        return 1  # Total failure
    else:
        return 2  # Partial success


# ============================================================================
# SIMPLE MODE (Attendee Only)
# ============================================================================

async def run_simple_mode(args):
    """Run the simple attendee-only automation flow using DataProvider API."""
    debug_mode = getattr(args, 'debug', False)
    debug_logger.enabled = debug_mode

    print("\n" + "="*60)
    print("🧾 Douzone Expense Automation - SIMPLE MODE (DataProvider)")
    print("="*60)
    print(f"\nUser: {args.user}")
    print(f"CDP URL: {args.cdp_url}")
    print(f"Method: DataProvider API (fast, no popup)")
    print(f"Debug Mode: {debug_mode}")

    bot = DouzoneAutomation(cdp_url=args.cdp_url, debug=debug_mode)

    try:
        # Connect without auto-calibration (not needed for DataProvider method)
        await bot.connect(auto_calibrate=False)

        print("\n🚀 Running DataProvider-based simple mode...")
        result = await bot.run_simple_mode_via_dataprovider(args.user)

        if 'error' in result:
            print(f"\n❌ Error: {result['error']}")
            return False

        # Print summary
        print("\n" + "─"*40)
        print("📊 Simple Mode Summary:")
        print(f"   Total rows: {result['total']}")
        print(f"   Filled attendees: {result['filled']}")
        print(f"   Already had attendee: {result['already_set']}")
        print(f"   Failed: {result['failed']}")
        if result.get('failed_rows'):
            print(f"   Failed rows: {result['failed_rows']}")
        if result.get('blocked_rows'):
            print(f"   Warning-dialog rows (auto-detected empty): {result['blocked_rows']}")

        return result['failed'] == 0

    except Exception as e:
        logger.error(f"Simple mode failed: {e}")
        print(f"\n❌ Error: {e}")
        return False

    finally:
        await bot.close()


async def main():
    parser = argparse.ArgumentParser(
        description="Douzone Expense Automation - MVP v6.2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
MVP Mode (recommended):
  python main.py --user "<이름>" --memo ./test_memo/memo.txt --receipts ./test_receipts
  python main.py --user "<이름>" --memo ./test_memo/memo.txt --receipts ./test_receipts --test-mode

Simple Mode (attendee only):
  python main.py --user "<이름>" --simple

Debug Mode (verbose logging):
  python main.py --user "<이름>" --memo ./test_memo/memo.txt --receipts ./test_receipts --debug --auto-approve

Stage 1 cache/report (debug only):
  python main.py --user "<이름>" --memo ./test_memo/memo.txt --receipts ./test_receipts --stage1-cache-out cache/stage1_cache.json --stage1-report-out reports/stage1_report.md --stage1-only
  python main.py --user "<이름>" --stage1-cache-in cache/stage1_cache.json --stage1-report-out reports/stage1_report.md --auto-approve

Receipt OCR only (debug only):
  python main.py --user "<이름>" --receipts ./test_receipts --receipts-only --receipt-provider gemini_cli --stage1-report-out reports/receipt_ocr_report.md
        """
    )
    
    # MVP mode arguments
    parser.add_argument(
        "--user",
        help="Default attendee name (e.g., '<이름>') - enables MVP mode"
    )
    parser.add_argument(
        "--memo",
        help="Path to memo.txt file"
    )
    parser.add_argument(
        "--simple",
        action="store_true",
        help="Simple mode: fill attendees only using DataProvider API (fast, no popup)"
    )
    parser.add_argument(
        "--receipts",
        help="Path to receipts folder"
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Run MVP without actual browser automation (for testing)"
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip interactive prompts and auto-approve all (for CI/automation)"
    )
    parser.add_argument(
        "--review-only",
        action="store_true",
        help="Display review summary without prompts or execution (for agent-assisted review)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging with timestamps and screenshots"
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress INFO/WARNING logs (only show errors and essential output)"
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Limit number of rows to process (0 = all rows, useful for testing)"
    )
    parser.add_argument(
        "--stage1-cache-in",
        help="Load Stage 1 cache from this path (debug only)"
    )
    parser.add_argument(
        "--stage1-cache-out",
        help="Write Stage 1 cache to this path (debug only)"
    )
    parser.add_argument(
        "--stage1-report-out",
        help="Write Stage 1 report to this path (debug only)"
    )
    parser.add_argument(
        "--stage1-only",
        action="store_true",
        help="Stop after Stage 1 data collection (debug only)"
    )
    parser.add_argument(
        "--receipts-only",
        action="store_true",
        help="Run receipt OCR only (debug only)"
    )
    parser.add_argument(
        "--receipt-provider",
        default="auto",
        help="Receipt OCR provider override (auto, gemini_cli, gemini, claude_code, anthropic)"
    )
    parser.add_argument(
        "--receipt-cache",
        help="Path to receipt OCR cache file (loads if exists, saves after OCR)"
    )
    parser.add_argument(
        "--stage2-cache-in",
        help="Load Stage 2 matching results from this path (debug only)"
    )
    parser.add_argument(
        "--stage2-cache-out",
        help="Write Stage 2 matching results to this path (debug only)"
    )
    parser.add_argument(
        "--stage2-only",
        action="store_true",
        help="Stop after Stage 2 matching (debug only)"
    )
    parser.add_argument(
        "--stage3-cache-in",
        help="Load Stage 3 reviewed plan from this path (debug only)"
    )
    parser.add_argument(
        "--stage3-cache-out",
        help="Write Stage 3 reviewed plan to this path (debug only)"
    )
    parser.add_argument(
        "--stage3-only",
        action="store_true",
        help="Stop after Stage 3 review (debug only)"
    )

    # Local mode arguments
    parser.add_argument(
        "--local",
        action="store_true",
        help="Local-only mode: no remote servers needed. Uses config.yaml for provider settings."
    )
    parser.add_argument(
        "--config",
        help="Path to config.yaml (default: auto-detect from project root or ~/.config/douzone-bot/)"
    )

    # Connection arguments
    parser.add_argument(
        "--cdp-url",
        default=None,
        help="Chrome DevTools Protocol URL (default: built from config chrome_debug_port)"
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip pre-flight API availability checks"
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Run pre-flight checks and exit without running automation"
    )

    args = parser.parse_args()

    # Setup logging
    debug_mode = getattr(args, 'debug', False)
    quiet_mode = getattr(args, 'quiet', False)
    log_file = None
    if debug_mode:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = f"logs/mvp_debug_{timestamp}.log"
    setup_logging(debug_mode=debug_mode, quiet_mode=quiet_mode, log_file=log_file)

    # In quiet mode, suppress print() calls but keep stderr for progress bar
    # Exception: --review-only needs print() for its formatted review output
    if quiet_mode and not getattr(args, 'review_only', False):
        import builtins
        builtins._original_print = builtins.print
        builtins.print = lambda *a, **kw: None

    # Load config early so preflight can be config-aware
    config = load_config(getattr(args, 'config', None))
    config = apply_cli_overrides(config, args)

    # Build cdp_url from config if not explicitly set via --cdp-url
    if args.cdp_url is None:
        args.cdp_url = f"http://localhost:{config.chrome_debug_port}"

    # Pre-flight API checks
    if not getattr(args, 'skip_preflight', False):
        from src.preflight import run_preflight
        report = await run_preflight(args, config=config)
        if not report.all_required_passed:
            sys.exit(1)

    # Exit early if --preflight-only
    if getattr(args, 'preflight_only', False):
        sys.exit(0)

    # Determine mode
    if args.simple:
        if not args.user:
            print("\n❌ Error: --simple requires --user")
            sys.exit(1)
        success = await run_simple_mode(args)
    elif args.user:
        # MVP mode
        success = await run_mvp_mode(args, config=config)
    else:
        # No mode specified
        parser.print_help()
        print("\n❌ Error: Specify either --user (MVP mode) or --simple")
        sys.exit(1)
    
    # run_mvp_mode returns int exit code (0=success, 1=failure, 2=partial)
    # Other modes return bool (True/False)
    if isinstance(success, int):
        sys.exit(success)
    else:
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
