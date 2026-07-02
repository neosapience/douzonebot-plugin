"""
Douzone Expense Automation - MVP v6.2

MVP Mode (Default):
    python main.py --user "<이름>" --memo ./test_memo/memo.txt --receipts ./test_receipts
    python main.py --user "<이름>" --memo ./test_memo/memo.txt --receipts ./test_receipts --test-mode

Simple Mode (attendee only):
    python main.py --user "<이름>" --simple
    python main.py --user "<이름>" --simple --row-count 30

Debug Mode (verbose logging):
    python main.py --user "<이름>" --memo ./test_memo/memo.txt --receipts ./test_receipts --debug

Test Mode (for debugging):
    python main.py --cdp-url <url> --test single-row
    python main.py --cdp-url <url> --test multi-row --rows 3
    python main.py --cdp-url <url> --test screenshot
    python main.py --cdp-url <url> --test grid-info
"""
import asyncio
from typing import Optional
import argparse
import logging
import sys
import os
from datetime import datetime

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
from src.models import ExpenseData
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
    stage2_in = getattr(args, 'stage2_cache_in', None)
    if stage2_in and not args.memo:
        print(f"Memo: (from cache)")
    else:
        print(f"Memo: {args.memo or '(none)'}")
    if stage2_in and not args.receipts:
        print(f"Receipts: (from cache)")
    else:
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
        skip_post_verify=getattr(args, 'skip_post_verify', False),
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
    """Run the simple attendee-only automation flow.

    Uses DataProvider API by default (fast, reliable).
    Use --legacy flag for the old popup-based method.
    """
    if getattr(args, 'legacy', False):
        return await run_simple_mode_legacy(args)
    else:
        return await run_simple_mode_fast(args)


async def run_simple_mode_fast(args):
    """Run simple mode using DataProvider API (no popup, fast)."""
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


