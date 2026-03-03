"""
Calibration Module for Douzone Grid

Provides multiple calibration strategies:
1. VisionCalibrator - Uses Claude Vision API (requires ANTHROPIC_API_KEY)
2. ClickTestCalibrator - Tests clicks and auto-adjusts (no API needed)
3. Manual calibration - User provides values directly

This eliminates hardcoded "magic numbers" and makes automation robust
across different browsers, zoom levels, and UI updates.
"""
import asyncio
import base64
import logging
import json
import os
import re
from dataclasses import dataclass
from typing import Optional, Tuple, List
from pathlib import Path
from datetime import datetime

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# anthropic is optional - only needed for VisionCalibrator
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False


@dataclass
class GridCalibration:
    """Calibrated grid layout parameters."""
    header_height: float      # Height of the header row (px)
    row_height: float         # Height of each data row (px)
    plus_button_offset: float # Distance from right edge to + button center (px)
    
    # Metadata
    canvas_width: float = 0
    canvas_height: float = 0
    calibrated_at: str = ""
    confidence: str = "unknown"
    method: str = "unknown"
    
    def to_dict(self) -> dict:
        return {
            "header_height": self.header_height,
            "row_height": self.row_height,
            "plus_button_offset": self.plus_button_offset,
            "canvas_width": self.canvas_width,
            "canvas_height": self.canvas_height,
            "calibrated_at": self.calibrated_at,
            "confidence": self.confidence,
            "method": self.method,
        }


# Default fallback values (used if calibration fails)
# Note: row_height should be queried from RealGrid API at runtime via getDisplayOptions()
# These are fallbacks only if the API query fails
DEFAULT_CALIBRATION = GridCalibration(
    header_height=42,  # Header height (API doesn't expose this directly)
    row_height=45,     # Updated: RealGrid API getDisplayOptions().rowHeight reports 45
    plus_button_offset=115,
    confidence="default",
    method="hardcoded"
)


class VerificationCalibrator:
    """
    Simple calibration that verifies defaults work with ONE test click.
    
    NO API KEY REQUIRED.
    
    Strategy:
    1. Use default values
    2. Do ONE test click
    3. Report if it worked or not
    4. Clean up any UI changes
    
    This is safe because:
    - Only ONE click (no loops)
    - Always cleans up (Escape key)
    - Returns clear pass/fail result
    """
    
    async def calibrate(self, page: Page, test_click: bool = True) -> GridCalibration:
        """
        Verify that default values work.
        
        Args:
            page: Playwright page
            test_click: If True, do one test click to verify. If False, just return defaults.
        
        Returns:
            GridCalibration with verification status
        """
        logger.info("Starting verification calibration...")
        
        # Get canvas bounding box
        canvas = page.locator('canvas[role=application]')
        box = await canvas.bounding_box()
        if not box:
            logger.warning("Could not get canvas bounding box")
            return DEFAULT_CALIBRATION
        
        logger.info(f"Canvas: {box['width']:.0f}x{box['height']:.0f} at ({box['x']:.0f}, {box['y']:.0f})")
        
        # Use default values
        header_height = DEFAULT_CALIBRATION.header_height
        row_height = DEFAULT_CALIBRATION.row_height
        plus_offset = DEFAULT_CALIBRATION.plus_button_offset
        
        # Calculate click position (row 0)
        click_y = box['y'] + header_height + (row_height / 2)
        click_x = box['x'] + box['width'] - plus_offset
        
        confidence = "unverified"
        method = "defaults"
        
        if test_click:
            logger.info(f"Testing click at ({click_x:.0f}, {click_y:.0f})...")
            
            # Do ONE test click
            await page.mouse.click(click_x, click_y)
            await asyncio.sleep(0.5)
            
            # Handle confirmation dialog if it appears
            confirm = page.locator('text="데이터 변경 확인"')
            if await confirm.is_visible():
                await page.locator('button:has-text("확인")').first.click()
                await asyncio.sleep(0.3)
            
            # Check if popup opened
            popup_opened = await self._check_popup_opened(page)
            
            # ALWAYS clean up - close any open popup/dialog
            await page.keyboard.press('Escape')
            await asyncio.sleep(0.2)
            await page.keyboard.press('Escape')  # Double escape to be safe
            await asyncio.sleep(0.1)
            
            if popup_opened:
                logger.info("✅ Default values VERIFIED - popup opened successfully")
                confidence = "verified"
                method = "verified_defaults"
            else:
                logger.warning("❌ Default values did NOT work - popup didn't open")
                logger.warning("   You may need to adjust values manually")
                confidence = "failed"
                method = "unverified_defaults"
        
        return GridCalibration(
            header_height=header_height,
            row_height=row_height,
            plus_button_offset=plus_offset,
            canvas_width=box['width'],
            canvas_height=box['height'],
            calibrated_at=datetime.now().isoformat(),
            confidence=confidence,
            method=method
        )
    
    async def _check_popup_opened(self, page: Page) -> bool:
        """Check if the expense popup is open."""
        indicators = [
            'input[placeholder*="참석자"]',
            'input[placeholder*="내용을 입력"]',
        ]
        for selector in indicators:
            try:
                elem = page.locator(selector).first
                if await elem.is_visible():
                    return True
            except:
                continue
        return False


