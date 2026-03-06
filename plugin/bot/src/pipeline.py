"""
Pipeline Module: Connects OCR/NLP extraction with Douzone automation.

This module bridges:
- Receipt OCR (extracts vendor info from images)
- Memo Parsing (extracts attendees, date/time from natural language)
- Douzone Automation (fills expense forms)

AI tasks use a configurable LLM provider (Claude CLI, Gemini CLI, or OpenRouter).
"""

import logging
import json
import re
from typing import Optional, List, Dict, Any, TYPE_CHECKING
from dataclasses import dataclass, field
from pathlib import Path

from .models import ExpenseData
from .ocr import extract_receipt, ReceiptData, ClaudeCodeReceiptExtractor, CLAUDE_CODE_AVAILABLE

if TYPE_CHECKING:
    from .llm_provider import LLMProvider

logger = logging.getLogger(__name__)


# ============================================================================
# MEMO PARSING (Claude Code CLI)
# ============================================================================

MEMO_PARSE_PROMPT = """Parse this Korean expense memo and extract structured information.

Memo: "{memo}"

Extract:
1) attendees: List of attendee NAMES only
2) date: Date in MM-DD format (e.g., "01-06")
3) time: Time in HH:MM format (24-hour, e.g., "15:00")
4) merchant_hint: List of merchant/store names mentioned (if any)

STRICT RULES FOR attendees:
- Attendees are ONLY person names.
- Names are typically 2–3 Hangul characters (allow 2–4). Keep only pure Hangul tokens.
- EXCLUDE any non-name words such as:
  점심, 저녁, 아침, 회식, 회의, 미팅, 커피, 간식, 식사, 디저트, 팀, 업무, 프로젝트,
  매니저, 리더, 파트, TF, 본부, 센터, 실, 부, 과
- EXCLUDE store/merchant names if present (e.g., 스타벅스, 배민, 쿠팡이츠, GS25, 이마트, 코엑스).
- EXCLUDE tokens containing digits or special characters (except commas/spaces as separators).
- If a line has "참석자:" or similar label, ONLY parse names after that label.
- If no valid attendee names remain, return attendees: [].

MERCHANT_HINT rules:
- If a store/merchant name is mentioned, put it in merchant_hint.
- merchant_hint should NOT overlap with attendees.
- If no merchant is mentioned, return [].

DATE/TIME rules:
- Parse explicit dates like "1/6", "1월6일", "2026-01-06".
- Return date as MM-DD (ignore year).
- Parse explicit times like "15:30", "15시 30분", "오전 10시", "오후 3시".
- If explicit time is missing, INFER time from keywords:
  - 아침/조식: 08:00
  - 점심/중식: 12:30
  - 브런치: 11:00
  - 간식/커피: 15:00
  - 저녁/석식/회식: 19:00
  - 야식: 22:00
- Only infer when time is missing. Do NOT override explicit times.

Return ONLY a JSON object like this:
{{
  "attendees": ["<이름>", "홍길동"],
  "date": "01-06",
  "time": "12:30",
  "merchant_hint": ["스타벅스"]
}}

If a field cannot be determined, use null.
Return ONLY the JSON, no other text."""