async def run_simple_mode_legacy(args):
    """Run the simple attendee-only automation flow (legacy popup-based method)."""
    from src.transaction_parser import TransactionParser

    debug_mode = getattr(args, 'debug', False)
    debug_logger.enabled = debug_mode

    print("\n" + "="*60)
    print("🧾 Douzone Expense Automation - SIMPLE MODE (Legacy)")
    print("="*60)
    print(f"\nUser: {args.user}")
    print(f"CDP URL: {args.cdp_url}")
    print(f"Row Count (manual): {args.row_count or '(auto via OCR)'}")
    print(f"Method: Legacy popup-based (slower)")
    print(f"Debug Mode: {debug_mode}")

    bot = DouzoneAutomation(cdp_url=args.cdp_url, debug=debug_mode)
    total_rows = args.row_count or 0
    max_popup_attempts = 3
    popup_timeout = 5.0
    use_grid_api = False
    dp_missing_rows = None
    dp_missing_set = None

    try:
        await bot.connect()

        if total_rows <= 0:
            print("\n🔍 Detecting total rows from grid interface...")
            try:
                grid_count = await bot.get_grid_row_count()
                if grid_count:
                    total_rows = grid_count
                    use_grid_api = True
                    print(f"✅ Grid interface detected {total_rows} rows")
                else:
                    print("⚠️ Grid interface not available, falling back to STEP2 badge...")
                    parser = TransactionParser(bot.page)
                    badge_count = await parser.get_total_rows_from_header_badge()
                    if badge_count:
                        total_rows = badge_count
                        print(f"✅ STEP2 badge detected {total_rows} rows")
                    else:
                        print("⚠️ STEP2 badge not found, falling back to OCR...")
                        tx_list = await parser.capture_all_transactions()
                        total_rows = tx_list.max_row_num or tx_list.count
                        extra_note = ""
                        if tx_list.max_row_num and tx_list.max_row_num != tx_list.count:
                            extra_note = f" (unique rows: {tx_list.count})"
                        print(f"✅ OCR detected {total_rows} rows (needs attendee: {tx_list.needs_attendee_count}){extra_note}")
            except Exception as e:
                logger.warning(f"OCR row count failed: {e}")
                visible_rows = await bot.get_visible_row_count()
                total_rows = visible_rows
                print(f"⚠️ OCR failed, fallback to visible rows: {visible_rows}")

        if total_rows <= 0:
            print("\n❌ No rows to process")
            return False

        max_rows = getattr(args, 'max_rows', 0)
        if max_rows > 0 and total_rows > max_rows:
            total_rows = max_rows
            print(f"\n⚠️ LIMITED MODE: Processing only first {total_rows} row(s)")

        visible_rows = None
        if use_grid_api:
            print("\n   Using grid API for row navigation...")
            visible_rows = await bot.get_visible_row_count()
            if visible_rows <= 0:
                visible_rows = None
            dp_missing_rows = await bot.get_missing_attendee_rows_from_dp()
            if dp_missing_rows is not None:
                dp_missing_set = set(dp_missing_rows)
        else:
            print("\n   Scrolling grid to top...")
            await bot.scroll_grid_to_top()

        filled = 0
        skipped_existing = 0
        skipped_blocked = 0
        errors = 0
        blocked_rows = []
        error_rows = []
        nav_failure_count = 0  # Track consecutive navigation failures

        for row_index in range(total_rows):
            if dp_missing_set is not None and (row_index + 1) not in dp_missing_set:
                skipped_existing += 1
                continue
            debug_logger.current_row = row_index
            print(f"\r🔄 Processing row {row_index + 1}/{total_rows}...", end="", flush=True)

            try:
                popup_open = False
                warning_skip = False
                row_saved = False
                popup_attempted = False
                row_skipped_existing = False
                view_index = None
                for attempt in range(max_popup_attempts):
                    if not await bot.ensure_popup_closed():
                        errors += 1
                        error_rows.append(row_index + 1)
                        popup_open = None
                        break

                    if use_grid_api:
                        # Use robust navigation with proper waits
                        view_index = await bot.navigate_to_row(row_index, total_rows, visible_rows)

                        if view_index is None:
                            nav_failure_count += 1
                            # Refresh visible_rows if we see multiple consecutive failures
                            if nav_failure_count >= 2:
                                logger.info("Refreshing visible_rows after navigation failures...")
                                visible_rows = await bot.get_visible_row_count()
                                if visible_rows <= 0:
                                    visible_rows = None

                            # Fallback: try scroll-based visibility
                            logger.warning(
                                f"Grid API navigation failed for row {row_index + 1}, "
                                f"falling back to scroll visibility"
                            )
                            row_visible = await bot.ensure_row_visible(row_index, margin_rows=1)
                            if not row_visible:
                                errors += 1
                                error_rows.append(row_index + 1)
                                popup_open = None
                                break
                            view_index = None
                        else:
                            # Reset failure counter on success
                            nav_failure_count = 0
                    else:
                        row_visible = await bot.ensure_row_visible(row_index, margin_rows=1)
                        if not row_visible:
                            errors += 1
                            error_rows.append(row_index + 1)
                            popup_open = None
                            break

                    if view_index is not None:
                        await bot.click_row_by_view(view_index)
                    await bot.click_plus_button(row_index, view_index=view_index)
                    outcome = await bot.wait_for_popup_or_warning(timeout=popup_timeout + (attempt * 2.0))

                    if outcome == "warning":
                        warning_skip = True
                        popup_open = False
                        break
                    if outcome == "popup":
                        popup_open = True
                        popup_attempted = True
                    else:
                        logger.warning(
                            f"Popup did not open for row {row_index + 1} "
                            f"(attempt {attempt + 1}/{max_popup_attempts})"
                        )
                        await asyncio.sleep(0.2)
                        continue

                    attendee_input = bot.page.locator('input[placeholder*="참석자"]').first
                    try:
                        await attendee_input.wait_for(state="visible", timeout=1200)
                    except Exception:
                        # Likely missing 용도/내용 warning dialog - skip in simple mode
                        if await bot.dismiss_missing_yongdo_warning():
                            warning_skip = True
                            popup_open = False
                            break
                        errors += 1
                        error_rows.append(row_index + 1)
                        await bot.close_popup()
                        continue
                    try:
                        await attendee_input.scroll_into_view_if_needed()
                    except Exception:
                        pass

                    current_value = (await attendee_input.input_value()).strip()
                    if current_value:
                        canceled = await bot.cancel_popup()
                        if canceled:
                            skipped_existing += 1
                        else:
                            errors += 1
                            error_rows.append(row_index + 1)
                        row_saved = False
                        row_skipped_existing = True
                        popup_open = False
                        break

                    await attendee_input.fill(args.user)
                    await attendee_input.evaluate("el => el.blur()")
                    for _ in range(5):
                        if (await attendee_input.input_value()).strip() == args.user:
                            break
                        await asyncio.sleep(0.1)
                    debug_logger.fill("참석자", args.user)
                    await asyncio.sleep(0.2)

                    saved = await bot.save_popup()
                    if saved:
                        verified = True
                        if dp_missing_set is not None:
                            # Verify via data provider that attendee is now set
                            verified = False
                            for _ in range(10):
                                await asyncio.sleep(0.2)
                                attendee_val = await bot.get_row_attendee_from_dp(row_index)
                                if attendee_val:
                                    verified = True
                                    break
                        if verified:
                            row_saved = True
                            break
                        logger.warning(f"Row {row_index + 1} save not reflected in data provider; retrying...")
                    else:
                        logger.warning(f"Row {row_index + 1} save failed; retrying...")

                    # retry open/fill if needed
                    popup_open = False
                    await asyncio.sleep(0.2)

                if popup_open is None:
                    continue
                if warning_skip:
                    skipped_blocked += 1
                    blocked_rows.append(row_index + 1)
                    continue
                if row_skipped_existing:
                    continue
                if row_saved:
                    filled += 1
                    if dp_missing_set is not None:
                        dp_missing_set.discard(row_index + 1)
                    continue
                if popup_attempted and not row_saved:
                    errors += 1
                    error_rows.append(row_index + 1)
                    continue
                if not popup_open:
                    skipped_blocked += 1
                    blocked_rows.append(row_index + 1)
                    continue
                errors += 1
                error_rows.append(row_index + 1)

            except Exception as e:
                logger.error(f"Simple mode error on row {row_index + 1}: {e}")
                errors += 1
                error_rows.append(row_index + 1)
                try:
                    await bot.close_popup()
                except Exception:
                    pass

            await asyncio.sleep(0.2)

        print()  # newline after progress

        print(f"\n{'─'*40}")
        print("📊 Simple Mode Summary:")
        print(f"   Total rows: {total_rows}")
        print(f"   Filled attendees: {filled}")
        print(f"   Skipped (already set): {skipped_existing}")
        print(f"   Skipped (blocked by empty 용도): {skipped_blocked}")
        print(f"   Errors: {errors}")

        if blocked_rows:
            print(f"\n⚠️ Blocked rows (popup didn't open): {blocked_rows}")
        if error_rows:
            print(f"\n❌ Error rows: {error_rows}")

        return errors == 0

    finally:
        await bot.close()