# Alias for backward compatibility
ClickTestCalibrator = VerificationCalibrator


class BrowserUseCalibrator:
    """
    Calibrates grid by analyzing screenshot with browser-use Cloud API.
    
    Requires: BROWSER_USE_API_KEY
    Cost: ~$0.01-0.02 per calibration
    
    How it works:
    1. Takes a screenshot locally
    2. Uploads to browser-use cloud
    3. Uses their LLM to measure pixel values
    4. Returns calibration
    """
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize calibrator.
        
        Args:
            api_key: browser-use API key. If None, reads from BROWSER_USE_API_KEY env var.
        """
        self.api_key = api_key or os.environ.get('BROWSER_USE_API_KEY')
        if not self.api_key:
            logger.warning("BROWSER_USE_API_KEY not set - BrowserUseCalibrator unavailable")
            self._available = False
            return
        
        try:
            from browser_use_sdk import BrowserUse
            self.client = BrowserUse(api_key=self.api_key)
            self._available = True
        except ImportError:
            logger.warning("browser_use_sdk not installed")
            self._available = False
        except Exception as e:
            logger.warning(f"Failed to initialize browser-use client: {e}")
            self._available = False
    
    @property
    def is_available(self) -> bool:
        return self._available
    
    async def calibrate(
        self, 
        page: Page, 
        screenshot_path: str = "screenshots/calibration.png",
        llm_model: str = "browser-use-llm"
    ) -> GridCalibration:
        """
        Calibrate by analyzing screenshot with browser-use LLM.
        
        Args:
            page: Playwright page
            screenshot_path: Where to save screenshot
            llm_model: LLM model to use (browser-use-llm is cheapest at $0.002/step)
        """
        if not self.is_available:
            logger.warning("BrowserUseCalibrator not available")
            return DEFAULT_CALIBRATION
        
        import requests
        import time
        
        logger.info("Starting browser-use calibration...")
        
        # Get canvas bounding box first
        canvas = page.locator('canvas[role=application]')
        box = await canvas.bounding_box()
        if not box:
            logger.warning("Could not get canvas bounding box")
            return DEFAULT_CALIBRATION
        
        # Take screenshot
        await page.screenshot(path=screenshot_path)
        logger.info(f"Screenshot saved: {screenshot_path}")
        
        # Create session and upload
        try:
            session = self.client.sessions.create_session()
            session_id = session.id
            logger.info(f"Created session: {session_id}")
            
            # Get upload URL
            file_size = os.path.getsize(screenshot_path)
            upload_info = self.client.files.agent_session_upload_file_presigned_url(
                session_id=session_id,
                file_name='calibration.png',
                content_type='image/png',
                size_bytes=file_size
            )
            
            # Upload file
            with open(screenshot_path, 'rb') as f:
                files = {'file': ('calibration.png', f, 'image/png')}
                response = requests.post(
                    upload_info.url,
                    data=upload_info.fields,
                    files=files
                )
            
            if response.status_code != 201:
                logger.error(f"Upload failed: {response.status_code}")
                return DEFAULT_CALIBRATION
            
            logger.info("Screenshot uploaded")
            
            # Create analysis task
            task = self.client.tasks.create_task(
                task='''Analyze the uploaded screenshot of a Douzone expense grid.

Measure these values in PIXELS by examining the grid carefully:
1. header_height: Height of the header row (row with column titles like "선택", "사용일시", "사용처")
2. row_height: Height of each data row (rows with expense entries)
3. plus_button_offset: Distance from the RIGHT EDGE of the grid to the CENTER of the blue "+" button

Return ONLY a JSON object with these three numeric values:
{"header_height": <number>, "row_height": <number>, "plus_button_offset": <number>}''',
                llm=llm_model,
                session_id=session_id,
                max_steps=3,
                vision=True
            )
            
            logger.info(f"Task created: {task.id}")
            
            # Wait for completion (max 60 seconds)
            for _ in range(30):
                await asyncio.sleep(2)
                result = self.client.tasks.get_task(task.id)
                if result.status == 'finished':
                    break
            else:
                logger.warning("Task timed out")
                return DEFAULT_CALIBRATION
            
            # Parse result from steps
            calibration = self._parse_result(result, box)
            
            # Clean up session
            try:
                self.client.sessions.update_session(session_id, action='stop')
            except:
                pass
            
            return calibration
            
        except Exception as e:
            logger.error(f"Browser-use calibration failed: {e}")
            return DEFAULT_CALIBRATION
    
    def _parse_result(self, result, canvas_box: dict) -> GridCalibration:
        """Parse the task result to extract measurements."""
        import re
        
        # Look for JSON in the output or steps
        json_pattern = r'\{[^{}]*"header_height"[^{}]*\}'
        
        # Check output first
        match = re.search(json_pattern, result.output or "")
        
        # If not found, check steps
        if not match:
            for step in result.steps:
                for action in step.actions:
                    match = re.search(json_pattern, action)
                    if match:
                        break
                if match:
                    break
        
        if not match:
            logger.warning("Could not find measurements in response")
            return DEFAULT_CALIBRATION
        
        try:
            data = json.loads(match.group())
            header = float(data.get('header_height', 32))
            row = float(data.get('row_height', 27))
            plus_offset = float(data.get('plus_button_offset', 115))
            
            logger.info(f"Parsed measurements: header={header}, row={row}, plus_offset={plus_offset}")
            
            return GridCalibration(
                header_height=header,
                row_height=row,
                plus_button_offset=plus_offset,
                canvas_width=canvas_box['width'],
                canvas_height=canvas_box['height'],
                calibrated_at=datetime.now().isoformat(),
                confidence="medium",
                method="browser_use"
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Failed to parse measurements: {e}")
            return DEFAULT_CALIBRATION


class ManualCalibrator:
    """
    Simple calibrator that accepts user-provided values.
    
    Usage:
        cal = ManualCalibrator()
        calibration = cal.calibrate(header=32, row=27, plus_offset=115)
    """
    
    def calibrate(
        self,
        header_height: float = 42,
        row_height: float = 40,
        plus_button_offset: float = 115
    ) -> GridCalibration:
        """Create calibration from manual values."""
        return GridCalibration(
            header_height=header_height,
            row_height=row_height,
            plus_button_offset=plus_button_offset,
            calibrated_at=datetime.now().isoformat(),
            confidence="manual",
            method="manual"
        )


class VisionCalibrator:
    """
    Calibrates grid layout using Claude Vision API.
    
    Requires: ANTHROPIC_API_KEY
    
    Usage:
        calibrator = VisionCalibrator(api_key)
        calibration = await calibrator.calibrate(page, screenshot_path)
    """
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the calibrator.
        
        Args:
            api_key: Anthropic API key. If None, reads from ANTHROPIC_API_KEY env var.
        """
        if not ANTHROPIC_AVAILABLE:
            logger.warning("anthropic package not installed - VisionCalibrator unavailable")
            self.client = None
            self._available = False
            return
        
        try:
            self.client = anthropic.Anthropic(api_key=api_key)
            self.model = "claude-sonnet-4-20250514"  # Good balance of speed and accuracy
            self._available = True
        except Exception as e:
            logger.warning(f"Failed to initialize Anthropic client: {e}")
            self.client = None
            self._available = False
    
    @property
    def is_available(self) -> bool:
        """Check if Vision calibration is available."""
        return self._available and self.client is not None
    
    async def calibrate(
        self, 
        page: Page, 
        screenshot_path: str = "screenshots/calibration.png"
    ) -> GridCalibration:
        """
        Calibrate grid parameters by analyzing a screenshot.
        
        Args:
            page: Playwright page object
            screenshot_path: Where to save the calibration screenshot
        
        Returns:
            GridCalibration with measured parameters
        """
        if not self.is_available:
            logger.warning("Vision calibration not available - falling back to defaults")
            return DEFAULT_CALIBRATION
        
        logger.info("Starting vision calibration...")
        
        # Step 1: Take screenshot
        await page.screenshot(path=screenshot_path)
        logger.info(f"Screenshot saved to {screenshot_path}")
        
        # Step 2: Get canvas bounding box for context
        canvas = page.locator('canvas[role=application]')
        box = await canvas.bounding_box()
        if not box:
            logger.warning("Could not get canvas bounding box, using defaults")
            return DEFAULT_CALIBRATION
        
        logger.info(f"Canvas: {box['width']:.0f}x{box['height']:.0f} at ({box['x']:.0f}, {box['y']:.0f})")
        
        # Step 3: Send to Claude Vision for analysis
        try:
            calibration = await self._analyze_with_vision(
                screenshot_path,
                canvas_width=box['width'],
                canvas_height=box['height']
            )
            return calibration
        except Exception as e:
            logger.error(f"Vision calibration failed: {e}")
            logger.warning("Falling back to default values")
            return DEFAULT_CALIBRATION
    
    async def _analyze_with_vision(
        self,
        screenshot_path: str,
        canvas_width: float,
        canvas_height: float
    ) -> GridCalibration:
        """Send screenshot to Claude Vision and parse the response."""
        
        # Read and encode image
        with open(screenshot_path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode("utf-8")
        
        # Craft the prompt
        prompt = """Analyze this Douzone expense grid screenshot and measure the following in PIXELS:

1. **Header Height**: The height of the header row (the row containing column titles like "선택", "사용일시", "사용처", etc.)

2. **Row Height**: The height of each DATA row (the rows below the header containing actual expense entries)

3. **Plus Button Offset**: The horizontal distance from the RIGHT EDGE of the grid to the CENTER of the blue "+" button in the "추가항목" column

Look at the grid carefully:
- The header row typically has a slightly different background color
- Data rows have alternating colors or consistent styling
- The "+" buttons are small blue icons in the rightmost column area

IMPORTANT: Provide measurements in PIXELS. Be precise.

Respond in this exact JSON format:
```json
{
  "header_height": <number>,
  "row_height": <number>,
  "plus_button_offset": <number>,
  "confidence": "high" | "medium" | "low",
  "reasoning": "<brief explanation of how you measured>"
}
```"""

        # Call Claude Vision API (sync, but we're in async context)
        response = await asyncio.to_thread(
            self._call_claude_vision,
            image_data,
            prompt
        )
        
        # Parse response
        return self._parse_response(response, canvas_width, canvas_height)
    
    def _call_claude_vision(self, image_data: str, prompt: str) -> str:
        """Make the actual API call to Claude."""
        message = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ],
                }
            ],
        )
        return message.content[0].text
    
    def _parse_response(
        self, 
        response: str, 
        canvas_width: float, 
        canvas_height: float
    ) -> GridCalibration:
        """Parse Claude's response into GridCalibration."""
        logger.debug(f"Claude response: {response}")
        
        # Extract JSON from response (it might be wrapped in markdown code block)
        json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            # Try to find raw JSON
            json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
            else:
                raise ValueError(f"Could not find JSON in response: {response}")
        
        data = json.loads(json_str)
        
        # Validate values are reasonable
        header_height = float(data.get("header_height", 42))
        row_height = float(data.get("row_height", 40))
        plus_offset = float(data.get("plus_button_offset", 115))
        confidence = data.get("confidence", "unknown")
        reasoning = data.get("reasoning", "")

        # Sanity checks
        if not (20 <= header_height <= 100):
            logger.warning(f"Header height {header_height} seems off, using default")
            header_height = 42
        if not (20 <= row_height <= 80):
            logger.warning(f"Row height {row_height} seems off, using default")
            row_height = 40
        if not (50 <= plus_offset <= 300):
            logger.warning(f"Plus offset {plus_offset} seems off, using default")
            plus_offset = 115
        
        logger.info(f"Calibration result: header={header_height}px, row={row_height}px, plus_offset={plus_offset}px")
        logger.info(f"Confidence: {confidence}")
        if reasoning:
            logger.info(f"Reasoning: {reasoning}")
        
        from datetime import datetime
        
        return GridCalibration(
            header_height=header_height,
            row_height=row_height,
            plus_button_offset=plus_offset,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            calibrated_at=datetime.now().isoformat(),
            confidence=confidence
        )