MEMO_BATCH_PARSE_PROMPT = """Parse these Korean expense memos (one per line) and extract structured information.

Memos:
{memos_text}

For each line that contains expense info, extract:
1. raw_text: The original line text
2. attendees: List of attendee NAMES only
3. date: Date in MM-DD format (e.g., "01-06")
4. time: Time in HH:MM format (24-hour, e.g., "15:00")
5. merchant_hint: List of merchant/store names mentioned (if any)

STRICT RULES FOR attendees:
- Attendees are ONLY person names.
- Names are typically 2–3 Hangul characters (allow 2–4). Keep only pure Hangul tokens.
- EXCLUDE any non-name words such as:
  점심, 저녁, 아침, 회식, 회의, 미팅, 커피, 간식, 식사, 디저트, 팀, 업무, 프로젝트,
  매니저, 리더, 파트, TF, 본부, 센터, 실, 부, 과
- EXCLUDE store/merchant names if present (e.g., 스타벅스, 배민, 쿠팡이츠, GS25, 이마트, 코엑스).
- EXCLUDE tokens containing digits or special characters (except commas/spaces as separators).
- If a line has "참석자:" or similar label, ONLY parse names after that label.
- If no valid attendee names remain, return attendees: [].

MERCHANT_HINT rules:
- If a store/merchant name is mentioned, put it in merchant_hint.
- merchant_hint should NOT overlap with attendees.
- If no merchant is mentioned, return [].

DATE/TIME rules:
- Parse explicit dates like "1/6", "1월6일", "2026-01-06".
- Return date as MM-DD (ignore year).
- Parse explicit times like "15:30", "15시 30분", "오전 10시", "오후 3시".
- If explicit time is missing, INFER time from keywords:
  - 아침/조식: 08:00
  - 점심/중식: 12:30
  - 브런치: 11:00
  - 간식/커피: 15:00
  - 저녁/석식/회식: 19:00
  - 야식: 22:00
- Only infer when time is missing. Do NOT override explicit times.

Return ONLY a JSON array of objects. Example:
[
  {{
    "raw_text": "1/6 15시 <이름> 홍길동",
    "attendees": ["<이름>", "홍길동"],
    "date": "01-06",
    "time": "15:00",
    "merchant_hint": ["스타벅스"]
  }}
]

Ignore empty lines or lines without expense info.
Return ONLY the JSON array, no other text."""