# ============================================================================
# TEST MODE (Legacy)
# ============================================================================


async def test_screenshot(bot: DouzoneAutomation):
    """Take a screenshot for debugging."""
    print("\n" + "="*60)
    print("TEST: Screenshot Capture")
    print("="*60)

    try:
        path = await bot.take_screenshot('/app/debug_screenshot.png')
        print(f"\n✅ Screenshot saved to: {path}")
        print("   Copy with: docker cp douzone-bot:/app/debug_screenshot.png ./")
        return True
    except Exception as e:
        print(f"\n❌ Screenshot failed: {e}")
        return False


async def test_grid_info(bot: DouzoneAutomation):
    """Show canvas grid information."""
    print("\n" + "="*60)
    print("TEST: Canvas Grid Info")
    print("="*60)

    try:
        grid = bot.get_canvas_grid()
        print(f"\n[Canvas Grid Position]")
        print(f"  X: {grid.x}")
        print(f"  Y: {grid.y}")
        print(f"  Width: {grid.width}")
        print(f"  Height: {grid.height}")

        print(f"\n[Calculated Positions]")
        print(f"  + Button X: {grid.get_plus_button_x():.0f}")
        print(f"  Row 0 Y: {grid.get_row_y(0):.0f}")
        print(f"  Row 1 Y: {grid.get_row_y(1):.0f}")
        print(f"  Row 2 Y: {grid.get_row_y(2):.0f}")

        visible_rows = await bot.get_visible_row_count()
        print(f"\n[Estimated Visible Rows]: {visible_rows}")

        print("\n✅ Grid info retrieved!")
        return True
    except Exception as e:
        print(f"\n❌ Grid info failed: {e}")
        return False


