"""
Transaction Parser for Douzone STEP 2.

Captures transaction data from the Canvas-based grid using screenshots
and Claude Code CLI for Vision parsing.
"""
import asyncio
import json
import subprocess
import os
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class Transaction:
    """A single transaction row from Douzone STEP 2."""
    row_num: int  # 1-based row index in the grid
    date_time: str  # Format: "YYYY-MM-DD HH:MM"
    merchant: str  # 사용처
    amount: int  # 청구금액 (won)
    yongdo: Optional[str] = None  # 용도 (may be auto-filled or empty)
    content: Optional[str] = None  # 내용 (may be auto-filled or empty)
    status: str = ""  # 검증결과 (e.g., "참석자 미입력", "적합")
    
    # Computed fields
    yongdo_filled: bool = field(default=False, repr=False)
    content_filled: bool = field(default=False, repr=False)
    needs_attendee: bool = field(default=True, repr=False)
    
    def __post_init__(self):
        """Determine field status after initialization."""
        self.yongdo_filled = bool(self.yongdo and self.yongdo.strip())
        self.content_filled = bool(self.content and self.content.strip())
        self.needs_attendee = "참석자 미입력" in self.status or "미입력" in self.status
    
    @property
    def date(self) -> str:
        """Extract date part (YYYY-MM-DD)."""
        return self.date_time.split()[0] if self.date_time else ""
    
    @property
    def time(self) -> str:
        """Extract time part (HH:MM)."""
        parts = self.date_time.split()
        return parts[1] if len(parts) > 1 else ""
    
    @property
    def date_short(self) -> str:
        """Short date format (M/D)."""
        try:
            dt = datetime.strptime(self.date, "%Y-%m-%d")
            return f"{dt.month}/{dt.day}"
        except:
            return self.date
    
    def matches_receipt(self, receipt_date: str, receipt_time: str, receipt_amount: int, 
                        time_tolerance_minutes: int = 5) -> bool:
        """Check if this transaction matches a receipt by date, time, and amount."""
        # Date must match exactly
        if self.date != receipt_date:
            return False
        
        # Amount must match exactly
        if self.amount != receipt_amount:
            return False
        
        # Time should be within tolerance
        try:
            tx_time = datetime.strptime(self.time, "%H:%M")
            rc_time = datetime.strptime(receipt_time, "%H:%M")
            diff = abs((tx_time - rc_time).total_seconds() / 60)
            return diff <= time_tolerance_minutes
        except:
            # If time parsing fails, just check date and amount
            return True
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "row_num": self.row_num,
            "date_time": self.date_time,
            "merchant": self.merchant,
            "amount": self.amount,
            "yongdo": self.yongdo,
            "content": self.content,
            "status": self.status,
            "yongdo_filled": self.yongdo_filled,
            "content_filled": self.content_filled,
            "needs_attendee": self.needs_attendee,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Transaction":
        """Create Transaction from a dict payload."""
        return cls(
            row_num=data.get("row_num", 0),
            date_time=data.get("date_time", ""),
            merchant=data.get("merchant", ""),
            amount=data.get("amount", 0),
            yongdo=data.get("yongdo"),
            content=data.get("content"),
            status=data.get("status", ""),
        )

    @classmethod
    def from_grid_api(cls, data: Dict[str, Any]) -> "Transaction":
        """
        Create Transaction from Grid API data (dataProvider.getJsonRow output).

        Grid API fields mapped to Transaction:
        - row_num (already 1-based from API)
        - datetime -> date_time
        - merchant -> merchant
        - amount -> amount (already int)
        - purpose_name -> yongdo
        - content -> content
        - validation -> status
        """
        return cls(
            row_num=data.get("row_num", 0),
            date_time=data.get("datetime", ""),
            merchant=data.get("merchant", ""),
            amount=data.get("amount", 0),
            yongdo=data.get("purpose_name"),
            content=data.get("content"),
            status=data.get("validation", ""),
        )


