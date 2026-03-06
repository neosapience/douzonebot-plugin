"""
Douzone Expense Automation Engine - v4.2 (Enhanced Debug Logging)

Architecture:
- Layer 0: Vision Calibration - Dynamically measure grid layout using Claude Vision
- Layer 1: Canvas coordinates - Calculate click positions from canvas bounding box
- Layer 2: Mouse clicks - Click on grid cells and buttons
- Layer 3: DOM popups - Fill popup forms using Playwright locators
- Layer 4: Screenshots - Use Playwright for fast screenshot capture

Key Insight: Douzone uses Canvas-based grid, so DOM selectors and keyboard
navigation don't work for grid cells. Must use mouse clicks at pixel coordinates.

v4.1: Added Vision Calibration to eliminate hardcoded "magic numbers".
v4.2: Added comprehensive debug logging for every action.
"""

import logging
import asyncio
import os
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

from playwright.async_api import async_playwright, Page

from .proxy import TunnelCDPProxy
from .models import ExpenseData, ProcessingResult, RowStatus
from .calibration import (
    GridCalibration,
    VisionCalibrator,
    ClickTestCalibrator,
    BrowserUseCalibrator,
    DEFAULT_CALIBRATION,
    quick_calibrate,
)

logger = logging.getLogger(__name__)


# Debug logging helper
class DebugLogger:
    """Enhanced debug logger with detailed action tracking."""

    def __init__(self, enabled: bool = True, screenshot_dir: str = "screenshots/debug"):
        self.enabled = enabled
        self.screenshot_dir = screenshot_dir
        self.action_count = 0
        self.current_row = -1
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(screenshot_dir, exist_ok=True)

    def _ts(self) -> str:
        """Current timestamp."""
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def action(self, action_type: str, message: str, **kwargs):
        """Log an action with details."""
        if not self.enabled:
            return
        self.action_count += 1
        row_str = f"[Row {self.current_row}]" if self.current_row >= 0 else ""
        extra = " ".join(f"{k}={v}" for k, v in kwargs.items()) if kwargs else ""
        print(f"  [{self._ts()}] {action_type:8} {row_str} {message} {extra}")

    def click(self, x: float, y: float, target: str):
        """Log a mouse click action."""
        self.action(
            "CLICK", f"→ ({x:.0f}, {y:.0f}) on {target}", x=f"{x:.0f}", y=f"{y:.0f}"
        )

    def keyboard(self, key: str, reason: str):
        """Log a keyboard action."""
        self.action("KEY", f"'{key}' - {reason}")

    def wait(self, seconds: float, reason: str):
        """Log a wait action."""
        self.action("WAIT", f"{seconds:.2f}s - {reason}")

    def popup(self, status: str, details: str = ""):
        """Log popup status."""
        self.action("POPUP", f"{status} {details}")

    def fill(self, field: str, value: str):
        """Log form fill action."""
        display_value = value[:50] + "..." if len(value) > 50 else value
        self.action("FILL", f"{field} = '{display_value}'")

    def state(self, description: str):
        """Log current state."""
        self.action("STATE", description)

    def error(self, message: str):
        """Log an error."""
        self.action("ERROR", f"❌ {message}")

    def success(self, message: str):
        """Log a success."""
        self.action("OK", f"✅ {message}")

    def screenshot_path(self, name: str) -> str:
        """Generate screenshot path."""
        row_str = f"row{self.current_row}_" if self.current_row >= 0 else ""
        return os.path.join(
            self.screenshot_dir,
            f"{row_str}{self.session_id}_{name}_{self.action_count:04d}.png",
        )


# Global debug logger instance
debug = DebugLogger(enabled=True)


# Default grid layout constants (fallback if API query fails)
# Note: These are fallbacks only - actual values are queried from RealGrid API at runtime
DEFAULT_HEADER_HEIGHT = 42  # Fallback header height
DEFAULT_ROW_HEIGHT = 45  # Updated: RealGrid API reports 45px (was 40px hardcoded)
DEFAULT_PLUS_OFFSET = 115  # Distance from right edge to + button column

# Column position constants (percentage from left edge of grid)
YONGDO_COLUMN_PCT = 0.55  # 용도 column at 55% from left

# 용도 (Purpose) option mapping - row index in popup
# These are the common options visible without scrolling
YONGDO_OPTIONS = {
    "중식대": 0,  # 100
    "석식대": 1,  # 110
    "회식대": 2,  # 120
    "간식/음료": 3,  # 130
    "건강검진": 4,  # 140
    "의약품": 5,  # 150
    "사내운영비": 6,  # 160
    "기타복리후생비": 7,  # 170
    "국내출장_항공": 8,  # 200
    "국내출장_숙박": 9,  # 210
}

# Trace rows for extra logging (comma-separated 1-based row numbers)
_TRACE_ROWS_ENV = os.getenv("TRACE_ROWS")
if _TRACE_ROWS_ENV:
    TRACE_ROWS = {
        int(x.strip()) for x in _TRACE_ROWS_ENV.split(",") if x.strip().isdigit()
    }
else:
    TRACE_ROWS = {25}


@dataclass
class CanvasGrid:
    """Canvas grid position and dimensions with calibration support."""

    x: float
    y: float
    width: float
    height: float

    # Calibrated values (or defaults)
    header_height: float = DEFAULT_HEADER_HEIGHT
    row_height: float = DEFAULT_ROW_HEIGHT
    plus_button_offset: float = DEFAULT_PLUS_OFFSET

    def get_row_y(self, row_index: int) -> float:
        """Get Y coordinate for center of a row (0-indexed)."""
        return (
            self.y
            + self.header_height
            + (row_index * self.row_height)
            + (self.row_height / 2)
        )

    def get_plus_button_x(self) -> float:
        """Get X coordinate for the + button column."""
        return self.x + self.width - self.plus_button_offset

    def get_plus_button_coords(self, row_index: int) -> Tuple[float, float]:
        """Get (x, y) coordinates for + button of a specific row."""
        return (self.get_plus_button_x(), self.get_row_y(row_index))

    def get_yongdo_x(self) -> float:
        """Get X coordinate for the 용도 (purpose) column."""
        return self.x + (self.width * YONGDO_COLUMN_PCT)

    def get_yongdo_coords(self, row_index: int) -> Tuple[float, float]:
        """Get (x, y) coordinates for 용도 cell of a specific row."""
        return (self.get_yongdo_x(), self.get_row_y(row_index))