async def quick_calibrate(
    page: Page, 
    method: str = "auto",
    api_key: Optional[str] = None
) -> GridCalibration:
    """
    Convenience function for quick calibration.
    
    Args:
        page: Playwright page object
        method: Calibration method - "auto", "vision", "click_test", or "default"
        api_key: Anthropic API key (only needed for "vision" method)
    
    Usage:
        from src.calibration import quick_calibrate
        
        # Auto-select best available method
        calibration = await quick_calibrate(page)
        
        # Force click-test (no API needed)
        calibration = await quick_calibrate(page, method="click_test")
        
        # Use vision (requires API key)
        calibration = await quick_calibrate(page, method="vision", api_key="sk-...")
    """
    if method == "default":
        return DEFAULT_CALIBRATION
    
    if method == "vision":
        calibrator = VisionCalibrator(api_key)
        if calibrator.is_available:
            return await calibrator.calibrate(page)
        else:
            logger.warning("Vision calibrator not available, falling back to click_test")
            method = "click_test"
    
    if method == "click_test":
        calibrator = ClickTestCalibrator()
        return await calibrator.calibrate(page)
    
    # Auto mode: try vision first, fall back to click_test
    if method == "auto":
        # Try vision if API key provided
        if api_key:
            vision = VisionCalibrator(api_key)
            if vision.is_available:
                try:
                    return await vision.calibrate(page)
                except Exception as e:
                    logger.warning(f"Vision calibration failed: {e}")
        
        # Fall back to click_test
        click_test = ClickTestCalibrator()
        return await click_test.calibrate(page)
    
    # Unknown method
    logger.warning(f"Unknown calibration method: {method}, using defaults")
    return DEFAULT_CALIBRATION