@dataclass
class MemoData:
    """Parsed memo information."""
    attendees: List[str]
    date: Optional[str] = None  # MM-DD format
    time: Optional[str] = None  # HH:MM format
    merchant_hint: List[str] = field(default_factory=list)
    raw_memo: str = ""
    
    @property
    def attendees_str(self) -> str:
        """Get attendees as comma-separated string."""
        return ", ".join(self.attendees) if self.attendees else ""

    @property
    def merchant_hint_str(self) -> str:
        """Get merchant hints as comma-separated string."""
        return ", ".join(self.merchant_hint) if self.merchant_hint else ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "attendees": self.attendees,
            "date": self.date,
            "time": self.time,
            "merchant_hint": self.merchant_hint,
        }

    def to_cache_dict(self) -> Dict[str, Any]:
        """Convert to dict payload for cache storage."""
        return {
            "attendees": self.attendees,
            "date": self.date,
            "time": self.time,
            "merchant_hint": self.merchant_hint,
            "raw_memo": self.raw_memo,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoData":
        """Create MemoData from a dict payload."""
        return cls(
            attendees=data.get("attendees", []) or [],
            date=data.get("date"),
            time=data.get("time"),
            merchant_hint=data.get("merchant_hint", []) or [],
            raw_memo=data.get("raw_memo", ""),
        )


async def parse_memo(memo: str, provider: "LLMProvider" = None) -> MemoData:
    """
    Parse a natural language memo to extract attendees and datetime.

    Args:
        memo: Natural language memo, e.g., "1/6 15시 <이름> 홍길동"
        provider: LLM provider to use (required).

    Returns:
        MemoData with extracted attendees, date, and time.

    Examples:
        >>> await parse_memo("1/6 15시 <이름> 홍길동", provider=provider)
        MemoData(attendees=["<이름>", "홍길동"], date="01-06", time="15:00")
    """
    if not provider:
        logger.error("No LLM provider available for memo parsing")
        return MemoData(attendees=[], raw_memo=memo)

    prompt = MEMO_PARSE_PROMPT.format(memo=memo)

    try:
        response_text = await provider.complete(prompt, timeout=30)
        data = _parse_json_response(response_text)
        return MemoData(
            attendees=data.get("attendees", []) or [],
            date=data.get("date"),
            time=data.get("time"),
            merchant_hint=data.get("merchant_hint", []) or [],
            raw_memo=memo,
        )
    except Exception as e:
        logger.error(f"Failed to parse memo via {provider.name}: {e}")
        return MemoData(attendees=[], raw_memo=memo)


async def parse_memos_batch(memos_text: str, provider: "LLMProvider" = None) -> List[MemoData]:
    """
    Parse multiple memos at once using a single AI call.

    Args:
        memos_text: Full content of memo.txt
        provider: LLM provider to use (required).

    Returns:
        List of MemoData objects
    """
    if not memos_text.strip():
        return []

    if not provider:
        logger.error("No LLM provider available for batch memo parsing")
        return []

    prompt = MEMO_BATCH_PARSE_PROMPT.format(memos_text=memos_text)

    try:
        response_text = await provider.complete(prompt, timeout=60)
        return _parse_batch_memo_response(response_text)
    except Exception as e:
        logger.error(f"Failed to batch parse memos via {provider.name}: {e}")
        return []


def _parse_batch_memo_response(response_text: str) -> List[MemoData]:
    """Parse batch memo response text into MemoData list."""
    data_list = _parse_json_response(response_text)

    if not isinstance(data_list, list):
        logger.warning(f"Expected JSON list from batch parse, got {type(data_list)}")
        if isinstance(data_list, dict):
            data_list = data_list.get("memos", []) or [data_list]
        else:
            return []

    results = []
    for item in data_list:
        if not isinstance(item, dict):
            continue
        memo_data = MemoData(
            attendees=item.get("attendees", []) or [],
            date=item.get("date"),
            time=item.get("time"),
            merchant_hint=item.get("merchant_hint", []) or [],
            raw_memo=item.get("raw_text", "")
        )
        results.append(memo_data)

    return results


def _parse_json_response(response: str) -> dict:
    """Parse JSON from Claude's response."""
    # Try direct parse
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    # Try to find JSON in markdown block
    json_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', response)
    if json_block_match:
        try:
            return json.loads(json_block_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find raw JSON array (for list of matches)
    json_array_match = re.search(r'\[[\s\S]*\]', response)
    if json_array_match:
        try:
            return json.loads(json_array_match.group())
        except json.JSONDecodeError:
            pass

    # Try to find raw JSON object
    json_match = re.search(r'\{[\s\S]*\}', response)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    return {}


# ============================================================================
# MATCHING LOGIC (Claude Code CLI)
# ============================================================================

@dataclass
class CardTransaction:
    """A card transaction row from Douzone grid."""
    row_index: int
    datetime: str        # "2026-01-06 15:23" format
    merchant: str        # "스타벅스", "우아한형제들"
    amount: int          # 4500
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "row": self.row_index,
            "datetime": self.datetime,
            "merchant": self.merchant,
            "amount": self.amount,
        }


@dataclass  
class MatchResult:
    """Result of matching an item to a transaction."""
    item_id: str                    # Memo ID or receipt filename
    item_type: str                  # "memo" or "receipt"
    matched_row: Optional[int]      # Row index in grid (None if no match)
    confidence: float               # 0.0 to 1.0
    reason: str                     # Why this match was made
    
    @property
    def needs_review(self) -> bool:
        """Returns True if this match should be reviewed by user."""
        return self.confidence < 0.8 or self.matched_row is None
    
    @property
    def is_confident(self) -> bool:
        """Returns True if this is a high-confidence match."""
        return self.confidence >= 0.8 and self.matched_row is not None


# Optimized concise prompt (70% shorter, faster processing)
MATCHING_PROMPT = """Match items to transactions by date/time/merchant.

Transactions: {transactions_json}
Items: {items_json}

Match rules:
1. Date match required
2. Exact time+amount = 0.9-1.0
3. Same date + merchant/time hint = 0.7-0.9
4. Same date only = 0.5-0.7
5. No match = 0.0-0.5 (matched_row: null)

IMPORTANT - Korean abbreviations (must match these):
- 비마게, 비마이게 = 비마이게스트
- 스벅 = 스타벅스, 스타벅스코리아
- 메가커피, MGC = 메가엠지씨커피
- 신프코, 코엑스몰 = 신세계프라퍼티코엑스몰
- 샐러디 = 샐러디아
- Match ANY partial/abbreviated merchant name to the full merchant name

IMPORTANT - Product hints: The memo may contain a product name instead of a merchant name.
Match the product to the merchant that sells it. Examples:
- 케이크 (cake) → bakeries like 파리크라상, 파리바게뜨, 뚜레쥬르, etc.
- 커피 → coffee shops like 스타벅스, 메가커피, etc.
- 치킨 → chicken restaurants
- 피자 → pizza restaurants
Use common sense to match product hints to merchants even when the merchant name doesn't literally contain the product word.

Context:
- Time hints: 점심=12-14h, 저녁=18-20h
- If memo has merchant hint (abbreviated or full), ALWAYS match to transaction with that merchant
- If memo has a product hint (e.g. 케이크), match to the merchant most likely to sell that product
- If multiple transactions on same date, match to the one closest to the memo's time hint
- Lower confidence (0.5-0.7) for ambiguous cases where human clarification is needed

CRITICAL: You MUST try to match every memo to a transaction. Use merchant names, product hints, time hints, and common sense. Return empty array [] ONLY if there is truly no date match.

YOU MUST RESPOND WITH ONLY A JSON ARRAY. No explanations, no markdown, just the raw JSON array.
Example: [{{"item_id": "memo_1", "matched_row": 5, "confidence": 0.85, "reason": "date+merchant match"}}]
If no matches: []"""


def _find_exact_receipt_matches(
    receipts: List[ReceiptData],
    transactions: List[CardTransaction]
) -> tuple:
    """
    Pre-match receipts with exact datetime + amount matches.

    Returns:
        Tuple of (exact_matches, unmatched_receipts)
    """
    exact_matches = []
    unmatched_receipts = []

    for receipt in receipts:
        if not receipt.transaction:
            unmatched_receipts.append(receipt)
            continue

        receipt_date = receipt.transaction.date
        receipt_time = receipt.transaction.time
        receipt_amount = receipt.transaction.amount

        if not receipt_date or not receipt_time or not receipt_amount:
            unmatched_receipts.append(receipt)
            continue

        # Normalize time format (remove seconds if present)
        receipt_time_short = receipt_time[:5] if len(receipt_time) > 5 else receipt_time

        matched = False
        for tx in transactions:
            tx_date = tx.datetime.split(' ')[0] if ' ' in tx.datetime else tx.datetime
            tx_time = tx.datetime.split(' ')[1][:5] if ' ' in tx.datetime else ""

            # Exact match: date + time (HH:MM ±1 minute) + amount
            date_match = tx_date == receipt_date
            amount_match = tx.amount == receipt_amount

            # Check time match with ±1 minute tolerance
            time_match = False
            if tx_time and receipt_time_short:
                try:
                    # Parse HH:MM to minutes
                    tx_h, tx_m = map(int, tx_time.split(':'))
                    receipt_h, receipt_m = map(int, receipt_time_short.split(':'))
                    tx_minutes = tx_h * 60 + tx_m
                    receipt_minutes = receipt_h * 60 + receipt_m
                    # Allow ±1 minute difference
                    time_match = abs(tx_minutes - receipt_minutes) <= 1
                except (ValueError, IndexError):
                    # Fallback to exact string match if parsing fails
                    time_match = tx_time == receipt_time_short

            if date_match and time_match and amount_match:

                # Use source_path for receipt ID (not raw_text which is JSON response)
                receipt_name = Path(receipt.source_path or 'unknown').name if receipt.source_path else 'unknown'
                # Determine if it's exact or near-exact match
                time_diff_text = ""
                if tx_time != receipt_time_short:
                    time_diff_text = f" (±1min: tx {tx_time} vs receipt {receipt_time_short})"

                exact_matches.append(MatchResult(
                    item_id=f"receipt_{receipt_name}",
                    item_type="receipt",
                    matched_row=tx.row_index,
                    confidence=1.0,
                    reason=f"Exact match: date {receipt_date}, time {receipt_time_short}, amount {receipt_amount}{time_diff_text}"
                ))
                matched = True
                logger.info(f"Exact match: receipt {receipt_name} → Row {tx.row_index + 1}{time_diff_text}")
                break

        if not matched:
            unmatched_receipts.append(receipt)

    return exact_matches, unmatched_receipts


def _rule_based_matching(
    date_txs: List[CardTransaction],
    date_items: List[Dict[str, Any]]
) -> List[MatchResult]:
    """
    Fallback rule-based matching when Claude times out.

    Simple heuristics:
    - Receipts with exact time+amount → 0.9
    - Items with no hints → 0.0 (needs review)
    """
    matches = []

    for item in date_items:
        item_id = item["id"]
        item_type = item["type"]

        if item_type == "receipt":
            # Try time + amount match
            item_time = item.get("time", "")[:5]
            item_amount = item.get("amount")

            best_match = None
            best_score = 0.0

            for tx in date_txs:
                tx_time = tx.datetime.split(' ')[1][:5] if ' ' in tx.datetime else ""

                # Time match
                time_match = (item_time == tx_time) if item_time else False
                # Amount match
                amount_match = (item_amount == tx.amount) if item_amount else False

                if time_match and amount_match:
                    score = 0.95
                elif time_match:
                    score = 0.7
                elif amount_match:
                    score = 0.6
                else:
                    score = 0.0

                if score > best_score:
                    best_score = score
                    best_match = tx.row_index

            matches.append(MatchResult(
                item_id=item_id,
                item_type=item_type,
                matched_row=best_match if best_score >= 0.6 else None,
                confidence=best_score,
                reason=f"Rule-based match: score {best_score:.2f}"
            ))
        else:
            # Memo with no hints - low confidence
            matches.append(MatchResult(
                item_id=item_id,
                item_type=item_type,
                matched_row=None,
                confidence=0.0,
                reason="Rule-based: memo without time/merchant hints, needs review"
            ))

    return matches


async def _match_single_batch_optimized(
    date_key: str,
    date_txs: List[CardTransaction],
    current_batch_items: List[Dict[str, Any]],
    timeout: int = 90,
    provider: "LLMProvider" = None,
) -> List[MatchResult]:
    """
    Match items for a single date using LLM provider with optimizations.

    OPTIMIZATIONS:
    - Concise prompt (70% shorter)
    - Fast model hint (haiku for Claude, flash for Gemini)
    - Extended timeout (90s)
    - Rule-based fallback on timeout

    Args:
        date_key: Date string (MM-DD)
        date_txs: Transactions for this date
        current_batch_items: Items to match
        timeout: CLI timeout in seconds
        provider: LLM provider to use. Falls back to Claude Code CLI if None.

    Returns:
        List of MatchResult for this batch
    """
    # Build concise prompt
    transactions_json = json.dumps([t.to_dict() for t in date_txs], ensure_ascii=False)
    items_json = json.dumps(current_batch_items, ensure_ascii=False)

    prompt = MATCHING_PROMPT.format(
        transactions_json=transactions_json,
        items_json=items_json,
    )

    try:
        logger.info(f"Matching batch for date {date_key}: {len(date_txs)} txs, {len(current_batch_items)} items ({timeout}s timeout)")

        # Use provider (required)
        if not provider:
            logger.error(f"No LLM provider available for matching date {date_key}")
            return _rule_based_matching(date_txs, current_batch_items)

        response_text = await provider.complete(prompt, model_hint="fast", timeout=timeout)

        # Parse the matches
        matches_data = _parse_json_response(response_text)

        if isinstance(matches_data, dict):
            matches_data = matches_data.get("matches", [])
        elif isinstance(matches_data, list):
            pass
        else:
            logger.error(f"Unexpected matches_data type: {type(matches_data)}")
            matches_data = []

        logger.info(f"Found {len(matches_data)} matches for date {date_key}")

        matches = []
        for match in matches_data:
            item_id = match.get("item_id", "")
            item_type = "memo" if item_id.startswith("memo_") else "receipt"
            matched_row_idx = match.get("matched_row")

            result_obj = MatchResult(
                item_id=item_id,
                item_type=item_type,
                matched_row=matched_row_idx,
                confidence=float(match.get("confidence", 0)),
                reason=match.get("reason", ""),
            )
            matches.append(result_obj)

        return matches

    except Exception as e:
        logger.error(f"Failed to match items for date {date_key}: {e}")
        logger.warning(f"Using rule-based fallback for date {date_key} due to error")
        return _rule_based_matching(date_txs, current_batch_items)


async def match_items_to_transactions(
    transactions: List[CardTransaction],
    memos: List[MemoData] = None,
    receipts: List[ReceiptData] = None,
    use_optimization: bool = True,
    provider: "LLMProvider" = None,
) -> List[MatchResult]:
    """
    Match memos and receipts to card transactions using LLM provider.

    OPTIMIZATIONS (when use_optimization=True):
    1. Pre-match exact receipts (skip LLM)
    2. Parallel LLM calls for different dates
    3. Concise prompts + fast model hint
    4. Timeout handling with rule-based fallback

    Args:
        transactions: List of card transactions from Douzone grid
        memos: List of parsed memos (with date/time)
        receipts: List of receipt data (with transaction date/time)
        use_optimization: Enable optimization features

    Returns:
        List of MatchResult with confidence scores.
    """
    if not provider:
        logger.error("No LLM provider available for matching")
        return []

    memos = memos or []
    receipts = receipts or []

    if not transactions:
        return []

    # OPTIMIZATION 1: Pre-match exact receipts (skip Claude)
    if use_optimization and receipts:
        logger.info("Pre-matching exact receipts...")
        exact_matches, unmatched_receipts = _find_exact_receipt_matches(receipts, transactions)
        logger.info(f"Found {len(exact_matches)} exact matches, {len(unmatched_receipts)} need Claude")
        receipts = unmatched_receipts  # Only process unmatched receipts
    else:
        exact_matches = []
    
    # Helper to extract MM-DD from YYYY-MM-DD or MM-DD strings
    def get_mm_dd(date_str: Optional[str]) -> Optional[str]:
        if not date_str:
            return None
        # Handle "YYYY-MM-DD"
        if len(date_str) == 10 and date_str[4] == '-':
            return date_str[5:]
        # Handle "MM-DD"
        if len(date_str) == 5 and date_str[2] == '-':
            return date_str
        return None

    # Group transactions by Date (MM-DD)
    tx_by_date: Dict[str, List[CardTransaction]] = {}
    for tx in transactions:
        date_key = get_mm_dd(tx.datetime.split(' ')[0])
        if date_key:
            if date_key not in tx_by_date:
                tx_by_date[date_key] = []
            tx_by_date[date_key].append(tx)
            
    # Group items by Date (MM-DD)
    items_by_date: Dict[str, List[Dict[str, Any]]] = {}
    items_no_date: List[Dict[str, Any]] = []
    
    # Process Memos
    for i, memo in enumerate(memos):
        item = {
            "id": f"memo_{i}",
            "type": "memo",
            "date": memo.date,
            "time": memo.time,
            "content": memo.raw_memo[:50],
            "merchant_hint": memo.merchant_hint or [],
        }
        date_key = get_mm_dd(memo.date)
        if date_key:
            if date_key not in items_by_date:
                items_by_date[date_key] = []
            items_by_date[date_key].append(item)
        else:
            items_no_date.append(item)
            
    # Process Receipts
    for receipt in receipts:
        # Use source_path for receipt ID (not raw_text which is JSON response)
        receipt_name = Path(receipt.source_path or 'unknown').name if receipt.source_path else 'unknown'
        item = {
            "id": f"receipt_{receipt_name}",
            "type": "receipt",
            "date": receipt.transaction.date if receipt.transaction else None,
            "time": receipt.transaction.time if receipt.transaction else None,
            "vendor": receipt.vendor_info.name if receipt.vendor_info else None,
            "amount": receipt.transaction.amount if receipt.transaction else None
        }
        date_key = get_mm_dd(item['date'])
        if date_key:
            if date_key not in items_by_date:
                items_by_date[date_key] = []
            items_by_date[date_key].append(item)
        else:
            items_no_date.append(item)

    all_results = exact_matches  # Start with pre-matched exact receipts

    # Iterate over each date present in transactions
    # Note: If an item has a date that isn't in transactions, we might miss it here.
    # But usually we want to match items TO transactions.
    # Let's iterate over ALL dates that have either transactions OR items.
    all_dates = set(tx_by_date.keys()) | set(items_by_date.keys())

    # OPTIMIZATION 2: Prepare parallel Claude calls
    if use_optimization:
        # Build all batch tasks
        batch_tasks = []
        for date_key in sorted(list(all_dates)):
            date_txs = tx_by_date.get(date_key, [])
            date_items = items_by_date.get(date_key, [])

            if not date_txs and not date_items:
                continue
            if not date_items and not items_no_date:
                continue

            current_batch_items = date_items + items_no_date
            if not current_batch_items:
                continue

            batch_tasks.append((date_key, date_txs, current_batch_items))

        # Process batches in parallel
        logger.info(f"Processing {len(batch_tasks)} date batches in parallel...")
        import asyncio
        tasks = [
            _match_single_batch_optimized(date_key, date_txs, items, provider=provider)
            for date_key, date_txs, items in batch_tasks
        ]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Aggregate results
        for i, result in enumerate(batch_results):
            if isinstance(result, Exception):
                logger.error(f"Batch {i} failed: {result}")
                continue

            logger.info(f"Batch {i} returned {len(result)} matches")
            all_results.extend(result)

        logger.info(f"Total aggregated matches: {len(all_results)} (exact_matches={len(exact_matches)} + batches)")

        return all_results

    # FALLBACK: Original sequential processing (use_optimization=False)
    for date_key in sorted(list(all_dates)):
        date_txs = tx_by_date.get(date_key, [])
        date_items = items_by_date.get(date_key, [])
        
        # Add undated items to every bucket? No, that's inefficient.
        # Maybe handle undated items separately against ALL transactions?
        # For now, let's just process dated items against dated transactions.
        
        if not date_txs and not date_items:
            continue
            
        if not date_items and not items_no_date:
            # No items to match for this date, and no fallback items
            continue
            
        # Combine specific date items + global undated items
        current_batch_items = date_items + items_no_date
        
        if not current_batch_items:
            continue
            
        # Build prompt for this batch
        transactions_json = json.dumps([t.to_dict() for t in date_txs], ensure_ascii=False, indent=2)
        items_json = json.dumps(current_batch_items, ensure_ascii=False, indent=2)

        prompt = MATCHING_PROMPT.format(
            transactions_json=transactions_json,
            items_json=items_json,
        )

        try:
            logger.info(f"Matching batch for date {date_key}: {len(date_txs)} txs, {len(current_batch_items)} items")

            if not provider:
                logger.error(f"No LLM provider available for matching date {date_key}")
                continue

            response_text = await provider.complete(prompt, timeout=60)

            # Parse the matches
            matches_data = _parse_json_response(response_text)
            if isinstance(matches_data, dict):
                matches_data = matches_data.get("matches", [])
            
            for match in matches_data:
                item_id = match.get("item_id", "")
                item_type = "memo" if item_id.startswith("memo_") else "receipt"
                
                # Verify match is valid (points to a valid row in THIS batch)
                matched_row_idx = match.get("matched_row")
                
                # Check if this item was already matched with higher confidence?
                # For now, just append. Orchestrator can handle duplicates if needed.
                
                all_results.append(MatchResult(
                    item_id=item_id,
                    item_type=item_type,
                    matched_row=matched_row_idx,
                    confidence=float(match.get("confidence", 0)),
                    reason=match.get("reason", ""),
                ))
                
        except Exception as e:
            logger.error(f"Failed to match items for date {date_key}: {e}")
            continue

    return all_results


def filter_matches_for_review(matches: List[MatchResult]) -> tuple:
    """
    Split matches into confident (auto-process) and needs-review.
    
    Returns:
        Tuple of (confident_matches, review_needed_matches)
    """
    confident = [m for m in matches if m.is_confident]
    needs_review = [m for m in matches if m.needs_review]
    return confident, needs_review


# ============================================================================
# RECEIPT OCR (existing)
# ============================================================================


async def extract_supplier_info(receipt_path: str) -> tuple:
    """
    Extract supplier (실공급자) information from a receipt image.
    
    Args:
        receipt_path: Path to the receipt image file.
        
    Returns:
        Tuple of (supplier_name, supplier_biz_no) or (None, None) if extraction fails.
    """
    try:
        logger.info(f"Extracting supplier info from: {receipt_path}")
        
        result = await extract_receipt(receipt_path)
        
        if not result.is_receipt:
            logger.warning(f"Image does not appear to be a receipt: {receipt_path}")
            return (None, None)
        
        vendor = result.vendor_info
        supplier_name = vendor.name
        supplier_biz_no = vendor.biz_num
        
        logger.info(f"Extracted: name={supplier_name}, biz_no={supplier_biz_no}, confidence={result.confidence}")
        
        return (supplier_name, supplier_biz_no)
        
    except Exception as e:
        logger.error(f"Failed to extract supplier info: {e}")
        return (None, None)


async def process_receipt_for_expense(
    receipt_path: str,
    merchant: str,
    yongdo: str,
    content: str,
    attendees: str,
) -> ExpenseData:
    """
    Process a receipt and create ExpenseData with extracted vendor info.
    
    This is the main integration function that:
    1. Runs OCR on the receipt image
    2. Extracts vendor (실공급자) information
    3. Creates ExpenseData with all fields filled
    
    Args:
        receipt_path: Path to the receipt image (배민 등 PG receipts)
        merchant: Merchant name from card statement (e.g., "우아한형제들")
        yongdo: Purpose code or name (e.g., "중식대")
        content: Expense description
        attendees: Attendee names
        
    Returns:
        ExpenseData with supplier_name and supplier_biz_no populated from OCR.
    """
    # Extract vendor info from receipt
    supplier_name, supplier_biz_no = await extract_supplier_info(receipt_path)
    
    # Create ExpenseData with all fields
    expense = ExpenseData(
        merchant=merchant,
        yongdo=yongdo,
        content=content,
        attendees=attendees,
        supplier_name=supplier_name,
        supplier_biz_no=supplier_biz_no,
        receipt_paths=[receipt_path],
    )

    return expense


async def enrich_expense_with_receipt(
    expense: ExpenseData,
    receipt_path: str,
) -> ExpenseData:
    """
    Enrich an existing ExpenseData with supplier info from a receipt.
    
    Use this when you already have ExpenseData and want to add supplier info.
    
    Args:
        expense: Existing ExpenseData object
        receipt_path: Path to the receipt image
        
    Returns:
        The same ExpenseData with supplier fields updated.
    """
    supplier_name, supplier_biz_no = await extract_supplier_info(receipt_path)
    
    expense.supplier_name = supplier_name
    expense.supplier_biz_no = supplier_biz_no
    expense.receipt_paths = [receipt_path]
    
    return expense


def is_pg_merchant(merchant: str) -> bool:
    """
    Check if a merchant name indicates a PG (payment gateway) transaction.
    These transactions require 실공급자 info to be filled.
    
    Args:
        merchant: Merchant name from card statement
        
    Returns:
        True if this is likely a PG transaction requiring vendor info.
    """
    pg_keywords = [
        '우아한형제들', '배달의민족', '배민',  # Baemin
        '쿠팡이츠', 'COUPANGEATS',            # Coupang Eats
        '요기요', 'YOGIYO',                    # Yogiyo
        'NHN KCP', 'KCP',                      # Payment gateways
        '이니시스', 'INICIS',
        '토스페이', 'TOSSPAY',
        '카카오페이', 'KAKAOPAY',
        '네이버페이', 'NAVERPAY',
        '페이코', 'PAYCO',
    ]
    
    merchant_upper = merchant.upper()
    return any(kw.upper() in merchant_upper for kw in pg_keywords)


# Batch processing helper
async def process_receipts_batch(
    receipt_merchant_pairs: list,
    default_yongdo: str = "중식대",
    default_content: str = "식사",
    default_attendees: str = "",
) -> list:
    """
    Process multiple receipts in batch.
    
    Args:
        receipt_merchant_pairs: List of (receipt_path, merchant_name) tuples
        default_yongdo: Default purpose if not specified
        default_content: Default content if not specified  
        default_attendees: Default attendees if not specified
        
    Returns:
        List of ExpenseData objects with extracted vendor info.
    """
    results = []
    
    for receipt_path, merchant in receipt_merchant_pairs:
        expense = await process_receipt_for_expense(
            receipt_path=receipt_path,
            merchant=merchant,
            yongdo=default_yongdo,
            content=default_content,
            attendees=default_attendees,
        )
        results.append(expense)
        logger.info(f"Processed: {merchant} → {expense.supplier_name}")
    
    return results


# Quick check if OCR is available
def is_ocr_available() -> bool:
    """Check if OCR capabilities are available."""
    return CLAUDE_CODE_AVAILABLE