class DouzoneAutomation:
    """
    Main automation engine for Douzone ERP expense claims.

    Implements Mouse-Click Canvas Strategy (v4.1):
    - Uses Playwright for all browser control
    - Vision Calibration for dynamic grid measurement (optional)
    - Calculates coordinates from canvas bounding box
    - Finds popup fields by placeholder text
    - Does NOT rely on keyboard navigation for grid
    """

    def __init__(
        self,
        cdp_url: str = "http://localhost:9222",
        browser_use_api_key: str = None,
        debug: bool = False,
    ):
        self.cdp_url = cdp_url
        self.browser_use_api_key = browser_use_api_key
        self.debug = debug  # Enable visual debugging overlay
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.calibration = DEFAULT_CALIBRATION
        self._canvas_grid = None
        self._calibration = None
        self._anthropic_api_key = None
        self._browser_use_api_key = browser_use_api_key
        self.scroll_offset_y = 0.0  # Track vertical scroll amount
        self._last_calibration_time = None  # Track when calibration was last done
        self._rows_since_calibration = 0  # Track rows processed since last calibration
        self.last_error = None  # Last process_row error (for diagnostics)

    def _should_trace_row(self, row_index: int) -> bool:
        """Return True if this row should emit extra debug logs."""
        return (row_index + 1) in TRACE_ROWS

    async def connect(self, auto_calibrate: bool = True):
        """Connect to the remote Chrome instance."""
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)
            self.context = self.browser.contexts[0]
            self.page = self.context.pages[0]

            # Reset zoom level to ensure coordinate accuracy
            await self.page.evaluate("document.body.style.zoom = '1.0'")

            # Inject debug overlay styles if debug mode is on
            if self.debug:
                await self.page.add_style_tag(
                    content="""
                    .debug-click-marker {
                        position: absolute;
                        width: 10px;
                        height: 10px;
                        background-color: red;
                        border-radius: 50%;
                        pointer-events: none;
                        z-index: 999999;
                        transform: translate(-50%, -50%);
                        box-shadow: 0 0 4px rgba(0,0,0,0.5);
                    }
                """
                )
                logger.info("Visual debug overlay enabled")

            logger.info(f"Connected to Chrome at {self.cdp_url}")

            # Initialize canvas position
            await self._update_canvas_position()

            # Reset scroll state
            await self.scroll_grid_to_top()

            if auto_calibrate:
                await self.calibrate()

        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            raise

    async def click_position(self, x: float, y: float):
        """Click at specific coordinates with visual feedback."""
        if not self.page:
            raise RuntimeError("Not connected to browser")

        if self.debug:
            # Visual debug: show where we are about to click
            await self.page.evaluate(
                """(pos) => {
                    const marker = document.createElement('div');
                    marker.className = 'debug-click-marker';
                    marker.style.left = pos.x + 'px';
                    marker.style.top = pos.y + 'px';
                    document.body.appendChild(marker);
                    setTimeout(() => marker.remove(), 1000);
                }""",
                {"x": x, "y": y},
            )
            await asyncio.sleep(0.2)  # Pause to let user see it

        await self.page.mouse.click(x, y)

    async def close(self):
        """Clean up resources."""
        if self.playwright:
            await self.playwright.stop()
            logger.info("Automation engine closed")

    # =========================================================================
    # Vision Calibration
    # =========================================================================

    async def calibrate(
        self, method: str = "browser_use", force: bool = False
    ) -> GridCalibration:
        """
        Calibrate grid layout parameters.

        Methods:
        - "browser_use": Use browser-use Cloud API (requires BROWSER_USE_API_KEY) - RECOMMENDED
        - "vision": Use Claude Vision API (requires ANTHROPIC_API_KEY)
        - "click_test": Use default values and verify (NO API KEY NEEDED)
        - "auto": Try browser_use → vision → click_test
        - "default": Use hardcoded defaults (no calibration)

        Args:
            method: Calibration method to use
            force: If True, re-calibrate even if already calibrated

        Returns:
            GridCalibration with measured/verified values
        """
        if self._calibration and not force:
            logger.info("Already calibrated, skipping (use force=True to re-calibrate)")
            return self._calibration

        logger.info(f"Running calibration (method={method})...")

        # Handle browser_use specially since quick_calibrate doesn't know about it yet
        if method == "browser_use":
            calibrator = BrowserUseCalibrator(api_key=self._browser_use_api_key)
            if calibrator.is_available:
                self._calibration = await calibrator.calibrate(self.page)
            else:
                logger.warning("browser-use not available, falling back to defaults")
                self._calibration = DEFAULT_CALIBRATION
        elif method == "auto":
            # Try browser_use first, then vision, then click_test
            calibrator = BrowserUseCalibrator(api_key=self._browser_use_api_key)
            if calibrator.is_available:
                self._calibration = await calibrator.calibrate(self.page)
            else:
                self._calibration = await quick_calibrate(
                    self.page, method="auto", api_key=self._anthropic_api_key
                )
        else:
            self._calibration = await quick_calibrate(
                self.page, method=method, api_key=self._anthropic_api_key
            )

        logger.info(f"Calibration complete: {self._calibration.to_dict()}")

        # Track calibration time and reset row counter
        import time

        self._last_calibration_time = time.time()
        self._rows_since_calibration = 0

        return self._calibration

    def get_calibration(self) -> GridCalibration:
        """Get current calibration (or defaults if not calibrated)."""
        return self._calibration or DEFAULT_CALIBRATION

    def should_recalibrate(
        self, rows_threshold: int = 10, time_threshold_minutes: int = 15
    ) -> bool:
        """
        Check if re-calibration is needed based on:
        1. Number of rows processed since last calibration
        2. Time elapsed since last calibration

        Args:
            rows_threshold: Re-calibrate every N rows (default 10)
            time_threshold_minutes: Re-calibrate after N minutes (default 15)

        Returns:
            True if re-calibration is recommended
        """
        import time

        # Never calibrated yet
        if self._last_calibration_time is None:
            return True

        # Check row threshold
        if self._rows_since_calibration >= rows_threshold:
            logger.info(
                f"Re-calibration needed: {self._rows_since_calibration} rows processed (threshold: {rows_threshold})"
            )
            return True

        # Check time threshold
        elapsed_minutes = (time.time() - self._last_calibration_time) / 60.0
        if elapsed_minutes >= time_threshold_minutes:
            logger.info(
                f"Re-calibration needed: {elapsed_minutes:.1f} minutes elapsed (threshold: {time_threshold_minutes})"
            )
            return True

        return False

    # =========================================================================
    # Canvas Grid Handling
    # =========================================================================

    async def _update_canvas_position(self):
        """Update the canvas grid position and dimensions."""
        canvas = self.page.locator("canvas[role=application]")
        if await canvas.count() == 0:
            raise ValueError("Canvas element not found - is the Douzone grid visible?")

        box = await canvas.bounding_box()
        if not box:
            raise ValueError("Could not get canvas bounding box")

        # Use calibrated values if available, otherwise defaults
        cal = self.get_calibration()

        # Query RealGrid API for actual row height (more reliable than hardcoded values)
        display_opts = await self.get_grid_display_options()
        row_height = cal.row_height  # Default from calibration
        if display_opts.get("success") and display_opts.get("rowHeight"):
            api_row_height = display_opts["rowHeight"]
            if api_row_height != row_height:
                logger.info(
                    f"Using RealGrid API rowHeight={api_row_height} (was {row_height})"
                )
                row_height = api_row_height

        self._canvas_grid = CanvasGrid(
            x=box["x"],
            y=box["y"],
            width=box["width"],
            height=box["height"],
            header_height=cal.header_height,
            row_height=row_height,
            plus_button_offset=cal.plus_button_offset,
        )
        logger.info(
            f"Canvas grid: x={box['x']}, y={box['y']}, w={box['width']}, h={box['height']} "
            f"(header={cal.header_height}, row={row_height}, plus_offset={cal.plus_button_offset})"
        )

    async def scroll_grid_to_top(self):
        """
        Scroll the Douzone grid to the very top.
        This ensures row indices match actual Douzone row numbers.

        WARNING: Do NOT use keyboard shortcuts like Ctrl+Home - they may trigger
        unintended batch operations in Douzone!
        """
        debug.action("SCROLL", "scrolling grid to top (mouse wheel only)")

        canvas = self.page.locator("canvas[role=application]")
        box = await canvas.bounding_box()
        if not box:
            debug.error("Canvas element not found for scrolling!")
            raise ValueError("Canvas element not found")

        debug.state(
            f"Canvas found: x={box['x']:.0f}, y={box['y']:.0f}, w={box['width']:.0f}, h={box['height']:.0f}"
        )

        # Click to focus the canvas first
        center_x = box["x"] + box["width"] / 2
        center_y = box["y"] + box["height"] / 2
        debug.click(center_x, center_y, "canvas center (to focus)")
        await self.page.mouse.click(center_x, center_y)
        await asyncio.sleep(0.3)

        # ONLY use mouse wheel scrolling - keyboard shortcuts can trigger batch operations!
        debug.action("WHEEL", "scrolling up with mouse wheel (30 iterations)")
        for i in range(30):
            await self.page.mouse.wheel(0, -5000)
            await asyncio.sleep(0.1)

        # Small pause to let grid settle
        debug.wait(0.5, "letting grid settle after scroll")
        await asyncio.sleep(0.5)

        # Take screenshot after scroll
        if debug.enabled:
            await self.page.screenshot(
                path=debug.screenshot_path("after_scroll_to_top")
            )
            debug.state("Screenshot saved after scroll to top")

        self.scroll_offset_y = 0.0
        debug.success("Scrolled grid to top")
        logger.info("Scrolled grid to top (mouse wheel only)")

    def get_canvas_grid(self) -> CanvasGrid:
        """Get the current canvas grid position."""
        if not self._canvas_grid:
            raise ValueError("Canvas position not initialized - call connect() first")
        return self._canvas_grid

    def _get_row_y(self, row_index: int) -> float:
        """Get absolute Y coordinate for a row, accounting for scroll offset."""
        grid = self.get_canvas_grid()
        return (
            grid.y
            + grid.header_height
            + (row_index * grid.row_height)
            - self.scroll_offset_y
            + (grid.row_height / 2)
        )

    def _get_view_row_y(self, view_index: int) -> float:
        """Get Y coordinate for a visible row index (0 = top visible row)."""
        grid = self.get_canvas_grid()
        return (
            grid.y
            + grid.header_height
            + (view_index * grid.row_height)
            + (grid.row_height / 2)
        )

    async def scroll_grid_by(self, delta_y: float):
        """Scroll the grid by a delta (positive = down, negative = up)."""
        if abs(delta_y) < 1:
            return

        canvas = self.page.locator("canvas[role=application]")
        box = await canvas.bounding_box()
        if not box:
            debug.error("Canvas element not found for scrolling!")
            raise ValueError("Canvas element not found")

        center_x = box["x"] + box["width"] / 2
        center_y = box["y"] + box["height"] / 2
        debug.click(center_x, center_y, "canvas center (to focus)")
        await self.page.mouse.click(center_x, center_y)
        await asyncio.sleep(0.1)

        debug.action("WHEEL", f"scrolling grid by {delta_y:.0f}px")
        await self.page.mouse.wheel(0, int(delta_y))
        await asyncio.sleep(0.2)

        self.scroll_offset_y = max(0.0, self.scroll_offset_y + delta_y)

    async def get_grid_row_count(self) -> Optional[int]:
        """
        Read total row count directly from the grid interface (fundamental source).
        Returns None if interface not accessible.
        """
        js = """
        () => {
          const el = document.querySelector('[data-orbit-component=\"OBTDataGrid\"]');
          if (!el) return null;
          const key = Object.keys(el).find(k => k.startsWith('__reactInternalInstance'));
          const fiber = key ? el[key] : null;
          let cur = fiber;
          while (cur) {
            const st = cur.memoizedState;
            if (st && typeof st === 'object' && st.interface) {
              const iface = st.interface;
              if (iface && typeof iface.getRowCount === 'function') {
                return iface.getRowCount();
              }
              const gv = iface && iface._gridView;
              if (gv && typeof gv.getItemCount === 'function') {
                return gv.getItemCount();
              }
            }
            cur = cur.return;
          }
          return null;
        }
        """
        try:
            value = await self.page.evaluate(js)
            return int(value) if value else None
        except Exception as e:
            logger.warning(f"Grid interface row count failed: {e}")
            return None

    async def set_grid_top_item(self, row_index: int) -> bool:
        """Scroll grid so the given row_index becomes the top visible row."""
        js = """
        (target) => {
          const el = document.querySelector('[data-orbit-component=\"OBTDataGrid\"]');
          if (!el) return false;
          const key = Object.keys(el).find(k => k.startsWith('__reactInternalInstance'));
          const fiber = key ? el[key] : null;
          let cur = fiber;
          while (cur) {
            const st = cur.memoizedState;
            if (st && typeof st === 'object' && st.interface) {
              const gv = st.interface._gridView;
              if (gv && typeof gv.setTopItem === 'function') {
                gv.setTopItem(target);
                return true;
              }
            }
            cur = cur.return;
          }
          return false;
        }
        """
        try:
            return bool(await self.page.evaluate(js, row_index))
        except Exception as e:
            logger.warning(f"set_grid_top_item failed: {e}")
            return False

    async def set_grid_current_row(self, row_index: int) -> bool:
        """Set current grid row using grid view API (no mouse click)."""
        js = """
        (target) => {
          const el = document.querySelector('[data-orbit-component=\"OBTDataGrid\"]');
          if (!el) return false;
          const key = Object.keys(el).find(k => k.startsWith('__reactInternalInstance'));
          const fiber = key ? el[key] : null;
          let cur = fiber;
          while (cur) {
            const st = cur.memoizedState;
            if (st && typeof st === 'object' && st.interface) {
              const gv = st.interface._gridView;
              if (gv && typeof gv.setCurrent === 'function') {
                gv.setCurrent({itemIndex: target, dataRow: target});
                return true;
              }
            }
            cur = cur.return;
          }
          return false;
        }
        """
        try:
            return bool(await self.page.evaluate(js, row_index))
        except Exception as e:
            logger.warning(f"set_grid_current_row failed: {e}")
            return False

    async def get_grid_current_row(self) -> Optional[int]:
        """Get current grid row index via grid view API."""
        js = """
        () => {
          const grid = window.Grids?.getActiveGrid();
          if (!grid || typeof grid.getCurrent !== 'function') return null;
          const cur = grid.getCurrent();
          return cur ? cur.itemIndex : null;
        }
        """
        try:
            value = await self.page.evaluate(js)
            return int(value) if value is not None else None
        except Exception as e:
            logger.warning(f"get_grid_current_row failed: {e}")
            return None

    async def get_grid_top_item(self) -> Optional[int]:
        """Get current top visible row index from grid view API."""
        js = """
        () => {
          const el = document.querySelector('[data-orbit-component="OBTDataGrid"]');
          if (!el) return null;
          const key = Object.keys(el).find(k => k.startsWith('__reactInternalInstance'));
          const fiber = key ? el[key] : null;
          let cur = fiber;
          while (cur) {
            const st = cur.memoizedState;
            if (st && typeof st === 'object' && st.interface) {
              const gv = st.interface._gridView;
              if (gv && typeof gv.getTopItem === 'function') {
                return gv.getTopItem();
              }
            }
            cur = cur.return;
          }
          return null;
        }
        """
        try:
            value = await self.page.evaluate(js)
            return int(value) if value is not None else None
        except Exception as e:
            logger.warning(f"get_grid_top_item failed: {e}")
            return None

    async def get_grid_display_options(self) -> dict:
        """
        Query RealGrid API for display options including rowHeight.

        Returns dict with 'rowHeight' and other display options.
        Falls back to defaults if API unavailable.
        """
        js = """
        () => {
            try {
                const grid = window.Grids?.getActiveGrid();
                if (grid && typeof grid.getDisplayOptions === 'function') {
                    const opts = grid.getDisplayOptions();
                    return {
                        rowHeight: opts.rowHeight || null,
                        minRowHeight: opts.minRowHeight || null,
                        maxRowHeight: opts.maxRowHeight || null,
                        success: true
                    };
                }
                return { success: false };
            } catch (e) {
                return { success: false, error: e.message };
            }
        }
        """
        try:
            result = await self.page.evaluate(js)
            if result.get("success") and result.get("rowHeight"):
                logger.info(f"RealGrid API: rowHeight={result['rowHeight']}")
                return result
            else:
                logger.warning(
                    f"Could not get display options from RealGrid API: {result}"
                )
                return {"success": False, "rowHeight": DEFAULT_ROW_HEIGHT}
        except Exception as e:
            logger.warning(f"get_grid_display_options failed: {e}")
            return {"success": False, "rowHeight": DEFAULT_ROW_HEIGHT}

    async def wait_for_row_in_view(
        self,
        row_index: int,
        visible_rows: Optional[int],
        timeout: float = 1.0,
        interval: float = 0.05,
    ) -> Optional[int]:
        """
        Wait until the given row index is within the current visible window.

        Returns the latest top_item (may be None if unavailable).
        """
        deadline = asyncio.get_event_loop().time() + timeout
        last_top = None
        while asyncio.get_event_loop().time() < deadline:
            top_item = await self.get_grid_top_item()
            if top_item is not None:
                last_top = top_item
                if visible_rows is None:
                    return top_item
                if top_item <= row_index < top_item + visible_rows:
                    return top_item
            await asyncio.sleep(interval)
        return last_top

    async def navigate_to_row(
        self, row_index: int, total_rows: int, visible_rows: Optional[int] = None
    ) -> Optional[int]:
        """
        Robustly navigate to a specific row and return its view_index.

        This method handles Grid API navigation with proper synchronization:
        1. Sets the target row as current (triggers auto-scroll)
        2. Waits for UI to settle
        3. If row not visible, explicitly scrolls to position it
        4. Returns the view_index (row position on screen) or None on failure

        Args:
            row_index: Target row (0-indexed)
            total_rows: Total number of rows in grid
            visible_rows: Number of visible rows (will be computed if None)

        Returns:
            view_index (0 = top visible row) or None on failure
        """
        # Get visible_rows if not provided
        if visible_rows is None:
            visible_rows = await self.get_visible_row_count()
            if visible_rows <= 0:
                logger.warning("Could not determine visible_rows, using estimate of 10")
                visible_rows = 10

        # Step 1: Set current row - this may trigger auto-scroll
        logger.debug(f"navigate_to_row: setting current row to {row_index}")
        await self.set_grid_current_row(row_index)

        # Step 2: Wait for the row to appear in view
        top_item = await self.wait_for_row_in_view(row_index, visible_rows, timeout=1.0)

        # Step 3: If row is not visible, explicitly scroll
        if top_item is None or not (top_item <= row_index < top_item + visible_rows):
            logger.debug(
                f"navigate_to_row: row {row_index} not visible (top={top_item}), forcing scroll"
            )

            # Calculate optimal top_item to position target row near middle of view
            target_top = max(0, row_index - visible_rows // 3)
            max_top = max(0, total_rows - visible_rows)
            target_top = min(target_top, max_top)

            await self.set_grid_top_item(target_top)
            await asyncio.sleep(0.3)  # Wait for scroll animation

            # Re-set current row and wait again
            await self.set_grid_current_row(row_index)
            top_item = await self.wait_for_row_in_view(
                row_index, visible_rows, timeout=1.0
            )

        # Step 4: Final verification and view_index calculation
        if top_item is None:
            logger.warning(f"navigate_to_row: Grid API unavailable for row {row_index}")
            return None

        view_index = row_index - top_item

        if view_index < 0 or view_index >= visible_rows:
            logger.warning(
                f"navigate_to_row: view_index out of bounds "
                f"(row={row_index}, top={top_item}, view={view_index}, visible={visible_rows})"
            )
            # Try one more time with direct scroll
            target_top = max(0, row_index - 1)
            await self.set_grid_top_item(target_top)
            await asyncio.sleep(0.3)
            top_item = await self.get_grid_top_item()
            if top_item is not None:
                view_index = row_index - top_item
                if 0 <= view_index < visible_rows:
                    logger.debug(f"navigate_to_row: recovered, view_index={view_index}")
                    return view_index
            return None

        logger.debug(
            f"navigate_to_row: success, row={row_index}, top={top_item}, view_index={view_index}"
        )
        return view_index

    async def get_row_attendee_from_dp(self, row_index: int) -> Optional[str]:
        """Read attendee (참석자) from grid data provider for a row (0-indexed)."""
        js = r"""
        (rowIndex) => {
          const el = document.querySelector('[data-orbit-component="OBTDataGrid"]');
          if (!el) return null;
          const key = Object.keys(el).find(k => k.startsWith('__reactInternalInstance'));
          const fiber = key ? el[key] : null;
          let cur = fiber;
          while (cur) {
            const st = cur.memoizedState;
            if (st && typeof st === 'object' && st.interface) {
              const iface = st.interface;
              const gv = iface._gridView || {};
              const ds = gv._dataSource || null;
              const dp = ds && ds._dp ? ds._dp : null;
              if (!dp) return null;
              const fields = dp._fieldNames || [];
              const values = dp._values || [];
              const idxColumnMap = fields.indexOf('COLUMNMAPLIST');
              const row = values[rowIndex];
              if (!row || idxColumnMap < 0) return null;
              const raw = row[idxColumnMap];
              if (typeof raw !== 'string' || !raw.startsWith('[')) return null;
              try {
                const arr = JSON.parse(raw);
                const att = arr.find(x => x && x.columnNm === '참석자');
                return att ? (att.columnDc || '').trim() : '';
              } catch (e) {
                return null;
              }
            }
            cur = cur.return;
          }
          return null;
        }
        """
        try:
            value = await self.page.evaluate(js, row_index)
            if value is None:
                return None
            return str(value)
        except Exception as e:
            logger.warning(f"get_row_attendee_from_dp failed: {e}")
            return None

    async def get_missing_attendee_rows_from_dp(self) -> Optional[list]:
        """Return 1-based row numbers missing attendee from grid data provider."""
        js = r"""
        () => {
          const el = document.querySelector('[data-orbit-component="OBTDataGrid"]');
          if (!el) return null;
          const key = Object.keys(el).find(k => k.startsWith('__reactInternalInstance'));
          const fiber = key ? el[key] : null;
          let cur = fiber;
          while (cur) {
            const st = cur.memoizedState;
            if (st && typeof st === 'object' && st.interface) {
              const iface = st.interface;
              const gv = iface._gridView || {};
              const ds = gv._dataSource || null;
              const dp = ds && ds._dp ? ds._dp : null;
              if (!dp) return null;
              const fields = dp._fieldNames || [];
              const values = dp._values || [];
              const idxColumnMap = fields.indexOf('COLUMNMAPLIST');
              const missing = [];
              for (let i = 0; i < values.length; i++) {
                const row = values[i];
                let attendee = '';
                if (idxColumnMap >= 0) {
                  const raw = row[idxColumnMap];
                  if (typeof raw === 'string' && raw.startsWith('[')) {
                    try {
                      const arr = JSON.parse(raw);
                      const att = arr.find(x => x && x.columnNm === '참석자');
                      attendee = att ? (att.columnDc || '').trim() : '';
                    } catch (e) {
                      attendee = '';
                    }
                  }
                }
                if (!attendee) missing.push(i + 1);
              }
              return missing;
            }
            cur = cur.return;
          }
          return null;
        }
        """
        try:
            value = await self.page.evaluate(js)
            return list(value) if value is not None else None
        except Exception as e:
            logger.warning(f"get_missing_attendee_rows_from_dp failed: {e}")
            return None

    async def read_all_transactions_from_grid(self) -> Optional[list]:
        """
        Read ALL transaction data directly from the Grid DataProvider.

        This is MUCH faster and more accurate than OCR-based parsing.
        Uses window.Grids.getActiveGrid().getDataSource().getJsonRow(i).

        Returns:
            List of transaction dicts with fields:
            - row_num (1-based)
            - datetime (issDtTime)
            - merchant (chainName)
            - amount (demandAm, integer)
            - purpose_code (typeCd)
            - purpose_name (typeNm)
            - attendees (columnDc180)
            - content (rmkDc)
            - validation (checkNm)
            - supplier_name (columnDc510)
            - supplier_biz_no (columnDc520)
            - needs_processing (checkNm != "적합")
        """
        js = """
        () => {
            // First try window.Grids API (cleaner)
            let grid = null;
            if (typeof window.Grids !== 'undefined' && typeof window.Grids.getActiveGrid === 'function') {
                grid = window.Grids.getActiveGrid();
            }

            if (!grid) {
                return { error: 'Grid not active. Click on the grid first.' };
            }

            const dp = grid.getDataSource();
            if (!dp) {
                return { error: 'DataProvider not found' };
            }

            const count = dp.getRowCount();
            const rows = [];

            for (let i = 0; i < count; i++) {
                const r = dp.getJsonRow(i);
                rows.push({
                    row_num: i + 1,
                    datetime: r.issDtTime || '',
                    merchant: r.chainName || '',
                    amount: parseInt(r.demandAm) || 0,
                    purpose_code: r.typeCd || '',
                    purpose_name: r.typeNm || '',
                    attendees: r.columnDc180 || '',
                    content: r.rmkDc || '',
                    validation: r.checkNm || '',
                    supplier_name: r.columnDc510 || '',
                    supplier_biz_no: r.columnDc520 || '',
                    needs_processing: r.checkNm !== '적합'
                });
            }

            return {
                success: true,
                row_count: count,
                transactions: rows
            };
        }
        """
        try:
            # Ensure grid is active by clicking on it first
            await self._activate_grid()

            result = await self.page.evaluate(js)

            if result.get("error"):
                logger.error(
                    f"read_all_transactions_from_grid failed: {result['error']}"
                )
                return None

            logger.info(f"Read {result['row_count']} transactions from Grid API")
            return result.get("transactions", [])

        except Exception as e:
            logger.error(f"read_all_transactions_from_grid exception: {e}")
            return None

    async def _activate_grid(self):
        """Click on the grid canvas to activate it (required for Grid API)."""
        try:
            canvas = self.page.locator('canvas[role="application"]')
            box = await canvas.bounding_box()
            if box:
                click_x = box["x"] + box["width"] / 2
                click_y = box["y"] + 60  # Below header
                await self.page.mouse.click(click_x, click_y)
                await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning(f"_activate_grid failed: {e}")

    async def get_rows_needing_attention(self) -> Optional[list]:
        """
        Get list of rows that need processing (checkNm != "적합").

        Returns list of dicts with row info for rows that need attention.
        """
        transactions = await self.read_all_transactions_from_grid()
        if transactions is None:
            return None

        return [tx for tx in transactions if tx.get("needs_processing", False)]

    # =========================================================================
    # DataProvider-based Direct Data Manipulation (No Popup Required)
    # =========================================================================

    async def activate_grid_for_api(self) -> bool:
        """
        Activate the grid for window.Grids API access.

        Must be called before using DataProvider methods.
        Returns True if grid is active, False otherwise.
        """
        await self._activate_grid()

        # Verify grid is active
        is_active = await self.page.evaluate("""
            () => {
                return !!(window.Grids && window.Grids.getActiveGrid());
            }
        """)

        if is_active:
            logger.info("Grid activated for API access")
        else:
            logger.warning("Grid activation failed - Grids API not available")

        return is_active

    async def get_row_fields_via_dp(self, row_index: int) -> Optional[Dict[str, str]]:
        """
        Read basic row fields (용도, 내용) from DataProvider for a specific row.

        Returns dict with keys: yongdo, content (empty string if missing),
        or None if API access failed.
        """
        await self.activate_grid_for_api()

        js = """
        (rowIndex) => {
            const grid = window.Grids?.getActiveGrid();
            if (!grid) return { error: 'Grid not active' };
            const dp = grid.getDataSource();
            if (!dp) return { error: 'No DataSource' };
            const row = dp.getJsonRow(rowIndex);
            return {
                yongdo: (row.typeNm || '').trim(),
                content: (row.rmkDc || '').trim()
            };
        }
        """
        try:
            result = await self.page.evaluate(js, row_index)
            if not result or result.get("error"):
                logger.warning(
                    f"get_row_fields_via_dp failed: {result.get('error') if result else 'no result'}"
                )
                return None
            return {
                "yongdo": result.get("yongdo", ""),
                "content": result.get("content", ""),
            }
        except Exception as e:
            logger.warning(f"get_row_fields_via_dp exception: {e}")
            return None

    async def get_row_status_via_dp(self, row_index: int) -> Optional[Dict[str, str]]:
        """
        Read row fields including validation from DataProvider.

        Returns dict with keys: yongdo, content, validation, attendee.
        """
        await self.activate_grid_for_api()

        js = """
        (rowIndex) => {
            const grid = window.Grids?.getActiveGrid();
            if (!grid) return { error: 'Grid not active' };
            const dp = grid.getDataSource();
            if (!dp) return { error: 'No DataSource' };
            const row = dp.getJsonRow(rowIndex);
            return {
                yongdo: (row.typeNm || '').trim(),
                content: (row.rmkDc || '').trim(),
                validation: (row.checkNm || '').trim(),
                attendee: (row.columnDc180 || '').trim()
            };
        }
        """
        try:
            result = await self.page.evaluate(js, row_index)
            if not result or result.get("error"):
                logger.warning(
                    f"get_row_status_via_dp failed: {result.get('error') if result else 'no result'}"
                )
                return None
            return {
                "yongdo": result.get("yongdo", ""),
                "content": result.get("content", ""),
                "validation": result.get("validation", ""),
                "attendee": result.get("attendee", ""),
            }
        except Exception as e:
            logger.warning(f"get_row_status_via_dp exception: {e}")
            return None

    async def get_column_offset_by_name(self, column_name: str) -> Optional[float]:
        """
        Return the X offset (from grid left) for the center of a column by name.
        Uses RealGrid column metadata (displayIndex/displayWidth).
        """
        js = """
        (colName) => {
            const grid = window.Grids?.getActiveGrid();
            if (!grid) return { error: 'Grid not active' };
            const cols = typeof grid.getColumns === 'function' ? grid.getColumns() : null;
            if (!cols) return { error: 'No columns' };
            const ordered = cols
                .filter(c => c && c.visible !== false)
                .sort((a, b) => (a.displayIndex ?? 0) - (b.displayIndex ?? 0));
            let x = 0;
            for (const col of ordered) {
                const name = col.name || col.fieldName || col.id;
                const w = col.displayWidth || col.width || 0;
                if (name === colName) {
                    return { x: x + (w / 2), width: w };
                }
                x += w;
            }
            return { error: 'Column not found' };
        }
        """
        try:
            result = await self.page.evaluate(js, column_name)
            if not result or result.get("error"):
                logger.warning(
                    f"get_column_offset_by_name failed for {column_name}: "
                    f"{result.get('error') if result else 'no result'}"
                )
                return None
            return float(result.get("x"))
        except Exception as e:
            logger.warning(f"get_column_offset_by_name exception: {e}")
            return None

    async def fill_content_cell_ui(self, row_index: int, content: str) -> bool:
        """
        Fill the 내용 cell using UI editing (click + F2 + type + Enter).
        This is more reliable than DP-only writes for validation.
        """
        if not content:
            return False

        await self._activate_grid()

        total_rows = await self.get_grid_row_count() or (row_index + 1)
        visible_rows = await self.get_visible_row_count() or 10
        view_index = await self.navigate_to_row(row_index, total_rows, visible_rows)
        if view_index is None:
            return False

        col_offset = await self.get_column_offset_by_name("rmkDc")
        if col_offset is None:
            return False

        grid = self.get_canvas_grid()
        rel_y = await self._find_rel_y_for_row(row_index, view_index)
        x = grid.x + col_offset
        y = grid.y + rel_y

        await self.click_position(x, y)
        await asyncio.sleep(0.2)
        await self.page.keyboard.press("F2")
        await asyncio.sleep(0.2)
        await self.page.keyboard.press("Control+A")
        await asyncio.sleep(0.1)
        await self.page.keyboard.type(content, delay=30)
        await asyncio.sleep(0.1)
        await self.page.keyboard.press("Enter")
        await asyncio.sleep(0.6)
        await self.page.evaluate("() => window.Grids?.getActiveGrid?.().commit?.(true)")
        await asyncio.sleep(0.4)

        verify = await self.get_row_fields_via_dp(row_index)
        return bool(verify and verify.get("content") == content)

    async def set_content_via_dp(self, row_index: int, content: str) -> bool:
        """
        Set 내용 (rmkDc) using DataProvider for a specific row.

        This is used before opening the popup when 내용 is required.
        Returns True if the value was written and verified.
        """
        if not content:
            return False

        await self.activate_grid_for_api()

        js_set = """
        (args) => {
            const grid = window.Grids?.getActiveGrid();
            if (!grid) return { error: 'Grid not active' };

            const dp = grid.getDataSource();
            if (!dp) return { error: 'No DataSource' };

            try {
                dp.setValue(args.row, 'rmkDc', args.content);
            } catch (e) {
                return { error: String(e) };
            }
            return { ok: true };
        }
        """
        js_get = """
        (rowIndex) => {
            const grid = window.Grids?.getActiveGrid();
            if (!grid) return { error: 'Grid not active' };
            const dp = grid.getDataSource();
            if (!dp) return { error: 'No DataSource' };
            const row = dp.getJsonRow(rowIndex);
            return { value: (row.rmkDc || '').trim() };
        }
        """
        js_commit = """
        () => {
            const grid = window.Grids?.getActiveGrid();
            if (!grid) return false;
            try {
                if (typeof grid.commit === 'function') {
                    grid.commit(true);
                    return true;
                }
                const gv = grid._gridView;
                if (gv && typeof gv.commit === 'function') {
                    gv.commit(true);
                    return true;
                }
            } catch (e) {
                return false;
            }
            return false;
        }
        """
        try:
            result = await self.page.evaluate(
                js_set, {"row": row_index, "content": content}
            )
            if not result or result.get("error"):
                logger.warning(
                    f"set_content_via_dp failed: {result.get('error') if result else 'no result'}"
                )
                return False

            # Press Enter to commit the edit in the grid UI
            await self._activate_grid()
            await self.page.keyboard.press("Enter")
            await asyncio.sleep(0.6)
            await self.page.evaluate(js_commit)
            await asyncio.sleep(0.4)

            for _ in range(3):
                verify = await self.page.evaluate(js_get, row_index)
                if not verify or verify.get("error"):
                    logger.warning(
                        f"set_content_via_dp verify failed: {verify.get('error') if verify else 'no result'}"
                    )
                    return False
                if (verify.get("value") or "").strip() == content.strip():
                    return True
                await asyncio.sleep(0.3)
            return False
        except Exception as e:
            logger.warning(f"set_content_via_dp exception: {e}")
            return False

    async def get_missing_attendee_rows_via_grids_api(self) -> Optional[list]:
        """
        Get rows missing attendee using window.Grids API (more reliable).

        Returns list of 0-indexed row numbers that need attendee filled.
        Also checks validation status (checkNm) since DataProvider writes
        don't trigger Douzone's validation - need popup-based entry.
        """
        js = """
        () => {
            const grid = window.Grids?.getActiveGrid();
            if (!grid) return { error: 'Grid not active' };

            const dp = grid.getDataSource();
            if (!dp) return { error: 'No DataSource' };

            const rowCount = dp.getRowCount();
            const missing = [];

            for (let i = 0; i < rowCount; i++) {
                const row = dp.getJsonRow(i);
                const attendee = (row.columnDc180 || '').trim();
                const checkNm = (row.checkNm || '').trim();
                // Row needs filling if:
                // 1. No attendee value, OR
                // 2. Validation shows attendee error (DataProvider write didn't trigger validation)
                if (!attendee || checkNm.includes('참석자')) {
                    missing.push(i);  // 0-indexed
                }
            }

            return { missing, total: rowCount };
        }
        """
        try:
            result = await self.page.evaluate(js)
            if "error" in result:
                logger.warning(
                    f"get_missing_attendee_rows_via_grids_api: {result['error']}"
                )
                return None
            logger.info(
                f"Found {len(result['missing'])} rows missing attendee out of {result['total']}"
            )
            return result["missing"]
        except Exception as e:
            logger.error(f"get_missing_attendee_rows_via_grids_api failed: {e}")
            return None

    async def set_attendee_via_dataprovider(
        self, row_index: int, attendee: str
    ) -> bool:
        """
        Set attendee (참석자) for a single row using DataProvider API.

        This directly modifies the grid data without opening the popup.

        Args:
            row_index: 0-indexed row number
            attendee: Attendee name(s) to set

        Returns:
            True if successful, False otherwise
        """
        js = """
        (args) => {
            const grid = window.Grids?.getActiveGrid();
            if (!grid) return { success: false, error: 'Grid not active' };

            const dp = grid.getDataSource();
            if (!dp) return { success: false, error: 'No DataSource' };

            try {
                dp.setValue(args.row, 'columnDc180', args.attendee);

                // Verify the value was set
                const row = dp.getJsonRow(args.row);
                const newValue = (row.columnDc180 || '').trim();

                return {
                    success: newValue === args.attendee.trim(),
                    newValue: newValue
                };
            } catch (e) {
                return { success: false, error: e.toString() };
            }
        }
        """
        try:
            result = await self.page.evaluate(
                js, {"row": row_index, "attendee": attendee}
            )
            if result.get("success"):
                logger.debug(f"Set attendee for row {row_index + 1}: '{attendee}'")
                return True
            else:
                logger.warning(
                    f"Failed to set attendee for row {row_index + 1}: {result.get('error', 'unknown')}"
                )
                return False
        except Exception as e:
            logger.error(f"set_attendee_via_dataprovider failed: {e}")
            return False

    async def set_attendees_batch(self, row_attendee_map: dict) -> dict:
        """
        Set attendees for multiple rows at once using DataProvider API.

        Args:
            row_attendee_map: Dict mapping 0-indexed row numbers to attendee strings
                              e.g., {0: "홍길동", 2: "홍길동, 김철수"}

        Returns:
            Dict with 'success_count', 'failed_rows', 'total'
        """
        js = """
        (rowMap) => {
            const grid = window.Grids?.getActiveGrid();
            if (!grid) return { error: 'Grid not active' };

            const dp = grid.getDataSource();
            if (!dp) return { error: 'No DataSource' };

            const results = { success: [], failed: [] };

            for (const [rowStr, attendee] of Object.entries(rowMap)) {
                const row = parseInt(rowStr);
                try {
                    dp.setValue(row, 'columnDc180', attendee);

                    // Verify
                    const data = dp.getJsonRow(row);
                    if ((data.columnDc180 || '').trim() === attendee.trim()) {
                        results.success.push(row);
                    } else {
                        results.failed.push(row);
                    }
                } catch (e) {
                    results.failed.push(row);
                }
            }

            return results;
        }
        """
        try:
            # Convert int keys to strings for JavaScript
            str_key_map = {str(k): v for k, v in row_attendee_map.items()}
            result = await self.page.evaluate(js, str_key_map)
            if "error" in result:
                logger.error(f"set_attendees_batch: {result['error']}")
                return {
                    "success_count": 0,
                    "failed_rows": list(row_attendee_map.keys()),
                    "total": len(row_attendee_map),
                }

            logger.info(
                f"Batch set attendees: {len(result['success'])} success, {len(result['failed'])} failed"
            )
            return {
                "success_count": len(result["success"]),
                "failed_rows": result["failed"],
                "total": len(row_attendee_map),
            }
        except Exception as e:
            logger.error(f"set_attendees_batch failed: {e}")
            return {
                "success_count": 0,
                "failed_rows": list(row_attendee_map.keys()),
                "total": len(row_attendee_map),
            }

    async def click_save_button(self, timeout: float = 5.0) -> bool:
        """
        Click the 저장 (Save) button to persist DataProvider changes.

        Args:
            timeout: Max seconds to wait for button to be enabled

        Returns True if save button was clicked successfully.
        """
        try:
            save_btn = self.page.get_by_role("button", name="저장")

            # Wait for button to be visible
            if not await save_btn.is_visible():
                logger.warning("저장 button not visible")
                return False

            # Check if button is enabled, with retry
            start = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start < timeout:
                is_disabled = await save_btn.get_attribute("disabled")
                if not is_disabled:
                    await save_btn.click()
                    await asyncio.sleep(1.0)  # Wait for save to complete
                    logger.info("Clicked 저장 button")
                    return True
                await asyncio.sleep(0.2)

            # Button stayed disabled - this is OK if there are still validation errors
            logger.info(
                "저장 button is disabled (may have validation errors remaining)"
            )
            return True  # Not a failure, just no changes to save

        except Exception as e:
            logger.error(f"click_save_button failed: {e}")
            return False

    async def calibrate_grid_layout(self) -> dict:
        """
        Runtime calibration: detect actual header_height and row_height by clicking.

        Returns dict with 'header_height', 'row_height', 'success'.
        """
        grid = self.get_canvas_grid()
        logger.info("Running runtime click-test calibration...")

        # Prefer mouseToIndex-based calibration (no clicks) if available
        try:
            sample = await self.page.evaluate(
                """(args) => {
                const el = document.querySelector('[data-orbit-component="OBTDataGrid"]');
                if (!el) return { error: 'no grid' };
                const key = Object.keys(el).find(k => k.startsWith('__reactInternalInstance'));
                const fiber = key ? el[key] : null;
                let cur = fiber;
                while (cur) {
                    const st = cur.memoizedState;
                    if (st && typeof st === 'object' && st.interface) {
                        const gv = st.interface._gridView;
                        if (!gv || typeof gv.mouseToIndex !== 'function') {
                            return { error: 'mouseToIndex unavailable' };
                        }
                        const topItem = gv.getTopItem ? gv.getTopItem() : 0;
                        const points = [];
                        for (let y = args.start; y <= args.max; y += args.step) {
                            const hit = gv.mouseToIndex(args.x, y);
                            if (hit && hit.dataRow !== undefined && hit.dataRow !== null) {
                                points.push([y, hit.dataRow]);
                            }
                        }
                        return { topItem, points };
                    }
                    cur = cur.return;
                }
                return { error: 'no interface' };
            }""",
                {
                    "x": int(min(200, max(40, grid.width * 0.2))),
                    "start": 20,
                    "max": int(min(grid.height - 10, 320)),
                    "step": 10,
                },
            )

            if sample and not sample.get("error"):
                points = sample.get("points", [])
                top_item = sample.get("topItem", 0)
                row_map = {}
                for y, row in points:
                    row_map.setdefault(row, []).append(y)

                if len(row_map) >= 2:
                    row_means = sorted(
                        (row, sum(ys) / len(ys)) for row, ys in row_map.items()
                    )

                    diffs = []
                    for i in range(len(row_means) - 1):
                        r0, y0 = row_means[i]
                        r1, y1 = row_means[i + 1]
                        if r1 - r0 == 1:
                            diffs.append(y1 - y0)

                    def median(vals):
                        vals = sorted(vals)
                        mid = len(vals) // 2
                        return (
                            vals[mid]
                            if len(vals) % 2 == 1
                            else (vals[mid - 1] + vals[mid]) / 2
                        )

                    if diffs:
                        row_height = median(diffs)
                        header_estimates = []
                        for row, y in row_means:
                            view_index = row - top_item
                            if view_index >= 0:
                                header_estimates.append(
                                    y - ((view_index + 0.5) * row_height)
                                )
                        header_height = (
                            median(header_estimates)
                            if header_estimates
                            else grid.header_height
                        )

                        if 20 <= row_height <= 80 and 10 <= header_height <= 120:
                            logger.info(
                                f"Calibration (mouseToIndex): header={header_height}, row_height={row_height}"
                            )
                            return {
                                "success": True,
                                "header_height": header_height,
                                "row_height": row_height,
                            }
                        else:
                            logger.warning(
                                f"mouseToIndex calibration out of range: header={header_height}, row_height={row_height}"
                            )
        except Exception as e:
            logger.warning(f"mouseToIndex calibration failed: {e}")

        # Click at various Y positions and see which row gets selected
        test_points = []
        for test_y_offset in [40, 60, 80, 100, 120, 140, 160, 180, 200]:
            y = grid.y + test_y_offset
            await self.page.mouse.click(grid.x + 200, y)
            await asyncio.sleep(0.15)

            selected = await self.page.evaluate("""
                () => {
                    const grid = window.Grids?.getActiveGrid();
                    if (!grid) return null;
                    const current = grid.getCurrent();
                    if (!current) return null;
                    return (current.dataRow !== undefined && current.dataRow !== null)
                        ? current.dataRow
                        : current.itemIndex;
                }
            """)
            if selected is not None:
                test_points.append((test_y_offset, selected))
                logger.debug(f"Calibration: y_offset={test_y_offset} -> row {selected}")

        if len(test_points) < 3:
            logger.warning("Calibration failed - not enough data points")
            return {
                "success": False,
                "header_height": grid.header_height,
                "row_height": grid.row_height,
            }

        # Find where row 0 starts (header_height) and row spacing (row_height)
        # Use first occurrences to estimate row centers
        test_points.sort(key=lambda x: x[0])
        row0_points = [p for p in test_points if p[1] == 0]
        row1_points = [p for p in test_points if p[1] == 1]

        if row0_points and row1_points:
            y0 = row0_points[0][0]
            y1 = row1_points[0][0]
            row_height = y1 - y0

            if row_height < 20 or row_height > 80:
                logger.warning(f"Calibration row_height out of range: {row_height}")
                row_height = grid.row_height

            header_height = y0 - (row_height / 2)
            if header_height < 10 or header_height > 100:
                logger.warning(
                    f"Calibration header_height out of range: {header_height}"
                )
                header_height = grid.header_height

            logger.info(
                f"Calibration result: header={header_height}, row_height={row_height}"
            )
            return {
                "success": True,
                "header_height": header_height,
                "row_height": row_height,
            }

        logger.warning("Calibration could not determine values")
        return {
            "success": False,
            "header_height": grid.header_height,
            "row_height": grid.row_height,
        }

    async def run_simple_mode_via_dataprovider(self, user_name: str) -> dict:
        """
        Run simple mode using hybrid approach:
        1. DataProvider to identify rows needing attendee (fast)
        2. Popup-based entry for actual filling (triggers validation)

        Args:
            user_name: Name to fill in the attendee field

        Returns:
            Dict with 'filled', 'already_set', 'failed', 'blocked', 'total'
        """
        # Step 1: Activate grid and get missing rows via DataProvider (fast)
        if not await self.activate_grid_for_api():
            return {
                "error": "Failed to activate grid",
                "filled": 0,
                "already_set": 0,
                "failed": 0,
                "blocked": 0,
                "total": 0,
            }

        missing_rows = await self.get_missing_attendee_rows_via_grids_api()
        if missing_rows is None:
            return {
                "error": "Failed to get missing rows",
                "filled": 0,
                "already_set": 0,
                "failed": 0,
                "blocked": 0,
                "total": 0,
            }

        total_rows = await self.get_grid_row_count() or 0
        already_set = total_rows - len(missing_rows)

        if not missing_rows:
            logger.info("All rows already have attendees")
            return {
                "filled": 0,
                "already_set": already_set,
                "failed": 0,
                "blocked": 0,
                "total": total_rows,
            }

        logger.info(
            f"Found {len(missing_rows)} rows needing attendee out of {total_rows}"
        )

        # Step 2: Runtime calibration (click-test) to align pixel coordinates
        cal = await self.calibrate_grid_layout()
        if cal.get("success"):
            self._canvas_grid.header_height = cal["header_height"]
            self._canvas_grid.row_height = cal["row_height"]
            logger.info(
                f"Applied runtime calibration: header={cal['header_height']}, "
                f"row_height={cal['row_height']}"
            )

        # Step 3: Process each missing row via popup
        filled = 0
        failed = 0
        blocked = 0
        failed_rows = []
        blocked_rows = []

        visible_rows = await self.get_visible_row_count()

        for idx, row_index in enumerate(missing_rows):
            debug.current_row = row_index
            logger.info(f"Processing row {row_index + 1}/{total_rows}")

            try:
                # Ensure popup is closed
                if not await self.ensure_popup_closed():
                    failed += 1
                    failed_rows.append(row_index + 1)
                    continue

                # Navigate to row via Grid API for reliable view_index
                actual_view_index = await self.navigate_to_row(
                    row_index, total_rows, visible_rows
                )

                if actual_view_index is None:
                    # Fallback: derive view_index from top_item (legacy behavior)
                    top_item = await self.get_grid_top_item()
                    if top_item is not None:
                        offset = 1 if top_item > 0 else 0
                        actual_view_index = row_index - top_item + offset

                        # If at edge, scroll to reposition
                        needs_scroll = actual_view_index < 0 or actual_view_index > 6
                        if needs_scroll:
                            target_top = max(0, row_index - 5)
                            logger.info(
                                f"Row {row_index + 1}: Scrolling to position (topItem={top_item} -> {target_top})"
                            )
                            await self.set_grid_top_item(target_top)
                            await asyncio.sleep(0.6)

                            top_item = await self.get_grid_top_item() or target_top
                            offset = 1 if top_item > 0 else 0
                            actual_view_index = row_index - top_item + offset
                            logger.info(
                                f"Row {row_index + 1}: After scroll (topItem={top_item}, view={actual_view_index})"
                            )

                        # Bounds check
                        if actual_view_index < 0 or actual_view_index > 12:
                            logger.warning(
                                f"Row {row_index + 1}: Invalid view_index {actual_view_index}, using fallback"
                            )
                            actual_view_index = 5
                    else:
                        actual_view_index = 5

                # Avoid clicking the last (partially visible) row in view
                if visible_rows and actual_view_index is not None:
                    if actual_view_index >= max(0, visible_rows - 1):
                        target_top = max(0, row_index - max(1, visible_rows - 2))
                        logger.info(
                            f"Row {row_index + 1}: Adjusting topItem to avoid bottom edge "
                            f"(view={actual_view_index} -> target_top={target_top})"
                        )
                        await self.set_grid_top_item(target_top)
                        await asyncio.sleep(0.4)
                        top_item = await self.get_grid_top_item()
                        if top_item is not None:
                            actual_view_index = row_index - top_item

                # Ensure row is selected/focused before clicking + (verified Y)
                if actual_view_index is not None:
                    grid = self.get_canvas_grid()
                    rel_y = await self._find_rel_y_for_row(row_index, actual_view_index)
                    x = grid.x + 120
                    y = grid.y + rel_y
                    debug.click(
                        x, y, f"row select (view {actual_view_index}, verified)"
                    )
                    logger.info(
                        f"Clicking row select (view {actual_view_index}, verified) at ({x:.0f}, {y:.0f})"
                    )
                    await self.click_position(x, y)
                    await asyncio.sleep(0.1)

                # Check row count before clicking
                row_count_before = await self.page.evaluate("""
                    () => {
                        const grid = window.Grids?.getActiveGrid();
                        return grid ? grid.getItemCount() : null;
                    }
                """)

                # Click + button using verified coordinates
                if actual_view_index is not None:
                    await self.click_plus_button_verified(
                        row_index, view_index=actual_view_index
                    )
                else:
                    await self.click_plus_button(
                        row_index, view_index=actual_view_index
                    )

                # Wait for popup or warning
                outcome = await self.wait_for_popup_or_warning(timeout=5.0)

                if outcome == "warning":
                    logger.warning(f"Row {row_index + 1}: Blocked by warning dialog")
                    blocked += 1
                    blocked_rows.append(row_index + 1)
                    continue

                if outcome != "popup":
                    logger.error(
                        f"Row {row_index + 1}: Popup did not open (outcome={outcome})"
                    )
                    failed += 1
                    failed_rows.append(row_index + 1)
                    continue

                # Verify popup corresponds to target row (avoid wrong-row fill)
                current_row = await self.get_grid_current_row()
                if current_row is not None and current_row != row_index:
                    logger.warning(
                        f"Row {row_index + 1}: Popup opened on different row "
                        f"(current={current_row + 1}). Retrying row focus."
                    )
                    await self.close_popup()
                    await asyncio.sleep(0.2)

                    # Re-navigate and re-open once
                    retry_view_index = await self.navigate_to_row(
                        row_index, total_rows, visible_rows
                    )
                    if retry_view_index is None:
                        retry_view_index = actual_view_index
                    if retry_view_index is not None:
                        await self.click_row_by_view(retry_view_index)
                        await asyncio.sleep(0.1)
                    if retry_view_index is not None:
                        await self.click_plus_button_verified(
                            row_index, view_index=retry_view_index
                        )
                    else:
                        await self.click_plus_button(
                            row_index, view_index=retry_view_index
                        )
                    outcome = await self.wait_for_popup_or_warning(timeout=3.0)

                    if outcome == "warning":
                        logger.warning(
                            f"Row {row_index + 1}: Blocked by warning dialog (retry)"
                        )
                        blocked += 1
                        blocked_rows.append(row_index + 1)
                        continue
                    if outcome != "popup":
                        logger.error(
                            f"Row {row_index + 1}: Popup did not open on retry (outcome={outcome})"
                        )
                        failed += 1
                        failed_rows.append(row_index + 1)
                        continue

                    # Re-check current row after retry
                    current_row = await self.get_grid_current_row()
                    if current_row is not None and current_row != row_index:
                        logger.error(
                            f"Row {row_index + 1}: Still mismatched after retry "
                            f"(current={current_row + 1}). Skipping to avoid wrong fill."
                        )
                        failed += 1
                        failed_rows.append(row_index + 1)
                        continue

                # Fill attendee with verification and retry
                attendee_input = self.page.locator('input[placeholder*="참석자"]').first
                max_attempts = 2
                row_filled = False

                for attempt in range(max_attempts):
                    try:
                        await attendee_input.wait_for(state="visible", timeout=2000)

                        # Clear and fill with proper delays (+10% slower)
                        await attendee_input.clear()
                        await asyncio.sleep(0.35)  # Wait for clear to register
                        await attendee_input.fill(user_name)
                        await asyncio.sleep(0.55)  # Wait for fill to fully register

                        # Verify input has the value before clicking
                        input_value = await attendee_input.input_value()
                        if input_value != user_name:
                            logger.warning(
                                f"Row {row_index + 1}: Input value mismatch, retrying fill"
                            )
                            await attendee_input.clear()
                            await asyncio.sleep(0.25)
                            await attendee_input.fill(user_name)
                            await asyncio.sleep(0.55)

                        # Click 확인 button and wait for popup to close
                        ok_btn = self.page.locator('button:has-text("확인")').first
                        if await ok_btn.is_visible():
                            await ok_btn.click()

                            # Wait for popup to close (important for validation to trigger)
                            try:
                                await attendee_input.wait_for(
                                    state="hidden", timeout=3000
                                )
                            except:
                                pass  # Popup may already be closed

                            await asyncio.sleep(
                                1.1
                            )  # Wait longer for async validation update (+10%)

                            # Check if row count changed (detecting duplication)
                            row_count_after = await self.page.evaluate("""
                                () => {
                                    const grid = window.Grids?.getActiveGrid();
                                    return grid ? grid.getItemCount() : null;
                                }
                            """)
                            if (
                                row_count_before
                                and row_count_after
                                and row_count_after != row_count_before
                            ):
                                logger.warning(
                                    f"Row {row_index + 1}: Row count changed from {row_count_before} to {row_count_after}! Duplication detected!"
                                )
                                # Take screenshot of duplication
                                await self.page.screenshot(
                                    path=f"/app/duplication_row_{row_index + 1}.png"
                                )

                            # Verify validation updated AND attendee data is correct for this specific row
                            check_result = await self.page.evaluate(
                                """
                                (rowIndex) => {
                                    const grid = window.Grids?.getActiveGrid();
                                    if (!grid) return null;
                                    const dp = grid.getDataSource();
                                    const row = dp.getJsonRow(rowIndex);

                                    return {
                                        checkNm: row.checkNm || '',
                                        columnDc180: (row.columnDc180 || '').trim(),
                                        currentRow: grid.getCurrent()?.itemIndex
                                    };
                                }
                            """,
                                row_index,
                            )

                            validation_cleared = (
                                check_result
                                and "참석자" not in check_result.get("checkNm", "")
                            )
                            attendee_value = (
                                check_result.get("columnDc180", "")
                                if check_result
                                else ""
                            )
                            attendee_correct = attendee_value == user_name
                            current_row = (
                                check_result.get("currentRow")
                                if check_result
                                else "unknown"
                            )

                            logger.info(
                                f"Row {row_index + 1}: Verification - validation_cleared={validation_cleared}, attendee='{attendee_value}', expected='{user_name}', match={attendee_correct}, current_row={current_row}"
                            )

                            if validation_cleared and attendee_correct:
                                filled += 1
                                row_filled = True
                                logger.info(f"Filled row {row_index + 1} (verified)")
                                break  # Success, exit retry loop
                            elif validation_cleared and not attendee_correct:
                                # Validation cleared but WRONG data - this indicates we filled the wrong row!
                                logger.error(
                                    f"Row {row_index + 1}: Validation cleared but attendee WRONG! Expected '{user_name}', got '{attendee_value}'"
                                )
                                await self.page.screenshot(
                                    path=f"/app/wrong_row_{row_index + 1}.png"
                                )
                                # Don't count as success, and abort to prevent cascade
                                break
                            elif attempt < max_attempts - 1:
                                # Validation didn't update - retry
                                logger.warning(
                                    f"Row {row_index + 1}: Validation not updated, retrying..."
                                )

                                # CRITICAL: Ensure popup is fully closed before retrying
                                # Otherwise the grid gets into a bad state causing row duplication
                                if not await self.ensure_popup_closed():
                                    logger.error(
                                        f"Row {row_index + 1}: Could not close popup for retry, aborting"
                                    )
                                    break

                                await asyncio.sleep(
                                    0.5
                                )  # Extra delay to let grid stabilize

                                # Re-click the + button to open popup again
                                if actual_view_index is not None:
                                    await self.click_plus_button_verified(
                                        row_index, view_index=actual_view_index
                                    )
                                else:
                                    await self.click_plus_button(
                                        row_index, view_index=actual_view_index
                                    )
                                await self.wait_for_popup_or_warning(timeout=3.0)
                                continue
                            else:
                                # Last attempt failed
                                filled += 1
                                row_filled = True
                                logger.warning(
                                    f"Row {row_index + 1}: Filled but validation not verified after {max_attempts} attempts"
                                )
                                break
                        else:
                            await self.close_popup()
                            break

                    except Exception as e:
                        if attempt < max_attempts - 1:
                            logger.warning(
                                f"Row {row_index + 1} attempt {attempt + 1} failed: {e}, retrying..."
                            )
                            await self.close_popup()
                            if actual_view_index is not None:
                                await self.click_plus_button_verified(
                                    row_index, view_index=actual_view_index
                                )
                            else:
                                await self.click_plus_button(
                                    row_index, view_index=actual_view_index
                                )
                            await self.wait_for_popup_or_warning(timeout=3.0)
                        else:
                            raise

                if not row_filled:
                    failed += 1
                    failed_rows.append(row_index + 1)

            except Exception as e:
                logger.error(f"Error processing row {row_index + 1}: {e}")
                failed += 1
                failed_rows.append(row_index + 1)

        return {
            "filled": filled,
            "already_set": already_set,
            "failed": failed,
            "failed_rows": failed_rows,
            "blocked": blocked,
            "blocked_rows": blocked_rows,
            "total": total_rows,
        }

    async def ensure_row_visible(
        self, row_index: int, margin_rows: int = 0, max_attempts: int = 5
    ) -> bool:
        """
        Ensure a row is visible within a safe viewport zone by scrolling.

        Returns:
            True if row is within the safe zone, False if not after attempts.
        """
        for _ in range(max_attempts):
            await self._update_canvas_position()
            grid = self.get_canvas_grid()

            row_y = self._get_row_y(row_index)
            effective_margin = max(0, min(margin_rows, row_index))
            safe_top = (
                grid.y + grid.header_height + (grid.row_height * effective_margin)
            )
            safe_bottom = grid.y + grid.height - (grid.row_height * effective_margin)

            if row_y < safe_top:
                delta = row_y - safe_top
                if abs(delta) < grid.row_height:
                    delta = -grid.row_height
                await self.scroll_grid_by(delta)
                continue
            if row_y > safe_bottom:
                delta = row_y - safe_bottom
                if abs(delta) < grid.row_height:
                    delta = grid.row_height
                await self.scroll_grid_by(delta)
                continue
            return True

        return False

    # =========================================================================
    # Screenshot Capture (Fast, using Playwright)
    # =========================================================================

    async def take_screenshot(self, path: str = "/app/screenshot.png") -> str:
        """Take a screenshot using Playwright (fast, works over tunnel)."""
        await self.page.screenshot(path=path)
        logger.debug(f"Screenshot saved to {path}")
        return path

    # =========================================================================
    # Mouse Click Actions
    # =========================================================================

    async def click_plus_button_api(self, row_index: int):
        """
        Click the '+' button with proper scrolling and positioning.
        This method:
        1. Gets current grid position
        2. Calculates optimal scroll position (row at view ~5)
        3. Scrolls if needed and waits for stabilization
        4. Clicks the + button at verified coordinates

        DOES NOT use setCurrent() to avoid interfering with popup save functionality.
        """
        # Step 1: Get current position
        top_item = await self.get_grid_top_item()
        if top_item is None:
            logger.warning(
                f"Row {row_index + 1}: Could not get topItem, using fallback positioning"
            )
            # Fallback: scroll to position row near middle
            target_top = max(0, row_index - 5)
            await self.set_grid_top_item(target_top)
            await asyncio.sleep(0.8)
            top_item = await self.get_grid_top_item() or 0

        # Step 2: Calculate view_index
        offset = 1 if top_item > 0 else 0
        view_index = row_index - top_item + offset

        logger.info(
            f"Row {row_index + 1}: Current position - topItem={top_item}, view={view_index}"
        )

        # Step 3: If at edge (view > 6 or < 0), scroll to reposition
        if view_index > 6 or view_index < 0:
            logger.info(f"Row {row_index + 1}: At edge, scrolling to reposition...")
            # Position row at view 5 (safe middle zone)
            target_top = max(0, row_index - 5)
            await self.set_grid_top_item(target_top)
            await asyncio.sleep(0.8)  # Longer wait for scroll to fully complete

            # Re-verify position after scroll
            top_item = await self.get_grid_top_item()
            if top_item is not None:
                offset = 1 if top_item > 0 else 0
                view_index = row_index - top_item + offset
                logger.info(
                    f"Row {row_index + 1}: After scroll - topItem={top_item}, view={view_index}"
                )
            else:
                logger.warning(
                    f"Row {row_index + 1}: Could not verify position after scroll"
                )
                view_index = 5  # Safe fallback

        # Step 4: Final bounds check
        if view_index < 0 or view_index > 12:
            logger.warning(
                f"Row {row_index + 1}: view_index {view_index} out of bounds, clamping to safe range"
            )
            view_index = max(0, min(view_index, 6))

        # Step 5: Click + button (row selection happens automatically from the click)
        grid = self.get_canvas_grid()
        x = grid.get_plus_button_x()
        y = self._get_view_row_y(view_index)

        logger.info(
            f"Row {row_index + 1}: Clicking '+' button at view {view_index}, coords ({x:.0f}, {y:.0f})"
        )

        if debug.enabled:
            await self.page.screenshot(path=debug.screenshot_path("before_plus_click"))

        await self.click_position(x, y)
        await asyncio.sleep(0.5)

        if debug.enabled:
            await self.page.screenshot(path=debug.screenshot_path("after_plus_click"))

    async def _mouse_to_index(self, rel_x: float, rel_y: float) -> Optional[dict]:
        """Return grid hit info for a relative (x,y) position using RealGrid mouseToIndex."""
        js = """
        (args) => {
          const el = document.querySelector('[data-orbit-component="OBTDataGrid"]');
          if (!el) return null;
          const key = Object.keys(el).find(k => k.startsWith('__reactInternalInstance'));
          const fiber = key ? el[key] : null;
          let cur = fiber;
          while (cur) {
            const st = cur.memoizedState;
            if (st && typeof st === 'object' && st.interface) {
              const gv = st.interface._gridView;
              if (!gv || typeof gv.mouseToIndex !== 'function') return null;
              try {
                return gv.mouseToIndex(args.x, args.y);
              } catch (e) {
                return null;
              }
            }
            cur = cur.return;
          }
          return null;
        }
        """
        try:
            return await self.page.evaluate(js, {"x": rel_x, "y": rel_y})
        except Exception as e:
            logger.debug(f"_mouse_to_index failed: {e}")
            return None

    async def _find_rel_y_for_row(self, row_index: int, view_index: int) -> float:
        """
        Find a relative Y (inside grid) that maps to the target row via mouseToIndex.
        Falls back to the expected center if mouseToIndex is unavailable.
        """
        grid = self.get_canvas_grid()
        base = (
            grid.header_height + (view_index * grid.row_height) + (grid.row_height / 2)
        )
        # Choose a stable X inside grid content
        rel_x = min(max(grid.width * 0.2, 40), grid.width - 40)

        hit = await self._mouse_to_index(rel_x, base)
        if hit and hit.get("dataRow") == row_index:
            return base

        # Scan around the base position to find the correct row
        step = max(2.0, grid.row_height / 10.0)
        max_steps = int(max(6, grid.row_height))
        for i in range(1, max_steps + 1):
            for sign in (1, -1):
                cand = base + (sign * step * i)
                if cand < grid.header_height or cand > (grid.height - 2):
                    continue
                hit = await self._mouse_to_index(rel_x, cand)
                if hit and hit.get("dataRow") == row_index:
                    logger.info(
                        f"Adjusted click Y for row {row_index + 1}: base={base:.1f} -> {cand:.1f}"
                    )
                    return cand

        # Fallback if no match found
        return base

    async def click_plus_button_verified(self, row_index: int, view_index: int):
        """
        Click '+' button using a mouseToIndex-verified Y position to avoid row mismatch.
        """
        grid = self.get_canvas_grid()
        rel_y = await self._find_rel_y_for_row(row_index, view_index)
        x = grid.get_plus_button_x()
        y = grid.y + rel_y

        label = f"row {row_index} (view {view_index}, verified)"
        debug.click(x, y, f"+button for {label}")
        logger.info(f"Clicking '+' button for {label} at ({x:.0f}, {y:.0f})")

        if debug.enabled:
            await self.page.screenshot(path=debug.screenshot_path("before_plus_click"))
            debug.state(f"Screenshot saved before clicking +button (verified)")

        await self.click_position(x, y)
        await asyncio.sleep(0.5)

        if debug.enabled:
            await self.page.screenshot(path=debug.screenshot_path("after_plus_click"))
            debug.state(f"Screenshot saved after clicking +button (verified)")

    async def click_plus_button(self, row_index: int, view_index: Optional[int] = None):
        """
        Click the '+' button for a specific row (0-indexed).
        Opens the expense detail popup.

        DEPRECATED: Use click_plus_button_api() instead for better reliability.
        """
        grid = self.get_canvas_grid()
        x = grid.get_plus_button_x()
        y = (
            self._get_view_row_y(view_index)
            if view_index is not None
            else self._get_row_y(row_index)
        )

        label = (
            f"row {row_index}"
            if view_index is None
            else f"row {row_index} (view {view_index})"
        )
        debug.click(x, y, f"+button for {label}")
        logger.info(f"Clicking '+' button for {label} at ({x:.0f}, {y:.0f})")

        # Take screenshot BEFORE click
        if debug.enabled:
            await self.page.screenshot(path=debug.screenshot_path("before_plus_click"))
            debug.state(f"Screenshot saved before clicking +button")

        await self.click_position(x, y)
        debug.wait(0.5, "waiting for popup to open")
        await asyncio.sleep(0.5)  # Wait for popup to open

        # Take screenshot AFTER click
        if debug.enabled:
            await self.page.screenshot(path=debug.screenshot_path("after_plus_click"))
            debug.state(f"Screenshot saved after clicking +button")

    async def click_row_by_view(self, view_index: int, x_offset: float = 120):
        """
        Click inside a visible row to ensure it is selected/focused.

        Args:
            view_index: Visible row index (0 = top visible row)
            x_offset: X offset from grid left edge for the click target
        """
        grid = self.get_canvas_grid()
        x = grid.x + x_offset
        y = self._get_view_row_y(view_index)
        debug.click(x, y, f"row select (view {view_index})")
        logger.info(f"Clicking row select (view {view_index}) at ({x:.0f}, {y:.0f})")
        await self.click_position(x, y)
        await asyncio.sleep(0.1)

    async def click_cell(self, row_index: int, column_offset_from_left: float):
        """
        Click on a specific cell in the grid.

        Args:
            row_index: Row number (0-indexed)
            column_offset_from_left: X offset from left edge of canvas
        """
        grid = self.get_canvas_grid()
        x = grid.x + column_offset_from_left
        y = self._get_row_y(row_index)

        logger.debug(
            f"Clicking cell at row {row_index}, x_offset {column_offset_from_left}"
        )
        await self.click_position(x, y)
        await asyncio.sleep(0.2)

    # =========================================================================
    # 용도 (Purpose) Selection
    # =========================================================================

    async def click_yongdo_cell(self, row_index: int):
        """
        Click on the 용도 (purpose) cell for a specific row.

        Args:
            row_index: Row number (0-indexed)
        """
        grid = self.get_canvas_grid()
        x = grid.get_yongdo_x()
        y = self._get_row_y(row_index)

        logger.debug(f"Clicking 용도 cell for row {row_index} at ({x:.0f}, {y:.0f})")
        await self.click_position(x, y)
        await asyncio.sleep(0.3)

    async def click_yongdo_cell_verified(self, row_index: int, view_index: int):
        """
        Click 용도 cell using a mouseToIndex-verified Y to avoid row mismatch.
        """
        grid = self.get_canvas_grid()
        rel_y = await self._find_rel_y_for_row(row_index, view_index)
        x = grid.get_yongdo_x()
        y = grid.y + rel_y

        debug.click(
            x, y, f"용도 cell for row {row_index} (view {view_index}, verified)"
        )
        logger.info(f"Clicking 용도 cell (verified) at ({x:.0f}, {y:.0f})")
        await self.click_position(x, y)
        await asyncio.sleep(0.3)

    async def _is_yongdo_popup_open(self) -> bool:
        """Check if the 용도 selection popup is open."""
        dialog_title = self.page.locator("h1.dialog_title")
        if await dialog_title.count() > 0:
            title = await dialog_title.first.inner_text()
            if "용도" in title:
                logger.info(f"Opened 용도 popup: {title}")
                return True
        return False

    async def open_yongdo_popup(self, row_index: int) -> bool:
        """
        Open the 용도 selection popup for a specific row.
        Uses Click + F2 method.

        Args:
            row_index: Row number (0-indexed)

        Returns:
            True if popup opened successfully, False otherwise.
        """
        await self._activate_grid()

        visible_rows = await self.get_visible_row_count()
        if not visible_rows or visible_rows <= 0:
            visible_rows = 10

        total_rows = await self.get_grid_row_count() or (row_index + 1)
        view_index = None
        if total_rows > 0:
            view_index = await self.navigate_to_row(row_index, total_rows, visible_rows)

        if view_index is None:
            top_item = await self.get_grid_top_item()
            if top_item is not None:
                offset = 1 if top_item > 0 else 0
                view_index = row_index - top_item + offset
                if view_index < 0 or view_index > 6:
                    target_top = max(0, row_index - 5)
                    logger.info(
                        f"Row {row_index + 1}: Scrolling to position (topItem={top_item} -> {target_top})"
                    )
                    await self.set_grid_top_item(target_top)
                    await asyncio.sleep(0.6)
                    top_item = await self.get_grid_top_item() or target_top
                    offset = 1 if top_item > 0 else 0
                    view_index = row_index - top_item + offset
                if view_index < 0 or view_index > 12:
                    logger.warning(
                        f"Row {row_index + 1}: Invalid view_index {view_index}, using fallback"
                    )
                    view_index = 5
            else:
                view_index = 5

        if visible_rows and view_index is not None:
            if view_index >= max(0, visible_rows - 1):
                target_top = max(0, row_index - max(1, visible_rows - 2))
                logger.info(
                    f"Row {row_index + 1}: Adjusting topItem to avoid bottom edge "
                    f"(view={view_index} -> target_top={target_top})"
                )
                await self.set_grid_top_item(target_top)
                await asyncio.sleep(0.4)
                top_item = await self.get_grid_top_item()
                if top_item is not None:
                    view_index = row_index - top_item
        for attempt in range(3):
            if await self._is_yongdo_popup_open():
                return True

            if view_index is not None:
                await self.click_yongdo_cell_verified(row_index, view_index)
            else:
                await self.click_yongdo_cell(row_index)
            await asyncio.sleep(0.2)
            await self.page.keyboard.press("F2")
            await asyncio.sleep(0.6)
            if await self._is_yongdo_popup_open():
                return True

            # Try Enter -> F2
            await self.page.keyboard.press("Enter")
            await asyncio.sleep(0.2)
            await self.page.keyboard.press("F2")
            await asyncio.sleep(0.6)
            if await self._is_yongdo_popup_open():
                return True

            # Try double click + F2
            if view_index is not None:
                grid = self.get_canvas_grid()
                rel_y = await self._find_rel_y_for_row(row_index, view_index)
                x = grid.get_yongdo_x()
                y = grid.y + rel_y
                await self.page.mouse.dblclick(x, y)
            else:
                await self.click_yongdo_cell(row_index)
            await asyncio.sleep(0.2)
            await self.page.keyboard.press("F2")
            await asyncio.sleep(0.6)
            if await self._is_yongdo_popup_open():
                return True

        logger.warning("Failed to open 용도 popup")
        return False

    async def select_yongdo(self, row_index: int, yongdo_option: int = 0) -> bool:
        """
        Select a 용도 (purpose/expense category) for a specific row.

        The popup displays options in a Canvas grid. Common options (visible without scrolling):
        - 0: 중식대 (100)
        - 1: 석식대 (110)
        - 2: 회식대 (120)
        - 3: 간식/음료 (130)
        - 4: 건강검진 (140)
        - 5: 의약품 (150)
        - etc.

        Flow:
        1. Click on 용도 cell + F2 to open popup
        2. Click on the option row in popup canvas
        3. Click 확인 button

        Args:
            row_index: Row number in main grid (0-indexed)
            yongdo_option: Option row number in popup (0=중식대, 1=석식대, etc.)

        Returns:
            True if selection successful, False otherwise.
        """
        # Popup canvas layout constants
        POPUP_HEADER_HEIGHT = 30
        POPUP_ROW_HEIGHT = 30

        try:
            # Step 1: Open popup
            if not await self.open_yongdo_popup(row_index):
                return False

            # Step 2: Find popup canvas (second canvas on page)
            all_canvas = self.page.locator("canvas[role=application]")
            if await all_canvas.count() < 2:
                logger.warning("Popup canvas not found")
                await self.page.keyboard.press("Escape")
                return False

            popup_canvas = all_canvas.nth(1)
            pbox = await popup_canvas.bounding_box()
            if not pbox:
                logger.warning("Could not get popup canvas bounding box")
                await self.page.keyboard.press("Escape")
                return False

            logger.debug(f"Popup canvas: x={pbox['x']:.0f}, y={pbox['y']:.0f}")

            # Step 3: Click on the option row
            click_x = pbox["x"] + 150  # Middle-left of the row
            click_y = (
                pbox["y"]
                + POPUP_HEADER_HEIGHT
                + (yongdo_option * POPUP_ROW_HEIGHT)
                + (POPUP_ROW_HEIGHT / 2)
            )

            logger.info(
                f"Clicking 용도 option {yongdo_option} at ({click_x:.0f}, {click_y:.0f})"
            )
            await self.click_position(click_x, click_y)
            await asyncio.sleep(0.3)

            # Step 4: Click 확인 button
            confirm_btn = self.page.locator('button:has-text("확인"):visible').first
            if await confirm_btn.is_visible():
                await confirm_btn.click()
                await asyncio.sleep(0.6)
            else:
                logger.warning("확인 button not found")
                await self.page.keyboard.press("Escape")
                return False

            # Verify popup closed
            dialog_title = self.page.locator("h1.dialog_title")
            if await dialog_title.count() == 0:
                for _ in range(3):
                    row_state = await self.get_row_fields_via_dp(row_index)
                    if row_state and row_state.get("yongdo"):
                        logger.info(
                            f"Successfully selected 용도 option {yongdo_option}"
                        )
                        return True
                    await asyncio.sleep(0.4)
                logger.warning("용도 selection not reflected in grid after retries")
            else:
                logger.warning("Popup still open after clicking 확인")
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
                return False

        except Exception as e:
            logger.error(f"Failed to select 용도: {e}")
            await self.page.keyboard.press("Escape")
            return False

    async def select_yongdo_by_search(self, row_index: int, search_term: str) -> bool:
        """
        Select a 용도 by searching (for options not visible without scrolling).

        Flow:
        1. Open popup
        2. Type search term in popup's first visible input
        3. Press Enter to trigger search
        4. Double-click first result row (auto-closes popup)

        Args:
            row_index: Row number in main grid (0-indexed)
            search_term: Search term (e.g., "소프트웨어", "국내출장")

        Returns:
            True if selection successful, False otherwise.
        """
        POPUP_HEADER_HEIGHT = 42
        POPUP_ROW_HEIGHT = 30

        if search_term in YONGDO_OPTIONS:
            return await self.select_yongdo(row_index, YONGDO_OPTIONS[search_term])

        try:
            for attempt in range(2):
                # Open popup
                if not await self.open_yongdo_popup(row_index):
                    return False
                await asyncio.sleep(0.5)

                popup = self.page.locator(".obtdialog")

                # Find search input — use first visible input (placeholder may be empty)
                search_input = popup.locator("input:visible").first
                if not await search_input.is_visible():
                    logger.warning("No visible input found in yongdo popup")
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(0.2)
                    continue

                # Type search term and press Enter to trigger search
                await search_input.click()
                await asyncio.sleep(0.2)
                await search_input.fill(search_term)
                await asyncio.sleep(0.3)
                await self.page.keyboard.press("Enter")
                await asyncio.sleep(1.0)
                logger.info(f"Searched for 용도: {search_term}")

                # Find popup canvas and double-click first result row
                all_canvas = self.page.locator("canvas[role=application]")
                canvas_count = await all_canvas.count()
                if canvas_count < 2:
                    logger.warning(
                        f"Expected >=2 canvases, found {canvas_count}"
                    )
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(0.2)
                    continue

                popup_canvas = all_canvas.nth(1)
                pbox = await popup_canvas.bounding_box()
                if not pbox:
                    logger.warning("Could not get popup canvas bounding box")
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(0.2)
                    continue

                # Double-click first data row (header + half row height)
                click_x = pbox["x"] + pbox["width"] * 0.3
                click_y = (
                    pbox["y"] + POPUP_HEADER_HEIGHT + (POPUP_ROW_HEIGHT / 2)
                )
                await self.page.mouse.dblclick(click_x, click_y)
                await asyncio.sleep(0.5)

                # Double-click auto-closes the popup — verify selection
                popup_count = await popup.count()
                if popup_count > 0:
                    # Popup still open — try clicking 확인 as fallback
                    confirm_btn = popup.locator(
                        'button:has-text("확인"):visible'
                    ).first
                    if await confirm_btn.count() > 0:
                        await confirm_btn.click()
                        await asyncio.sleep(0.6)
                    else:
                        await self.page.keyboard.press("Escape")
                        await asyncio.sleep(0.3)

                # Verify the value was set
                for _ in range(3):
                    row_state = await self.get_row_fields_via_dp(row_index)
                    if row_state and row_state.get("yongdo"):
                        logger.info(
                            f"Successfully selected 용도 by search: "
                            f"'{row_state['yongdo']}'"
                        )
                        return True
                    await asyncio.sleep(0.4)

                logger.warning(
                    f"용도 selection not reflected in grid (attempt "
                    f"{attempt + 1}), retrying..."
                )

            return False

        except Exception as e:
            logger.error(f"Failed to select 용도 by search: {e}")
            await self.page.keyboard.press("Escape")
            return False

    # =========================================================================
    # Popup Handling
    # =========================================================================

    async def is_popup_open(self) -> bool:
        """Check if an expense detail popup is currently open."""
        # Look for expense popup indicators (avoid generic confirm dialogs)
        try:
            attendee_inputs = self.page.locator('input[placeholder*="참석자"]')
            if await attendee_inputs.count() > 0:
                debug.popup("DETECTED", "via 참석자 input")
                return True
        except Exception:
            pass
        # Also detect the popup variant without 참석자 (e.g. card/SaaS transactions)
        # Use 내용 input as indicator — present in both popup variants, not in page header
        try:
            content_input = self.page.locator('input[placeholder*="내용을 입력"]:visible')
            if await content_input.count() > 0:
                debug.popup("DETECTED", "via 내용 input (card/SaaS popup variant)")
                return True
        except Exception:
            pass
        return False

    async def dismiss_missing_yongdo_warning(self) -> bool:
        """
        Dismiss warning dialog that appears when 용도/내용 is missing.

        Returns True if a matching warning dialog was found and dismissed.
        """
        try:
            dialogs = self.page.locator('[data-orbit-component="OBTDialog2"]:visible')
            target = dialogs.filter(has_text="필수입력 확인")
            if await target.count() == 0:
                target = dialogs.filter(has_text="용도를 입력해주세요")
            if await target.count() > 0:
                dialog = target.first
                ok_btn = dialog.locator('button:has-text("확인")').first
                if await ok_btn.is_visible():
                    await ok_btn.click()
                    await asyncio.sleep(0.3)
                    return True

            # Fallback: warning text visible but dialog container not found
            warning_visible = False
            try:
                if await self.page.locator('text="필수입력 확인"').is_visible():
                    warning_visible = True
            except Exception:
                pass
            if not warning_visible:
                try:
                    if await self.page.locator(
                        'text="용도를 입력해주세요"'
                    ).is_visible():
                        warning_visible = True
                except Exception:
                    pass

            if warning_visible:
                attendee_visible = False
                try:
                    attendee = self.page.locator('input[placeholder*="참석자"]').first
                    attendee_visible = await attendee.is_visible()
                except Exception:
                    attendee_visible = False
                if not attendee_visible:
                    ok_btn = self.page.locator('button:has-text("확인"):visible').first
                    if await ok_btn.is_visible():
                        await ok_btn.click()
                        await asyncio.sleep(0.3)
                        return True

            return False
        except Exception:
            return False

    async def handle_confirmation_dialog(self) -> bool:
        """
        Handle the "데이터 변경 확인" confirmation dialog if it appears.
        This dialog asks if user wants to proceed when existing data may be overwritten.

        Returns True if dialog was handled, False if no dialog found.
        """
        # Look for confirmation dialog (different from expense popup)
        confirm_text = self.page.locator('text="데이터 변경 확인"')
        if await confirm_text.is_visible():
            logger.info("Confirmation dialog detected, clicking 확인...")
            ok_btn = self.page.locator('button:has-text("확인")').first
            if await ok_btn.is_visible():
                await ok_btn.click()
                await asyncio.sleep(0.5)
                return True
        return False

    async def ensure_popup_closed(self) -> bool:
        """
        Aggressively ensure no popups are open.
        This resets the state for the next row operation.

        Returns:
            True if all popups closed, False if something stuck
        """
        try:
            # Dismiss warning dialog if present (e.g., 용도/내용 required)
            if await self.dismiss_missing_yongdo_warning():
                await asyncio.sleep(0.1)

            # Check if popup is open
            if await self.is_popup_open():
                debug.action("CLEANUP", "Closing leftover popup")
                logger.info("Closing leftover popup before processing row...")

                # Try clicking 취소 button first, then Escape
                await self.cancel_popup()
                await asyncio.sleep(0.3)

                # Check again
                if await self.is_popup_open():
                    debug.error("Popup stuck open after cancel, trying Escape")
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(0.5)
                    if await self.is_popup_open():
                        # Last resort: click X close button if present
                        try:
                            close_btn = self.page.locator(
                                'button[class*="close"]:visible, '
                                'button[aria-label="Close"]:visible, '
                                '.dialog_close:visible'
                            ).first
                            if await close_btn.count() > 0:
                                await close_btn.click()
                                await asyncio.sleep(0.5)
                        except Exception:
                            pass
                    return not await self.is_popup_open()

            return True
        except Exception as e:
            debug.error(f"Error ensuring popup closed: {e}")
            return False

    async def wait_for_popup(self, timeout: float = 5.0):
        """Wait for the expense detail popup to appear."""
        debug.wait(timeout, f"waiting for popup (max {timeout}s)")
        logger.debug("Waiting for popup...")
        start = asyncio.get_event_loop().time()
        check_count = 0
        while asyncio.get_event_loop().time() - start < timeout:
            check_count += 1
            # First check for and handle confirmation dialog
            if await self.handle_confirmation_dialog():
                debug.popup("HANDLED", "confirmation dialog")
                continue  # Dialog handled, keep waiting for actual popup

            if await self.is_popup_open():
                elapsed = asyncio.get_event_loop().time() - start
                debug.success(
                    f"Popup opened after {elapsed:.2f}s ({check_count} checks)"
                )
                logger.debug("Popup is open")
                return
            await asyncio.sleep(0.1)
        debug.error(f"Popup did NOT appear within {timeout}s ({check_count} checks)")
        logger.warning("Popup did not appear within timeout")

    async def wait_for_popup_or_warning(self, timeout: float = 5.0) -> str:
        """
        Wait for either the expense popup or a missing 용도/내용 warning.

        Returns:
            "popup"   -> expense popup opened
            "warning" -> missing 용도/내용 warning detected (and dismissed)
            "timeout" -> nothing detected within timeout
        """
        debug.wait(timeout, f"waiting for popup/warning (max {timeout}s)")
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            if await self.handle_confirmation_dialog():
                debug.popup("HANDLED", "confirmation dialog")
                continue
            if await self.is_popup_open():
                debug.success("Popup opened")
                return "popup"
            if await self.dismiss_missing_yongdo_warning():
                debug.popup("HANDLED", "missing yongdo warning")
                return "warning"
            await asyncio.sleep(0.1)
        debug.error("Popup/warning did NOT appear within timeout")
        return "timeout"

    async def fill_popup(self, data: ExpenseData) -> bool:
        """
        Fill the expense detail popup with provided data.

        Handles:
        - Basic fields (참석자, 내용)
        - Supplier info for PG/배민 transactions
        - File attachments (receipt images)
        - Pending receipts (adds note to 비고 field)

        Returns True if successful, False otherwise.
        """
        import os

        debug.popup("FILLING", f"merchant={data.merchant}")
        logger.info(f"Filling popup for: {data.merchant}")

        try:
            # Fill 참석자 (Attendees) - REQUIRED for validation (when present)
            # Note: Some popup variants (e.g. card/SaaS transactions) don't have 참석자
            if data.attendees:
                attendee_input = self.page.locator('input[placeholder*="참석자"]').first
                is_visible = await attendee_input.is_visible()
                debug.state(f"참석자 input visible: {is_visible}")
                if not is_visible:
                    logger.info("No 참석자 input in popup (card/SaaS transaction type)")
                if is_visible:
                    # Robust fill with verification + explicit focus-away to ensure commit
                    filled_ok = False
                    for fill_attempt in range(2):
                        await attendee_input.clear()
                        await asyncio.sleep(0.2)
                        await attendee_input.fill(data.attendees)
                        await asyncio.sleep(0.4)
                        value = await attendee_input.input_value()
                        if (value or "").strip() == data.attendees.strip():
                            filled_ok = True
                            # FIX: Transfer focus to 내용 input to reliably commit attendee
                            # Clicking on another input field is more reliable than arbitrary coords
                            debug.action("FOCUS", "transferring focus to 내용 input to commit attendee")
                            try:
                                content_input = self.page.locator(
                                    'input[placeholder*="내용을 입력"]'
                                ).first
                                if await content_input.is_visible():
                                    await content_input.click()
                                    await asyncio.sleep(1.0)  # Increased from 0.5s to 1.0s
                                else:
                                    # Fallback: Tab twice to move focus away
                                    await self.page.keyboard.press("Tab")
                                    await asyncio.sleep(0.4)
                                    await self.page.keyboard.press("Tab")
                                    await asyncio.sleep(0.6)
                            except Exception:
                                # Final fallback: Tab twice
                                await self.page.keyboard.press("Tab")
                                await asyncio.sleep(0.4)
                                await self.page.keyboard.press("Tab")
                                await asyncio.sleep(0.6)

                            # FIX: Re-verify attendee value after focus transfer
                            post_value = await attendee_input.input_value()
                            if (post_value or "").strip() != data.attendees.strip():
                                debug.error(f"Attendee value changed after focus transfer! '{post_value}' != '{data.attendees}'")
                                filled_ok = False
                                continue  # Try again
                            break
                    debug.fill("참석자", data.attendees)
                    logger.info(f"Filled 참석자: {data.attendees} (ok={filled_ok})")
                else:
                    debug.error("참석자 input not visible!")
                    logger.warning("참석자 input not visible")

            # Fill 내용 (Content) - if provided and different from current
            if data.content:
                content_input = self.page.locator(
                    'input[placeholder*="내용을 입력"]'
                ).first
                is_visible = await content_input.is_visible()
                debug.state(f"내용 input visible: {is_visible}")
                if is_visible:
                    current_value = await content_input.input_value()
                    debug.state(f"내용 current value: '{current_value}'")
                    if current_value != data.content:
                        await content_input.clear()
                        await asyncio.sleep(0.2)
                        await content_input.fill(data.content)
                        await asyncio.sleep(0.3)
                        debug.fill("내용", data.content)
                        logger.info(f"Filled 내용: {data.content}")
                    else:
                        debug.state("내용 unchanged, skipping")

            # Handle receipt attachment
            all_receipts = data.receipt_paths or []
            valid_receipts = [p for p in all_receipts if os.path.exists(p)]
            missing_receipts = [p for p in all_receipts if not os.path.exists(p)]
            if missing_receipts:
                for mp in missing_receipts:
                    debug.error(f"Receipt file not found (skipped): {mp}")
                    logger.warning(f"Receipt file not found, skipping attachment: {mp}")
            debug.state(f"Has {len(valid_receipts)} receipt(s)" +
                        (f", {len(missing_receipts)} missing" if missing_receipts else ""))

            if valid_receipts:
                # Has receipt(s) - fill supplier info and attach file(s)
                if data.needs_supplier_info:
                    debug.state("Needs supplier info, filling...")
                    # 실공급자상호
                    if data.supplier_name:
                        supplier_input = self.page.locator(
                            'input[placeholder*="실공급자상호"]'
                        ).first
                        if await supplier_input.is_visible():
                            await supplier_input.fill(data.supplier_name)
                            debug.fill("실공급자상호", data.supplier_name)
                            logger.info(f"Filled 실공급자상호: {data.supplier_name}")

                    # 실공급자 사업자등록번호
                    if data.supplier_biz_no:
                        biz_input = self.page.locator(
                            'input[placeholder*="사업자등록번호"]'
                        ).first
                        if await biz_input.is_visible():
                            await biz_input.fill(data.supplier_biz_no)
                            debug.fill("사업자번호", data.supplier_biz_no)
                            logger.info(f"Filled 사업자번호: {data.supplier_biz_no}")

                # Attach receipt file(s)
                for receipt_path in valid_receipts:
                    debug.state(f"Attaching receipt: {receipt_path}")
                    attached = await self.attach_file(receipt_path)
                    if not attached:
                        debug.error(f"Failed to attach receipt")
                        logger.warning(f"Failed to attach receipt: {receipt_path}")
                    else:
                        debug.success(f"Receipt attached: {os.path.basename(receipt_path)}")

            elif data.needs_supplier_info:
                # Missing receipt for a transaction that needs supplier info
                # Add note to 비고 field
                pending_reason = (
                    getattr(data, "pending_reason", None) or "영수증 미첨부"
                )
                note = f"[영수증 대기] {pending_reason}"
                debug.state(f"Adding pending receipt note to 비고")
                await self.fill_bigo_field(note)
                debug.fill("비고", note)
                logger.info(f"Added pending receipt note to 비고: {note}")

            # Take screenshot after filling
            if debug.enabled:
                await self.page.screenshot(path=debug.screenshot_path("after_fill"))
                debug.state("Screenshot saved after fill")

            debug.success("Popup fill complete")
            return True

        except Exception as e:
            debug.error(f"Failed to fill popup: {e}")
            logger.error(f"Failed to fill popup: {e}")
            return False

    async def save_popup(self) -> bool:
        """
        Click the 확인 (OK) button to save the popup.

        Returns True if successful, False otherwise.
        """
        try:
            for attempt in range(3):
                # If popup already closed, treat as success
                if not await self.is_popup_open():
                    return True

                ok_button = self.page.locator('button:has-text("확인")').first
                is_visible = await ok_button.is_visible()
                debug.state(f"확인 button visible: {is_visible}")

                if is_visible:
                    # Small delay to allow field values to commit before saving
                    await asyncio.sleep(0.3 if attempt == 0 else 0.6)
                    # Get button position for logging
                    box = await ok_button.bounding_box()
                    if box:
                        debug.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                            "확인 button",
                        )

                    try:
                        await ok_button.click()
                    except Exception:
                        await ok_button.click(force=True)
                    debug.action("CLICK", "→ 확인 button clicked")
                    logger.info("Clicked 확인 button")
                else:
                    # Fallback: press Enter to submit
                    await self.page.keyboard.press("Enter")
                    await asyncio.sleep(0.4)

                # Handle confirmation dialog if it appears
                await self.handle_confirmation_dialog()

                # Wait for popup to disappear (detached from DOM or hidden)
                debug.wait(5.0, "waiting for popup to close")
                try:
                    # Expect the button itself to be detached (popup removed)
                    await ok_button.wait_for(state="detached", timeout=5000)
                    debug.success("Popup closed (detached)")
                    break
                except Exception:
                    # Fallback check
                    if not await self.is_popup_open():
                        debug.success("Popup closed (verified)")
                        break

                await asyncio.sleep(0.4)

            # Take screenshot after save
            if debug.enabled:
                await self.page.screenshot(path=debug.screenshot_path("after_save"))
                debug.state("Screenshot saved after save")

            return not await self.is_popup_open()
        except Exception as e:
            debug.error(f"Failed to click 확인: {e}")
            logger.error(f"Failed to click 확인: {e}")
            return False

    async def close_popup(self):
        """Close any open popup by pressing Escape."""
        await self.page.keyboard.press("Escape")
        await asyncio.sleep(0.2)

    async def cancel_popup(self) -> bool:
        """Close popup without saving by clicking 취소 if visible, fallback to Escape."""
        try:
            cancel_btn = self.page.locator('button:has-text("취소"):visible').first
            if await cancel_btn.is_visible():
                await cancel_btn.click()
                await asyncio.sleep(0.3)
            else:
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.2)
            return not await self.is_popup_open()
        except Exception as e:
            logger.error(f"Failed to cancel popup: {e}")
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            return False

    # =========================================================================
    # File Attachment
    # =========================================================================

    async def attach_file(self, file_path: str) -> bool:
        """
        Attach a file (receipt image) to the current expense popup.

        Flow:
        1. Validate file (existence, size, type, readability)
        2. Click the file add button (btn_fileAdd) to open dropdown menu
        3. Click "PC에서 선택" option
        4. Use Playwright's file chooser to set the file
        5. Verify upload success

        Args:
            file_path: Path to the file to attach (must exist on the server)

        Returns:
            True if successful, False otherwise
        """
        import os
        import mimetypes

        debug.action("ATTACH", f"attaching file: {os.path.basename(file_path)}")

        # === VALIDATION PHASE ===

        # 1. Verify file exists
        if not os.path.exists(file_path):
            debug.error(f"File not found: {file_path}")
            logger.error(f"File not found: {file_path}")
            return False

        # 2. Verify file size
        file_size = os.path.getsize(file_path)
        MAX_SIZE = 10 * 1024 * 1024  # 10MB (Douzone typical limit)
        MIN_SIZE = 100  # 100 bytes (to catch corrupted/empty files)

        if file_size < MIN_SIZE:
            debug.error(
                f"File too small ({file_size} bytes < {MIN_SIZE} bytes): likely corrupted"
            )
            logger.error(f"File too small or empty: {file_path} ({file_size} bytes)")
            return False

        if file_size > MAX_SIZE:
            debug.error(f"File too large ({file_size} bytes > {MAX_SIZE} bytes)")
            logger.error(
                f"File exceeds maximum size: {file_path} ({file_size} bytes > {MAX_SIZE} bytes)"
            )
            return False

        debug.state(f"File size OK: {file_size} bytes ({file_size / 1024:.1f} KB)")

        # 3. Verify file type
        mime_type, _ = mimetypes.guess_type(file_path)
        allowed_types = ["image/jpeg", "image/png", "image/jpg", "application/pdf"]

        if mime_type not in allowed_types:
            # Try checking by extension as fallback
            ext = os.path.splitext(file_path)[1].lower()
            allowed_exts = [".jpg", ".jpeg", ".png", ".pdf"]
            if ext not in allowed_exts:
                debug.error(f"Invalid file type: {mime_type} (extension: {ext})")
                logger.error(
                    f"Invalid file type: {file_path} (mime={mime_type}, ext={ext})"
                )
                return False
            else:
                debug.state(f"File type OK (by extension): {ext}")
        else:
            debug.state(f"File type OK: {mime_type}")

        # 4. Verify file is readable
        try:
            with open(file_path, "rb") as f:
                # Try reading first 1KB to verify file is readable
                f.read(1024)
            debug.state("File is readable")
        except Exception as e:
            debug.error(f"File not readable: {e}")
            logger.error(f"Cannot read file: {file_path} - {e}")
            return False

        logger.info(
            f"File validated: {file_path} ({file_size} bytes, {mime_type or 'unknown type'})"
        )

        try:
            # Step 1: Find and click the visible file add button
            add_btn = self.page.locator("input.btn_fileAdd")
            count = await add_btn.count()
            debug.state(f"Found {count} file add buttons")

            # Find the visible one (usually the second one, index 1)
            visible_btn = None
            for i in range(count):
                btn = add_btn.nth(i)
                if await btn.is_visible():
                    visible_btn = btn
                    debug.state(f"Using file add button index {i}")
                    break

            if not visible_btn:
                debug.error("No visible file add button found")
                logger.error("No visible file add button found")
                return False

            debug.action("CLICK", "file add button to open menu")
            logger.info("Clicking file add button to open menu...")
            await visible_btn.click()
            debug.wait(0.5, "waiting for dropdown menu")
            await asyncio.sleep(0.5)

            # Step 2: Click "PC에서 선택" option
            pc_option = self.page.locator("text=PC에서 선택")
            is_visible = await pc_option.is_visible()
            debug.state(f"'PC에서 선택' visible: {is_visible}")

            if not is_visible:
                # Retry: close menu, re-click add button, check again
                debug.state("'PC에서 선택' not visible, retrying...")
                logger.warning("PC에서 선택 option not visible, retrying")
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
                await visible_btn.click()
                await asyncio.sleep(0.8)
                is_visible = await pc_option.is_visible()
                if not is_visible:
                    debug.error("'PC에서 선택' option not visible after retry")
                    logger.error("PC에서 선택 option not visible after retry")
                    await self.page.keyboard.press("Escape")
                    return False
                debug.state("'PC에서 선택' visible after retry")

            # Step 3: Use file chooser
            debug.action("FILECHOOSER", "waiting for file chooser dialog")
            async with self.page.expect_file_chooser(timeout=10000) as fc_info:
                debug.action("CLICK", "'PC에서 선택' option")
                logger.info("Clicking PC에서 선택...")
                await pc_option.click()

            file_chooser = await fc_info.value
            debug.action("SETFILE", f"setting file: {file_path}")
            await file_chooser.set_files(file_path)

            debug.success(f"File set in chooser: {os.path.basename(file_path)}")
            logger.info(f"File attached: {file_path}")

            # === VERIFICATION PHASE ===

            # Wait for upload to complete (longer wait for large files)
            wait_time = min(
                2.0, 0.5 + (file_size / (1024 * 1024))
            )  # Base 0.5s + 1s per MB
            debug.wait(wait_time, f"waiting for upload ({file_size / 1024:.1f} KB)")
            await asyncio.sleep(wait_time)

            # Verify attachment - multiple checks
            verification_passed = False
            verification_details = []

            # Check 1: Look for .ico_file indicator
            try:
                attach_locator = self.page.locator(".ico_file")
                attach_count = await attach_locator.count()

                if attach_count > 0:
                    attach_text = await attach_locator.first.inner_text()
                    debug.state(f"Attachment indicator found: '{attach_text}'")
                    verification_details.append(f"indicator='{attach_text}'")

                    # Check if text indicates files attached
                    if "개" in attach_text:  # "1개", "2개" etc.
                        try:
                            num_files = int(
                                "".join(c for c in attach_text if c.isdigit())
                            )
                            if num_files > 0:
                                verification_passed = True
                                verification_details.append(f"files={num_files}")
                        except ValueError:
                            pass
                else:
                    debug.state("No .ico_file indicator found")
                    verification_details.append("indicator=none")

            except Exception as ve:
                debug.action("WARN", f"Could not check .ico_file: {ve}")
                verification_details.append(f"indicator_error={ve}")

            # Check 2: Look for file list entries in popup
            try:
                file_list = self.page.locator(
                    '.file_list li, .file_item, [class*="upload"]'
                )
                file_count = await file_list.count()
                if file_count > 0:
                    debug.state(f"Found {file_count} file list entries")
                    verification_details.append(f"list_entries={file_count}")
                    verification_passed = True
            except Exception as e:
                debug.action("WARN", f"Could not check file list: {e}")

            # Final verification result
            if verification_passed:
                debug.success(f"Attachment verified: {', '.join(verification_details)}")
                logger.info(
                    f"File upload verified: {file_path} ({', '.join(verification_details)})"
                )
                return True
            else:
                debug.error(
                    f"Attachment verification FAILED: {', '.join(verification_details)}"
                )
                logger.error(
                    f"File upload verification failed: {file_path} ({', '.join(verification_details)})"
                )
                return False

        except Exception as e:
            debug.error(f"Failed to attach file: {e}")
            logger.error(f"Failed to attach file: {e}")
            # Try to close any open menu
            debug.keyboard("Escape", "cleanup after error")
            await self.page.keyboard.press("Escape")
            return False

    async def fill_bigo_field(self, text: str) -> bool:
        """
        Fill the 비고 (remarks) field in the popup.

        Args:
            text: Text to put in the remarks field

        Returns:
            True if successful, False otherwise
        """
        try:
            bigo_input = self.page.locator('input[placeholder*="비고"]').first
            if await bigo_input.is_visible():
                current_value = await bigo_input.input_value()
                if current_value:
                    # Append to existing value
                    text = f"{current_value} / {text}"
                await bigo_input.fill(text)
                logger.info(f"Filled 비고: {text}")
                return True
            else:
                logger.warning("비고 input not visible")
                return False
        except Exception as e:
            logger.error(f"Failed to fill 비고: {e}")
            return False

    # =========================================================================
    # Row Processing
    # =========================================================================

    async def process_row(
        self,
        row_index: int,
        data: ExpenseData,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> bool:
        """
        Process a single expense row with retry logic.

        Steps:
        1. Fill 용도 (if needed) - MUST be done before [+] click!
        2. Click '+' button to open popup
        3. Wait for popup (with retry)
        4. Fill popup fields (참석자, etc.)
        5. Save popup

        IMPORTANT: Douzone requires 용도 to be filled BEFORE clicking [+].
        If 용도 is empty, the [+] button won't work!

        Args:
            row_index: Row number (0-indexed)
            data: Expense data to fill
            max_retries: Maximum number of retry attempts (default 3)
            retry_delay: Base delay between retries in seconds (default 1.0)

        Returns:
            True if successful, False otherwise.
        """
        debug.current_row = row_index
        print(f"\n{'=' * 60}")
        print(f"  🔄 PROCESSING ROW {row_index + 1}: {data.merchant}")
        print(f"     Attendees: {data.attendees}")
        print(
            f"     용도: {data.yongdo} {'(needs fill)' if data.needs_yongdo else '(already set)'}"
        )
        receipt_list = ", ".join(data.receipt_paths) if data.receipt_paths else "None"
        print(f"     Receipts: {receipt_list}")
        print(f"     Supplier: {data.supplier_name or 'None'}")
        print(f"{'=' * 60}")

        total_rows = await self.get_grid_row_count() or (row_index + 1)
        self.last_error = None
        last_error = None

        for attempt in range(max_retries):
            try:
                debug.action("ATTEMPT", f"{attempt + 1}/{max_retries}")

                # Pre-flight check: Ensure state is clean
                if not await self.ensure_popup_closed():
                    debug.error("Could not clean up popup state!")
                    raise Exception("State unclean (popup stuck)")

                if attempt > 0:
                    delay = retry_delay * (attempt + 1)  # Increasing delay
                    debug.wait(delay, f"retry delay before attempt {attempt + 1}")
                    logger.info(
                        f"Retry {attempt}/{max_retries} for row {row_index} (waiting {delay:.1f}s)..."
                    )
                    await asyncio.sleep(delay)

                # Periodic re-calibration to handle layout changes (window resize, zoom, etc.)
                if self.should_recalibrate(rows_threshold=10):
                    debug.action("RECALIBRATE", "running periodic calibration check")
                    logger.info("Running periodic re-calibration...")
                    try:
                        await self.calibrate(force=True)
                        debug.success("Re-calibration complete")
                    except Exception as e:
                        debug.action(
                            "WARN",
                            f"Re-calibration failed, using previous calibration: {e}",
                        )
                        logger.warning(
                            f"Re-calibration failed, continuing with previous calibration: {e}"
                        )

                # Refresh canvas position in case page layout changed
                debug.action("REFRESH", "updating canvas position")
                await self._update_canvas_position()
                debug.state(
                    f"Canvas: x={self._canvas_grid.x:.0f}, y={self._canvas_grid.y:.0f}, "
                    f"w={self._canvas_grid.width:.0f}, h={self._canvas_grid.height:.0f}"
                )
                if self._should_trace_row(row_index):
                    status = await self.get_row_status_via_dp(row_index)
                    logger.info(f"[trace row {row_index + 1}] pre_status={status}")

                # Pre-check actual grid values to avoid relying on stale flags
                row_state = await self.get_row_fields_via_dp(row_index)
                if row_state:
                    if not row_state.get("yongdo") and data.yongdo:
                        data.needs_yongdo = True
                    if not row_state.get("content") and data.content:
                        data.needs_content = True
                slow_mode = bool(data.needs_yongdo or data.needs_content)

                # Step 1: Fill 용도 if needed (MUST be before [+] click!)
                if data.needs_yongdo:
                    debug.action("STEP1", f"filling 용도: {data.yongdo}")
                    yongdo_success = await self.select_yongdo_by_search(
                        row_index, data.yongdo
                    )
                    if not yongdo_success:
                        await asyncio.sleep(0.4)
                        row_state = await self.get_row_fields_via_dp(row_index)
                        if row_state and row_state.get("yongdo"):
                            yongdo_success = True
                    if not yongdo_success:
                        # one more retry for stubborn rows
                        yongdo_success = await self.select_yongdo_by_search(
                            row_index, data.yongdo
                        )
                    if not yongdo_success:
                        debug.error(f"Failed to select 용도: {data.yongdo}")
                        raise Exception(f"Failed to select 용도: {data.yongdo}")
                    debug.success(f"용도 set to: {data.yongdo}")
                    # Exit cell-edit mode so the + button works afterward
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(0.3)
                    await self.page.mouse.click(
                        self._canvas_grid.x + 50,
                        self._canvas_grid.y + self._canvas_grid.height - 10,
                    )
                    await asyncio.sleep(0.6)  # Extra pause after 용도 selection
                    if self._should_trace_row(row_index):
                        status = await self.get_row_status_via_dp(row_index)
                        logger.info(
                            f"[trace row {row_index + 1}] after_yongdo={status}"
                        )

                # Step 1B: Fill 내용 if needed (must be before [+] click)
                if data.needs_content and data.content:
                    debug.action("STEP1B", f"filling 내용: {data.content}")
                    content_success = await self.fill_content_cell_ui(
                        row_index, data.content
                    )
                    if not content_success:
                        content_success = await self.set_content_via_dp(
                            row_index, data.content
                        )
                    if not content_success:
                        debug.error(f"Failed to set 내용: {data.content}")
                        raise Exception(f"Failed to set 내용: {data.content}")
                    debug.success(f"내용 set to: {data.content}")
                    await asyncio.sleep(0.6)
                    if self._should_trace_row(row_index):
                        status = await self.get_row_status_via_dp(row_index)
                        logger.info(
                            f"[trace row {row_index + 1}] after_content={status}"
                        )
                if slow_mode:
                    await asyncio.sleep(0.4)

                # Step 2: Navigate to row and click '+' (verified)
                debug.action("STEP2", "navigating to row and clicking +")
                await self._activate_grid()

                visible_rows = await self.get_visible_row_count()
                if not visible_rows or visible_rows <= 0:
                    visible_rows = 10

                actual_view_index = None
                if total_rows > 0:
                    actual_view_index = await self.navigate_to_row(
                        row_index, total_rows, visible_rows
                    )

                if actual_view_index is None:
                    top_item = await self.get_grid_top_item()
                    if top_item is not None:
                        offset = 1 if top_item > 0 else 0
                        actual_view_index = row_index - top_item + offset

                        if actual_view_index < 0 or actual_view_index > 6:
                            target_top = max(0, row_index - 5)
                            logger.info(
                                f"Row {row_index + 1}: Scrolling to position "
                                f"(topItem={top_item} -> {target_top})"
                            )
                            await self.set_grid_top_item(target_top)
                            await asyncio.sleep(0.6)

                            top_item = await self.get_grid_top_item() or target_top
                            offset = 1 if top_item > 0 else 0
                            actual_view_index = row_index - top_item + offset

                        if actual_view_index < 0 or actual_view_index > 12:
                            logger.warning(
                                f"Row {row_index + 1}: Invalid view_index {actual_view_index}, using fallback"
                            )
                            actual_view_index = 5
                    else:
                        actual_view_index = 5

                if visible_rows and actual_view_index is not None:
                    if actual_view_index >= max(0, visible_rows - 1):
                        target_top = max(0, row_index - max(1, visible_rows - 2))
                        logger.info(
                            f"Row {row_index + 1}: Adjusting topItem to avoid bottom edge "
                            f"(view={actual_view_index} -> target_top={target_top})"
                        )
                        await self.set_grid_top_item(target_top)
                        await asyncio.sleep(0.4)
                        top_item = await self.get_grid_top_item()
                        if top_item is not None:
                            actual_view_index = row_index - top_item

                if actual_view_index is not None:
                    grid = self.get_canvas_grid()
                    rel_y = await self._find_rel_y_for_row(row_index, actual_view_index)
                    x = grid.x + 120
                    y = grid.y + rel_y
                    debug.click(
                        x, y, f"row select (view {actual_view_index}, verified)"
                    )
                    logger.info(
                        f"Clicking row select (view {actual_view_index}, verified) at ({x:.0f}, {y:.0f})"
                    )
                    await self.click_position(x, y)
                    await asyncio.sleep(0.1)
                    await self.click_plus_button_verified(
                        row_index, view_index=actual_view_index
                    )
                else:
                    await self.click_plus_button_api(row_index)

                # Step 3: Wait for popup with extended timeout on retries
                timeout = 5.0 + (attempt * 2.0)  # Longer timeout on retries
                debug.action("STEP3", f"waiting for popup/warning (timeout={timeout}s)")
                outcome = await self.wait_for_popup_or_warning(timeout=timeout)
                if outcome == "warning":
                    debug.error("Missing required fields warning detected")
                    raise Exception("Missing required fields warning")
                if outcome != "popup":
                    debug.error("Popup did not open!")
                    raise Exception("Popup did not open after click")

                # Verify popup corresponds to target row (avoid wrong-row fill)
                current_row = await self.get_grid_current_row()
                if current_row is not None and current_row != row_index:
                    logger.warning(
                        f"Row {row_index + 1}: Popup opened on different row "
                        f"(current={current_row + 1}). Retrying row focus."
                    )
                    await self.close_popup()
                    await asyncio.sleep(0.2)

                    retry_view_index = None
                    if total_rows > 0:
                        retry_view_index = await self.navigate_to_row(
                            row_index, total_rows, visible_rows
                        )
                    if retry_view_index is None:
                        retry_view_index = actual_view_index
                    if retry_view_index is not None:
                        await self.click_row_by_view(retry_view_index)
                        await asyncio.sleep(0.1)
                        await self.click_plus_button_verified(
                            row_index, view_index=retry_view_index
                        )
                    else:
                        await self.click_plus_button_api(row_index)

                    outcome = await self.wait_for_popup_or_warning(timeout=3.0)
                    if outcome == "warning":
                        raise Exception("Missing required fields warning (retry)")
                    if outcome != "popup":
                        raise Exception("Popup did not open on retry")

                    current_row = await self.get_grid_current_row()
                    if current_row is not None and current_row != row_index:
                        raise Exception(
                            f"Popup opened on wrong row after retry (current={current_row + 1})"
                        )

                # Step 4: Fill popup
                debug.action("STEP4", "filling popup fields")
                filled = await self.fill_popup(data)
                if not filled:
                    debug.error("fill_popup returned False!")
                    raise Exception("Failed to fill popup fields")
                # Commit popup fields by blurring focus
                try:
                    await self.page.keyboard.press("Tab")
                    await asyncio.sleep(0.2)
                except Exception:
                    pass
                if self._should_trace_row(row_index):
                    try:
                        attendee_val = await self.page.locator(
                            'input[placeholder*="참석자"]'
                        ).first.input_value()
                        content_val = await self.page.locator(
                            'input[placeholder*="내용을 입력"]'
                        ).first.input_value()
                        logger.info(
                            f"[trace row {row_index + 1}] popup_values attendee='{attendee_val}' content='{content_val}'"
                        )
                    except Exception as e:
                        logger.info(
                            f"[trace row {row_index + 1}] popup_values_read_failed: {e}"
                        )
                    try:
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        path = os.path.join(
                            debug.screenshot_dir,
                            f"trace_row{row_index + 1}_{ts}_before_save.png",
                        )
                        await self.page.screenshot(path=path)
                        logger.info(f"[trace row {row_index + 1}] screenshot={path}")
                    except Exception as e:
                        logger.info(
                            f"[trace row {row_index + 1}] screenshot_failed: {e}"
                        )
                if slow_mode:
                    await asyncio.sleep(0.6)

                # FIX: Pre-save verification - check attendee before save
                # This catches cases where the value didn't commit properly
                if data.attendees:
                    debug.action("VERIFY", "pre-save attendee check")
                    attendee_input = self.page.locator('input[placeholder*="참석자"]').first
                    try:
                        # Skip verification if 참석자 input doesn't exist (card/SaaS popup)
                        if await attendee_input.count() == 0 or not await attendee_input.is_visible():
                            debug.state("참석자 input not present, skipping pre-save check")
                        else:
                            pre_save_value = await attendee_input.input_value()
                            expected = data.attendees.strip()
                            actual = (pre_save_value or "").strip()
                            if actual != expected:
                                debug.error(f"Pre-save mismatch: '{actual}' != '{expected}', re-filling")
                                logger.warning(f"Row {row_index + 1}: Pre-save attendee mismatch, re-filling")
                                await attendee_input.clear()
                                await asyncio.sleep(0.2)
                                await attendee_input.fill(data.attendees)
                                await asyncio.sleep(0.3)
                                await self.page.keyboard.press("Tab")
                                await asyncio.sleep(0.8)
                                final_value = await attendee_input.input_value()
                                if (final_value or "").strip() == expected:
                                    debug.success("Pre-save re-fill successful")
                                else:
                                    debug.error(f"Pre-save re-fill failed: '{final_value}'")
                            else:
                                debug.success(f"Pre-save attendee OK: '{actual}'")
                    except Exception as e:
                        debug.error(f"Pre-save verification failed: {e}")

                # Step 5: Save popup
                debug.action("STEP5", "saving popup (click 확인)")
                saved = await self.save_popup()
                if not saved:
                    if self._should_trace_row(row_index):
                        status = await self.get_row_status_via_dp(row_index)
                        logger.info(
                            f"[trace row {row_index + 1}] save_failed_status={status}"
                        )
                        try:
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            path = os.path.join(
                                debug.screenshot_dir,
                                f"trace_row{row_index + 1}_{ts}_save_failed.png",
                            )
                            await self.page.screenshot(path=path)
                            logger.info(
                                f"[trace row {row_index + 1}] screenshot={path}"
                            )
                        except Exception as e:
                            logger.info(
                                f"[trace row {row_index + 1}] screenshot_failed: {e}"
                            )
                    debug.error("save_popup returned False!")
                    raise Exception("Failed to save popup")
                # FIX: Always check post-save validation, not just in slow_mode
                # Rows without needs_yongdo/needs_content were not being validated
                await asyncio.sleep(0.8 if slow_mode else 0.5)
                post_status = await self.get_row_status_via_dp(row_index)
                if post_status:
                    validation = post_status.get("validation", "")
                    if "미입력" in validation:
                        if self._should_trace_row(row_index):
                            logger.info(
                                f"[trace row {row_index + 1}] post_save_validation={validation}"
                            )
                        # === SIMPLE-MODE ATTENDEE RETRY (for "row 25" problem) ===
                        # If validation fails because attendee not entered, try once more:
                        # 1. Re-open popup for this row
                        # 2. Re-fill attendee with simple fill (no retry loop)
                        # 3. Save again
                        debug.action(
                            "RETRY",
                            "simple-mode attendee retry after validation failure",
                        )
                        logger.info(
                            f"Retrying attendee fill for row {row_index}: {validation}"
                        )
                        try:
                            # Step 1: Re-open popup
                            debug.action(
                                "RETRY_STEP1", "re-opening popup for attendee retry"
                            )
                            await self._activate_grid()
                            # Navigate to row again
                            total_rows_retry = await self.get_grid_row_count() or (
                                row_index + 1
                            )
                            visible_rows_retry = await self.get_visible_row_count()
                            if not visible_rows_retry or visible_rows_retry <= 0:
                                visible_rows_retry = 10
                            actual_view_index_retry = None
                            if total_rows_retry > 0:
                                actual_view_index_retry = (
                                    await self.navigate_to_row(
                                        row_index,
                                        total_rows_retry,
                                        visible_rows_retry,
                                    )
                                )
                            if actual_view_index_retry is None:
                                top_item = await self.get_grid_top_item()
                                if top_item is not None:
                                    offset = 1 if top_item > 0 else 0
                                    actual_view_index_retry = (
                                        row_index - top_item + offset
                                    )
                                    if (
                                        actual_view_index_retry < 0
                                        or actual_view_index_retry > 6
                                    ):
                                        target_top = max(0, row_index - 5)
                                        await self.set_grid_top_item(target_top)
                                        await asyncio.sleep(0.4)
                                        top_item = (
                                            await self.get_grid_top_item()
                                            or target_top
                                        )
                                        offset = 1 if top_item > 0 else 0
                                        actual_view_index_retry = (
                                            row_index - top_item + offset
                                        )
                                        if (
                                            actual_view_index_retry < 0
                                            or actual_view_index_retry > 12
                                        ):
                                            actual_view_index_retry = 5
                                else:
                                    actual_view_index_retry = 5
                            if actual_view_index_retry is not None:
                                grid = self.get_canvas_grid()
                                rel_y = await self._find_rel_y_for_row(
                                    row_index, actual_view_index_retry
                                )
                                x = grid.x + 120
                                y = grid.y + rel_y
                                await self.click_position(x, y)
                                await asyncio.sleep(0.1)
                                await self.click_plus_button_verified(
                                    row_index, view_index=actual_view_index_retry
                                )
                            else:
                                await self.click_plus_button_api(row_index)
                            # Wait for popup
                            debug.action(
                                "RETRY_STEP2", "waiting for popup to reopen"
                            )
                            outcome = await self.wait_for_popup_or_warning(
                                timeout=5.0
                            )
                            if outcome != "popup":
                                logger.warning(
                                    f"Failed to reopen popup for attendee retry (outcome={outcome})"
                                )
                                raise Exception(
                                    f"Failed to reopen popup for attendee retry: {validation}"
                                )
                            # Step 2: Simple attendee fill (no retry loop, focus then wait)
                            debug.action(
                                "RETRY_STEP3",
                                f"simple fill: 참석자 = '{data.attendees}'",
                            )
                            attendee_input = self.page.locator(
                                'input[placeholder*="참석자"]'
                            ).first
                            is_visible = await attendee_input.is_visible()
                            if is_visible:
                                await attendee_input.clear()
                                await asyncio.sleep(0.2)
                                await attendee_input.fill(data.attendees)
                                await asyncio.sleep(0.3)
                                # FIX: Click on 내용 input to reliably commit (same as fill_popup fix)
                                debug.action(
                                    "FOCUS",
                                    "clicking 내용 input to commit attendee value",
                                )
                                try:
                                    content_input = self.page.locator(
                                        'input[placeholder*="내용을 입력"]'
                                    ).first
                                    if await content_input.is_visible():
                                        await content_input.click()
                                        await asyncio.sleep(1.0)  # Long wait for commit
                                    else:
                                        # Fallback: Tab twice
                                        await self.page.keyboard.press("Tab")
                                        await asyncio.sleep(0.4)
                                        await self.page.keyboard.press("Tab")
                                        await asyncio.sleep(0.6)
                                except Exception:
                                    await self.page.keyboard.press("Tab")
                                    await asyncio.sleep(0.4)
                                    await self.page.keyboard.press("Tab")
                                    await asyncio.sleep(0.6)
                                # Verify the fill
                                retry_value = await attendee_input.input_value()
                                if (retry_value or "").strip() == data.attendees.strip():
                                    debug.success(f"Attendee retry fill verified: '{retry_value}'")
                                else:
                                    debug.error(f"Attendee retry fill mismatch: '{retry_value}' != '{data.attendees}'")
                                logger.info(
                                    f"Attendee retry fill: {data.attendees}"
                                )
                            else:
                                logger.warning(
                                    "참석자 input not visible during retry"
                                )
                            # Step 3: Save again
                            debug.action(
                                "RETRY_STEP4", "saving popup after attendee retry"
                            )
                            saved_retry = await self.save_popup()
                            if not saved_retry:
                                # Final check - still failed?
                                post_retry_status = (
                                    await self.get_row_status_via_dp(row_index)
                                )
                                if (
                                    post_retry_status
                                    and "미입력"
                                    in post_retry_status.get("validation", "")
                                ):
                                    raise Exception(
                                        f"Post-save validation still failed after attendee retry: {post_retry_status.get('validation')}"
                                    )
                            debug.success("Attendee retry successful")
                        except Exception as retry_err:
                            logger.warning(f"Attendee retry failed: {retry_err}")
                            raise Exception(
                                f"Post-save validation not cleared (retry failed): {validation}"
                            )

                debug.success(f"Row {row_index + 1} completed successfully!")
                logger.info(f"Successfully processed row {row_index}")
                print(f"\n  ✅ ROW {row_index + 1} COMPLETE\n")

                # Track rows processed for periodic re-calibration
                self._rows_since_calibration += 1

                return True

            except Exception as e:
                last_error = e
                self.last_error = str(e)
                debug.error(f"Attempt {attempt + 1} failed: {e}")
                logger.warning(
                    f"Attempt {attempt + 1}/{max_retries} failed for row {row_index}: {e}"
                )

                # Take screenshot on error
                if debug.enabled:
                    try:
                        await self.page.screenshot(path=debug.screenshot_path("error"))
                        debug.state("Error screenshot saved")
                    except:
                        pass

                # Try to clean up
                try:
                    debug.action("CLEANUP", "pressing Escape to close any popup")
                    await self.close_popup()
                except:
                    pass

        # All retries exhausted
        debug.error(f"FAILED after {max_retries} attempts: {last_error}")
        logger.error(
            f"Failed to process row {row_index} after {max_retries} attempts: {last_error}"
        )
        print(f"\n  ❌ ROW {row_index + 1} FAILED: {last_error}\n")
        return False

    async def process_multiple_rows(
        self, data_list: List[ExpenseData], start_row: int = 0
    ) -> ProcessingResult:
        """
        Process multiple expense rows.

        Args:
            data_list: List of ExpenseData to fill (one per row)
            start_row: Starting row index (0-indexed)

        Returns:
            ProcessingResult with statistics
        """
        result = ProcessingResult()
        result.total_rows = len(data_list)

        for i, data in enumerate(data_list):
            row_index = start_row + i
            logger.info(
                f"Processing row {row_index + 1}/{result.total_rows}: {data.merchant}"
            )

            success = await self.process_row(row_index, data)
            if success:
                result.processed_rows += 1
            else:
                result.failed_rows += 1
                result.errors.append(f"Row {row_index}: {data.merchant}")

            # Small delay between rows
            await asyncio.sleep(0.3)

        logger.info(
            f"Processing complete: {result.processed_rows}/{result.total_rows} successful"
        )
        return result

    # =========================================================================
    # Utility Methods
    # =========================================================================

    async def get_visible_row_count(self) -> int:
        """
        Estimate the number of visible rows in the grid.
        Based on canvas height and row height.
        """
        grid = self.get_canvas_grid()
        available_height = grid.height - grid.header_height
        return int(available_height / grid.row_height)

    async def scroll_grid_down(self):
        """Scroll the grid down to see more rows."""
        await self.scroll_grid_by(300)

    async def scroll_grid_up(self):
        """Scroll the grid up to see previous rows."""
        await self.scroll_grid_by(-300)


# =========================================================================
# Legacy Compatibility (for existing test code)
# =========================================================================

# Keep old method names for backward compatibility
DouzoneAutomation.navigate_to_first_cell = lambda self: asyncio.sleep(0)
DouzoneAutomation.vision_analyze_grid = None  # Removed - use coordinate-based approach
