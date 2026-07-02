"""
Douzone Ad-hoc Operations API.

Thin wrapper around DouzoneAutomation for targeted, composable operations.
Used by the Claude Code agent to perform individual edits outside the pipeline.

Usage (from agent):
    from src.operations import connect, read_grid, edit_row, attach_receipt, fill_supplier

    auto = await connect("http://localhost:9222")
    grid = await read_grid(auto)
    await attach_receipt(auto, row=5, path="/path/to/receipt.jpg")
    await fill_supplier(auto, row=5, name="맛나분식", biz_no="123-45-67890")
    await disconnect(auto)
"""

import os
import logging
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)


async def connect(cdp_url: str = "http://localhost:9222") -> 'DouzoneAutomation':
    """Connect to Douzone via CDP. Returns automation instance."""
    from .automation import DouzoneAutomation
    auto = DouzoneAutomation(cdp_url)
    await auto.connect()
    return auto


async def disconnect(auto: 'DouzoneAutomation') -> None:
    """Close the automation connection."""
    await auto.close()


async def read_grid(auto: 'DouzoneAutomation') -> List[Dict[str, Any]]:
    """
    Read all rows from the Douzone grid via DataProvider API.

    Returns list of dicts, each with:
        row_index (0-based), merchant, amount, date_time, yongdo, content,
        validation, attendee, status
    """
    total = await auto.get_grid_row_count() or 0
    rows = []
    for i in range(total):
        status = await auto.get_row_status_via_dp(i)
        if status:
            status['row_index'] = i
            status['row_number'] = i + 1
            rows.append(status)
    return rows


async def get_row_status(auto: 'DouzoneAutomation', row: int) -> Optional[Dict[str, str]]:
    """
    Read a single row's status (yongdo, content, validation, attendee).
    Row is 1-based.
    """
    return await auto.get_row_status_via_dp(row - 1)


async def get_row_fields(auto: 'DouzoneAutomation', row: int) -> Optional[Dict[str, str]]:
    """
    Read a single row's basic fields (yongdo, content).
    Row is 1-based.
    """
    return await auto.get_row_fields_via_dp(row - 1)


async def get_grid_info(auto: 'DouzoneAutomation') -> Dict[str, Any]:
    """Get grid metadata: total rows, visible rows, current top item."""
    total = await auto.get_grid_row_count() or 0
    visible = await auto.get_visible_row_count()
    top = await auto.get_grid_top_item()
    return {
        "total_rows": total,
        "visible_rows": visible,
        "top_item": top,
    }


async def scroll_to_top(auto: 'DouzoneAutomation') -> None:
    """Scroll grid to the top."""
    await auto.scroll_grid_to_top()


async def scroll_to_row(auto: 'DouzoneAutomation', row: int) -> Optional[int]:
    """
    Navigate grid to show a specific row. Row is 1-based.
    Returns the view index (position in visible area) or None on failure.
    """
    total = await auto.get_grid_row_count() or row
    visible = await auto.get_visible_row_count() or 10
    return await auto.navigate_to_row(row - 1, total, visible)


async def edit_row(
    auto: 'DouzoneAutomation',
    row: int,
    fields: Dict[str, str],
) -> bool:
    """
    Open a row's popup, fill specified fields, and save.
    Row is 1-based.

    Supported fields:
        attendee  - 참석자
        supplier_name - 실공급자상호
        supplier_biz_no - 실공급자 사업자등록번호
        bigo - 비고 (remarks)

    Returns True on success.
    """
    idx = row - 1
    if not await auto._open_popup_for_row(idx):
        logger.error(f"Failed to open popup for row {row}")
        return False

    try:
        # Fill requested fields
        if 'attendee' in fields:
            attendee_input = auto.page.locator('input[placeholder*="참석자"]').first
            if await attendee_input.is_visible():
                await attendee_input.fill(fields['attendee'])

        if 'supplier_name' in fields:
            supplier_input = auto.page.locator('input[placeholder*="실공급자상호"]').first
            if await supplier_input.is_visible():
                await supplier_input.fill(fields['supplier_name'])

        if 'supplier_biz_no' in fields:
            biz_input = auto.page.locator('input[placeholder*="사업자등록번호"]').first
            if await biz_input.is_visible():
                await biz_input.fill(fields['supplier_biz_no'])

        if 'bigo' in fields:
            await auto.fill_bigo_field(fields['bigo'])

        # Save
        saved = await auto.save_popup()
        if not saved:
            logger.error(f"Failed to save popup for row {row}")
            return False

        return True

    except Exception as e:
        logger.error(f"Error editing row {row}: {e}")
        await auto.cancel_popup()
        return False


async def attach_receipt(
    auto: 'DouzoneAutomation',
    row: int,
    path: str,
) -> bool:
    """
    Open a row's popup, attach a receipt file, and save.
    Row is 1-based. Path must be an absolute path to a JPG/PNG/PDF file.

    Returns True on success.
    """
    if not os.path.exists(path):
        logger.error(f"Receipt file not found: {path}")
        return False

    idx = row - 1
    if not await auto._open_popup_for_row(idx):
        logger.error(f"Failed to open popup for row {row}")
        return False

    try:
        attached = await auto.attach_file(path)
        if not attached:
            logger.error(f"Failed to attach file to row {row}")
            await auto.cancel_popup()
            return False

        saved = await auto.save_popup()
        if not saved:
            logger.error(f"Failed to save after attaching file to row {row}")
            return False

        return True

    except Exception as e:
        logger.error(f"Error attaching receipt to row {row}: {e}")
        await auto.cancel_popup()
        return False


async def fill_supplier(
    auto: 'DouzoneAutomation',
    row: int,
    name: str,
    biz_no: str,
) -> bool:
    """
    Open a row's popup, fill supplier info (실공급자), and save.
    Row is 1-based.

    Args:
        name: 실공급자상호 (real supplier name)
        biz_no: 실공급자 사업자등록번호 (business registration number)

    Returns True on success.
    """
    return await edit_row(auto, row, {
        'supplier_name': name,
        'supplier_biz_no': biz_no,
    })