async def test_single_row(bot: DouzoneAutomation, row_index: int = 0):
    """Process a single row with test data."""
    print("\n" + "="*60)
    print(f"TEST: Single Row Processing (Row {row_index})")
    print("="*60)

    try:
        # Create test data
        test_data = ExpenseData(
            merchant="테스트",
            yongdo="130",
            content="자동화 테스트",
            attendees="<이름>",
        )

        print(f"\n[Test Data]")
        print(f"  참석자: {test_data.attendees}")
        print(f"  내용: {test_data.content}")

        # Take before screenshot
        await bot.take_screenshot('/app/before_process.png')
        print("\n[1] Before screenshot saved")

        # Process the row
        print(f"\n[2] Processing row {row_index}...")
        success = await bot.process_row(row_index, test_data)

        # Take after screenshot
        await bot.take_screenshot('/app/after_process.png')
        print("[3] After screenshot saved")

        if success:
            print("\n✅ Single row processed successfully!")
            print("   Verify: 검증결과 should show '적합'")
        else:
            print("\n⚠️ Single row processing had issues")

        return success

    except Exception as e:
        print(f"\n❌ Single row test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_multi_row(bot: DouzoneAutomation, num_rows: int = 3):
    """Process multiple rows with test data."""
    print("\n" + "="*60)
    print(f"TEST: Multi-Row Processing ({num_rows} rows)")
    print("="*60)

    try:
        # Create test data for each row
        data_list = []
        for i in range(num_rows):
            data_list.append(ExpenseData(
                merchant=f"테스트_{i}",
                yongdo="130",
                content=f"자동화 테스트 {i+1}",
                attendees="<이름>",
            ))

        print(f"\n[Processing {num_rows} rows...]")

        # Take before screenshot
        await bot.take_screenshot('/app/before_multi.png')

        # Process all rows
        result = await bot.process_multiple_rows(data_list, start_row=0)

        # Take after screenshot
        await bot.take_screenshot('/app/after_multi.png')

        print(f"\n[Results]")
        print(f"  Total: {result.total_rows}")
        print(f"  Success: {result.processed_rows}")
        print(f"  Failed: {result.failed_rows}")
        print(f"  Success Rate: {result.success_rate:.1f}%")

        if result.errors:
            print(f"\n[Errors]")
            for err in result.errors:
                print(f"  - {err}")

        if result.failed_rows == 0:
            print("\n✅ All rows processed successfully!")
        else:
            print("\n⚠️ Some rows failed")

        return result.failed_rows == 0

    except Exception as e:
        print(f"\n❌ Multi-row test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_click_plus(bot: DouzoneAutomation, row_index: int = 0):
    """Test clicking the + button to open popup."""
    print("\n" + "="*60)
    print(f"TEST: Click + Button (Row {row_index})")
    print("="*60)

    try:
        # Take before screenshot
        await bot.take_screenshot('/app/before_click.png')
        print("\n[1] Before screenshot saved")

        # Click the + button
        print(f"\n[2] Clicking + button for row {row_index}...")
        await bot.click_plus_button(row_index)

        # Wait a moment
        await asyncio.sleep(1)

        # Take after screenshot
        await bot.take_screenshot('/app/after_click.png')
        print("[3] After screenshot saved")

        # Check if popup opened
        if await bot.is_popup_open():
            print("\n✅ Popup opened successfully!")
            print("   Close with: bot.close_popup()")

            # List visible inputs
            inputs = bot.page.locator('input:visible')
            count = await inputs.count()
            print(f"\n[Visible Inputs: {count}]")
            for i in range(min(count, 10)):  # Show first 10
                inp = inputs.nth(i)
                placeholder = await inp.get_attribute('placeholder') or ''
                if placeholder:
                    print(f"  - {placeholder[:50]}")
        else:
            print("\n⚠️ Popup did not open - check screenshot")

        return True

    except Exception as e:
        print(f"\n❌ Click + test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def run_test_mode(args):
    """Run legacy test mode."""
    # Header
    print("\n" + "="*60)
    print("Douzone Expense Automation - Test Runner v4.0")
    print("="*60)
    print(f"\nCDP URL: {args.cdp_url}")
    print(f"Test: {args.test}")

    bot = DouzoneAutomation(args.cdp_url)

    try:
        # Connect
        print("\n[Connecting...]")
        await bot.connect()
        print("✓ Connected successfully")

        # Run selected test
        if args.test == 'screenshot':
            success = await test_screenshot(bot)
        elif args.test == 'grid-info':
            success = await test_grid_info(bot)
        elif args.test == 'click-plus':
            success = await test_click_plus(bot, args.row)
        elif args.test == 'single-row':
            success = await test_single_row(bot, args.row)
        elif args.test == 'multi-row':
            success = await test_multi_row(bot, args.rows)
        else:
            print(f"Unknown test: {args.test}")
            success = False

        # Summary
        print("\n" + "="*60)
        if success:
            print("TEST RESULT: PASSED ✅")
        else:
            print("TEST RESULT: FAILED ❌")
        print("="*60 + "\n")
        
        return success

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
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
  python main.py --user "<이름>" --simple --row-count 30

Debug Mode (verbose logging):
  python main.py --user "<이름>" --memo ./test_memo/memo.txt --receipts ./test_receipts --debug --auto-approve

Test Mode (debugging):
  python main.py --cdp-url http://localhost:9222 --test screenshot
  python main.py --cdp-url http://localhost:9222 --test grid-info
  python main.py --cdp-url http://localhost:9222 --test single-row
  python main.py --cdp-url http://localhost:9222 --test multi-row --rows 5

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
        "--legacy",
        action="store_true",
        help="Use legacy popup-based method for --simple mode (slower, for debugging)"
    )
    parser.add_argument(
        "--row-count",
        type=int,
        default=0,
        help="Total rows in grid for --simple --legacy mode (0 = detect via OCR)"
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
        help="Receipt OCR provider override (auto, qwen25vl, gemini_cli, gemini, claude_code, anthropic)"
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

    # Test mode arguments
    parser.add_argument(
        "--cdp-url",
        default="http://localhost:9222",
        help="Chrome DevTools Protocol URL (default: http://localhost:9222)"
    )
    parser.add_argument(
        "--test",
        choices=['screenshot', 'grid-info', 'click-plus', 'single-row', 'multi-row'],
        help="Test to run (legacy test mode)"
    )
    parser.add_argument(
        "--row",
        type=int,
        default=0,
        help="Row index for single-row or click-plus test (0-indexed, default: 0)"
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=3,
        help="Number of rows for multi-row test (default: 3)"
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip pre-flight API availability checks"
    )
    parser.add_argument(
        "--skip-post-verify",
        action="store_true",
        help="Skip post-verification checks after automation"
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

    # Load config early so preflight can be config-aware
    config = load_config(getattr(args, 'config', None))
    config = apply_cli_overrides(config, args)

    # Resolve CDP URL from config.yaml if not explicitly set via CLI
    if args.cdp_url == "http://localhost:9222":
        # Check config.yaml for chrome_debug_port
        try:
            import yaml as _yaml
            for _cp in [os.path.join(os.path.expanduser("~"), "douzone-bot", "config.yaml"),
                        os.path.join(os.path.expanduser("~"), ".config", "douzone-bot", "config.yaml")]:
                if os.path.exists(_cp):
                    with open(_cp) as _f:
                        _cfg = _yaml.safe_load(_f) or {}
                    _port = _cfg.get("chrome_debug_port")
                    if _port and _port != 9222:
                        args.cdp_url = f"http://localhost:{_port}"
                    break
        except Exception:
            pass

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
    elif args.test:
        # Test mode
        success = await run_test_mode(args)
    else:
        # No mode specified
        parser.print_help()
        print("\n❌ Error: Specify either --user (MVP mode), --simple, or --test")
        sys.exit(1)
    
    # run_mvp_mode returns int exit code (0=success, 1=failure, 2=partial)
    # Other modes return bool (True/False)
    if isinstance(success, int):
        sys.exit(success)
    else:
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