@dataclass
class TransactionList:
    """Complete list of transactions from Douzone STEP 2."""
    transactions: List[Transaction] = field(default_factory=list)
    total_amount: int = 0
    captured_at: str = ""
    max_row_num: int = 0
    
    def __post_init__(self):
        if not self.captured_at:
            self.captured_at = datetime.now().isoformat()
        if not self.max_row_num and self.transactions:
            self.max_row_num = max((t.row_num for t in self.transactions), default=0)
    
    @property
    def count(self) -> int:
        return len(self.transactions)
    
    @property
    def needs_attendee_count(self) -> int:
        return sum(1 for t in self.transactions if t.needs_attendee)
    
    def get_by_row(self, row_num: int) -> Optional[Transaction]:
        """Get transaction by row number."""
        for t in self.transactions:
            if t.row_num == row_num:
                return t
        return None
    
    def get_by_date(self, date: str) -> List[Transaction]:
        """Get all transactions for a given date (YYYY-MM-DD or M/D format)."""
        # Normalize date format
        if "/" in date and "-" not in date:
            # Convert M/D to YYYY-MM-DD (assume current year)
            parts = date.split("/")
            if len(parts) == 2:
                month, day = int(parts[0]), int(parts[1])
                year = datetime.now().year
                date = f"{year}-{month:02d}-{day:02d}"
        
        return [t for t in self.transactions if t.date == date]
    
    def find_matching_transaction(self, receipt_date: str, receipt_time: str, 
                                   receipt_amount: int) -> Optional[Transaction]:
        """Find a transaction that matches a receipt."""
        for t in self.transactions:
            if t.matches_receipt(receipt_date, receipt_time, receipt_amount):
                return t
        return None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "transactions": [t.to_dict() for t in self.transactions],
            "total_amount": self.total_amount,
            "captured_at": self.captured_at,
            "count": self.count,
            "needs_attendee_count": self.needs_attendee_count,
            "max_row_num": self.max_row_num,
        }

    @classmethod
    def from_grid_api(cls, grid_data: List[Dict[str, Any]]) -> "TransactionList":
        """
        Create TransactionList from Grid API output (list of transaction dicts).

        This is the preferred method when using Grid API instead of OCR.
        Much faster and 100% accurate.

        Args:
            grid_data: List of dicts from automation.read_all_transactions_from_grid()

        Returns:
            TransactionList with all transactions
        """
        transactions = [Transaction.from_grid_api(d) for d in grid_data]
        total = sum(t.amount for t in transactions)
        max_row = max((t.row_num for t in transactions), default=0)

        return cls(
            transactions=transactions,
            total_amount=total,
            captured_at=datetime.now().isoformat(),
            max_row_num=max_row,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TransactionList":
        """Create TransactionList from a dict payload."""
        transactions = [Transaction.from_dict(t) for t in data.get("transactions", [])]
        return cls(
            transactions=transactions,
            total_amount=data.get("total_amount", 0),
            captured_at=data.get("captured_at", ""),
            max_row_num=data.get("max_row_num", 0),
        )


class TransactionParser:
    """
    Parses Douzone STEP 2 transaction grid using screenshots and Vision AI.

    Supports multiple Vision backends: Gemini (recommended), Claude.
    No API keys needed - uses existing CLI subscriptions.
    """

    def __init__(self, page, screenshot_dir: str = "screenshots", backend: str = "gemini"):
        """
        Initialize parser.

        Args:
            page: Playwright page object (connected to Douzone)
            screenshot_dir: Directory to save screenshots
            backend: Vision backend to use ("gemini", "claude", or "qwen25vl")
                     Default: "gemini" (more accurate for Korean OCR)
        """
        self.page = page
        self.screenshot_dir = screenshot_dir
        self.backend = backend.lower()
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(screenshot_dir, exist_ok=True)

        # Validate backend
        if self.backend not in ["gemini", "claude", "qwen25vl"]:
            raise ValueError(f"Unsupported backend: {backend}. Use 'gemini', 'claude', or 'qwen25vl'")

        # Initialize qwen25vl client if needed (server-mode only)
        if self.backend == "qwen25vl":
            try:
                import socket
                from ocr_api.client import OCRClient

                # Quick reachability check (2 second timeout)
                sock = socket.create_connection(("200.168.0.41", 8810), timeout=2)
                sock.close()

                self.ocr_client = OCRClient(host="200.168.0.41", port=8810, timeout=30)
                logger.info("Initialized Qwen2.5-VL API client (200.168.0.41:8810)")
            except (ImportError, OSError, socket.timeout, ConnectionRefusedError) as e:
                logger.warning(f"Qwen2.5-VL not available ({e}), falling back to claude backend")
                self.backend = "claude"

        logger.info(f"Using {self.backend.upper()} backend for vision parsing")

    def _safe_screenshot_path(self, filename: str) -> str:
        """Return a writable screenshot path, avoiding unwritable existing files."""
        path = os.path.join(self.screenshot_dir, filename)
        if os.path.exists(path) and not os.access(path, os.W_OK):
            alt = f"{self.session_id}_{filename}"
            return os.path.join(self.screenshot_dir, alt)
        return path

    async def get_total_rows_from_header_badge(self) -> Optional[int]:
        """
        Try to read total row count from the STEP2 header badge (red number).

        Returns:
            int if found, else None.
        """
        step = self.page.locator('text=STEP2').first
        if await step.count() == 0:
            step = self.page.locator('text=지출정보등록').first
            if await step.count() == 0:
                return None

        js = """
        (el) => {
          function isNumeric(text){
            if (!text) return false;
            for (let i = 0; i < text.length; i++) {
              const ch = text[i];
              if (ch < '0' || ch > '9') return false;
            }
            return true;
          }
          function parseRgb(bg){
            if (!bg) return null;
            let s = bg.replace('rgba', '').replace('rgb', '').replace('(', '').replace(')', '');
            const parts = s.split(',').map(p => parseInt(p.trim(), 10)).filter(n => !Number.isNaN(n));
            if (parts.length < 3) return null;
            return {r: parts[0], g: parts[1], b: parts[2]};
          }
          function isRed(bg){
            const rgb = parseRgb(bg);
            if (!rgb) return false;
            return rgb.r >= 150 && rgb.g < 100 && rgb.b < 100;
          }
          const stepRect = el.getBoundingClientRect();
          let root = el;
          for (let i=0; i<4 && root; i++) root = root.parentElement;
          if (!root) root = el.parentElement || el;
          const nodes = root.querySelectorAll('span,div,em,b,strong');
          const candidates = [];
          for (const node of nodes){
            const text = (node.textContent || '').trim();
            if (!isNumeric(text)) continue;
            const rect = node.getBoundingClientRect();
            if (!rect || rect.width < 6 || rect.height < 6) continue;
            const centerY = rect.y + rect.height / 2;
            const stepCenterY = stepRect.y + stepRect.height / 2;
            if (Math.abs(centerY - stepCenterY) > 80) continue;
            if (rect.x + rect.width < stepRect.x) continue;
            const style = window.getComputedStyle(node);
            candidates.push({
              text: text,
              bg: style.backgroundColor || ''
            });
          }
          if (!candidates.length) return null;
          const reds = candidates.filter(c => isRed(c.bg));
          const list = reds.length ? reds : candidates;
          let best = list[0];
          for (const c of list){
            if (parseInt(c.text,10) > parseInt(best.text,10)) best = c;
          }
          return parseInt(best.text, 10);
        }
        """

        try:
            value = await step.evaluate(js)
            return int(value) if value else None
        except Exception as e:
            logger.warning(f"Failed to read STEP2 badge count: {e}")
            return None
    
    async def capture_all_transactions(self, max_scrolls: int = 50,
                                       stop_after_n_empty: int = 3) -> TransactionList:
        """
        Capture all transactions from the grid by scrolling and taking screenshots.

        Args:
            max_scrolls: Maximum number of scroll operations (safety limit, default: 50)
            stop_after_n_empty: Stop after N consecutive screenshots with no new rows (default: 3)

        Returns:
            TransactionList with all transactions
        """
        all_transactions: Dict[str, Transaction] = {}  # Key: date_time+amount for dedup
        consecutive_empty = 0  # Track consecutive screenshots with no new rows
        highest_row_num = 0  # Track highest row number seen

        # Get canvas element and bounding box for preprocessing
        canvas = self.page.locator('canvas[role=application]')
        canvas_box = await canvas.bounding_box()
        if not canvas_box:
            raise Exception("Canvas element not found")

        # Click to focus canvas
        center_x = canvas_box['x'] + canvas_box['width'] / 2
        center_y = canvas_box['y'] + canvas_box['height'] / 2
        await self.page.mouse.click(center_x, center_y)
        await asyncio.sleep(0.3)

        await self._scroll_to_top_with_verify(canvas_box)

        # Capture screenshots while scrolling
        screenshot_paths = []
        for i in range(max_scrolls + 1):
            # Take screenshot
            path = self._safe_screenshot_path(f"grid_capture_{i}.png")
            await self.page.screenshot(path=path, full_page=False)
            screenshot_paths.append(path)

            # Parse this screenshot
            transactions = await self._parse_screenshot(path)

            # Add to collection (dedup by date_time + amount)
            new_count = 0
            duplicate_count = 0
            for t in transactions:
                key = f"{t.date_time}_{t.amount}"
                if key not in all_transactions:
                    all_transactions[key] = t
                    new_count += 1
                    # Track highest row number
                    if t.row_num > highest_row_num:
                        highest_row_num = t.row_num
                else:
                    duplicate_count += 1

            # Log progress with more detail
            total_unique = len(all_transactions)
            logger.info(f"Screenshot {i+1}/{max_scrolls+1}: Found {len(transactions)} rows "
                       f"({new_count} new, {duplicate_count} duplicates) | "
                       f"Total unique: {total_unique} | Highest row: {highest_row_num}")

            # Track consecutive empty screenshots
            if new_count == 0 and i > 0:
                consecutive_empty += 1
                logger.info(f"No new rows found ({consecutive_empty}/{stop_after_n_empty} consecutive)")

                # Stop if we've seen N consecutive screenshots with no new data
                if consecutive_empty >= stop_after_n_empty:
                    logger.info(f"Reached end: {consecutive_empty} consecutive screenshots with no new rows")
                    break
            else:
                # Reset counter if we found new rows
                consecutive_empty = 0

            # Scroll down for next capture
            if i < max_scrolls:
                # Scroll amount: 60% of canvas height to ensure 40% overlap
                # This prevents skipping rows on small screens
                scroll_amount = int(canvas_box['height'] * 0.6)
                # Ensure minimum scroll but don't exceed reasonable jump
                scroll_amount = max(100, min(scroll_amount, 300))
                
                await self.page.mouse.wheel(0, scroll_amount)
                await asyncio.sleep(0.3)
        
        # Sort by row number and rebuild row numbers
        sorted_transactions = sorted(
            all_transactions.values(),
            key=lambda t: (t.date_time, t.amount)
        )
        
        # Reassign row numbers based on order
        for idx, t in enumerate(sorted_transactions, 1):
            t.row_num = idx
        
        # Calculate total
        total_amount = sum(t.amount for t in sorted_transactions)
        
        result = TransactionList(
            transactions=sorted_transactions,
            total_amount=total_amount,
            max_row_num=highest_row_num,
        )
        
        logger.info(f"Captured {result.count} total transactions, total: {total_amount:,}원")
        
        return result

    async def _scroll_to_top_with_verify(self, canvas_box: Dict, max_attempts: int = 10) -> None:
        """
        Scroll to the top of the grid and verify via screenshot parsing.

        Uses Vision parsing to confirm the smallest visible row number is 1.

        Args:
            canvas_box: Canvas bounding box for preprocessing
        """
        # Click center of canvas to ensure focus
        center_x = canvas_box['x'] + canvas_box['width'] / 2
        center_y = canvas_box['y'] + canvas_box['height'] / 2
        await self.page.mouse.click(center_x, center_y)
        await asyncio.sleep(0.3)

        # Very aggressive initial scroll-up (doubled from 15 to 30)
        logger.info("Performing aggressive scroll to top...")
        for i in range(30):
            await self.page.mouse.wheel(0, -5000)  # Increased from -3000
            await asyncio.sleep(0.1)

        # Wait for grid to settle
        await asyncio.sleep(0.8)

        last_min_row = None
        for attempt in range(max_attempts):
            path = self._safe_screenshot_path(f"grid_top_check_{attempt}.png")
            await self.page.screenshot(path=path, full_page=False)
            await asyncio.sleep(0.3)  # Wait after screenshot

            rows = await self._parse_screenshot(path)
            row_nums = [t.row_num for t in rows if t.row_num > 0]

            if row_nums:
                last_min_row = min(row_nums)
                logger.info(f"Verification attempt {attempt+1}: min row = {last_min_row}")

                if last_min_row <= 1:
                    logger.info(f"✅ Reached top of grid (min row: {last_min_row})")
                    return

                # Still not at top, scroll more
                logger.info(f"Not at top yet (min row: {last_min_row}), scrolling up more...")
                for _ in range(5):
                    await self.page.mouse.wheel(0, -3000)
                    await asyncio.sleep(0.1)
                await asyncio.sleep(0.5)
            else:
                logger.warning(f"No row numbers detected on attempt {attempt+1}")
                # Try scrolling up anyway
                for _ in range(3):
                    await self.page.mouse.wheel(0, -3000)
                    await asyncio.sleep(0.1)
                await asyncio.sleep(0.5)

        if last_min_row is not None and last_min_row > 1:
            logger.error(f"⚠️  Failed to reach top! Closest: row {last_min_row}. First {last_min_row-1} rows may be missing!")
        elif last_min_row is None:
            logger.error("⚠️  Could not verify scroll position - row numbers not detected")
    
    async def _parse_screenshot(self, screenshot_path: str, max_retries: int = 2) -> List[Transaction]:
        """
        Parse a single screenshot using selected Vision backend with retry and fallback.

        Args:
            screenshot_path: Path to screenshot image
            max_retries: Number of retries before falling back (default: 2)

        Returns:
            List of Transaction objects
        """
        # Get just the filename for the prompt
        filename = os.path.basename(screenshot_path)

        prompt = f"""Read the file {filename}. This is a Douzone STEP 2 expense grid screenshot.

Extract ALL visible transaction rows. For each row, extract:
- row_num: The row number shown (1-based)
- date_time: 사용일시 column (format: "YYYY-MM-DD HH:MM")
- merchant: 사용처 column (merchant/vendor name)
- amount: 청구금액 column (number only, no comma or won)
- yongdo: 용도 column (category, or null if empty)
- content: 내용 column (description, or null if empty)
- status: 검증결과 column (e.g., "참석자 미입력", "적합")

IMPORTANT:
- Extract ALL visible rows, including partially visible ones
- For amount, return as integer (e.g., 3500 not "3,500")
- If a field is empty/blank, return null
- Skip the header row and 합계 (total) row

Return ONLY a JSON array of objects, no explanation."""

        # Try primary backend with retries
        primary_backend = self.backend
        for attempt in range(max_retries + 1):
            try:
                if primary_backend == "gemini":
                    response_text = await self._parse_with_gemini(screenshot_path, prompt)
                elif primary_backend == "qwen25vl":
                    response_text = await self._parse_with_qwen25vl(screenshot_path, prompt)
                else:  # claude
                    response_text = await self._parse_with_claude(screenshot_path, prompt)

                # Success - break retry loop
                break

            except Exception as e:
                if attempt < max_retries:
                    logger.warning(f"{primary_backend.upper()} failed (attempt {attempt+1}/{max_retries+1}): {str(e)[:100]}")
                    await asyncio.sleep(1)  # Brief delay before retry
                    continue
                else:
                    # Max retries reached - try fallback
                    logger.error(f"{primary_backend.upper()} failed after {max_retries+1} attempts: {str(e)[:100]}")

                    # Fallback to Claude if Gemini/Qwen fails
                    if primary_backend in ["gemini", "qwen25vl"]:
                        logger.warning(f"Falling back to CLAUDE for {filename}")
                        try:
                            response_text = await self._parse_with_claude(screenshot_path, prompt)
                            logger.info(f"✅ Claude fallback succeeded for {filename}")
                        except Exception as fallback_error:
                            logger.error(f"❌ Claude fallback also failed: {str(fallback_error)[:100]}")
                            logger.error(f"SKIPPING screenshot: {filename}")
                            return []  # Return empty list to continue processing
                    else:
                        # Claude was primary and failed - no fallback available
                        logger.error(f"SKIPPING screenshot: {filename}")
                        return []

        # Parse response
        try:

            # Extract JSON array from response
            json_start = response_text.find('[')
            json_end = response_text.rfind(']') + 1

            if json_start == -1 or json_end == 0:
                logger.error(f"No JSON array found in response: {response_text[:200]}")
                return []

            json_str = response_text[json_start:json_end]
            data = json.loads(json_str)
            
            # Convert to Transaction objects
            transactions = []
            for item in data:
                try:
                    # Normalize row_num (may be string)
                    row_num = item.get('row_num', 0)
                    if isinstance(row_num, str):
                        row_num = int(row_num)
                    else:
                        row_num = int(row_num) if row_num else 0

                    # Normalize amount (Gemini may return string with comma)
                    amount = item.get('amount', 0)
                    if isinstance(amount, str):
                        amount = int(amount.replace(',', '').replace('원', '').strip())
                    else:
                        amount = int(amount)

                    t = Transaction(
                        row_num=row_num,
                        date_time=item.get('date_time', ''),
                        merchant=item.get('merchant', ''),
                        amount=amount,
                        yongdo=item.get('yongdo'),
                        content=item.get('content'),
                        status=item.get('status', ''),
                    )
                    transactions.append(t)
                except Exception as e:
                    logger.warning(f"Failed to parse transaction: {item}, error: {e}")
            
            return transactions
            
        except subprocess.TimeoutExpired:
            logger.error(f"{self.backend.upper()} CLI timed out")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON: {e}")
            return []
        except Exception as e:
            logger.error(f"Error parsing screenshot: {e}")
            return []

    async def _parse_with_claude(self, screenshot_path: str, prompt: str) -> str:
        """Parse screenshot using Claude Code CLI."""
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--no-session-persistence"
        ]

        # Strip ALL CLAUDE* env vars to avoid nested-session block
        _env = {k: v for k, v in os.environ.items()
                if not k.startswith("CLAUDE")}

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            cwd=os.path.dirname(screenshot_path),
            env=_env,
        )

        if result.returncode != 0:
            raise Exception(f"Claude CLI error: {result.stderr}")

        if not result.stdout:
            raise Exception(f"Claude CLI returned empty output (stdout=None). stderr: {result.stderr}")

        # Parse JSON output from Claude Code CLI
        try:
            cli_output = json.loads(result.stdout)
            if cli_output.get("is_error"):
                raise Exception(f"Claude error: {cli_output.get('result')}")
            return cli_output.get("result") or ""
        except (json.JSONDecodeError, TypeError):
            return result.stdout or ""

    async def _parse_with_gemini(self, screenshot_path: str, prompt: str) -> str:
        """Parse screenshot using Gemini CLI."""
        cmd = [
            "gemini",
            "-m", "gemini-3-flash-preview",  # Use single fast model (not dual models)
            "-o", "json",
            prompt
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,  # Faster with single model
            cwd=os.path.dirname(screenshot_path)
        )

        if result.returncode != 0:
            raise Exception(f"Gemini CLI error: {result.stderr}")

        # Parse JSON output from Gemini CLI
        cli_output = json.loads(result.stdout)
        response = cli_output.get("response", "")

        # Normalize amount strings to integers (Gemini returns "3,500" format)
        return response

    async def _parse_with_qwen25vl(self, screenshot_path: str, prompt: str) -> str:
        """Parse screenshot using Qwen2.5-VL API."""
        # Remove the "Read the file {filename}. " prefix as API doesn't need it
        # The prompt already contains the rest of the instructions
        if "Read the file " in prompt:
            prompt = prompt.split(". ", 1)[1] if ". " in prompt else prompt

        result = self.ocr_client.ocr(screenshot_path, prompt=prompt)

        if not result["success"]:
            raise Exception(f"Qwen2.5-VL API error: {result.get('error', 'Unknown error')}")

        logger.info(f"Qwen2.5-VL latency: {result['latency_ms']:.0f}ms")
        return result["text"]

    async def capture_single_screenshot(self) -> str:
        """Capture a single screenshot of current view."""
        path = os.path.join(self.screenshot_dir, "current_view.png")
        await self.page.screenshot(path=path, full_page=False)
        return path


# Convenience function for direct use
async def parse_douzone_transactions(page, screenshot_dir: str = "screenshots") -> TransactionList:
    """
    Parse all transactions from Douzone STEP 2.
    
    Args:
        page: Playwright page object connected to Douzone
        screenshot_dir: Directory for screenshots
        
    Returns:
        TransactionList with all transactions
    """
    parser = TransactionParser(page, screenshot_dir)
    return await parser.capture_all_transactions()
