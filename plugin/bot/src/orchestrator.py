"""
MVP Orchestrator for Douzone Expense Automation.

This module ties together all components to deliver the MVP flow:
1. Data Collection - Parse Douzone grid, memos, receipts
2. Matching - Match transactions ↔ memos ↔ receipts
3. Review & Confirm - Show plan with inline clarifications, get approval (merged Stage 3+4)
5. Automation - Fill Douzone with progress indicator

Usage:
    python main.py --user "<이름>" --memo ./test_memo/memo.txt --receipts ./test_receipts
"""

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple, Set
from pathlib import Path
from datetime import datetime

from .models import (
    ExpenseData, ExecutionPlan, RowAction, ActionType, ExecutionStatus,
    VerificationIssue, VerificationIssueType, PostVerificationResult,
)
from .automation import DouzoneAutomation
from .transaction_parser import TransactionParser, Transaction, TransactionList
from .pipeline import parse_memo, MemoData
from .ocr import extract_receipt, extract_receipt_from_text, find_preocr_file, ReceiptData, CLAUDE_CODE_AVAILABLE
from .image_converter import convert_image, convert_pdf_to_image, is_heic_file, is_pdf_file

logger = logging.getLogger(__name__)

STAGE1_CACHE_VERSION = 1
DEFAULT_STAGE1_CACHE_PATH = os.path.join("cache", "stage1_cache.json")
DEFAULT_STAGE1_REPORT_DIR = "reports"
DEFAULT_STAGE3_CACHE_PATH = os.path.join("cache", "execution_plan.json")
RECEIPT_OCR_MAX_WORKERS = 10

# Rough per-row automation cost (open popup + fill + attach + save). Used only to
# print a wall-clock estimate so a large batch can be run in the background.
SEC_PER_ROW_ESTIMATE = 8
# At/above this row count, warn that a foreground run may hit a timeout and that
# a resumed re-run is safe (already-filled rows auto-skip).
BACKGROUND_ROW_THRESHOLD = 40

# ============================================================================
# PURPOSE CODE MAPPING (from Douzone 용도 코드)
# ============================================================================
PURPOSE_CODES = {
    "중식대": "100",
    "석식대": "110",
    "회식대": "120",
    "간식/음료": "130",
    "건강검진": "140",
    "의약품": "150",
    "사내운영비(노사협의회 등)": "160",
    "기타복리후생비": "170",
    "사내행사비(워크샵,컬쳐데이)": "180",
    "국내출장_항공": "200",
    "국내출장_숙박": "210",
    "국내출장_교통": "220",
    "국내출장_식비": "230",
    "국내출장_기타": "240",
    "국외출장_항공": "250",
    "국외출장_숙박": "260",
    "국외출장_교통": "270",
    "국외출장_식비": "280",
    "국외출장_기타": "290",
    "외근_택시": "300",
    "외근_대중교통": "310",
    "유류비": "320",
    "자차유류비지원": "330",
    "통행료": "340",
    "주차비_정기": "350",
    "주차비_일회성": "360",
    "거래처식음료접대": "400",
    "거래처경조사비": "410",
    "거래처선물": "420",
    "우편/등기": "440",
    "이동전화료": "450",
    "기타통신비": "460",
    "비품수선비(노트북포함)": "470",
    "기타수선비": "480",
    "퀵서비스/택배": "500",
    "기타운반비": "510",
    "사외교육비(온라인포함)": "520",
    "도서": "530",
    "신문간행물구독": "540",
    "출력/인쇄": "550",
    "명함": "560",
    "기타도서인쇄비": "570",
    "회의비": "580",
    "사무실비품": "600",
    "사무용품": "610",
    "전산소모품": "620",
    "기타소모품비": "630",
    "소프트웨어구독료": "700",
    "플랫폼수수료": "710",
    "제증명발급수수료": "720",
    "기타지급수수료": "730",
    "인터뷰사례": "800",
    "면접사례": "810",
    "기타잡비": "820",
}

# Reverse mapping: code → name
PURPOSE_NAMES = {v: k for k, v in PURPOSE_CODES.items()}

# PG (Payment Gateway) merchants that require 실공급자 info
PG_MERCHANTS = [
    '배민', '우아한형제들', '쿠팡이츠', '요기요', '배달의민족',
    'KCP', 'NHN KCP', '이니시스', '토스', '카카오페이', '네이버페이',
    '사이렌오더', '스타벅스사이렌', 'SIREN',
]

# Heuristic patterns suggesting a PG/대행사 merchant not yet on any known list.
# Surfaced as UNKNOWN_PATTERN during post-verify so /douzonebot:troubleshoot
# can resolve it via the bounded agent-extension flow.
PG_SUSPECT_PATTERNS = [
    '결제대행', '결제 대행', '대행사',
    'PG', '사이버결제', '정보통신', '페이먼트', 'PAYMENT',
]

# 용도 codes that require a non-empty 참석자 field (Rule 5: 참석자 기재 필수).
ATTENDEE_REQUIRED_YONGDOS = {
    "중식대", "석식대", "회식대", "회의비",
    "거래처식음료접대",
    "국내출장_식비", "국외출장_식비",
    "간식/음료",
    "사내행사비(워크샵,컬쳐데이)",
}

# 용도 codes for entertainment-with-business-partners (Rule 8: 소속/직급/성명 필요).
ENTERTAINMENT_YONGDOS = {
    "거래처식음료접대",
    "거래처경조사비",
}

# Keywords indicating proper 접대비 attendee formatting (소속/직급).
ENTERTAINMENT_TITLE_KEYWORDS = [
    '회계법인', '법무법인', '세무법인', '법률사무소', '주식회사', '(주)',
    '대표', '이사', '상무', '전무', '부사장', '회장',
    '부장', '차장', '과장', '대리', '주임', '사원',
    '팀장', '실장', '본부장', '센터장', '소장',
    '교수', '박사', '회계사', '세무사', '변호사', '변리사',
]

# Merchants that are likely snacks/beverages
SNACK_MERCHANTS = [
    '스타벅스', '커피', '카페', 'cafe', 'coffee', '빽다방', '이디야',
    '투썸', '할리스', '파스쿠찌', 'gs25', 'cu', '세븐일레븐', '편의점',
    '베이커리', '빵', '던킨', '크리스피', '메가커피', '메가엠지씨',
    '컴포즈', '빈스빈스', '더벤티', '감성커피', '아티제',
]

# SaaS / Software subscription merchants → 소프트웨어구독료
SAAS_MERCHANTS = [
    'claude', 'anthropic', 'openai', 'chatgpt', 'gpt',
    'cursor', 'github', 'copilot', 'notion', 'slack',
    'figma', 'linear', 'vercel', 'aws', 'azure', 'gcp',
    'google cloud', 'dropbox', 'zoom', 'microsoft 365',
    'adobe', 'canva', 'grammarly', 'deepl',
    'subscription', '구독',
]

# SaaS merchant → receipt vendor keyword mapping (for FX invoice matching)
# Card merchant keywords → possible receipt vendor keywords
SAAS_VENDOR_MAP = {
    'cursor': ['anysphere', 'cursor'],
    'claude': ['anthropic', 'claude'],
    'anthropic': ['anthropic'],
    'openai': ['openai'],
    'chatgpt': ['openai'],
    'github': ['github'],
    'copilot': ['github', 'copilot'],
    'notion': ['notion'],
    'slack': ['slack', 'salesforce'],
    'figma': ['figma'],
    'linear': ['linear'],
    'vercel': ['vercel'],
    'aws': ['amazon web services', 'aws'],
    'azure': ['microsoft', 'azure'],
    'gcp': ['google cloud', 'google'],
    'google cloud': ['google cloud', 'google'],
    'dropbox': ['dropbox'],
    'zoom': ['zoom'],
    'adobe': ['adobe'],
    'canva': ['canva'],
    'deepl': ['deepl'],
}

# Plausible KRW/USD exchange rate range for FX amount matching
FX_RATE_MIN = 1100
FX_RATE_MAX = 1600

# Same-currency tolerance for SaaS receipt↔card matching. Overseas SaaS invoices
# (OpenAI/Anthropic 등) are billed in KRW but the card statement amount differs by
# FX/해외수수료, so an exact-amount match fails. e.g. 영수증 ₩144,545 vs 카드 148,229
# (ratio 1.025). ±10% covers the typical FX+fee spread without colliding with
# unrelated SaaS line items.
SAAS_AMOUNT_TOLERANCE = 0.10

# Large malls/department stores that require receipt attachment
RECEIPT_REQUIRED_MERCHANTS = [
    '스타필드', '코엑스', '현대백화점', '롯데백화점', '신세계백화점',
    '이마트', '홈플러스', '롯데마트', '코스트코',
]

# Merchants where 실공급자상호 + 실공급자 사업자등록번호 MUST be filled.
# These are PG/백화점 style merchants where the card statement shows the
# payment gateway or mall operator, not the actual vendor. Physical receipt
# is required to read the real supplier details.
SUPPLIER_REQUIRED_MERCHANTS = [
    '우아한형제', '배달의민족', '배민',                          # 배민
    '현대백화점',                                                # 현대백화점 전지점
    '신세계프라퍼티', '코엑스몰', '코엑스',                      # 신세계프라퍼티(코엑스몰)
    '나이스정보통신', '나이스정보',                              # NICE 정보통신
    '엔에이치엔한국사이버결제', '한국사이버결제', 'NHN KCP', 'KCP',  # NHN KCP
    '케이지이니시스', '이니시스', 'INICIS',                      # KG이니시스
]


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass
class MatchedRow:
    """A transaction row with matched memo and receipt data."""
    row_index: int  # 0-based index for automation
    transaction: Transaction

    # Matched data
    attendees: str = ""
    memo_source: Optional[str] = None  # Which memo line matched
    matched_memo: Optional[Dict[str, Any]] = None  # MatchResult as dict

    receipt_paths: List[str] = field(default_factory=list)  # Multiple receipts per row
    supplier_name: Optional[str] = None
    supplier_biz_no: Optional[str] = None
    matched_receipts: List[Dict[str, Any]] = field(default_factory=list)  # MatchResults as dicts

    # 용도/내용 handling (Stage 2 determines these)
    needs_yongdo: bool = False  # True if 용도 column is empty and needs filling
    target_yongdo: Optional[str] = None  # 용도 to select (e.g., "중식대", "간식/음료")
    needs_content: bool = False  # True if 내용 column is empty and needs filling
    target_content: Optional[str] = None  # 내용 to fill (defaults to merchant name)

    # Status
    confidence: str = "HIGH"  # HIGH or LOW
    needs_clarification: bool = False
    clarification_reason: Optional[str] = None
    user_confirmed: bool = False
    pending_receipt: bool = False
    pending_reason: Optional[str] = None

    # For clarification phase - candidate memos for this date
    candidate_memos: List[Any] = field(default_factory=list)

    def __str__(self):
        status = "✅" if self.confidence == "HIGH" else "⚠️"
        return f"{status} Row {self.row_index + 1}: {self.transaction.date_short} {self.transaction.merchant} {self.transaction.amount:,}원"

    def to_expense_data(self, default_yongdo: str = "중식대") -> ExpenseData:
        """Convert to ExpenseData for automation."""
        # Determine 용도: use target_yongdo if set, else transaction's yongdo, else default
        yongdo = self.target_yongdo or self.transaction.yongdo or default_yongdo

        # Determine 내용: use target_content if set, else transaction's content, else merchant
        content = self.target_content or self.transaction.content or self.transaction.merchant

        return ExpenseData(
            merchant=self.transaction.merchant,
            yongdo=yongdo,
            content=content,
            attendees=self.attendees,
            supplier_name=self.supplier_name,
            supplier_biz_no=self.supplier_biz_no,
            receipt_paths=self.receipt_paths,
            pending_reason=self.pending_reason,
            needs_yongdo=self.needs_yongdo,
            needs_content=self.needs_content,
        )


@dataclass
class ProcessingPlan:
    """Complete plan for processing all rows."""
    rows: List[MatchedRow] = field(default_factory=list)
    user_name: str = ""
    created_at: str = ""
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    @property
    def total_rows(self) -> int:
        return len(self.rows)
    
    @property
    def high_confidence_count(self) -> int:
        return sum(1 for r in self.rows if r.confidence == "HIGH")
    
    @property
    def needs_clarification_count(self) -> int:
        return sum(1 for r in self.rows if r.needs_clarification)
    
    @property
    def with_receipt_count(self) -> int:
        return sum(1 for r in self.rows if r.receipt_paths)
    
    @property
    def pending_receipt_count(self) -> int:
        return sum(1 for r in self.rows if r.pending_receipt)


# ============================================================================
# ORCHESTRATOR
# ============================================================================

class MVPOrchestrator:
    """
    Main orchestrator for the MVP flow.

    Flow:
    1. collect_data() - Parse Douzone grid, memos, receipts
    2. match_data() - Match transactions ↔ memos ↔ receipts
    3. review_and_confirm() - Show plan with inline clarifications, get approval
    4. execute() - Fill Douzone with progress indicator
    """
    
    def __init__(
        self,
        user_name: str,
        memo_path: Optional[str] = None,
        receipts_path: Optional[str] = None,
        cdp_url: str = "http://localhost:9222",
        auto_approve: bool = False,
        stage1_cache_in: Optional[str] = None,
        stage1_cache_out: Optional[str] = None,
        stage1_report_out: Optional[str] = None,
        stage1_only: bool = False,
        receipts_only: bool = False,
        receipt_provider: str = "auto",
        receipt_cache: Optional[str] = None,
        max_rows: int = 0,
        stage2_cache_in: Optional[str] = None,
        stage2_cache_out: Optional[str] = None,
        stage3_cache_in: Optional[str] = None,
        stage3_cache_out: Optional[str] = None,
        provider=None,
    ):
        """
        Initialize orchestrator.

        Args:
            user_name: Default attendee name (e.g., "<이름>")
            memo_path: Path to memo.txt file
            receipts_path: Path to receipts folder
            cdp_url: Chrome DevTools Protocol URL
            auto_approve: Skip interactive prompts (for testing/automation)
            stage1_cache_in: Load Stage 1 cache from this path (debug only)
            stage1_cache_out: Save Stage 1 cache to this path (debug only)
            stage1_report_out: Write Stage 1 report to this path (debug only)
            stage1_only: Stop after Stage 1 (debug only)
            receipts_only: Run receipt OCR only (debug only)
            receipt_provider: Receipt OCR provider override (debug only)
            receipt_cache: Path to receipt OCR cache file (load if exists, save after OCR)
            max_rows: Limit number of rows to process (0 = all)
            stage2_cache_in: Load Stage 2 cache from this path (debug only)
            stage2_cache_out: Save Stage 2 matching results to this path (debug only)
            stage3_cache_in: Load Stage 3 reviewed plan from this path (debug only)
            stage3_cache_out: Save Stage 3 execution plan to this path (debug only)
        """
        self.user_name = user_name
        self.memo_path = memo_path
        self.receipts_path = receipts_path
        self.cdp_url = cdp_url
        self.auto_approve = auto_approve
        self.stage1_cache_in = stage1_cache_in
        self.stage1_cache_out = stage1_cache_out
        self.stage1_report_out = stage1_report_out
        self.stage1_only = stage1_only
        self.receipts_only = receipts_only
        self.receipt_provider = receipt_provider
        self.receipt_cache = receipt_cache
        self.max_rows = max_rows
        self.stage2_cache_in = stage2_cache_in
        self.stage2_cache_out = stage2_cache_out
        self.stage3_cache_in = stage3_cache_in
        self.stage3_cache_out = stage3_cache_out
        self.provider = provider  # LLM provider for AI tasks
        self.stage2_only = False  # Will be set by run_mvp if needed
        self.stage3_only = False  # Will be set by run_mvp if needed
        self.review_only = False  # Will be set by run_mvp if needed
        # Optional targeted re-processing: when set, ONLY these 1-based grid row
        # numbers are processed and they bypass the already-filled (적합/완료) skip.
        # Used by --only-rows to correct specific rows post-hoc.
        self.only_rows: Optional[Set[int]] = None
        self.skip_post_verify = False  # Will be set by run_mvp if needed
        
        # Data stores
        self.transactions: Optional[TransactionList] = None
        self.memos: List[MemoData] = []
        self.receipts: Dict[str, ReceiptData] = {}  # path -> ReceiptData
        self.receipt_errors: List[Dict[str, str]] = []
        self.receipt_duplicates: List[Dict[str, str]] = []
        self.plan: Optional[ProcessingPlan] = None
        self.execution_plan: Optional[ExecutionPlan] = None
        self.execution_plan_path: Optional[str] = None
        
        # Automation engine
        self.automation: Optional[DouzoneAutomation] = None
    
    # =========================================================================
    # STAGE 1: Data Collection
    # =========================================================================
    
    async def collect_data(self, skip_transactions: bool = False) -> None:
        """
        Collect all data from Douzone, memos, and receipts.
        
        Args:
            skip_transactions: If True, skip Douzone parsing (for testing)
        """
        print("\n" + "="*60)
        print("📥 STAGE 1: Data Collection")
        print("="*60)

        if self.stage1_cache_in or self.stage1_cache_out or self.stage1_report_out or self.stage1_only:
            print("   NOTE: Stage 1 caching is debug-only and should not be used in production.")

        if self.stage1_cache_in:
            print(f"\n[cache] Loading Stage 1 cache: {self.stage1_cache_in}")
            self._load_stage1_cache(self.stage1_cache_in)
            print("\n" + "-"*40)
            print("📊 Data Collection Summary (cache):")
            if self.transactions:
                print(f"   Transactions: {self.transactions.count}")
            print(f"   Memos: {len(self.memos)}")
            print(f"   Receipts: {len(self.receipts)}")
            if self.stage1_report_out:
                self._write_stage1_report(self.stage1_report_out)
            return
        
        # Run all three data collection tasks in parallel.
        # They have zero data dependencies on each other:
        #   - Grid Parse: reads from CDP (Chrome)
        #   - Memo Parse: sends text to LLM
        #   - Receipt OCR: sends images to LLM/OCR
        tasks = []

        if not skip_transactions:
            print("\n[1/3] Parsing Douzone STEP 2 grid...")
            tasks.append(self._parse_douzone_transactions())
        else:
            print("\n[1/3] Skipping Douzone parsing (test mode)")

        print("\n[2/3] Parsing memos...")
        tasks.append(self._parse_memos())

        print("\n[3/3] Processing receipts...")
        tasks.append(self._parse_receipts())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Log individual task failures
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Data collection task {i} failed: {result}")

        # Fail-fast if required stage (grid parse) failed
        if not skip_transactions and not self.transactions:
            # Find the actual grid parse error for a clear message
            grid_errors = [r for r in results if isinstance(r, Exception)]
            error_msg = str(grid_errors[0]) if grid_errors else "unknown error"
            raise RuntimeError(
                f"Grid parsing failed: {error_msg}. "
                "Ensure the Douzone STEP 2 page is open in Chrome debug mode."
            )

        if self.stage1_cache_out:
            print(f"\n[cache] Saving Stage 1 cache: {self.stage1_cache_out}")
            self._save_stage1_cache(self.stage1_cache_out)

        if self.stage1_report_out:
            print(f"\n[report] Writing Stage 1 report: {self.stage1_report_out}")
            self._write_stage1_report(self.stage1_report_out)
        
        # Summary
        print("\n" + "-"*40)
        print("📊 Data Collection Summary:")
        if self.transactions:
            print(f"   Transactions: {self.transactions.count}")
        print(f"   Memos: {len(self.memos)}")
        print(f"   Receipts: {len(self.receipts)}")

    # =========================================================================
    # TIER 2: Progressive Pipeline
    # =========================================================================

    def _normalize_date(self, date_str: str) -> str:
        """Normalize date formats like '01-06' and '1/6' to comparable form."""
        if not date_str:
            return ""
        return date_str.replace('-', '/').lstrip('0').replace('/0', '/')

    def _match_memos_only(self) -> ProcessingPlan:
        """
        Rule-based memo-to-transaction matching (no LLM needed).

        Uses date matching to assign memos to transactions:
        - 1 memo, 1 transaction on same date → auto-assign (HIGH)
        - 1 memo, N transactions → mark needs_clarification
        - No memo for date → use default user_name

        Also determines 용도/내용 for all rows.

        Returns:
            ProcessingPlan with memo-matched rows (no receipt data yet).
        """
        if not self.transactions:
            raise ValueError("No transactions loaded.")

        print("\n" + "="*60)
        print("🔗 MEMO MATCHING (rule-based, no LLM)")
        print("="*60)

        # Group memos by normalized date
        from collections import defaultdict
        memos_by_date = defaultdict(list)
        for i, memo in enumerate(self.memos):
            if memo.date:
                date_key = self._normalize_date(memo.date)
                memos_by_date[date_key].append((i, memo))

        # Build MatchedRow for each transaction
        matched_rows = []
        used_memo_indices = set()

        for idx, tx in enumerate(self.transactions.transactions):
            row = MatchedRow(
                row_index=idx,
                transaction=tx,
                attendees=self.user_name,  # Default
            )

            tx_date_norm = self._normalize_date(tx.date_short)
            date_memos = memos_by_date.get(tx_date_norm, [])

            # Check if there's exactly one memo for this date AND
            # exactly one transaction on this date
            same_date_tx_count = sum(
                1 for t in self.transactions.transactions
                if self._normalize_date(t.date_short) == tx_date_norm
            )

            if len(date_memos) == 1 and same_date_tx_count == 1:
                # Unambiguous 1:1 match
                memo_idx, memo = date_memos[0]
                if memo_idx not in used_memo_indices:
                    row.attendees = memo.attendees_str
                    row.memo_source = memo.raw_memo
                    row.matched_memo = {
                        "item_id": f"memo_{memo_idx}",
                        "item_type": "memo",
                        "matched_row": idx,
                        "confidence": 1.0,
                        "reason": "Rule-based: 1 memo, 1 transaction on date"
                    }
                    row.confidence = "HIGH"
                    used_memo_indices.add(memo_idx)
                    print(f"   Row {idx+1}: ✅ {tx.date_short} → {row.attendees}")

            elif len(date_memos) >= 1 and same_date_tx_count > 1:
                # Multiple transactions on this date with memo(s) → clarification
                has_unused = any(mi not in used_memo_indices for mi, _ in date_memos)
                if has_unused:
                    row.needs_clarification = True
                    row.confidence = "LOW"
                    row.clarification_reason = (
                        f"Multiple transactions on {tx.date_short}, "
                        f"{len(date_memos)} memo(s) need assignment"
                    )
                    print(f"   Row {idx+1}: ⚠️ {tx.date_short} needs clarification")

            else:
                print(f"   Row {idx+1}: {tx.date_short} → default ({self.user_name})")

            # Determine 용도
            if not tx.yongdo_filled:
                row.needs_yongdo = True
                row.target_yongdo = self._determine_yongdo(tx)

            # Determine 내용
            if not tx.content_filled:
                row.needs_content = True
                content_yongdo = row.target_yongdo or tx.yongdo
                if self._is_meal_yongdo(content_yongdo):
                    row.target_content = "식대"
                else:
                    row.target_content = tx.merchant

            # Mark PG merchants as pending receipt
            if self._might_need_receipt(tx.merchant):
                row.pending_receipt = True
                row.pending_reason = "영수증 대기 (OCR 진행 중)"

            matched_rows.append(row)

        plan = ProcessingPlan(rows=matched_rows, user_name=self.user_name)
        self.plan = plan

        print(f"\n   Total: {plan.total_rows}, "
              f"High: {plan.high_confidence_count}, "
              f"Clarify: {plan.needs_clarification_count}, "
              f"Pending receipt: {plan.pending_receipt_count}")

        return plan

    def _apply_receipts_to_plan(self) -> ProcessingPlan:
        """
        Match receipts to existing plan rows and update.

        Called after receipt OCR completes. Uses exact date+time+amount
        matching (same logic as _find_exact_receipt_matches) plus
        LLM matching for remaining receipts.

        Returns:
            Updated ProcessingPlan with receipt data applied.
        """
        if not self.plan:
            raise ValueError("No plan to apply receipts to.")

        if not self.receipts:
            print("   No receipts to apply.")
            return self.plan

        print("\n" + "-"*40)
        print("📎 Applying receipts to plan...")

        matched_count = 0

        for path, receipt in self.receipts.items():
            if not receipt.transaction:
                continue

            receipt_date = receipt.transaction.date
            receipt_time = receipt.transaction.time
            receipt_amount = receipt.transaction.amount

            if not receipt_date or not receipt_amount:
                continue

            receipt_time_short = (receipt_time[:5] if receipt_time and len(receipt_time) > 5
                                  else receipt_time or "")

            # Find matching row by date + time + amount
            best_row = None
            best_score = 0

            for row in self.plan.rows:
                tx = row.transaction
                tx_date = tx.date_time.split(' ')[0] if ' ' in tx.date_time else tx.date_time
                tx_time = tx.date_time.split(' ')[1][:5] if ' ' in tx.date_time else ""

                date_match = (tx_date == receipt_date)
                amount_match = (tx.amount == receipt_amount)

                time_match = False
                if tx_time and receipt_time_short:
                    try:
                        tx_h, tx_m = map(int, tx_time.split(':'))
                        r_h, r_m = map(int, receipt_time_short.split(':'))
                        time_match = abs((tx_h * 60 + tx_m) - (r_h * 60 + r_m)) <= 1
                    except (ValueError, IndexError):
                        time_match = (tx_time == receipt_time_short)

                score = 0
                if date_match and time_match and amount_match:
                    score = 3
                elif date_match and amount_match:
                    score = 2
                elif date_match and time_match:
                    score = 1

                if score > best_score:
                    best_score = score
                    best_row = row

            if best_row and best_score >= 2:
                receipt_name = Path(receipt.source_path or path).name
                if path in best_row.receipt_paths:
                    continue  # Avoid duplicate receipt
                best_row.receipt_paths.append(path)
                best_row.matched_receipts.append({
                    "item_id": f"receipt_{receipt_name}",
                    "item_type": "receipt",
                    "matched_row": best_row.row_index,
                    "confidence": 1.0 if best_score == 3 else 0.8,
                    "reason": f"Progressive match: score {best_score}"
                })

                if receipt.vendor_info:
                    best_row.supplier_name = receipt.vendor_info.name
                    best_row.supplier_biz_no = receipt.vendor_info.biz_num

                best_row.pending_receipt = False
                best_row.pending_reason = None

                # Clear clarification if it was only due to missing receipt
                if (best_row.needs_clarification and
                        best_row.clarification_reason and
                        "No receipt" in best_row.clarification_reason):
                    best_row.needs_clarification = False
                    best_row.confidence = "HIGH"
                    best_row.clarification_reason = None

                matched_count += 1
                n_receipts = len(best_row.receipt_paths)
                suffix = f" ({n_receipts} files)" if n_receipts > 1 else ""
                print(f"   ✅ Receipt → Row {best_row.row_index+1}: "
                      f"{best_row.transaction.merchant} ({receipt_name}){suffix}")

        # SaaS matching pass: match unmatched receipts to unmatched SaaS rows
        # using merchant name mapping, FX-plausible amounts, and filename hints
        matched_receipt_paths = set()
        for row in self.plan.rows:
            matched_receipt_paths.update(row.receipt_paths)

        unmatched_saas_rows = [
            row for row in self.plan.rows
            if not row.receipt_paths and self._is_saas_merchant(row.transaction.merchant)
        ]
        unmatched_receipts = [
            (path, receipt) for path, receipt in self.receipts.items()
            if path not in matched_receipt_paths
        ]

        if unmatched_saas_rows and unmatched_receipts:
            saas_matched = self._match_saas_receipts(unmatched_saas_rows, unmatched_receipts)
            matched_count += saas_matched

        # Any remaining PG merchants without receipt → flag
        for row in self.plan.rows:
            if (row.pending_receipt and not row.receipt_paths
                    and self._is_pg_merchant(row.transaction.merchant)):
                row.pending_reason = (
                    "영수증 매칭 실패" if self._has_candidate_receipt(row.transaction.merchant)
                    else "영수증 미첨부"
                )
                if not row.needs_clarification:
                    row.needs_clarification = True
                    row.confidence = "LOW"
                    row.clarification_reason = "No receipt for PG transaction"

        print(f"   Matched {matched_count} receipts to plan rows")
        return self.plan

    async def collect_receipts_only(self) -> None:
        """Collect receipts only and write cache/report (debug only)."""
        print("\n" + "="*60)
        print("📥 STAGE 1: Receipt OCR Only")
        print("="*60)
        print("   NOTE: Receipt-only mode is debug-only and should not be used in production.")
        print(f"   Receipt provider: {self.receipt_provider}")

        await self._parse_receipts()

        if self.stage1_cache_out:
            print(f"\n[cache] Saving Stage 1 cache: {self.stage1_cache_out}")
            self._save_stage1_cache(self.stage1_cache_out)

        if self.stage1_report_out:
            print(f"\n[report] Writing Stage 1 report: {self.stage1_report_out}")
            self._write_stage1_report(self.stage1_report_out)

        print("\n" + "-"*40)
        print("📊 Receipt OCR Summary:")
        print(f"   Receipts: {len(self.receipts)}")
    
    async def _parse_douzone_transactions(self) -> None:
        """
        Parse transactions from Douzone STEP 2.

        Uses Grid API (fast, 100% accurate) with OCR fallback.
        """
        if not self.automation:
            self.automation = DouzoneAutomation(self.cdp_url)
            await self.automation.connect()

        # Try Grid API first (much faster and more accurate)
        print("   Attempting Grid API read (fast)...")
        grid_data = await self.automation.read_all_transactions_from_grid()

        if grid_data:
            # Success! Use Grid API data
            self.transactions = TransactionList.from_grid_api(grid_data)
            print(f"   ✅ Grid API: {self.transactions.count} transactions")
            print(f"   Total amount: {self.transactions.total_amount:,}원")
            print(f"   Needs attendee: {self.transactions.needs_attendee_count}")

            # Show rows needing attention
            needs_attention = [tx for tx in grid_data if tx.get('needs_processing')]
            if needs_attention:
                print(f"   ⚠️  {len(needs_attention)} rows need attention:")
                for tx in needs_attention[:5]:
                    print(f"      Row {tx['row_num']}: {tx['datetime']} | {tx['merchant']} | {tx['validation']}")
                if len(needs_attention) > 5:
                    print(f"      ... and {len(needs_attention) - 5} more")
            return

        # Fallback to OCR-based parsing
        print("   ⚠️  Grid API failed, falling back to OCR...")
        tp_backend = "claude" if os.environ.get("DOUZONE_LOCAL_MODE") == "1" else "gemini"
        parser = TransactionParser(self.automation.page, screenshot_dir="screenshots", backend=tp_backend)
        self.transactions = await parser.capture_all_transactions()

        print(f"   Found {self.transactions.count} transactions")
        print(f"   Total amount: {self.transactions.total_amount:,}원")
        print(f"   Needs attendee: {self.transactions.needs_attendee_count}")
    
    async def _parse_memos(self) -> None:
        """Parse memos from memo.txt file using batch processing."""
        if not self.memo_path or not os.path.exists(self.memo_path):
            if not self.stage2_cache_in:
                print(f"   ⚠️  No memo file found at: {self.memo_path}")
            return
        
        with open(self.memo_path, 'r', encoding='utf-8') as f:
            memos_text = f.read()
        
        if not memos_text.strip():
            print("   ⚠️  Memo file is empty")
            return

        from .pipeline import parse_memos_batch
        
        print(f"   Batch parsing memos...")
        self.memos = await parse_memos_batch(memos_text, provider=self.provider)
        
        for memo in self.memos:
            print(f"   → Date: {memo.date}, Attendees: {memo.attendees_str}")
        
        print(f"   Total memos parsed: {len(self.memos)}")
    
    async def _parse_receipts(self) -> None:
        """Parse receipts from receipts folder (with optional caching)."""
        if not self.receipts_path or not os.path.exists(self.receipts_path):
            if not self.stage2_cache_in:
                print(f"   ⚠️  No receipts folder found at: {self.receipts_path}")
            return

        # Find all receipt files, deduplicating heic/jpg pairs by stem
        receipt_extensions = {'.jpg', '.jpeg', '.png', '.heic', '.pdf'}
        files_by_stem: dict[str, str] = {}  # stem -> best path (for image dedup)
        pdf_files: list[str] = []  # PDF files skip stem dedup

        for f in sorted(os.listdir(self.receipts_path)):
            ext = os.path.splitext(f)[1].lower()
            if ext not in receipt_extensions:
                continue
            full_path = os.path.join(self.receipts_path, f)
            if not os.path.isfile(full_path):
                continue
            if ext == '.pdf':
                pdf_files.append(full_path)
                continue
            stem = os.path.splitext(f)[0].lower()
            if stem in files_by_stem:
                # Prefer jpg/jpeg/png over heic (already converted)
                existing_ext = os.path.splitext(files_by_stem[stem])[1].lower()
                if existing_ext == '.heic' and ext != '.heic':
                    files_by_stem[stem] = full_path
                # else keep existing (first non-heic wins)
            else:
                files_by_stem[stem] = full_path

        receipt_files = sorted(files_by_stem.values()) + sorted(pdf_files)
        print(f"   Found {len(receipt_files)} receipt files")

        if not receipt_files:
            return

        # Load receipt cache if specified
        receipt_cache_data = {}
        cache_hits = 0
        cache_misses = 0
        if self.receipt_cache:
            receipt_cache_data = self._load_receipt_cache()
            if receipt_cache_data:
                print(f"   Loaded receipt cache with {len(receipt_cache_data)} entries")

        max_workers = min(RECEIPT_OCR_MAX_WORKERS, len(receipt_files))
        print(f"   Receipt OCR parallelism: {max_workers}")

        semaphore = asyncio.Semaphore(max_workers)

        async def process_one(index: int, path: str) -> Dict[str, Any]:
            nonlocal cache_hits, cache_misses
            filename = os.path.basename(path)

            # Check cache first (by filename — use original name before HEIC conversion)
            if filename in receipt_cache_data:
                cache_hits += 1
                cached_entry = receipt_cache_data[filename]
                receipt_data = self._receipt_from_cache(cached_entry)
                return {"index": index, "path": path, "data": receipt_data, "from_cache": True}

            # Resolve HEIC → JPG upfront so all downstream lookups (preocr, OCR) use the JPG path.
            # Claude CLI cannot read HEIC directly — always operate on the converted JPG if available.
            resolved_path = path
            if is_heic_file(path):
                try:
                    converted_dir = os.path.join(self.receipts_path, "converted")
                    os.makedirs(converted_dir, exist_ok=True)
                    jpg_path = os.path.join(converted_dir, os.path.splitext(filename)[0] + ".jpg")
                    if not os.path.exists(jpg_path):
                        jpg_path = convert_image(path, converted_dir)
                    resolved_path = jpg_path
                    logger.info(f"HEIC resolved to JPG: {path} -> {resolved_path}")
                except Exception as e:
                    return {"index": index, "path": path, "error": f"HEIC conversion failed: {e}"}
            elif is_pdf_file(path):
                # If a pre-OCR companion (.ocr.md/.txt/.json) exists, no rendering is
                # needed — the companion text is used below and the original PDF is
                # attached. This must be checked BEFORE rasterizing so a PDF with a
                # companion works even without PyMuPDF installed.
                if find_preocr_file(path):
                    resolved_path = path  # keep PDF; companion text used, PDF attached
                else:
                    # No companion → vision OCR needs an image. Rasterize the first
                    # page to JPG. A clear error is surfaced (not silently dropped) if
                    # PyMuPDF is missing or rendering fails.
                    try:
                        converted_dir = os.path.join(self.receipts_path, "converted")
                        os.makedirs(converted_dir, exist_ok=True)
                        jpg_path = os.path.join(converted_dir, os.path.splitext(filename)[0] + ".jpg")
                        if not os.path.exists(jpg_path):
                            converted = convert_pdf_to_image(path, converted_dir)
                            if not converted:
                                return {"index": index, "path": path,
                                        "error": "PDF→이미지 변환 불가 (PyMuPDF 미설치 또는 렌더 실패) — "
                                                 "매칭에서 제외됨. .ocr.md 동반 파일을 만들거나 수동으로 JPG 변환하세요."}
                            jpg_path = converted
                        resolved_path = jpg_path
                        logger.info(f"PDF rasterized to JPG for OCR: {path} -> {resolved_path}")
                    except Exception as e:
                        return {"index": index, "path": path, "error": f"PDF conversion failed: {e}"}

            # Check for pre-prepared OCR companion file (next to JPG or HEIC)
            preocr_path = find_preocr_file(resolved_path)
            if not preocr_path and resolved_path != path:
                # Also check next to the original HEIC path
                preocr_path = find_preocr_file(path)
            if preocr_path:
                try:
                    ocr_text = Path(preocr_path).read_text(encoding='utf-8-sig')
                    if ocr_text.strip():
                        # Fast path: .ocr.json with ReceiptData-shaped content
                        if preocr_path.endswith('.ocr.json'):
                            try:
                                data = json.loads(ocr_text)
                                if "vendor_info" in data or "is_receipt" in data:
                                    receipt_data = ReceiptData.from_dict(data)
                                    receipt_data.provider = "preocr_json"
                                    receipt_data.raw_text = ocr_text
                                    return {"index": index, "path": path, "data": receipt_data,
                                            "from_cache": False, "from_preocr": True}
                            except (json.JSONDecodeError, Exception):
                                pass  # Not structured, fall through to LLM parsing

                        # Normal path: LLM parses free-form text into ReceiptData
                        receipt_data = await extract_receipt_from_text(
                            ocr_text, image_path=path, provider=self.receipt_provider
                        )
                        return {"index": index, "path": path, "data": receipt_data,
                                "from_cache": False, "from_preocr": True}
                except Exception as e:
                    logger.warning(f"Pre-OCR failed for {filename}, falling back to vision OCR: {e}")
                    # Fall through to normal vision OCR below

            cache_misses += 1
            async with semaphore:
                # HEIC was already resolved to JPG at the top of process_one.
                # Use resolved_path for the actual OCR call.
                receipt_path = resolved_path

                # Retry with backoff for rate limits, timeouts, and transient errors
                max_attempts = 4
                for attempt in range(max_attempts):
                    try:
                        receipt_data = await asyncio.wait_for(
                            asyncio.to_thread(self._extract_receipt_blocking, receipt_path),
                            timeout=90,
                        )
                        return {"index": index, "path": receipt_path, "data": receipt_data, "from_cache": False}
                    except asyncio.TimeoutError:
                        logger.warning(f"Receipt OCR timeout (attempt {attempt + 1}/{max_attempts}): {filename}")
                        if attempt < max_attempts - 1:
                            await asyncio.sleep(2 ** (attempt + 1))
                    except Exception as e:
                        err_str = str(e).lower()
                        is_retryable = ("429" in str(e) or "rate" in err_str or "quota" in err_str
                                        or "empty output" in err_str or "empty stdout" in err_str
                                        or "concurrent" in err_str)
                        if is_retryable and attempt < max_attempts - 1:
                            wait = 3 * (attempt + 1)
                            logger.warning(f"OCR retryable error (attempt {attempt + 1}/{max_attempts}), waiting {wait}s: {e}")
                            await asyncio.sleep(wait)
                        else:
                            return {"index": index, "path": path, "error": str(e)}

                return {"index": index, "path": path, "error": f"OCR failed after {max_attempts} attempts (timeout)"}

        tasks = [asyncio.create_task(process_one(i, path)) for i, path in enumerate(receipt_files, 1)]

        receipt_results = []
        new_cache_entries = {}

        for finished in asyncio.as_completed(tasks):
            result = await finished
            index = result.get("index")
            path = result.get("path")
            filename = os.path.basename(path) if path else "unknown"
            from_cache = result.get("from_cache", False)

            if "error" in result:
                error = result.get("error", "Unknown error")
                self.receipt_errors.append({"path": path or "", "error": error})
                print(f"   [{index}/{len(receipt_files)}] {filename} → ❌ Error: {error}")
                continue

            receipt_data = result.get("data")
            from_preocr = result.get("from_preocr", False)
            if receipt_data and receipt_data.is_receipt:
                vendor = receipt_data.vendor_info
                tx = receipt_data.transaction
                if from_cache:
                    source_tag = " [cached]"
                elif from_preocr:
                    source_tag = " [pre-ocr]"
                else:
                    source_tag = ""
                print(f"   [{index}/{len(receipt_files)}] {filename} → Vendor: {vendor.name if vendor else 'N/A'}{source_tag}")
                if tx:
                    print(f"       → Date: {tx.date}, Time: {tx.time}, Amount: {tx.amount}")
                receipt_results.append({"path": path, "data": receipt_data})

                # Add to new cache entries if not from cache
                if not from_cache:
                    new_cache_entries[filename] = self._receipt_to_cache(receipt_data)
            else:
                self.receipt_errors.append({"path": path or "", "error": "Not a valid receipt"})
                print(f"   [{index}/{len(receipt_files)}] {filename} → ⚠️  Not a valid receipt")

        # Print stats
        preocr_hits = sum(1 for r in receipt_results if r.get("data") and r["data"].provider
                         and r["data"].provider.startswith("preocr_"))
        vision_hits = len(receipt_results) - preocr_hits - cache_hits
        if preocr_hits:
            print(f"   Pre-OCR: {preocr_hits} receipt(s) parsed from companion text files")
        if preocr_hits or vision_hits:
            # Machine-readable summary (visible even in quiet mode)
            sys.stderr.write(f"OCR_PREOCR={preocr_hits} OCR_VISION={vision_hits} OCR_TOTAL={len(receipt_results)}\n")
        if self.receipt_cache:
            print(f"   Cache stats: {cache_hits} hits, {cache_misses} misses")

        # Save updated cache if we have new entries
        if self.receipt_cache and new_cache_entries:
            receipt_cache_data.update(new_cache_entries)
            self._save_receipt_cache(receipt_cache_data)
            print(f"   Saved {len(new_cache_entries)} new entries to receipt cache")

        kept_entries, duplicates = self._dedupe_receipts(receipt_results)
        # Store receipts with source_path set on each ReceiptData
        self.receipts = {}
        for entry in kept_entries:
            if entry.get("path"):
                receipt_data = entry["data"]
                receipt_data.source_path = entry["path"]  # Set source_path for matching
                self.receipts[entry["path"]] = receipt_data
        self.receipt_duplicates = duplicates
        if duplicates:
            print(f"   Deduped receipts: {len(duplicates)} duplicate(s) discarded")

    def _load_receipt_cache(self) -> Dict[str, Any]:
        """Load receipt cache from file."""
        if not self.receipt_cache or not os.path.exists(self.receipt_cache):
            return {}
        try:
            with open(self.receipt_cache, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load receipt cache: {e}")
            return {}

    def _save_receipt_cache(self, cache_data: Dict[str, Any]) -> None:
        """Save receipt cache to file."""
        if not self.receipt_cache:
            return
        try:
            os.makedirs(os.path.dirname(self.receipt_cache) or '.', exist_ok=True)
            with open(self.receipt_cache, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save receipt cache: {e}")

    def _receipt_to_cache(self, receipt: ReceiptData) -> Dict[str, Any]:
        """Convert ReceiptData to cache-friendly dict."""
        result = {
            "is_receipt": receipt.is_receipt,
            "confidence": receipt.confidence,
            "cached_at": datetime.now().isoformat(),
        }
        if receipt.vendor_info:
            result["vendor"] = {
                "name": receipt.vendor_info.name,
                "biz_num": receipt.vendor_info.biz_num,
                "address": receipt.vendor_info.address,
            }
        if receipt.platform_info:
            result["platform"] = {
                "name": receipt.platform_info.name,
                "biz_num": receipt.platform_info.biz_num,
                "address": receipt.platform_info.address,
            }
        if receipt.transaction:
            result["transaction"] = {
                "date": receipt.transaction.date,
                "time": receipt.transaction.time,
                "amount": receipt.transaction.amount,
            }
        return result

    def _receipt_from_cache(self, cached: Dict[str, Any]) -> ReceiptData:
        """Convert cache dict back to ReceiptData."""
        from .ocr import BusinessInfo, TransactionInfo

        vendor_info = BusinessInfo()
        if cached.get("vendor"):
            v = cached["vendor"]
            vendor_info = BusinessInfo(
                name=v.get("name"),
                biz_num=v.get("biz_num"),
                address=v.get("address"),
            )

        platform_info = BusinessInfo()
        if cached.get("platform"):
            p = cached["platform"]
            platform_info = BusinessInfo(
                name=p.get("name"),
                biz_num=p.get("biz_num"),
                address=p.get("address"),
            )

        transaction = TransactionInfo()
        if cached.get("transaction"):
            t = cached["transaction"]
            transaction = TransactionInfo(
                date=t.get("date"),
                time=t.get("time"),
                amount=t.get("amount"),
            )

        return ReceiptData(
            is_receipt=cached.get("is_receipt", True),
            confidence=cached.get("confidence", "medium"),
            vendor_info=vendor_info,
            platform_info=platform_info,
            transaction=transaction,
        )

    def _extract_receipt_blocking(self, receipt_path: str) -> ReceiptData:
        """Run receipt extraction in a blocking context for thread parallelism."""
        import asyncio as _asyncio
        return _asyncio.run(extract_receipt(receipt_path, provider=self.receipt_provider))

    def _normalize_time(self, time_str: Optional[str]) -> str:
        """Normalize time to HH:MM for signature matching."""
        if not time_str:
            return ""
        parts = time_str.split(":")
        if len(parts) >= 2:
            return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"
        return time_str

    def _receipt_signature(self, receipt: ReceiptData) -> Optional[Tuple[str, str, int, str]]:
        """Build a signature to detect duplicate receipt images."""
        tx = receipt.transaction
        if not tx or not tx.date or not tx.time or tx.amount is None:
            return None

        vendor_key = ""
        if receipt.vendor_info and receipt.vendor_info.biz_num:
            vendor_key = receipt.vendor_info.biz_num
        elif receipt.vendor_info and receipt.vendor_info.name:
            vendor_key = receipt.vendor_info.name
        if not vendor_key:
            return None

        return (
            tx.date,
            self._normalize_time(tx.time),
            tx.amount,
            vendor_key.strip().lower(),
        )

    def _receipt_score(self, receipt: ReceiptData) -> int:
        """Score receipts to pick the best version among duplicates."""
        conf_rank = {"low": 1, "medium": 2, "high": 3}.get(receipt.confidence, 0)
        tx = receipt.transaction
        completeness = 0
        if receipt.vendor_info and receipt.vendor_info.name:
            completeness += 1
        if receipt.vendor_info and receipt.vendor_info.biz_num:
            completeness += 1
        if tx and tx.date:
            completeness += 1
        if tx and tx.time:
            completeness += 1
        if tx and tx.amount is not None:
            completeness += 1
        return conf_rank * 10 + completeness

    def _dedupe_receipts(self, entries: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        """Deduplicate receipts by signature, keeping the best entry."""
        kept_by_sig: Dict[Tuple[str, str, int, str], Dict[str, Any]] = {}
        kept_entries: List[Dict[str, Any]] = []
        duplicates: List[Dict[str, str]] = []

        for entry in entries:
            receipt = entry.get("data")
            if not receipt:
                continue
            signature = self._receipt_signature(receipt)
            if not signature:
                kept_entries.append(entry)
                continue

            if signature not in kept_by_sig:
                kept_by_sig[signature] = entry
                continue

            current = kept_by_sig[signature]
            if self._receipt_score(receipt) > self._receipt_score(current["data"]):
                duplicates.append({
                    "path": current.get("path", ""),
                    "kept_path": entry.get("path", "")
                })
                kept_by_sig[signature] = entry
            else:
                duplicates.append({
                    "path": entry.get("path", ""),
                    "kept_path": current.get("path", "")
                })

        kept_entries.extend(kept_by_sig.values())
        return kept_entries, duplicates

    def _save_stage1_cache(self, cache_path: str) -> None:
        """Save Stage 1 outputs to a JSON cache file (debug only)."""
        payload = {
            "version": STAGE1_CACHE_VERSION,
            "created_at": datetime.now().isoformat(),
            "user_name": self.user_name,
            "memo_path": self.memo_path,
            "receipts_path": self.receipts_path,
            "transactions": self.transactions.to_dict() if self.transactions else None,
            "memos": [m.to_cache_dict() for m in self.memos],
            "receipts": [],
            "receipt_errors": self.receipt_errors,
            "receipt_duplicates": self.receipt_duplicates,
        }

        for path in sorted(self.receipts.keys()):
            receipt = self.receipts[path]
            receipt_data = receipt.to_dict()
            receipt_data["raw_text"] = receipt.raw_text
            payload["receipts"].append({
                "path": path,
                "data": receipt_data,
            })

        cache_dir = os.path.dirname(cache_path)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        print(f"   Stage 1 cache saved: {cache_path}")

    def _load_stage1_cache(self, cache_path: str) -> None:
        """Load Stage 1 outputs from a JSON cache file (debug only)."""
        if not os.path.exists(cache_path):
            raise FileNotFoundError(f"Stage 1 cache not found: {cache_path}")

        with open(cache_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        version = payload.get("version")
        if version != STAGE1_CACHE_VERSION:
            print(f"   Warning: cache version {version} != expected {STAGE1_CACHE_VERSION}")

        tx_data = payload.get("transactions")
        self.transactions = TransactionList.from_dict(tx_data) if tx_data else None
        self.memos = [MemoData.from_dict(m) for m in payload.get("memos", [])]

        self.receipts = {}
        self.receipt_errors = payload.get("receipt_errors", [])
        self.receipt_duplicates = payload.get("receipt_duplicates", [])
        for entry in payload.get("receipts", []):
            path = entry.get("path")
            data = entry.get("data", {}) or {}
            receipt = ReceiptData.from_dict(data)
            receipt.raw_text = data.get("raw_text")
            receipt.source_path = path  # Set source_path for matching
            if path:
                self.receipts[path] = receipt

    def _write_stage1_report(self, report_path: str) -> None:
        """Write a human-readable Stage 1 extraction report (debug only)."""
        report_dir = os.path.dirname(report_path)
        if report_dir:
            os.makedirs(report_dir, exist_ok=True)

        def safe(text: Optional[str]) -> str:
            if text is None:
                return ""
            return str(text).replace("\n", " ").replace("|", "\\|")

        lines = []
        lines.append("# Stage 1 Extraction Report")
        lines.append("")
        lines.append(f"Created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        cache_hint = self.stage1_cache_in or self.stage1_cache_out
        if cache_hint:
            lines.append(f"Stage 1 cache: {cache_hint}")
        if self.receipts_only:
            lines.append("Mode: receipts-only")
        lines.append("Note: Raw OCR text is stored in the Stage 1 cache file.")

        lines.append("")
        lines.append("## Douzone STEP 2")
        if self.transactions and self.transactions.transactions:
            lines.append(f"- Count: {self.transactions.count}")
            lines.append("")
            lines.append("| Row | DateTime | Merchant | Amount | Yongdo | Content | Status |")
            lines.append("| --- | -------- | -------- | ------ | ------ | ------- | ------ |")
            for tx in self.transactions.transactions:
                lines.append(
                    f"| {tx.row_num} | {safe(tx.date_time)} | {safe(tx.merchant)} | {tx.amount} | "
                    f"{safe(tx.yongdo)} | {safe(tx.content)} | {safe(tx.status)} |"
                )
        else:
            lines.append("- No transactions captured.")

        lines.append("")
        lines.append("## memo.txt")
        if self.memos:
            lines.append(f"- Count: {len(self.memos)}")
            lines.append("")
            lines.append("| # | Raw Memo | Date | Time | Attendees | Merchant Hint |")
            lines.append("| - | -------- | ---- | ---- | --------- | ------------- |")
            for idx, memo in enumerate(self.memos, 1):
                lines.append(
                    f"| {idx} | {safe(memo.raw_memo)} | {safe(memo.date)} | {safe(memo.time)} | "
                    f"{safe(memo.attendees_str)} | {safe(memo.merchant_hint_str)} |"
                )
        else:
            lines.append("- No memos parsed.")

        lines.append("")
        lines.append("## Receipt Images")
        if self.receipts:
            lines.append(f"- Count: {len(self.receipts)}")
            lines.append("")
            lines.append("| # | File | IsReceipt | Vendor | BizNo | Date | Time | Amount | Confidence | Provider | Model |")
            lines.append("| - | ---- | --------- | ------ | ----- | ---- | ---- | ------ | ---------- | -------- | ----- |")
            for idx, (path, receipt) in enumerate(sorted(self.receipts.items()), 1):
                vendor = receipt.vendor_info
                tx = receipt.transaction
                lines.append(
                    f"| {idx} | {safe(os.path.basename(path))} | {receipt.is_receipt} | "
                    f"{safe(vendor.name)} | {safe(vendor.biz_num)} | {safe(tx.date)} | "
                    f"{safe(tx.time)} | {tx.amount if tx.amount is not None else ''} | "
                    f"{safe(receipt.confidence)} | {safe(receipt.provider)} | {safe(receipt.model)} |"
                )
        else:
            lines.append("- No receipts parsed.")

        expected_prefix = "2026-01"
        date_warnings = []
        for path, receipt in self.receipts.items():
            date_val = receipt.transaction.date if receipt.transaction else None
            if date_val and not date_val.startswith(expected_prefix):
                date_warnings.append(f"{os.path.basename(path)} → {date_val}")

        if date_warnings:
            lines.append("")
            lines.append(f"## Receipt Date Warnings (expected {expected_prefix}-xx)")
            for item in sorted(date_warnings):
                lines.append(f"- {safe(item)}")

        if self.receipt_duplicates:
            lines.append("")
            lines.append("## Receipt Duplicates (discarded)")
            for dup in self.receipt_duplicates:
                lines.append(f"- {safe(dup.get('path'))} (kept: {safe(dup.get('kept_path'))})")

        if self.receipt_errors:
            lines.append("")
            lines.append("## Receipt Errors")
            for entry in self.receipt_errors:
                lines.append(f"- {safe(entry.get('path'))}: {safe(entry.get('error'))}")

        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        print(f"   Stage 1 report written: {report_path}")

    def _match_to_dict(self, match: Any) -> Dict[str, Any]:
        """Convert a MatchResult to a serializable dict."""
        return {
            "item_id": match.item_id,
            "item_type": match.item_type,
            "matched_row": match.matched_row,
            "confidence": float(match.confidence),
            "reason": match.reason,
        }

    def _expense_data_to_dict(self, expense: ExpenseData) -> Dict[str, Any]:
        """Convert ExpenseData to a serializable dict for ExecutionPlan."""
        return {
            "merchant": expense.merchant,
            "yongdo": expense.yongdo,
            "content": expense.content,
            "attendees": expense.attendees,
            "supplier_name": expense.supplier_name,
            "supplier_biz_no": expense.supplier_biz_no,
            "receipt_paths": expense.receipt_paths,
            "pending_reason": expense.pending_reason,
            "bigo_notes": expense.bigo_notes,
            "needs_yongdo": expense.needs_yongdo,
            "needs_content": expense.needs_content,
            "requires_supplier_info": expense.requires_supplier_info,
        }

    def _is_already_filled(self, tx: Transaction) -> bool:
        """Check if a row is already completed in Douzone."""
        status = (tx.status or "").strip()
        return status in ("적합", "완료")

    def _plan_from_cache_payload(self, payload: dict) -> ProcessingPlan:
        """Reconstruct ProcessingPlan from a stage cache payload."""
        matched_rows = []
        for row_dict in payload["rows"]:
            tx_dict = row_dict["transaction"]
            transaction = Transaction(
                row_num=tx_dict["row_num"],
                date_time=tx_dict["date_time"],
                merchant=tx_dict["merchant"],
                amount=tx_dict["amount"],
                yongdo=tx_dict.get("yongdo"),
                content=tx_dict.get("content"),
                status=tx_dict.get("status", ""),
            )

            matched_row = MatchedRow(
                row_index=row_dict["row_index"],
                transaction=transaction,
                attendees=row_dict["attendees"],
                memo_source=row_dict.get("memo_source"),
                matched_memo=row_dict.get("matched_memo"),
                receipt_paths=row_dict.get("receipt_paths") or ([row_dict["receipt_path"]] if row_dict.get("receipt_path") else []),
                supplier_name=row_dict.get("supplier_name"),
                supplier_biz_no=row_dict.get("supplier_biz_no"),
                matched_receipts=row_dict.get("matched_receipts") or ([row_dict["matched_receipt"]] if row_dict.get("matched_receipt") else []),
                needs_yongdo=row_dict.get("needs_yongdo", False),
                target_yongdo=row_dict.get("target_yongdo"),
                needs_content=row_dict.get("needs_content", False),
                target_content=row_dict.get("target_content"),
                confidence=row_dict.get("confidence", "HIGH"),
                needs_clarification=row_dict.get("needs_clarification", False),
                clarification_reason=row_dict.get("clarification_reason"),
                user_confirmed=row_dict.get("user_confirmed", False),
                pending_receipt=row_dict.get("pending_receipt", False),
                pending_reason=row_dict.get("pending_reason"),
            )
            matched_rows.append(matched_row)

        return ProcessingPlan(
            rows=matched_rows,
            user_name=payload["user_name"],
        )

    def _load_stage2_cache(self, cache_path: str) -> None:
        """Load Stage 2 matching results from a JSON cache file (debug only)."""
        if not os.path.exists(cache_path):
            raise FileNotFoundError(f"Stage 2 cache not found: {cache_path}")

        with open(cache_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        print(f"[cache] Loading Stage 2 cache: {cache_path}")
        print(f"   Created at: {payload['created_at']}")
        print(f"   User: {payload['user_name']}")
        print(f"   Total rows: {payload['total_rows']}")

        if "memos" in payload:
            self.memos = [MemoData.from_dict(m) for m in payload.get("memos", [])]
            print(f"   Memos: {len(self.memos)} (from cache)")

        self.plan = self._plan_from_cache_payload(payload)

        print(f"   Loaded {len(self.plan.rows)} rows")
        print(f"   High confidence: {self.plan.high_confidence_count}")
        print(f"   Needs clarification: {self.plan.needs_clarification_count}")

    def _ensure_receipt_files_exist(self) -> None:
        """Validate receipt file paths in the plan and re-convert HEIC if needed.

        When loading from cache, converted HEIC→JPG files may not exist
        (e.g., temp directory was cleaned). This method re-converts them.
        """
        if not self.plan:
            return

        reconverted = 0
        for row in self.plan.rows:
            fixed_paths = []
            for p in row.receipt_paths:
                if os.path.exists(p):
                    fixed_paths.append(p)
                    continue

                # Check if this is a converted path (e.g., .../converted/IMG_1234.jpg)
                # and the original HEIC still exists
                path_obj = Path(p)
                if path_obj.parent.name == "converted":
                    original_dir = path_obj.parent.parent
                    stem = path_obj.stem
                    for ext in [".heic", ".HEIC", ".heif", ".HEIF"]:
                        original = original_dir / (stem + ext)
                        if original.exists():
                            try:
                                converted_dir = str(path_obj.parent)
                                os.makedirs(converted_dir, exist_ok=True)
                                jpg_path = convert_image(str(original), converted_dir)
                                fixed_paths.append(jpg_path)
                                reconverted += 1
                            except Exception as e:
                                logger.warning(f"HEIC re-conversion failed for {original}: {e}")
                                print(f"   ⚠️  HEIC re-conversion failed: {original} ({e})")
                            break
                    else:
                        print(f"   ⚠️  Receipt file missing (no HEIC original found): {p}")
                else:
                    print(f"   ⚠️  Receipt file missing: {p}")

            row.receipt_paths = fixed_paths

        if reconverted:
            print(f"   Re-converted {reconverted} HEIC file(s) for attachment")

    def _load_stage3_cache(self, cache_path: str) -> None:
        """Load Stage 3 reviewed plan from a JSON cache file (debug only)."""
        if not os.path.exists(cache_path):
            raise FileNotFoundError(f"Stage 3 cache not found: {cache_path}")

        with open(cache_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        print(f"[cache] Loading Stage 3 cache: {cache_path}")
        print(f"   Created at: {payload['created_at']}")
        print(f"   User: {payload['user_name']}")
        print(f"   Total rows: {payload['total_rows']}")
        if not payload.get("reviewed", False):
            print("   ⚠️  Cache does not indicate review completed (reviewed=false)")

        if "memos" in payload:
            self.memos = [MemoData.from_dict(m) for m in payload.get("memos", [])]
            print(f"   Memos: {len(self.memos)} (from cache)")

        self.plan = self._plan_from_cache_payload(payload)

        print(f"   Loaded {len(self.plan.rows)} rows")
        print(f"   High confidence: {self.plan.high_confidence_count}")
        print(f"   Needs clarification: {self.plan.needs_clarification_count}")

    def _save_stage2_cache(self, cache_path: str) -> None:
        """Save Stage 2 matching results to a JSON cache file (debug only)."""
        if not self.plan:
            print("   ⚠️ No plan to save")
            return

        def matched_row_to_dict(row: MatchedRow) -> dict:
            return {
                "row_index": row.row_index,
                "transaction": {
                    "row_num": row.transaction.row_num,
                    "date_time": row.transaction.date_time,
                    "merchant": row.transaction.merchant,
                    "amount": row.transaction.amount,
                    "yongdo": row.transaction.yongdo,
                    "content": row.transaction.content,
                    "status": row.transaction.status,
                },
                "attendees": row.attendees,
                "memo_source": row.memo_source,
                "matched_memo": row.matched_memo,
                "receipt_paths": row.receipt_paths,
                "supplier_name": row.supplier_name,
                "supplier_biz_no": row.supplier_biz_no,
                "matched_receipts": row.matched_receipts,
                "needs_yongdo": row.needs_yongdo,
                "target_yongdo": row.target_yongdo,
                "needs_content": row.needs_content,
                "target_content": row.target_content,
                "confidence": row.confidence,
                "needs_clarification": row.needs_clarification,
                "clarification_reason": row.clarification_reason,
                "user_confirmed": row.user_confirmed,
                "pending_receipt": row.pending_receipt,
                "pending_reason": row.pending_reason,
            }

        payload = {
            "version": 1,
            "created_at": datetime.now().isoformat(),
            "user_name": self.plan.user_name,
            "total_rows": self.plan.total_rows,
            "high_confidence_count": self.plan.high_confidence_count,
            "needs_clarification_count": self.plan.needs_clarification_count,
            "with_receipt_count": self.plan.with_receipt_count,
            "pending_receipt_count": self.plan.pending_receipt_count,
            "memos": [m.to_cache_dict() for m in self.memos],
            "rows": [matched_row_to_dict(r) for r in self.plan.rows],
        }

        cache_dir = os.path.dirname(cache_path)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        print(f"   Stage 2 cache saved: {cache_path}")

    def _save_stage3_cache(self, cache_path: str) -> None:
        """Save Stage 3 ExecutionPlan to a JSON cache file (debug only)."""
        if not self.execution_plan:
            if self.plan:
                self.execution_plan = self._build_execution_plan()
            else:
                print("   ⚠️ No execution plan to save")
                return

        payload = self.execution_plan.to_dict()
        payload["version"] = 1
        payload["reviewed"] = True

        cache_dir = os.path.dirname(cache_path)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        print(f"   Stage 3 cache saved: {cache_path}")

    def _load_stage3_cache(self, cache_path: str) -> ExecutionPlan:
        """Load Stage 3 execution plan from a JSON cache file (debug only)."""
        if not os.path.exists(cache_path):
            raise FileNotFoundError(f"Stage 3 cache not found: {cache_path}")

        with open(cache_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        plan = ExecutionPlan.from_dict(payload)
        # Normalize counters in case of older cache formats
        plan.rows_to_process = len([
            r for r in plan.rows
            if r.action in (ActionType.AUTO_FILL.value, ActionType.USER_CONFIRMED.value)
        ])
        plan.rows_to_skip = len([r for r in plan.rows if r.action == ActionType.SKIP.value])
        plan.rows_already_filled = len([r for r in plan.rows if r.action == ActionType.ALREADY_FILLED.value])
        self._refresh_execution_counts(plan)
        self.execution_plan = plan
        return plan

    # =========================================================================
    # STAGE 2: Matching
    # =========================================================================
    
    async def match_data(self) -> ProcessingPlan:
        """
        Match transactions with memos and receipts.

        Returns:
            ProcessingPlan with all matched rows
        """
        print("\n" + "="*60)
        print("🔗 STAGE 2: Matching")
        print("="*60)

        # Check if we should load from cache instead
        if self.stage2_cache_in:
            self._load_stage2_cache(self.stage2_cache_in)
            self._ensure_receipt_files_exist()
            return self.plan

        if not self.transactions:
            raise ValueError("No transactions loaded. Run collect_data() first.")
        
        from .pipeline import match_items_to_transactions
        from .pipeline import CardTransaction as PipelineCardTransaction
        
        # Convert Transaction to pipeline CardTransaction
        pipeline_txs = []
        for tx in self.transactions.transactions:
            pipeline_txs.append(PipelineCardTransaction(
                row_index=tx.row_num - 1, # row_num is 1-based, pipeline expects 0-based
                datetime=tx.date_time,
                merchant=tx.merchant,
                amount=tx.amount
            ))
            
        print(f"   Matching {len(pipeline_txs)} transactions with {len(self.memos)} memos and {len(self.receipts)} receipts...")
        
        # Run AI matching
        matches = await match_items_to_transactions(
            transactions=pipeline_txs,
            memos=self.memos,
            receipts=list(self.receipts.values()),
            provider=self.provider,
        )
        
        # Map matches back to transactions
        matched_rows = []
        
        # Create a map for easy lookup: row_index -> MatchResult
        match_map = {m.matched_row: m for m in matches if m.matched_row is not None}
        
        for idx, tx in enumerate(self.transactions.transactions):
            row = MatchedRow(
                row_index=idx,
                transaction=tx,
                attendees=self.user_name,  # Default
            )
            
            # Check if this row has any matches
            # Note: A row might have multiple matches (e.g. memo AND receipt)
            # The current pipeline returns list of matches, one per ITEM. 
            # We need to find all items matched to THIS row.
            
            row_matches = [m for m in matches if m.matched_row == idx]
            
            memo_match = next((m for m in row_matches if m.item_type == 'memo'), None)
            receipt_match = next((m for m in row_matches if m.item_type == 'receipt'), None)
            
            # Apply Memo Match
            if memo_match:
                row.matched_memo = self._match_to_dict(memo_match)
                # Find the actual memo object
                memo_data = next((m for i, m in enumerate(self.memos) if f"memo_{i}" == memo_match.item_id), None)
                if memo_data:
                    row.attendees = memo_data.attendees_str
                    row.memo_source = memo_data.raw_memo
                    print(f"   Row {idx+1}: Memo matched ({memo_match.confidence:.2f}) → {row.attendees}")
            else:
                # No memo - use default user name
                print(f"   Row {idx+1}: No memo, using default: {self.user_name}")
            
            # Apply Receipt Match (AI matching)
            if receipt_match:
                row.matched_receipts.append(self._match_to_dict(receipt_match))
                # Find the actual receipt object using source_path
                found_receipt = None
                found_path = None
                for path, r in self.receipts.items():
                    # Use source_path which contains the actual file path
                    r_name = Path(r.source_path or path).name if r.source_path else Path(path).name
                    if f"receipt_{r_name}" == receipt_match.item_id:
                        found_receipt = r
                        found_path = path
                        break

                if found_receipt:
                    if found_path not in row.receipt_paths: row.receipt_paths.append(found_path)
                    if found_receipt.vendor_info:
                        row.supplier_name = found_receipt.vendor_info.name
                        row.supplier_biz_no = found_receipt.vendor_info.biz_num
                    print(f"   Row {idx+1}: Receipt matched ({receipt_match.confidence:.2f}) → {row.supplier_name or 'N/A'}")
            
            # Handling PG / SaaS Transactions — Missing Receipts
            if not row.receipt_paths and self._might_need_receipt(tx.merchant):
                row.pending_receipt = True
                is_saas = self._is_saas_merchant(tx.merchant)
                # Distinguish '미첨부' (no receipt for this vendor) from '매칭 실패'
                # (a plausible receipt exists but auto-match failed — usually FX
                # amount drift). Different cause → different fix, so label them apart.
                has_candidate = self._has_candidate_receipt(tx.merchant)
                if is_saas:
                    row.pending_reason = "SaaS 구독 영수증 매칭 실패" if has_candidate else "SaaS 구독 영수증 미첨부"
                else:
                    row.pending_reason = "영수증 매칭 실패" if has_candidate else "영수증 미첨부"
                if not row.needs_clarification:
                     row.needs_clarification = True
                     row.confidence = "LOW"
                     reason_type = "SaaS subscription" if is_saas else "PG transaction"
                     row.clarification_reason = f"No receipt for {reason_type}"
                print(f"   Row {idx+1}: ⚠️ No receipt for {tx.merchant}")
                
            # Set confidence based on AI matches
            # If AI was low confidence on either match, mark row as LOW
            if (memo_match and memo_match.confidence < 0.8) or (receipt_match and receipt_match.confidence < 0.8):
                row.confidence = "LOW"
                row.needs_clarification = True
                row.clarification_reason = f"AI low confidence match (Memo: {memo_match.confidence if memo_match else 'N/A'}, Receipt: {receipt_match.confidence if receipt_match else 'N/A'})"

            # Get receipt time for more accurate meal classification
            receipt_time = None
            if row.receipt_paths:
                for rpath in row.receipt_paths:
                    r = self.receipts.get(rpath)
                    if r and r.transaction and r.transaction.time:
                        receipt_time = r.transaction.time
                        break

            # Determine 용도 (purpose) if not already filled
            if not tx.yongdo_filled:
                row.needs_yongdo = True
                row.target_yongdo = self._determine_yongdo(tx, receipt_time=receipt_time)
                if receipt_time and receipt_time != tx.time:
                    print(f"   Row {idx+1}: 용도 empty → will fill '{row.target_yongdo}' (using receipt time {receipt_time})")
                else:
                    print(f"   Row {idx+1}: 용도 empty → will fill '{row.target_yongdo}'")

            # Determine 내용 (content) if not already filled
            if not tx.content_filled:
                row.needs_content = True
                content_yongdo = row.target_yongdo or tx.yongdo
                if self._is_meal_yongdo(content_yongdo):
                    row.target_content = "식대"
                else:
                    row.target_content = tx.merchant  # Default to merchant name
                print(f"   Row {idx+1}: 내용 empty → will fill '{row.target_content}'")

            matched_rows.append(row)

        # CRITICAL FIX: Detect unused memos and mark corresponding transactions for clarification
        # This handles the case where a memo exists for a date but wasn't matched to any specific transaction
        print("\n" + "-"*40)
        print("🔍 Checking for unused memos...")

        # Find which memos were used
        used_memo_indices = set()
        for row in matched_rows:
            if row.memo_source:
                # Find which memo index was used
                for i, memo in enumerate(self.memos):
                    if memo.raw_memo == row.memo_source:
                        used_memo_indices.add(i)
                        break

        # Check each memo
        for i, memo in enumerate(self.memos):
            if i in used_memo_indices:
                continue  # This memo was already matched

            # This memo was NOT used - find transactions on this date
            memo_date = memo.date  # Format: "01-06" or similar
            if not memo_date:
                continue

            # Find all transactions on this date
            date_transactions = []
            for row in matched_rows:
                tx_date = row.transaction.date_short  # Format: "1/6"
                # Normalize formats: "01-06" vs "1/6"
                if memo_date.replace('-', '/').lstrip('0').replace('/0', '/') == tx_date.lstrip('0').replace('/0', '/'):
                    date_transactions.append(row)

            if not date_transactions:
                print(f"   ⚠️ Memo '{memo.raw_memo[:30]}...' has no matching transactions on {memo_date}")
                continue

            if len(date_transactions) == 1:
                # Only 1 transaction on this date - auto-apply the memo
                row = date_transactions[0]
                row.attendees = memo.attendees_str
                row.memo_source = memo.raw_memo
                row.matched_memo = {
                    "item_id": f"memo_{i}",
                    "item_type": "memo",
                    "matched_row": row.row_index,
                    "confidence": 1.0,
                    "reason": "Auto-applied (only transaction on date)"
                }
                row.needs_clarification = False  # Clear any previous clarification flag
                row.confidence = "HIGH"
                print(f"   ✅ Auto-applied memo to Row {row.row_index+1} (only transaction on {memo_date})")
                used_memo_indices.add(i)
            else:
                # Multiple transactions on this date — only flag UNMATCHED ones
                unmatched_on_date = [r for r in date_transactions if not r.matched_memo]
                print(f"   ⚠️ Memo '{memo.raw_memo[:30]}...' → {len(date_transactions)} transactions on {memo_date} ({len(unmatched_on_date)} unmatched)")
                print(f"      Unmatched rows: {[r.row_index+1 for r in unmatched_on_date]}")

                if len(unmatched_on_date) == 1:
                    # Only 1 unmatched transaction left — auto-assign the unused memo
                    row = unmatched_on_date[0]
                    row.attendees = memo.attendees_str
                    row.memo_source = memo.raw_memo
                    row.matched_memo = {
                        "item_id": f"memo_{i}",
                        "item_type": "memo",
                        "matched_row": row.row_index,
                        "confidence": 0.9,
                        "reason": "Auto-applied (only unmatched transaction on date after LLM matching)"
                    }
                    row.needs_clarification = False
                    row.confidence = "HIGH"
                    print(f"   ✅ Auto-applied memo to Row {row.row_index+1} (only unmatched on {memo_date})")
                    used_memo_indices.add(i)
                else:
                    # Multiple unmatched — mark only those for clarification
                    print(f"      → Marking {len(unmatched_on_date)} unmatched rows for clarification")
                    for row in unmatched_on_date:
                        row.needs_clarification = True
                        row.confidence = "LOW"
                        if not row.clarification_reason:
                            row.clarification_reason = f"Multiple transactions on {memo_date}, need to assign memo: '{memo.raw_memo[:30]}...'"

        # Create plan
        self.plan = ProcessingPlan(
            rows=matched_rows,
            user_name=self.user_name,
        )
        
        # Summary
        print("\n" + "-"*40)
        print("📊 Matching Summary:")
        print(f"   Total rows: {self.plan.total_rows}")
        print(f"   High confidence: {self.plan.high_confidence_count}")
        print(f"   Needs clarification: {self.plan.needs_clarification_count}")
        print(f"   With receipts: {self.plan.with_receipt_count}")
        print(f"   Pending receipts: {self.plan.pending_receipt_count}")

        # Save Stage 2 cache if requested
        if self.stage2_cache_out:
            print(f"\n[cache] Saving Stage 2 cache: {self.stage2_cache_out}")
            self._save_stage2_cache(self.stage2_cache_out)

        return self.plan

    def _might_need_receipt(self, merchant: str) -> bool:
        """Check if a merchant might need receipt attachment."""
        return (
            self._is_pg_merchant(merchant)
            or self._is_saas_merchant(merchant)
            or self._requires_receipt_attachment(merchant)
            or self._requires_supplier_info(merchant)
        )

    def _determine_yongdo(self, tx: Transaction, receipt_time: Optional[str] = None) -> str:
        """
        Determine appropriate 용도 (purpose) for a transaction.

        Logic:
        1. Check SaaS/software keywords → 소프트웨어구독료
        2. Check snack/coffee keywords → 간식/음료
        3. Check time of day for meals (receipt time preferred over transaction time)
        4. Default to 중식대 for unclassified

        Args:
            tx: Transaction data
            receipt_time: Optional time from matched receipt (HH:MM:SS or HH:MM), preferred over tx.time

        Returns:
            용도 name (e.g., "중식대", "간식/음료", "소프트웨어구독료")
        """
        merchant_lower = tx.merchant.lower()

        # SaaS/software subscriptions → 소프트웨어구독료
        if any(kw.lower() in merchant_lower for kw in SAAS_MERCHANTS):
            return "소프트웨어구독료"

        # Coffee/snack merchants → 간식/음료
        if any(kw.lower() in merchant_lower for kw in SNACK_MERCHANTS):
            return "간식/음료"

        # Try to determine by time of day (receipt time > transaction time)
        try:
            time_str = receipt_time or tx.time  # Prefer receipt time
            if time_str:
                hour = int(time_str.split(':')[0])

                # Dinner time: 17:00 - 21:00
                if 17 <= hour <= 21:
                    return "석식대"

                # Lunch time: 11:00 - 14:00
                if 11 <= hour <= 14:
                    return "중식대"

                # Late night (after 21:00) - likely 회식
                if hour >= 21 or hour < 5:
                    return "회식대"

                # Morning snack/coffee: 7:00 - 10:00
                if 7 <= hour <= 10:
                    return "간식/음료"

        except (ValueError, IndexError):
            pass  # Time parsing failed, use default

        # Default to 중식대 (most common)
        return "중식대"

    def _is_meal_yongdo(self, yongdo: Optional[str]) -> bool:
        """Check if 용도 indicates a meal category (use '식대' as 내용)."""
        if not yongdo:
            return False
        return yongdo in {"중식대", "석식대", "회식대", "조식대", "거래처식음료접대",
                         "국내출장_식비", "국외출장_식비"}

    def _get_purpose_code(self, purpose_name: str) -> str:
        """
        Get the Douzone purpose code for a purpose name.

        Args:
            purpose_name: 용도명 (e.g., "중식대", "간식/음료")

        Returns:
            typeCd (e.g., "100", "130")
        """
        return PURPOSE_CODES.get(purpose_name, "100")  # Default to 중식대

    def _is_pg_merchant(self, merchant: str) -> bool:
        """Check if merchant is a PG (requires 실공급자 info)."""
        merchant_lower = merchant.lower()
        return any(kw.lower() in merchant_lower for kw in PG_MERCHANTS)

    def _is_saas_merchant(self, merchant: str) -> bool:
        """Check if merchant is a SaaS/software subscription."""
        merchant_lower = merchant.lower()
        return any(kw.lower() in merchant_lower for kw in SAAS_MERCHANTS)

    def _match_saas_receipts(
        self,
        saas_rows: list,
        unmatched_receipts: list,
    ) -> int:
        """
        Match SaaS receipts using merchant name mapping, FX amount, and filename hints.

        Scoring:
          - Merchant name match (via SAAS_VENDOR_MAP or filename): +2
          - FX-plausible amount (KRW/foreign ratio in range): +1
          - Date within 35 days (billing cycle): +1
          - Require score >= 2 to match

        Returns:
            Number of newly matched receipts.
        """
        from datetime import datetime, timedelta

        matched = 0

        for path, receipt in unmatched_receipts:
            receipt_vendor = (receipt.vendor_info.name or "").lower() if receipt.vendor_info else ""
            receipt_filename = os.path.basename(path).lower()
            receipt_amount = receipt.transaction.amount if receipt.transaction else None
            receipt_date_str = receipt.transaction.date if receipt.transaction else None

            best_row = None
            best_score = 0

            for row in saas_rows:
                if path in row.receipt_paths:
                    continue

                tx = row.transaction
                merchant_lower = tx.merchant.lower()
                score = 0

                # 1. Merchant name match via SAAS_VENDOR_MAP
                name_matched = False
                for saas_kw, vendor_keywords in SAAS_VENDOR_MAP.items():
                    if saas_kw in merchant_lower:
                        if any(vk in receipt_vendor for vk in vendor_keywords):
                            name_matched = True
                            break
                        # Also check filename for vendor keywords
                        if any(vk in receipt_filename for vk in vendor_keywords):
                            name_matched = True
                            break

                # 2. Filename hint: check if filename contains any SaaS merchant keyword
                if not name_matched:
                    for saas_kw in SAAS_VENDOR_MAP:
                        if saas_kw in merchant_lower and saas_kw in receipt_filename:
                            name_matched = True
                            break

                if name_matched:
                    score += 2

                # 3. Amount corroboration: FX-plausible OR same-currency near-match.
                #    Overseas SaaS bills in KRW but the card amount drifts by
                #    FX/수수료, so allow a ±SAAS_AMOUNT_TOLERANCE same-currency band
                #    in addition to the USD-style FX ratio band. Either is +1.
                if receipt_amount and receipt_amount > 0 and tx.amount > 0:
                    ratio = tx.amount / receipt_amount
                    fx_plausible = FX_RATE_MIN <= ratio <= FX_RATE_MAX
                    near_amount = (1 - SAAS_AMOUNT_TOLERANCE) <= ratio <= (1 + SAAS_AMOUNT_TOLERANCE)
                    if fx_plausible or near_amount:
                        score += 1

                # 4. Date within 35 days
                if receipt_date_str:
                    try:
                        tx_date_str = tx.date_time.split(' ')[0] if ' ' in tx.date_time else tx.date_time
                        receipt_dt = datetime.strptime(receipt_date_str, "%Y-%m-%d")
                        tx_dt = datetime.strptime(tx_date_str, "%Y-%m-%d")
                        if abs((tx_dt - receipt_dt).days) <= 35:
                            score += 1
                    except (ValueError, TypeError):
                        pass

                if score > best_score:
                    best_score = score
                    best_row = row

            if best_row and best_score >= 2:
                receipt_name = Path(receipt.source_path or path).name
                best_row.receipt_paths.append(path)
                best_row.matched_receipts.append({
                    "item_id": f"receipt_{receipt_name}",
                    "item_type": "receipt",
                    "matched_row": best_row.row_index,
                    "confidence": 0.7 if best_score >= 3 else 0.6,
                    "reason": f"SaaS match: score {best_score}"
                })

                best_row.pending_receipt = False
                best_row.pending_reason = None

                if (best_row.needs_clarification and
                        best_row.clarification_reason and
                        "No receipt" in best_row.clarification_reason):
                    best_row.needs_clarification = False
                    best_row.confidence = "HIGH"
                    best_row.clarification_reason = None

                matched += 1
                print(f"   ✅ SaaS Receipt → Row {best_row.row_index+1}: "
                      f"{best_row.transaction.merchant} ({receipt_name}, score={best_score})")

        return matched

    def _requires_receipt_attachment(self, merchant: str) -> bool:
        """Check if merchant requires physical receipt attachment (백화점/코엑스 등)."""
        merchant_lower = merchant.lower()
        return any(kw.lower() in merchant_lower for kw in RECEIPT_REQUIRED_MERCHANTS)

    def _requires_supplier_info(self, merchant: str) -> bool:
        """Check if merchant requires 실공급자상호/실공급자번호 to be filled."""
        merchant_lower = (merchant or '').lower()
        return any(kw.lower() in merchant_lower for kw in SUPPLIER_REQUIRED_MERCHANTS)

    @staticmethod
    def _normalize_vendor_name(name: str) -> str:
        """Normalize a 상호 for cross-vendor lookup: lowercase, collapse spaces."""
        return ' '.join((name or '').lower().split())

    def _backfill_supplier_biz_no(self) -> None:
        """Backfill missing 실공급자 사업자번호 from sibling receipts of the same vendor.

        A single receipt for a vendor may lack the 사업자번호 (OCR miss / not printed),
        while other receipts from the same vendor in this batch carry it. When a row
        has supplier_name but no supplier_biz_no, look up the number by normalized
        상호 across all collected receipts and fill it in.

        Without this, Post-verify (_check_pg_missing_supplier) flags the row and the
        user re-enters a number that was already available elsewhere in the same run.
        """
        if not self.plan or not getattr(self, 'receipts', None):
            return

        # Build 상호 → 사업자번호 map from receipts that have BOTH.
        biz_by_vendor: Dict[str, str] = {}
        for receipt in self.receipts.values():
            vi = getattr(receipt, 'vendor_info', None)
            if not vi:
                continue
            name = self._normalize_vendor_name(getattr(vi, 'name', '') or '')
            biz = (getattr(vi, 'biz_num', '') or '').strip()
            if name and biz:
                biz_by_vendor.setdefault(name, biz)

        # Also seed from rows that already resolved both fields.
        for row in self.plan.rows:
            name = self._normalize_vendor_name(row.supplier_name or '')
            biz = (row.supplier_biz_no or '').strip()
            if name and biz:
                biz_by_vendor.setdefault(name, biz)

        if not biz_by_vendor:
            return

        filled = 0
        for row in self.plan.rows:
            if row.supplier_name and not (row.supplier_biz_no or '').strip():
                key = self._normalize_vendor_name(row.supplier_name)
                biz = biz_by_vendor.get(key)
                if biz:
                    row.supplier_biz_no = biz
                    filled += 1
                    print(f"   🔗 사업자번호 백필: {row.supplier_name} → {biz} "
                          f"(Row {row.row_index + 1})")
        if filled:
            print(f"   사업자번호 교차 보완 {filled}건")

    def _has_candidate_receipt(self, merchant: str) -> bool:
        """True if an unmatched receipt plausibly belongs to `merchant`.

        Used to distinguish '미첨부' (no receipt at all) from '매칭 실패'
        (a likely-matching receipt exists but auto-match did not bind it).
        Matches a SaaS vendor keyword against receipt vendor name or filename.
        """
        if not getattr(self, 'receipts', None):
            return False
        merchant_lower = (merchant or '').lower()
        vendor_keywords: List[str] = []
        for saas_kw, kws in SAAS_VENDOR_MAP.items():
            if saas_kw in merchant_lower:
                vendor_keywords.extend(kws)
        if not vendor_keywords:
            return False

        matched_paths = set()
        for row in self.plan.rows:
            matched_paths.update(row.receipt_paths)

        for path, receipt in self.receipts.items():
            if path in matched_paths:
                continue
            vendor = ''
            if getattr(receipt, 'vendor_info', None):
                vendor = (receipt.vendor_info.name or '').lower()
            filename = os.path.basename(path).lower()
            if any(vk in vendor or vk in filename for vk in vendor_keywords):
                return True
        return False

    def _build_execution_plan(self) -> ExecutionPlan:
        """Build ExecutionPlan from the reviewed ProcessingPlan."""
        if not self.plan:
            raise ValueError("No ProcessingPlan available to build ExecutionPlan.")

        # Cross-fill 사업자번호 from sibling receipts of the same vendor before
        # rows are frozen into expense data (idempotent across review reruns).
        self._backfill_supplier_biz_no()

        row_actions: List[RowAction] = []

        for row in self.plan.rows:
            tx = row.transaction
            transaction_dict = tx.to_dict()

            # Determine action.
            # --only-rows: process ONLY the listed 1-based rows; those bypass the
            # already-filled skip (targeted post-hoc correction), everything else
            # is skipped. Without --only-rows, the normal already-filled gate applies.
            if self.only_rows is not None and tx.row_num not in self.only_rows:
                action = ActionType.SKIP.value
                execution_status = ExecutionStatus.SKIPPED.value
                skip_reason = "not_in_only_rows"
            elif self.only_rows is None and self._is_already_filled(tx):
                action = ActionType.ALREADY_FILLED.value
                execution_status = ExecutionStatus.SKIPPED.value
                skip_reason = "already_filled"
            else:
                action = ActionType.USER_CONFIRMED.value if row.user_confirmed else ActionType.AUTO_FILL.value
                execution_status = ExecutionStatus.PENDING.value
                skip_reason = None

            is_processing = action in (ActionType.AUTO_FILL.value, ActionType.USER_CONFIRMED.value)

            expense_data = row.to_expense_data()

            # Attendee is required by most 식대/회의 popups; an empty value makes the
            # popup save fail ("Failed to save popup"). If a row will be processed but
            # has no attendee (e.g. a memo matched only a receipt with no names),
            # fall back to the default user so the save succeeds. Harmless for
            # card/SaaS popups that lack a 참석자 field (fill_popup skips when absent).
            if is_processing and not (expense_data.attendees or "").strip():
                expense_data.attendees = self.user_name
                logger.info(
                    f"Row {tx.row_num}: empty attendee → defaulted to user '{self.user_name}'"
                )

            # Compute merchant-driven requirement flag (independent of OCR output).
            requires_supplier = (
                self._requires_supplier_info(tx.merchant)
                or self._is_pg_merchant(tx.merchant)
            )
            expense_data.requires_supplier_info = requires_supplier
            fill_data = self._expense_data_to_dict(expense_data)

            bigo_notes: List[str] = []
            if row.pending_reason:
                bigo_notes.append(f"[영수증 대기] {row.pending_reason}")

            # Early warning: PG/실공급자-required merchant is missing supplier data.
            # Two cases: (a) nothing extracted, (b) 상호 present but 사업자등록번호
            # missing (common when a hand-written .ocr.md omits the biz number — the
            # critical field for PG tax filing). Surface in 비고 for pre/at-automation.
            if requires_supplier and is_processing:
                if not expense_data.has_supplier_data:
                    warn_note = "⚠️ 실공급자 정보 수동 확인 필요 (OCR 미추출)"
                elif not (expense_data.supplier_biz_no or "").strip():
                    warn_note = "⚠️ 실공급자 사업자등록번호 누락 — 수동 확인 필요"
                else:
                    warn_note = None
                if warn_note and warn_note not in bigo_notes:
                    bigo_notes.append(warn_note)

            row_action = RowAction(
                row_number=tx.row_num,
                transaction=transaction_dict,
                matched_receipts=row.matched_receipts,
                matched_memo=row.matched_memo,
                action=action,
                fill_data=fill_data,
                bigo_notes=bigo_notes,
                skip_reason=skip_reason,
                execution_status=execution_status,
            )

            row_actions.append(row_action)

        rows_to_process = len([
            r for r in row_actions
            if r.action in (ActionType.AUTO_FILL.value, ActionType.USER_CONFIRMED.value)
        ])
        rows_to_skip = len([r for r in row_actions if r.action == ActionType.SKIP.value])
        rows_already_filled = len([r for r in row_actions if r.action == ActionType.ALREADY_FILLED.value])

        stage1_cache_path = self.stage1_cache_out or self.stage1_cache_in or ""

        plan = ExecutionPlan(
            user_name=self.user_name,
            created_at=datetime.now().isoformat(),
            stage1_cache_path=stage1_cache_path,
            total_rows=len(row_actions),
            rows_to_process=rows_to_process,
            rows_to_skip=rows_to_skip,
            rows_already_filled=rows_already_filled,
            rows=row_actions,
        )

        self.execution_plan = plan
        return plan
    
    # =========================================================================
    # STAGE 3+4: Review & Confirm (Merged)
    # =========================================================================

    async def review_and_confirm(self) -> bool:
        """
        Merged Stage 3+4: Review plan and confirm with inline clarifications.

        Shows the complete execution plan with:
        - All rows grouped by date for readability
        - Inline questions for ambiguous items (memo matching, missing receipts)
        - Single approval at the end

        Returns:
            True if user approves, False otherwise
        """
        if not self.plan:
            raise ValueError("No plan created. Run match_data() first.")

        # Apply --mark-lost-receipt overrides before review prints / execution plan
        # is built. Marker lands in pending_reason; the existing bigo builder picks
        # it up as "[영수증 대기] 영수증 분실" and post-verify's LOST_RECEIPT_MARKERS
        # check skips re-flagging these rows.
        lost_rows = getattr(self, 'lost_receipt_rows', None) or set()
        if lost_rows:
            applied = 0
            for row in self.plan.rows:
                if row.row_number in lost_rows:
                    row.pending_reason = "영수증 분실"
                    applied += 1
            if applied:
                print(f"📎 영수증 분실 마커 적용: {applied}건 (rows={sorted(lost_rows)})")

        print("\n" + "="*60)
        print("📋 REVIEW & CONFIRM")
        print("="*60)

        # Count items needing input
        ambiguous_count = sum(1 for r in self.plan.rows if r.needs_clarification)

        print(f"\nExecution Plan: {self.plan.total_rows} rows")
        print(f"User: {self.plan.user_name}")
        if ambiguous_count > 0:
            print(f"⚠️  {ambiguous_count} item(s) need your input below")
        print(f"{'─'*60}")

        if not self.memos:
            print("\n⚠️ No memos loaded. Memo-based clarification prompts will be skipped.")
            print("   Provide --memo or --stage1-cache-in to enable memo matching.")

        if ambiguous_count > 0 and not self.auto_approve and not self.review_only and not sys.stdin.isatty():
            print("\n⚠️ Interactive review required, but stdin is not a TTY.")
            print("   Re-run in an interactive terminal, or pass --auto-approve or --review-only.")
            return False

        # review_only takes precedence over auto_approve
        if self.auto_approve and self.review_only:
            self.auto_approve = False

        if self.auto_approve:
            # Auto-approve mode: resolve all ambiguous with defaults, show summary, proceed
            for row in self.plan.rows:
                if row.needs_clarification:
                    row.needs_clarification = False
                    row.confidence = "HIGH"
                    row.user_confirmed = True
            self._print_plan_summary()
            print("\n✅ [Auto-approve mode] Proceeding with automation...")

            # Build execution plan
            self._build_execution_plan()

            # Save Stage 3 cache if requested (auto-approve mode)
            if self.stage3_cache_out:
                print(f"\n[cache] Saving Stage 3 cache: {self.stage3_cache_out}")
                self._save_stage3_cache(self.stage3_cache_out)
                self.execution_plan_path = self.stage3_cache_out

            return True

        # Group rows by date for better organization
        rows_by_date: Dict[str, List[MatchedRow]] = {}
        for row in self.plan.rows:
            date_key = row.transaction.date_short or "Unknown"
            if date_key not in rows_by_date:
                rows_by_date[date_key] = []
            rows_by_date[date_key].append(row)

        # Process each date group (sort numerically: 2/1, 2/2, ..., 2/10)
        def _date_sort_key(d):
            try:
                parts = d.split("/")
                return (int(parts[0]), int(parts[1]))
            except (ValueError, IndexError):
                return (9999, 9999)

        for date_key in sorted(rows_by_date.keys(), key=_date_sort_key):
            date_rows = rows_by_date[date_key]
            ambiguous_in_date = [r for r in date_rows if r.needs_clarification]

            # Date header
            date_status = "⚠️" if ambiguous_in_date else "✅"
            print(f"\n{date_status} 📅 {date_key} ({len(date_rows)} transaction{'s' if len(date_rows) > 1 else ''})")
            print(f"{'─'*50}")

            # Check if there are memos to match for this date
            # Normalize both formats to "MM-DD" for comparison
            normalized_date_key = date_key.replace('/', '-')  # "1/9" → "1-9"
            if '-' in normalized_date_key:
                parts = normalized_date_key.split('-')
                normalized_date_key = f"{parts[0].zfill(2)}-{parts[1].zfill(2)}"  # "1-9" → "01-09"

            memos_for_date = [m for m in self.memos
                             if m.date and m.date.lstrip('0').replace('-0', '-') == normalized_date_key.lstrip('0').replace('-0', '-')]

            # Determine if we need clarification for this date
            # Only prompt when rows are explicitly marked as needs_clarification.
            needs_user_clarification = bool(ambiguous_in_date)

            if needs_user_clarification:
                await self._handle_date_memo_matching(date_key, date_rows, memos_for_date)
            else:
                # Just display each row
                for row in date_rows:
                    await self._display_row_with_inline_prompt(row)

        # Final summary
        self._print_plan_summary()

        if self.review_only:
            print(f"\n{'─'*60}")
            print("[review-only] Review complete. Run with --auto-approve to execute.")
            return False

        # Final approval
        try:
            print(f"\n{'─'*60}")
            response = input("✅ Proceed with automation? (y/n): ").strip().lower()
            approved = response in ('y', 'yes', '')

            if approved:
                print("\n🚀 Starting automation...")
            else:
                print("\n❌ Cancelled by user")

            if approved:
                # Build execution plan
                self._build_execution_plan()

                # Save Stage 3 cache if requested (after review/approval)
                if self.stage3_cache_out:
                    print(f"\n[cache] Saving Stage 3 cache: {self.stage3_cache_out}")
                    self._save_stage3_cache(self.stage3_cache_out)
                    self.execution_plan_path = self.stage3_cache_out

            return approved

        except EOFError:
            print("\n⚠️ Non-interactive mode, proceeding...")

            # Build execution plan
            self._build_execution_plan()

            # Save Stage 3 cache if requested (non-interactive mode)
            if self.stage3_cache_out:
                print(f"\n[cache] Saving Stage 3 cache: {self.stage3_cache_out}")
                self._save_stage3_cache(self.stage3_cache_out)
                self.execution_plan_path = self.stage3_cache_out

            return True

    async def _handle_date_memo_matching(
        self,
        date_key: str,
        date_rows: List[MatchedRow],
        memos: List[MemoData]
    ) -> None:
        """Handle memo-to-transaction matching for a specific date inline."""

        # Show all transactions for this date
        print(f"\n   Transactions on {date_key}:")
        for i, row in enumerate(date_rows, 1):
            tx = row.transaction
            status = "⚠️" if row.needs_clarification else "✅"
            attendee_info = f"→ {row.attendees}" if row.attendees else ""
            print(f"   [{i}] {status} {tx.time or '??:??'} {tx.merchant[:20]:<20} {tx.amount:>8,}원 {attendee_info}")

        # Show memos available for this date (only those needing confirmation)
        if memos:
            memos_to_confirm = []
            for memo in memos:
                assigned_rows = [r for r in date_rows if r.memo_source == memo.raw_memo]
                if not assigned_rows or any(r.needs_clarification for r in assigned_rows):
                    memos_to_confirm.append(memo)

            if memos_to_confirm:
                print(f"\n   Available memo(s) for {date_key}:")
                for j, memo in enumerate(memos_to_confirm, 1):
                    print(f"   ({j}) \"{memo.attendees_str}\" - {memo.raw_memo[:30]}")

                if self.review_only:
                    # Display-only: show what needs assignment without prompting
                    for j, memo in enumerate(memos_to_confirm, 1):
                        print(f"   ⚠️ Memo ({j}) \"{memo.attendees_str}\" → needs assignment to a transaction")
                else:
                    # Ask user to match each memo
                    try:
                        for j, memo in enumerate(memos_to_confirm, 1):
                            print(f"\n   → Memo ({j}) \"{memo.attendees_str}\" applies to which transaction?")
                            response = input(f"     Enter [1-{len(date_rows)}] or 0 to skip: ").strip()

                            try:
                                choice = int(response)
                                if 1 <= choice <= len(date_rows):
                                    selected_row = date_rows[choice - 1]
                                    selected_row.attendees = memo.attendees_str
                                    selected_row.memo_source = memo.raw_memo
                                    selected_row.needs_clarification = False
                                    selected_row.confidence = "HIGH"
                                    selected_row.user_confirmed = True
                                    try:
                                        memo_index = self.memos.index(memo)
                                    except ValueError:
                                        memo_index = None
                                    if memo_index is not None:
                                        selected_row.matched_memo = {
                                            "item_id": f"memo_{memo_index}",
                                            "item_type": "memo",
                                            "matched_row": selected_row.row_index,
                                            "confidence": 1.0,
                                            "reason": "User assigned during review"
                                        }
                                    print(f"     ✓ Assigned to Row {selected_row.row_index + 1}")
                                elif choice == 0:
                                    print(f"     ⏭ Skipped")
                                else:
                                    print(f"     ⚠️ Invalid choice, skipped")
                            except ValueError:
                                print(f"     ⚠️ Invalid input, skipped")

                    except EOFError:
                        print(f"   ⚠️ Non-interactive, using defaults")

        # Handle remaining ambiguous rows (e.g., missing receipts)
        for row in date_rows:
            if row.needs_clarification:
                await self._handle_row_clarification(row)

        if not self.review_only:
            # Mark any still-unresolved as resolved with defaults
            for row in date_rows:
                if row.needs_clarification:
                    row.needs_clarification = False
                    row.confidence = "HIGH"
                    row.user_confirmed = True

    async def _display_row_with_inline_prompt(self, row: MatchedRow) -> None:
        """Display a single row, with inline prompt if it needs clarification."""
        tx = row.transaction
        status = "✅" if row.confidence == "HIGH" and not row.needs_clarification else "⚠️"

        # Basic row info
        print(f"\n   {status} Row {row.row_index + 1}: {tx.time or '??:??'} {tx.merchant}")
        print(f"      금액: {tx.amount:,}원")
        print(f"      참석자: {row.attendees}")

        # Supplier (실공급자) — for PG/대행 거래, 상호 + 사업자등록번호 are required.
        requires_supplier = (
            self._requires_supplier_info(tx.merchant) or self._is_pg_merchant(tx.merchant)
        )
        if row.supplier_name:
            biz = (row.supplier_biz_no or "").strip()
            if biz:
                print(f"      실공급자: {row.supplier_name} / {biz}")
            elif requires_supplier:
                print(f"      실공급자: {row.supplier_name} / ⚠️ 사업자등록번호 누락")
            else:
                print(f"      실공급자: {row.supplier_name} / N/A")
        elif requires_supplier:
            print(f"      실공급자: ⚠️ 미입력 (PG/대행 거래 — 실공급자 상호+사업자등록번호 필요)")

        if row.receipt_paths:
            names = [os.path.basename(p) for p in row.receipt_paths]
            attach_mark = "✅"
            # PG/대행 거래인데 사업자번호가 비면 첨부가 있어도 주의 표시.
            if requires_supplier and not (row.supplier_biz_no or "").strip():
                attach_mark = "⚠️ (실공급자 정보 확인 필요)"
            print(f"      첨부: {', '.join(names)} {attach_mark}")
        elif requires_supplier:
            print(f"      첨부: ⚠️ 영수증 필요 (PG/대행 거래 — 실공급자 확인용)")
        elif row.pending_receipt:
            print(f"      첨부: ❌ 영수증 없음")

        # Handle inline clarification if needed
        if row.needs_clarification:
            await self._handle_row_clarification(row)

    async def _handle_row_clarification(self, row: MatchedRow) -> None:
        """Handle clarification for a single row inline."""
        if not row.needs_clarification:
            return

        # Review-only: display what needs clarification, don't modify state
        if self.review_only:
            if row.pending_receipt:
                print(f"      ⚠️ No receipt — needs note decision for 비고")
            elif row.clarification_reason:
                print(f"      ⚠️ Needs clarification: {row.clarification_reason}")
            else:
                print(f"      ⚠️ Needs clarification")
            return

        try:
            if row.pending_receipt:
                response = input(f"      → No receipt. Add note to 비고? (y/n/custom text): ").strip()
                if response.lower() == 'n':
                    row.pending_receipt = False
                    row.pending_reason = None
                    print(f"      ✓ No note will be added")
                elif response.lower() in ('y', ''):
                    row.pending_reason = "영수증 미첨부"
                    print(f"      ✓ Will add \"{row.pending_reason}\" to 비고")
                else:
                    row.pending_reason = response
                    print(f"      ✓ Will add \"{response}\" to 비고")
                row.needs_clarification = False
                row.confidence = "HIGH"
                row.user_confirmed = True
            else:
                # Generic clarification - just mark as resolved
                row.needs_clarification = False
                row.confidence = "HIGH"
                row.user_confirmed = True

        except EOFError:
            row.needs_clarification = False
            row.confidence = "HIGH"
            row.user_confirmed = True

    def _is_memo_assigned(self, memo: MemoData) -> bool:
        """Check if a memo has already been assigned to a row."""
        if not self.plan:
            return False
        return any(r.memo_source == memo.raw_memo for r in self.plan.rows)

    def _print_plan_summary(self) -> None:
        """Print a summary of the execution plan."""
        print(f"\n{'─'*60}")
        print("📊 Summary")
        print(f"{'─'*60}")
        print(f"   Total rows: {self.plan.total_rows}")
        print(f"   With receipts: {self.plan.with_receipt_count}")
        print(f"   Pending receipts: {self.plan.pending_receipt_count}")

        # Count by confidence
        high_conf = sum(1 for r in self.plan.rows if r.confidence == "HIGH")
        low_conf = self.plan.total_rows - high_conf
        if low_conf > 0:
            print(f"   ⚠️ Low confidence: {low_conf}")

    def _row_action_to_expense_data(self, row_action: RowAction) -> Optional[ExpenseData]:
        """Convert RowAction to ExpenseData for automation."""
        if not row_action.fill_data:
            return None

        fd = row_action.fill_data
        tx = row_action.transaction or {}
        needs_yongdo = fd.get("needs_yongdo")
        if needs_yongdo is None:
            needs_yongdo = not tx.get("yongdo_filled", False)
        needs_content = fd.get("needs_content")
        if needs_content is None:
            needs_content = not tx.get("content_filled", False)

        return ExpenseData(
            merchant=fd.get("merchant", ""),
            yongdo=fd.get("yongdo", ""),
            content=fd.get("content", ""),
            attendees=fd.get("attendees", ""),
            supplier_name=fd.get("supplier_name"),
            supplier_biz_no=fd.get("supplier_biz_no"),
            receipt_paths=fd.get("receipt_paths") or ([fd["receipt_path"]] if fd.get("receipt_path") else []),
            pending_reason=fd.get("pending_reason"),
            bigo_notes=row_action.bigo_notes or [],
            needs_yongdo=bool(needs_yongdo),
            needs_content=bool(needs_content),
            requires_supplier_info=bool(
                fd.get("requires_supplier_info")
                or self._requires_supplier_info(fd.get("merchant", ""))
                or self._is_pg_merchant(fd.get("merchant", ""))
            ),
        )

    def _refresh_execution_counts(self, plan: ExecutionPlan) -> None:
        """Recalculate Stage 4 counters from row execution_status."""
        plan.stage4_success_count = len([
            r for r in plan.rows if r.execution_status == ExecutionStatus.SUCCESS.value
        ])
        plan.stage4_failed_count = len([
            r for r in plan.rows if r.execution_status == ExecutionStatus.FAILED.value
        ])
        plan.stage4_skipped_count = len([
            r for r in plan.rows if r.execution_status == ExecutionStatus.SKIPPED.value
        ])

    # =========================================================================
    # LEGACY: Separate Stage 3 & 4 (kept for backward compatibility)
    # =========================================================================

    async def clarify(self) -> None:
        """
        [LEGACY] Interactive clarification for ambiguous matches.
        Use review_and_confirm() instead for merged flow.
        """
        # Delegate to merged method's clarification logic
        pass  # No-op when using merged flow

    async def review(self) -> bool:
        """
        [LEGACY] Show full plan and get user approval.
        Use review_and_confirm() instead for merged flow.
        """
        # Delegate to merged method
        return await self.review_and_confirm()
    
    # =========================================================================
    # STAGE 5: Automation
    # =========================================================================
    
    async def execute(self) -> Dict[str, Any]:
        """
        Execute the automation plan with progress indicator.
        
        Returns:
            Result dictionary with success/failure counts
        """
        if not self.execution_plan:
            if self.plan:
                self.execution_plan = self._build_execution_plan()
            else:
                raise ValueError("No plan created. Run match_data() first.")

        plan = self.execution_plan

        if not self.execution_plan_path:
            self.execution_plan_path = self.stage3_cache_out or self.stage3_cache_in
        if self.stage3_cache_in and not self.stage3_cache_out:
            if self.execution_plan_path == self.stage3_cache_in:
                gt_hint = (f"{os.sep}gt{os.sep}" in self.execution_plan_path) or self.execution_plan_path.endswith("_gt.json")
                if gt_hint:
                    base_dir = os.path.dirname(self.execution_plan_path)
                    filename = os.path.basename(self.execution_plan_path)
                    if filename.endswith("_gt.json"):
                        filename = filename.replace("_gt.json", "_run.json")
                    else:
                        filename = f"run_{filename}"
                    run_dir = base_dir.replace(f"{os.sep}gt", f"{os.sep}run")
                    if run_dir == base_dir:
                        run_dir = os.path.join(base_dir, "run")
                    os.makedirs(run_dir, exist_ok=True)
                    self.execution_plan_path = os.path.join(run_dir, filename)
                    print(f"   Using Stage 4 run cache: {self.execution_plan_path}")
        
        print("\n" + "="*60)
        print("🚀 STAGE 5: Automation")
        print("="*60)
        
        if not self.automation:
            self.automation = DouzoneAutomation(self.cdp_url)
            await self.automation.connect()
        
        # CRITICAL: Scroll grid to top before processing
        # This ensures row indices match actual Douzone row numbers
        print("   Scrolling grid to top...")
        await self.automation.scroll_grid_to_top()
        
        # Mark skip/already-filled rows
        for row in plan.rows:
            if row.action in (ActionType.SKIP.value, ActionType.ALREADY_FILLED.value):
                row.execution_status = ExecutionStatus.SKIPPED.value

        # Resume handling: reset any in-progress rows
        for row in plan.rows:
            if row.execution_status == ExecutionStatus.PROCESSING.value:
                row.execution_status = ExecutionStatus.PENDING.value
                row.execution_error = "Interrupted - reset to pending"

        self._refresh_execution_counts(plan)

        if not plan.stage4_started_at:
            plan.stage4_started_at = datetime.now().isoformat()

        # Determine rows to process
        rows_to_process = [
            r for r in plan.rows
            if r.action in (ActionType.AUTO_FILL.value, ActionType.USER_CONFIRMED.value)
            and r.execution_status == ExecutionStatus.PENDING.value
        ]
        rows_to_process = sorted(rows_to_process, key=lambda r: r.row_number)

        if self.max_rows > 0:
            rows_to_process = rows_to_process[:self.max_rows]
            print(f"   ⚠️  LIMITED MODE: Processing only {len(rows_to_process)} of {plan.total_rows} rows")
        
        total = len(rows_to_process)
        failures = []

        # Estimate wall-clock so the agent/user can decide on background execution.
        if total > 0:
            est_min = max(1, round(total * SEC_PER_ROW_ESTIMATE / 60))
            print(f"   ⏱️  예상 소요: 약 {est_min}분 ({total}행 × ~{SEC_PER_ROW_ESTIMATE}초/행)")
            if total >= BACKGROUND_ROW_THRESHOLD:
                print(f"   ℹ️  행이 많습니다({total}행). 포그라운드 실행이 타임아웃(예: 10분)에 걸릴 수 있으니")
                print(f"      백그라운드 실행을 권장합니다. 중단되어도 이미 처리된 행은 자동 스킵되어")
                print(f"      안전하게 재개됩니다(재실행 시 중복 없음).")

        if total == 0:
            print("\n✅ No pending rows to process.")
            return {
                "total": 0,
                "success": 0,
                "failures": [],
                "success_rate": 100.0,
            }
        
        for i, row in enumerate(rows_to_process, 1):
            row_number = row.row_number
            idx = row_number - 1
            progress = i / total * 100
            
            # Progress bar
            bar_len = 40
            filled = int(bar_len * i / total)
            bar = "█" * filled + "░" * (bar_len - filled)
            
            merchant = (row.transaction or {}).get("merchant", "") if row.transaction else ""
            print(f"\r🔄 [{bar}] {progress:.0f}% - Row {row_number}/{total}: {merchant[:15]}...", 
                  end="", flush=True)

            row.execution_status = ExecutionStatus.PROCESSING.value
            row.execution_timestamp = datetime.now().isoformat()
            if self.execution_plan_path:
                with open(self.execution_plan_path, "w", encoding="utf-8") as f:
                    json.dump(plan.to_dict(), f, ensure_ascii=False, indent=2)
            
            try:
                expense_data = self._row_action_to_expense_data(row)
                if not expense_data:
                    row.execution_status = ExecutionStatus.SKIPPED.value
                    row.execution_error = "No fill_data"
                    self._refresh_execution_counts(plan)
                    if self.execution_plan_path:
                        with open(self.execution_plan_path, "w", encoding="utf-8") as f:
                            json.dump(plan.to_dict(), f, ensure_ascii=False, indent=2)
                    continue

                result = await self.automation.process_row(idx, expense_data)
                
                if result:
                    row.execution_status = ExecutionStatus.SUCCESS.value
                    row.execution_error = None
                    # Track which receipts actually attached (for post-verification)
                    attached = getattr(expense_data, '_attached_receipts', None)
                    if attached is not None and row.fill_data:
                        row.fill_data['attached_receipts'] = attached
                else:
                    row.execution_status = ExecutionStatus.FAILED.value
                    err = getattr(self.automation, "last_error", None)
                    row.execution_error = err or "process_row returned False"
                    failures.append(f"Row {row_number}: {row.execution_error}")
                    
            except Exception as e:
                row.execution_status = ExecutionStatus.FAILED.value
                row.execution_error = str(e)
                failures.append(f"Row {row_number}: {str(e)}")
                logger.error(f"Error processing row {row_number}: {e}")

            self._refresh_execution_counts(plan)
            if self.execution_plan_path:
                with open(self.execution_plan_path, "w", encoding="utf-8") as f:
                    json.dump(plan.to_dict(), f, ensure_ascii=False, indent=2)
        
        print()  # New line after progress bar

        # Close any leftover popup/dialog from the last row
        try:
            await self.automation.close_popup()
        except Exception:
            pass

        plan.stage4_completed_at = datetime.now().isoformat()
        if self.execution_plan_path:
            with open(self.execution_plan_path, "w", encoding="utf-8") as f:
                json.dump(plan.to_dict(), f, ensure_ascii=False, indent=2)
        
        # Results summary
        plan_total = plan.rows_to_process
        print(f"\n{'─'*40}")
        print("📊 Automation Results:")
        print(f"   Total: {plan_total}")
        print(f"   Success: {plan.stage4_success_count}")
        print(f"   Failed: {plan.stage4_failed_count}")
        print(f"   Skipped: {plan.stage4_skipped_count}")
        print(f"   Success rate: {plan.success_rate:.1f}%")
        
        if failures:
            print(f"\n⚠️ Failures:")
            for f in failures[:10]:  # Show first 10
                print(f"   - {f}")
            if len(failures) > 10:
                print(f"   ... and {len(failures) - 10} more")
        
        return {
            "total": plan_total,
            "success": plan.stage4_success_count,
            "failures": failures,
            "success_rate": plan.success_rate,
        }
    
    # =========================================================================
    # STAGE 5: POST-VERIFICATION
    # =========================================================================

    # Parking keywords for detection
    PARKING_KEYWORDS = ['주차', '주차장', '주차비', '파킹', 'parking']
    PARKING_CAP = 200000  # 200,000원

    # Markers in pending_reason / 비고 that indicate the user has already
    # acknowledged the missing receipt — post-verify should not re-flag.
    LOST_RECEIPT_MARKERS = ['영수증 분실', '영수증 누락', '영수증 폐기', '재발급 불가']

    def _parse_amount(self, amount_str) -> Optional[int]:
        """Parse amount from various formats to integer (won)."""
        if amount_str is None:
            return None
        if isinstance(amount_str, (int, float)):
            return int(amount_str)
        s = str(amount_str).strip()
        # Remove currency markers and commas
        s = s.replace('원', '').replace('₩', '').replace(',', '').replace(' ', '')
        # Handle parentheses as negative: (50000) -> -50000
        if s.startswith('(') and s.endswith(')'):
            s = '-' + s[1:-1]
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return None

    def _tokenize_merchant(self, merchant: str) -> List[str]:
        """Tokenize merchant name for fuzzy matching.
        Min length 2 to handle short Korean names like 배민, 토스."""
        import re
        tokens = re.split(r'[\s\-\/\(\)\[\]]+', merchant.strip().lower())
        return [t for t in tokens if len(t) >= 2]

    def _merchants_match(self, a: str, b: str) -> bool:
        """Check if two merchant names match using shared-keyword substring matching."""
        tokens_a = self._tokenize_merchant(a)
        tokens_b = self._tokenize_merchant(b)
        if not tokens_a or not tokens_b:
            return a.strip().lower() == b.strip().lower()
        for ta in tokens_a:
            for tb in tokens_b:
                if ta in tb or tb in ta:
                    return True
        return False

    def _is_parking_transaction(self, row: 'RowAction') -> bool:
        """Check if a row is a parking transaction."""
        fields_to_check = []
        txn = row.transaction or {}
        fields_to_check.append(txn.get('merchant', ''))
        if row.fill_data:
            fields_to_check.append(row.fill_data.get('yongdo', ''))
            fields_to_check.append(row.fill_data.get('content', ''))
        combined = ' '.join(fields_to_check).lower()
        return any(kw in combined for kw in self.PARKING_KEYWORDS)

    def _check_pg_missing_receipts(self, plan: ExecutionPlan) -> List['VerificationIssue']:
        """Check for rows that need receipts but don't have them (PG, SaaS, large malls)."""
        # Uses VerificationIssue, VerificationIssueType from top-level import
        issues = []
        for row in plan.rows:
            if row.execution_status != ExecutionStatus.SUCCESS.value:
                continue
            merchant = (row.transaction or {}).get('merchant', '')
            if not self._might_need_receipt(merchant):
                continue
            # Refund/cancel rows (negative amount) have no receipt by nature —
            # don't flag them as 'PG 영수증 누락'. Standalone cancels are surfaced
            # separately by _check_charge_cancel_pairs as CANCEL_ONLY.
            amount_signed = self._parse_amount((row.transaction or {}).get('amount'))
            if amount_signed is not None and amount_signed < 0:
                continue
            fill = row.fill_data or {}
            attached = fill.get('attached_receipts')
            if attached is not None:
                # Use explicit tracking from Stage 4
                has_valid_receipts = len(attached) > 0
            else:
                # Fallback for plans without tracking (e.g., loaded from old cache)
                receipt_paths = fill.get('receipt_paths', [])
                has_valid_receipts = bool(receipt_paths)
            if not has_valid_receipts:
                # User-acknowledged loss → don't re-flag.
                # Marker can sit in pending_reason (matcher path) or 비고 (manual edit).
                reason_text = (row.pending_reason or '').strip()
                bigo_text = (fill.get('bigo') or fill.get('note') or '').strip()
                marker_hit = any(
                    m in reason_text or m in bigo_text
                    for m in self.LOST_RECEIPT_MARKERS
                )
                # Supplier info already filled means user has independently
                # established the real vendor — receipt is no longer load-bearing.
                supplier_filled = bool(fill.get('supplier_name')) and bool(fill.get('supplier_biz_no'))
                if marker_hit or supplier_filled:
                    continue
                amount = self._parse_amount((row.transaction or {}).get('amount')) or 0
                issues.append(VerificationIssue(
                    issue_type=VerificationIssueType.PG_MISSING_RECEIPT.value,
                    row_numbers=[row.row_number],
                    merchant=merchant,
                    amount=amount,
                    description=f"PG 거래 ({merchant}) 영수증 미첨부 — 실공급자 확인을 위해 영수증 필수",
                ))
        return issues

    def _check_pg_missing_supplier(self, plan: ExecutionPlan) -> List['VerificationIssue']:
        """Check rows that require 실공급자 info but have it missing.

        Covers PG merchants and other SUPPLIER_REQUIRED_MERCHANTS
        (배민/백화점/코엑스/NICE/NHN KCP/이니시스 등).

        Scans SUCCESS and FAILED rows — a failed row for a required merchant
        still needs to be surfaced for manual attention.
        """
        # Uses VerificationIssue, VerificationIssueType from top-level import
        issues = []
        scanned_statuses = {
            ExecutionStatus.SUCCESS.value,
            ExecutionStatus.FAILED.value,
        }
        for row in plan.rows:
            if row.execution_status not in scanned_statuses:
                continue
            merchant = (row.transaction or {}).get('merchant', '')
            if not (self._requires_supplier_info(merchant) or self._is_pg_merchant(merchant)):
                continue
            fill = row.fill_data or {}
            supplier_name = fill.get('supplier_name', '')
            supplier_biz_no = fill.get('supplier_biz_no', '')
            if not supplier_name or not supplier_biz_no:
                amount = self._parse_amount((row.transaction or {}).get('amount')) or 0
                missing = []
                if not supplier_name:
                    missing.append('실공급자상호')
                if not supplier_biz_no:
                    missing.append('실공급자번호')
                status_suffix = ""
                if row.execution_status == ExecutionStatus.FAILED.value:
                    status_suffix = " [행 실행 실패]"
                issues.append(VerificationIssue(
                    issue_type=VerificationIssueType.PG_MISSING_SUPPLIER.value,
                    row_numbers=[row.row_number],
                    merchant=merchant,
                    amount=amount,
                    description=f"실공급자 정보 필요 거래처 ({merchant}) {', '.join(missing)} 누락{status_suffix}",
                ))
        return issues

    def _check_charge_cancel_pairs(self, plan: ExecutionPlan) -> List['VerificationIssue']:
        """Check for charge+cancellation pairs submitted incorrectly."""
        # Uses VerificationIssue, VerificationIssueType from top-level import
        issues = []
        # Collect all rows with parsed amounts and dates
        parsed_rows = []
        for row in plan.rows:
            txn = row.transaction or {}
            amount = self._parse_amount(txn.get('amount'))
            if amount is None:
                continue
            date_str = txn.get('date', '') or txn.get('date_time', '') or ''
            # Extract date part (first 10 chars for YYYY-MM-DD or similar)
            date_str = date_str[:10].strip()
            parsed_rows.append({
                'row': row,
                'merchant': txn.get('merchant', ''),
                'amount': amount,
                'date_str': date_str,
            })

        # Find charge+cancel pairs
        matched_indices = set()
        for i, a in enumerate(parsed_rows):
            if i in matched_indices:
                continue
            if a['amount'] >= 0:
                continue  # Look for negative (cancel) rows first
            for j, b in enumerate(parsed_rows):
                if j in matched_indices or j == i:
                    continue
                if b['amount'] <= 0:
                    continue  # b should be the charge (positive)
                # Check: same |amount|
                if abs(a['amount']) != abs(b['amount']):
                    continue
                # Check: merchants match (fuzzy)
                if not self._merchants_match(a['merchant'], b['merchant']):
                    continue
                # Check: dates within 2 days
                try:
                    from datetime import datetime as _dt, timedelta
                    date_a = date_b = None
                    for fmt in ('%Y-%m-%d', '%Y.%m.%d', '%Y/%m/%d'):
                        try:
                            date_a = _dt.strptime(a['date_str'], fmt).date()
                            break
                        except ValueError:
                            continue
                    for fmt in ('%Y-%m-%d', '%Y.%m.%d', '%Y/%m/%d'):
                        try:
                            date_b = _dt.strptime(b['date_str'], fmt).date()
                            break
                        except ValueError:
                            continue
                    if date_a and date_b and abs((date_a - date_b).days) > 2:
                        continue
                except (ValueError, TypeError):
                    # Date parse failed — skip date check, match on amount+merchant only
                    pass

                # Found a pair: check if only one side was submitted
                cancel_row = a['row']
                charge_row = b['row']
                cancel_submitted = cancel_row.execution_status == ExecutionStatus.SUCCESS.value
                charge_submitted = charge_row.execution_status == ExecutionStatus.SUCCESS.value

                # Only flag when exactly one side was submitted (asymmetric)
                # Both submitted or both skipped are valid states per spec
                if cancel_submitted != charge_submitted:
                    submitted_side = "취소분" if cancel_submitted else "결제분"
                    desc = f"결제+취소 쌍 중 {submitted_side}만 제출됨 ({b['merchant']} {abs(b['amount']):,}원)"
                    issues.append(VerificationIssue(
                        issue_type=VerificationIssueType.CHARGE_CANCEL_PAIR.value,
                        row_numbers=[charge_row.row_number],
                        paired_row_number=cancel_row.row_number,
                        merchant=b['merchant'],
                        amount=abs(b['amount']),
                        description=desc,
                    ))
                    matched_indices.add(i)
                    matched_indices.add(j)
                    break

        # Pass 2: unequal charge+cancel pairs (partial refund pattern, Rule 3).
        # Both rows submitted but charge ≠ |cancel| → user should claim only the net.
        # Asymmetric+unequal is intentionally skipped (too noisy to flag reliably).
        for i, a in enumerate(parsed_rows):
            if i in matched_indices:
                continue
            if a['amount'] >= 0:
                continue
            for j, b in enumerate(parsed_rows):
                if j in matched_indices or j == i:
                    continue
                if b['amount'] <= 0:
                    continue
                if abs(a['amount']) == abs(b['amount']):
                    continue  # Equal case — handled in pass 1.
                if not self._merchants_match(a['merchant'], b['merchant']):
                    continue
                try:
                    from datetime import datetime as _dt
                    date_a = date_b = None
                    for fmt in ('%Y-%m-%d', '%Y.%m.%d', '%Y/%m/%d'):
                        try:
                            date_a = _dt.strptime(a['date_str'], fmt).date()
                            break
                        except ValueError:
                            continue
                    for fmt in ('%Y-%m-%d', '%Y.%m.%d', '%Y/%m/%d'):
                        try:
                            date_b = _dt.strptime(b['date_str'], fmt).date()
                            break
                        except ValueError:
                            continue
                    if date_a and date_b and abs((date_a - date_b).days) > 3:
                        continue
                except (ValueError, TypeError):
                    pass

                cancel_row = a['row']
                charge_row = b['row']
                cancel_submitted = cancel_row.execution_status == ExecutionStatus.SUCCESS.value
                charge_submitted = charge_row.execution_status == ExecutionStatus.SUCCESS.value
                if not (cancel_submitted and charge_submitted):
                    continue

                net = b['amount'] + a['amount']  # b is positive charge, a is negative cancel
                desc = (
                    f"부분 환불: 결제 {b['amount']:,}원 + 취소 {a['amount']:,}원 → "
                    f"차액 {net:,}원만 청구 필요 (현재 둘 다 제출됨)"
                )
                issues.append(VerificationIssue(
                    issue_type=VerificationIssueType.CHARGE_CANCEL_UNEQUAL.value,
                    row_numbers=[charge_row.row_number],
                    paired_row_number=cancel_row.row_number,
                    merchant=b['merchant'],
                    amount=abs(b['amount']),
                    description=desc,
                ))
                matched_indices.add(i)
                matched_indices.add(j)
                break

        # Pass 3: standalone cancels (negative, no matching charge in this batch).
        # These are refunds the user submitted with no paired payment — there is no
        # receipt to attach, so they must NOT read as 'PG 영수증 누락'. Surface them
        # as an informational prompt to confirm the submission is intentional.
        for i, a in enumerate(parsed_rows):
            if i in matched_indices:
                continue
            if a['amount'] >= 0:
                continue
            cancel_row = a['row']
            if cancel_row.execution_status != ExecutionStatus.SUCCESS.value:
                continue
            issues.append(VerificationIssue(
                issue_type=VerificationIssueType.CANCEL_ONLY.value,
                row_numbers=[cancel_row.row_number],
                merchant=a['merchant'],
                amount=abs(a['amount']),
                description=(
                    f"취소(환불)분만 존재 — 대응 결제건 없음 "
                    f"({a['merchant']} {abs(a['amount']):,}원). 제출 여부 확인 필요"
                ),
            ))
        return issues

    def _check_parking_cap(self, plan: ExecutionPlan) -> List['VerificationIssue']:
        """Check for parking transactions exceeding 200,000원 cap."""
        # Uses VerificationIssue, VerificationIssueType from top-level import
        issues = []
        for row in plan.rows:
            if row.execution_status != ExecutionStatus.SUCCESS.value:
                continue
            if not self._is_parking_transaction(row):
                continue
            amount = self._parse_amount((row.transaction or {}).get('amount')) or 0
            if amount > self.PARKING_CAP:
                merchant = (row.transaction or {}).get('merchant', '')
                excess = amount - self.PARKING_CAP
                issues.append(VerificationIssue(
                    issue_type=VerificationIssueType.PARKING_OVER_CAP.value,
                    row_numbers=[row.row_number],
                    merchant=merchant,
                    amount=amount,
                    description=f"주차비 {amount:,}원 > 한도 {self.PARKING_CAP:,}원 (초과: {excess:,}원)",
                ))
        return issues

    def _matches_pg_suspect(self, merchant: str) -> bool:
        """Heuristic: merchant name matches a PG/대행사-style pattern."""
        if not merchant:
            return False
        m = merchant.lower()
        return any(p.lower() in m for p in PG_SUSPECT_PATTERNS)

    def _check_unknown_patterns(self, plan: ExecutionPlan) -> List['VerificationIssue']:
        """
        Flag merchants that LOOK like PG/대행사 but aren't on any known list.
        Surfaces candidates for the agent-extensible operations flow
        (/douzonebot:troubleshoot 단계 4).
        """
        # Uses VerificationIssue, VerificationIssueType from top-level import
        issues = []
        for row in plan.rows:
            if row.execution_status != ExecutionStatus.SUCCESS.value:
                continue
            merchant = (row.transaction or {}).get('merchant', '') or ''
            if not merchant:
                continue
            # Skip merchants already covered by other checks.
            if self._requires_supplier_info(merchant) or self._is_pg_merchant(merchant):
                continue
            if not self._matches_pg_suspect(merchant):
                continue
            amount = self._parse_amount((row.transaction or {}).get('amount')) or 0
            issues.append(VerificationIssue(
                issue_type=VerificationIssueType.UNKNOWN_PATTERN.value,
                row_numbers=[row.row_number],
                merchant=merchant,
                amount=amount,
                description=f"미등록 PG 패턴 — '{merchant}'가 알려진 거래처 리스트에 없음",
            ))
        return issues

    def _check_missing_attendees(self, plan: ExecutionPlan) -> List['VerificationIssue']:
        """
        Flag SUCCESS rows whose 용도 requires an attendee but field is empty.
        Rule 5: 1인 식사라도 본인 이름 기재 필수.
        """
        # Uses VerificationIssue, VerificationIssueType from top-level import
        issues = []
        for row in plan.rows:
            if row.execution_status != ExecutionStatus.SUCCESS.value:
                continue
            fill = row.fill_data or {}
            yongdo = fill.get('yongdo') or ''
            if yongdo not in ATTENDEE_REQUIRED_YONGDOS:
                continue
            attendees = (fill.get('attendees') or '').strip()
            if attendees:
                continue
            merchant = (row.transaction or {}).get('merchant', '') or ''
            amount = self._parse_amount((row.transaction or {}).get('amount')) or 0
            issues.append(VerificationIssue(
                issue_type=VerificationIssueType.MISSING_ATTENDEE.value,
                row_numbers=[row.row_number],
                merchant=merchant,
                amount=amount,
                description=f"참석자 누락 ({yongdo}) — 1인 식사라도 본인 이름 기재 필요",
            ))
        return issues

    def _check_entertainment_format(self, plan: ExecutionPlan) -> List['VerificationIssue']:
        """
        Flag 거래처 접대 rows whose 참석자 lacks 소속/직급 keywords.
        Rule 8: 접대 상대방의 소속/직급/성명 (예: '신우회계법인 김은서 과장').
        """
        # Uses VerificationIssue, VerificationIssueType from top-level import
        issues = []
        for row in plan.rows:
            if row.execution_status != ExecutionStatus.SUCCESS.value:
                continue
            fill = row.fill_data or {}
            yongdo = fill.get('yongdo') or ''
            if yongdo not in ENTERTAINMENT_YONGDOS:
                continue
            attendees = (fill.get('attendees') or '').strip()
            if not attendees:
                continue  # Already caught by _check_missing_attendees.
            if any(kw in attendees for kw in ENTERTAINMENT_TITLE_KEYWORDS):
                continue
            merchant = (row.transaction or {}).get('merchant', '') or ''
            amount = self._parse_amount((row.transaction or {}).get('amount')) or 0
            issues.append(VerificationIssue(
                issue_type=VerificationIssueType.ENTERTAINMENT_FORMAT.value,
                row_numbers=[row.row_number],
                merchant=merchant,
                amount=amount,
                description=(
                    f"접대비 형식 — 참석자 '{attendees}'에 소속/직급 누락 "
                    f"(예: '신우회계법인 김은서 과장')"
                ),
            ))
        return issues

    def _scan_all_issues(self, plan: ExecutionPlan) -> List['VerificationIssue']:
        """Run all post-verification checks and return sorted issues."""
        issues = []
        issues.extend(self._check_pg_missing_receipts(plan))
        issues.extend(self._check_pg_missing_supplier(plan))
        issues.extend(self._check_charge_cancel_pairs(plan))
        issues.extend(self._check_parking_cap(plan))
        issues.extend(self._check_unknown_patterns(plan))
        issues.extend(self._check_missing_attendees(plan))
        issues.extend(self._check_entertainment_format(plan))
        issues.sort(key=lambda i: i.row_numbers[0])
        return issues

    def _print_verification_report(self, issues: List['VerificationIssue']) -> None:
        """Print grouped post-verification report."""
        from .models import VerificationIssueType
        print("\n" + "=" * 60)
        print("🔍 STAGE 6: Post-Verification")
        print("=" * 60)

        if not issues:
            print("\n✅ Post-verification passed — no issues found.")
            return

        print(f"\n⚠️  Found {len(issues)} issue(s):\n")

        # Group by type
        groups = {}
        for issue in issues:
            groups.setdefault(issue.issue_type, []).append(issue)

        type_labels = {
            VerificationIssueType.PG_MISSING_RECEIPT.value: "📎 PG 거래 영수증 누락",
            VerificationIssueType.PG_MISSING_SUPPLIER.value: "🏢 실공급자 정보 누락 (배민/백화점/PG 등)",
            VerificationIssueType.CHARGE_CANCEL_PAIR.value: "🔄 결제+취소 쌍 불일치",
            VerificationIssueType.PARKING_OVER_CAP.value: "🅿️ 주차비 한도 초과",
            VerificationIssueType.UNKNOWN_PATTERN.value: "🆕 미등록 PG 패턴 (확인 필요)",
            VerificationIssueType.MISSING_ATTENDEE.value: "👥 참석자 누락",
            VerificationIssueType.CHARGE_CANCEL_UNEQUAL.value: "💱 부분 환불 (둘 다 제출됨)",
            VerificationIssueType.ENTERTAINMENT_FORMAT.value: "🎭 접대비 형식 (소속/직급)",
            VerificationIssueType.CANCEL_ONLY.value: "↩️ 취소분만 존재 (결제건 없음)",
        }

        for issue_type, group_issues in groups.items():
            label = type_labels.get(issue_type, issue_type)
            print(f"  {label} ({len(group_issues)}건)")
            for issue in group_issues:
                rows_str = ", ".join(str(r) for r in issue.row_numbers)
                if issue.paired_row_number:
                    rows_str += f" + {issue.paired_row_number}"
                print(f"    Row {rows_str}: {issue.description}")
            print()

    async def _prompt_fix_receipt(self, issue: 'VerificationIssue', plan: ExecutionPlan) -> None:
        """Interactive fix for missing PG receipt."""
        row_num = issue.row_numbers[0]
        print(f"\n  📎 Row {row_num}: {issue.merchant} — 영수증 누락")
        response = input("     영수증 파일 경로 입력 (또는 's' = skip): ").strip()

        if response.lower() == 's' or not response:
            issue.resolved = True
            issue.resolution = "skipped"
            print("     → 건너뜀")
            return

        # Validate file exists
        if not os.path.exists(response):
            print(f"     ❌ 파일 없음: {response}")
            issue.resolved = True
            issue.resolution = "file_not_found"
            return

        # Open popup and attach
        idx = row_num - 1
        popup_ok = await self.automation._open_popup_for_row(idx)
        if not popup_ok:
            print("     ❌ 팝업 열기 실패")
            issue.resolved = True
            issue.resolution = "popup_open_failed"
            return

        attached = await self.automation.attach_file(response)
        if not attached:
            print("     ❌ 파일 첨부 실패")
            await self.automation.cancel_popup()
            issue.resolved = True
            issue.resolution = "attach_failed"
            return

        saved = await self.automation.save_popup()
        if not saved:
            print("     ❌ 저장 실패")
            issue.resolved = True
            issue.resolution = "save_failed"
            return

        # Update fill_data
        for row in plan.rows:
            if row.row_number == row_num and row.fill_data:
                paths = row.fill_data.get('receipt_paths', [])
                paths.append(response)
                row.fill_data['receipt_paths'] = paths
                break

        issue.resolved = True
        issue.resolution = "fixed"
        print("     ✅ 영수증 첨부 완료")

    async def _prompt_fix_supplier(self, issue: 'VerificationIssue', plan: ExecutionPlan) -> None:
        """Interactive fix for missing supplier info."""
        row_num = issue.row_numbers[0]
        print(f"\n  🏢 Row {row_num}: {issue.merchant} — 실공급자 정보 누락")

        # Check if row has receipt for OCR retry
        target_row = None
        for row in plan.rows:
            if row.row_number == row_num:
                target_row = row
                break

        supplier_name = None
        supplier_biz_no = None
        receipt_paths = (target_row.fill_data or {}).get('receipt_paths', []) if target_row else []
        valid_receipts = [p for p in receipt_paths if os.path.exists(p)]

        if valid_receipts:
            response = input("     영수증에서 OCR 재시도? (y/n/직접입력): ").strip()
            if response.lower() == 'y':
                try:
                    from .pipeline import extract_supplier_info
                    supplier_name, supplier_biz_no = await extract_supplier_info(valid_receipts[0])
                    if supplier_name:
                        print(f"     OCR 결과: {supplier_name} / {supplier_biz_no or 'N/A'}")
                except Exception as e:
                    print(f"     OCR 실패: {e}")

        if not supplier_name:
            supplier_name = input("     실공급자상호 입력 (또는 's' = skip): ").strip()
            if supplier_name.lower() == 's' or not supplier_name:
                issue.resolved = True
                issue.resolution = "skipped"
                print("     → 건너뜀")
                return
            supplier_biz_no = input("     실공급자 사업자번호 입력: ").strip()

        # Open popup and fill
        idx = row_num - 1
        popup_ok = await self.automation._open_popup_for_row(idx)
        if not popup_ok:
            print("     ❌ 팝업 열기 실패")
            issue.resolved = True
            issue.resolution = "popup_open_failed"
            return

        # Fill supplier fields
        try:
            if supplier_name:
                supplier_input = self.automation.page.locator(
                    'input[placeholder*="실공급자상호"]'
                ).first
                if await supplier_input.is_visible():
                    await supplier_input.fill(supplier_name)
            if supplier_biz_no:
                biz_input = self.automation.page.locator(
                    'input[placeholder*="사업자등록번호"]'
                ).first
                if await biz_input.is_visible():
                    await biz_input.fill(supplier_biz_no)
        except Exception as e:
            print(f"     ❌ 필드 입력 실패: {e}")
            await self.automation.cancel_popup()
            issue.resolved = True
            issue.resolution = "fill_failed"
            return

        saved = await self.automation.save_popup()
        if not saved:
            print("     ❌ 저장 실패")
            issue.resolved = True
            issue.resolution = "save_failed"
            return

        # Update fill_data
        if target_row and target_row.fill_data:
            target_row.fill_data['supplier_name'] = supplier_name
            target_row.fill_data['supplier_biz_no'] = supplier_biz_no

        issue.resolved = True
        issue.resolution = "fixed"
        print(f"     ✅ 실공급자 정보 입력 완료: {supplier_name} / {supplier_biz_no}")

    async def _prompt_fix_pair(self, issue: 'VerificationIssue', plan: ExecutionPlan) -> None:
        """Interactive fix for charge+cancel pair."""
        charge_row = issue.row_numbers[0]
        cancel_row = issue.paired_row_number
        print(f"\n  🔄 Row {charge_row} + {cancel_row}: {issue.merchant} — 결제+취소 쌍 ({issue.amount:,}원)")
        print(f"     현재: 한쪽만 제출됨")
        response = input("     [s] 둘 다 건너뛰기 (기본) / [b] 둘 다 제출 / [i] 무시: ").strip().lower()

        if response == 'b':
            issue.resolved = True
            issue.resolution = "submit_both"
            print("     → 둘 다 제출 (수동 확인 필요)")
        elif response == 'i':
            issue.resolved = True
            issue.resolution = "ignored"
            print("     → 무시")
        else:
            issue.resolved = True
            issue.resolution = "skip_both"
            print("     → 둘 다 건너뛰기 (기본)")

    async def _prompt_fix_parking(self, issue: 'VerificationIssue', plan: ExecutionPlan) -> None:
        """Interactive fix for parking cap overage."""
        row_num = issue.row_numbers[0]
        excess = issue.amount - self.PARKING_CAP
        print(f"\n  🅿️ Row {row_num}: {issue.merchant} — 주차비 {issue.amount:,}원 (한도 초과 {excess:,}원)")
        response = input("     [a] 금액 수정 / [s] 건너뛰기 / [k] 초과 인정: ").strip().lower()

        if response == 'a':
            new_amount = input(f"     수정 금액 입력 (현재: {issue.amount:,}원): ").strip()
            parsed = self._parse_amount(new_amount)
            if parsed is None or parsed <= 0:
                print("     ❌ 잘못된 금액")
                issue.resolved = True
                issue.resolution = "invalid_amount"
                return

            idx = row_num - 1
            popup_ok = await self.automation._open_popup_for_row(idx)
            if not popup_ok:
                print("     ❌ 팝업 열기 실패")
                issue.resolved = True
                issue.resolution = "popup_open_failed"
                return

            # Try to find and fill the amount field
            try:
                amount_input = self.automation.page.locator(
                    'input[placeholder*="금액"]'
                ).first
                if await amount_input.is_visible():
                    await amount_input.fill(str(parsed))
            except Exception as e:
                print(f"     ❌ 금액 수정 실패: {e}")
                await self.automation.close_popup()
                issue.resolved = True
                issue.resolution = "fill_failed"
                return

            saved = await self.automation.save_popup()
            if not saved:
                print("     ❌ 저장 실패")
                issue.resolved = True
                issue.resolution = "save_failed"
                return

            issue.resolved = True
            issue.resolution = f"adjusted_to_{parsed}"
            print(f"     ✅ 금액 수정 완료: {parsed:,}원")
        elif response == 'k':
            issue.resolved = True
            issue.resolution = "acknowledged"
            print("     → 초과 인정")
        else:
            issue.resolved = True
            issue.resolution = "skipped"
            print("     → 건너뜀")

    async def _prompt_unknown_pattern(self, issue: 'VerificationIssue', plan: ExecutionPlan) -> None:
        """
        Surface UNKNOWN_PATTERN to the user without auto-fixing.

        Resolution happens in /douzonebot:troubleshoot 단계 4 (bounded
        agent-extensible flow): the agent proposes adding the merchant to
        SUPPLIER_REQUIRED_MERCHANTS or writes a new operations.py helper,
        runs validate_extension, dry-runs on one row, then persists.
        """
        row_num = issue.row_numbers[0]
        print(f"\n  🆕 Row {row_num}: {issue.merchant} — 미등록 PG 패턴")
        print(f"     /douzonebot:troubleshoot 로 확장 등록 가능")
        issue.resolved = True
        issue.resolution = "deferred_to_troubleshoot"

    async def _set_attendee_on_row(
        self, row_num: int, attendee: str, plan: ExecutionPlan
    ) -> tuple:
        """
        Open popup, fill 참석자, save. Returns (success: bool, resolution_tag: str).
        Shared by _prompt_fix_missing_attendee and _prompt_fix_entertainment_format.
        """
        idx = row_num - 1
        if not await self.automation._open_popup_for_row(idx):
            return False, "popup_open_failed"
        try:
            attendee_input = self.automation.page.locator(
                'input[placeholder*="참석자"]'
            ).first
            if await attendee_input.is_visible():
                await attendee_input.fill(attendee)
        except Exception as e:
            logger.error(f"Attendee fill failed for row {row_num}: {e}")
            await self.automation.cancel_popup()
            return False, "fill_failed"
        if not await self.automation.save_popup():
            return False, "save_failed"
        for row in plan.rows:
            if row.row_number == row_num and row.fill_data:
                row.fill_data['attendees'] = attendee
                break
        return True, "fixed"

    async def _prompt_fix_missing_attendee(self, issue: 'VerificationIssue', plan: ExecutionPlan) -> None:
        """Interactive fix for missing 참석자 (Rule 5)."""
        row_num = issue.row_numbers[0]
        print(f"\n  👥 Row {row_num}: {issue.merchant} — 참석자 누락")
        response = input("     참석자 입력 (1인 식사면 본인 이름 / 's' = skip): ").strip()
        if response.lower() == 's' or not response:
            issue.resolved = True
            issue.resolution = "skipped"
            print("     → 건너뜀")
            return
        ok, tag = await self._set_attendee_on_row(row_num, response, plan)
        issue.resolved = True
        issue.resolution = tag
        if ok:
            print(f"     ✅ 참석자 입력 완료: {response}")
        else:
            print(f"     ❌ {tag}")

    async def _prompt_fix_unequal_pair(self, issue: 'VerificationIssue', plan: ExecutionPlan) -> None:
        """Informational handler for unequal charge+cancel pairs (Rule 3 partial refund)."""
        charge_row = issue.row_numbers[0]
        cancel_row = issue.paired_row_number
        print(f"\n  💱 Row {charge_row} + {cancel_row}: {issue.merchant} — 부분 환불 거래")
        print(f"     {issue.description}")
        print("     [s] 둘 다 건너뛰기 / [m] 수동 수정 (기본) / [k] 그대로 두기")
        response = input("     선택: ").strip().lower()
        if response == 's':
            issue.resolved = True
            issue.resolution = "skip_both"
            print("     → 둘 다 건너뛰기 (수동 확인 필요)")
        elif response == 'k':
            issue.resolved = True
            issue.resolution = "kept"
            print("     → 그대로 둠 (사용자 확인 완료)")
        else:
            issue.resolved = True
            issue.resolution = "manual_edit"
            print("     → 차액 청구로 수동 수정 필요. /douzonebot:troubleshoot 로 row 수정 가능")

    async def _prompt_fix_entertainment_format(self, issue: 'VerificationIssue', plan: ExecutionPlan) -> None:
        """Interactive fix for 접대비 attendee missing 소속/직급 (Rule 8)."""
        row_num = issue.row_numbers[0]
        print(f"\n  🎭 Row {row_num}: {issue.merchant} — 접대비 형식 (소속/직급/성명)")
        print(f"     예: '신우회계법인 김은서 과장' 또는 '(주)ABC 김민수 부장'")
        response = input("     참석자 재입력 (또는 's' = skip): ").strip()
        if response.lower() == 's' or not response:
            issue.resolved = True
            issue.resolution = "skipped"
            print("     → 건너뜀")
            return
        ok, tag = await self._set_attendee_on_row(row_num, response, plan)
        issue.resolved = True
        issue.resolution = tag
        if ok:
            print(f"     ✅ 참석자 재입력 완료: {response}")
        else:
            print(f"     ❌ {tag}")

    async def post_verify(self, plan: ExecutionPlan) -> 'PostVerificationResult':
        """
        Run post-verification scan on completed execution plan.
        Detects issues, prints report, offers interactive fixes.
        """
        # Uses PostVerificationResult, VerificationIssueType from top-level import

        result = PostVerificationResult(
            started_at=datetime.now().isoformat(),
        )

        # Scan for issues
        issues = self._scan_all_issues(plan)
        result.issues = issues
        result.total_issues = len(issues)
        result.passed = len(issues) == 0

        # Print report
        self._print_verification_report(issues)

        # Interactive fixes (only if stdin is a TTY)
        # When running non-interactively (e.g., agent-driven via /douzonebot:go),
        # skip prompts — the agent reads the report and can use operations.py
        # (attach_receipt, fill_supplier, edit_row) to fix issues conversationally.
        # Use --skip-post-verify to skip the entire stage.
        is_interactive = sys.stdin.isatty()
        if issues and not is_interactive:
            print(f"\n{'─'*40}")
            print("   비대화형 모드: 수정 프롬프트 생략")
            print(f"   에이전트가 위 리포트를 읽고 operations.py로 수정할 수 있습니다.")
            print(f"{'─'*40}")
            for issue in issues:
                issue.resolved = False
                issue.resolution = "non_interactive_skip"
        elif issues:
            print(f"\n{'─'*40}")
            print("🔧 Interactive Fix")
            print(f"{'─'*40}")

            fix_handlers = {
                VerificationIssueType.PG_MISSING_RECEIPT.value: self._prompt_fix_receipt,
                VerificationIssueType.PG_MISSING_SUPPLIER.value: self._prompt_fix_supplier,
                VerificationIssueType.CHARGE_CANCEL_PAIR.value: self._prompt_fix_pair,
                VerificationIssueType.PARKING_OVER_CAP.value: self._prompt_fix_parking,
                VerificationIssueType.UNKNOWN_PATTERN.value: self._prompt_unknown_pattern,
                VerificationIssueType.MISSING_ATTENDEE.value: self._prompt_fix_missing_attendee,
                VerificationIssueType.CHARGE_CANCEL_UNEQUAL.value: self._prompt_fix_unequal_pair,
                VerificationIssueType.ENTERTAINMENT_FORMAT.value: self._prompt_fix_entertainment_format,
            }

            for issue in issues:
                handler = fix_handlers.get(issue.issue_type)
                if handler:
                    try:
                        await handler(issue, plan)
                    except (KeyboardInterrupt, EOFError):
                        print("\n     → 중단됨")
                        break
                    except Exception as e:
                        logger.error(f"Fix handler error for row {issue.row_numbers}: {e}")
                        issue.resolved = True
                        issue.resolution = f"error: {e}"

                # Save after each fix
                if self.execution_plan_path:
                    plan.post_verification = result.to_dict()
                    with open(self.execution_plan_path, "w", encoding="utf-8") as f:
                        json.dump(plan.to_dict(), f, ensure_ascii=False, indent=2)
        result.resolved_issues = sum(1 for i in issues if i.resolved and i.resolution == "fixed")
        result.completed_at = datetime.now().isoformat()

        # Persist final results
        plan.post_verification = result.to_dict()
        if self.execution_plan_path:
            with open(self.execution_plan_path, "w", encoding="utf-8") as f:
                json.dump(plan.to_dict(), f, ensure_ascii=False, indent=2)

        return result

    # =========================================================================
    # MAIN RUNNER
    # =========================================================================

    async def run(self, skip_transactions: bool = False) -> Dict[str, Any]:
        """
        Run the complete MVP flow.
        
        Args:
            skip_transactions: If True, skip Douzone parsing (for testing without browser)
            
        Returns:
            Result dictionary
        """
        try:
            if self.receipts_only:
                await self.collect_receipts_only()
                return {"receipts_only": True}

            if self.stage3_cache_in:
                print(f"\n[cache] Loading Stage 3 cache: {self.stage3_cache_in}")
                self._load_stage3_cache(self.stage3_cache_in)
                self.execution_plan_path = self.stage3_cache_in
                if self.stage3_only:
                    print("\nStage 3 cache mode: stopping after cache load.")
                    return {"stage3_only": True, "execution_plan": self.execution_plan}
                if skip_transactions:
                    print("\n⚠️ Test mode: Skipping actual automation")
                    return {"test_mode": True, "execution_plan": self.execution_plan}
                result = await self.execute()

                # Post-verification
                pv_summary = ""
                if not self.skip_post_verify and result.get("success", 0) > 0:
                    pv_result = await self.post_verify(self.execution_plan)
                    result["post_verification"] = pv_result.to_dict()
                    if pv_result.total_issues > 0:
                        pv_summary = f" | Post-verify: {pv_result.resolved_issues}/{pv_result.total_issues} resolved"
                    else:
                        pv_summary = " | Post-verify: passed"

                print("\n" + "="*60)
                if result["success"] == result["total"]:
                    print(f"🎉 ALL DONE! Expense claim automation complete.{pv_summary}")
                else:
                    print(f"⚠️ Completed with some failures. Check logs.{pv_summary}")
                print("="*60)
                return result

            # Stage 1: Collect data
            await self.collect_data(skip_transactions=skip_transactions)

            if self.stage1_only:
                print("\nStage 1 only mode: stopping after data collection.")
                return {"stage1_only": True}
            
            if not skip_transactions and not self.transactions:
                print("\n❌ No transactions found. Aborting.")
                return {"error": "No transactions found"}
            
            # For testing without transactions, create dummy data
            if skip_transactions:
                print("\n⚠️ Test mode: Creating dummy transaction data...")
                await self._create_dummy_transactions()
            
            # Stage 2: Match data
            await self.match_data()

            if self.stage2_only:
                total = self.plan.total_rows if self.plan else 0
                high = sum(1 for r in (self.plan.rows if self.plan else []) if r.confidence == "HIGH")
                low = sum(1 for r in (self.plan.rows if self.plan else []) if r.confidence == "LOW")
                needs_clarify = sum(1 for r in (self.plan.rows if self.plan else []) if r.needs_clarification)
                has_receipt = sum(1 for r in (self.plan.rows if self.plan else []) if r.receipt_paths)
                sys.stderr.write(f"\nSTAGE2_OK=true\n")
                sys.stderr.write(f"STAGE2_TOTAL={total}\n")
                sys.stderr.write(f"STAGE2_HIGH={high}\n")
                sys.stderr.write(f"STAGE2_LOW={low}\n")
                sys.stderr.write(f"STAGE2_NEEDS_CLARIFICATION={needs_clarify}\n")
                sys.stderr.write(f"STAGE2_RECEIPTS={has_receipt}\n")
                return {"stage2_only": True, "plan": self.plan}

            # Stage 3+4: Review & Confirm (merged)
            approved = await self.review_and_confirm()

            if self.review_only:
                return {"review_only": True, "plan_rows": self.plan.total_rows}

            if self.stage3_only:
                print("\nStage 3 only mode: stopping after review.")
                return {"stage3_only": True, "execution_plan": self.execution_plan}

            if not approved:
                print("\n❌ User cancelled. Aborting.")
                return {"error": "User cancelled"}

            # Stage 5: Execute
            if skip_transactions:
                print("\n⚠️ Test mode: Skipping actual automation")
                return {"test_mode": True, "execution_plan": self.execution_plan}
            
            result = await self.execute()

            # Post-verification
            pv_summary = ""
            if not self.skip_post_verify and result.get("success", 0) > 0:
                pv_result = await self.post_verify(self.execution_plan)
                result["post_verification"] = pv_result.to_dict()
                if pv_result.total_issues > 0:
                    pv_summary = f" | Post-verify: {pv_result.resolved_issues}/{pv_result.total_issues} resolved"
                else:
                    pv_summary = " | Post-verify: passed"

            success = result.get("success", 0)
            total = result.get("total", 0)
            failed = total - success

            sys.stderr.write("\n" + "="*60 + "\n")
            if success == total:
                sys.stderr.write(f"🎉 ALL DONE! Expense claim automation complete.{pv_summary}\n")
            else:
                sys.stderr.write(f"⚠️ Completed with some failures. Check logs.{pv_summary}\n")
            sys.stderr.write("="*60 + "\n")

            # Machine-parseable summary
            if success == total:
                result_tag = "success"
            elif success == 0:
                result_tag = "failed"
            else:
                result_tag = "partial"
            sys.stderr.write(f"RESULT={result_tag} SUCCESS={success} FAILED={failed} TOTAL={total}\n")

            return result
            
        finally:
            if self.automation:
                await self.automation.close()
    
    async def run_to_matching(
        self,
        user_name: str,
        memo_path: Optional[str] = None,
        receipts_path: Optional[str] = None,
        cdp_url: str = "http://localhost:9222"
    ) -> ProcessingPlan:
        """
        Run Stage 1 and Stage 2 and return the processing plan.
        This is used by the UI to show the plan before automation.
        """
        self.user_name = user_name
        self.memo_path = memo_path
        self.receipts_path = receipts_path
        self.cdp_url = cdp_url
        
        # Stage 1: Collect data
        await self.collect_data()
        
        # Stage 2: Match data
        plan = await self.match_data()
        return plan

    async def run_automation_single_row(
        self,
        row: Any,  # MatchedRow or similar
        cdp_url: str = "http://localhost:9222"
    ) -> bool:
        """
        Execute automation for a single row.
        Used by the UI to process rows one by one with progress updates.
        """
        self.cdp_url = cdp_url
        
        if not self.automation:
            self.automation = DouzoneAutomation(self.cdp_url)
            await self.automation.connect()
            
        # row can be a MatchedRow or a RowAction from ExecutionPlan
        # Let's handle both
        if hasattr(row, 'to_expense_data'):
            expense_data = row.to_expense_data()
            row_index = row.row_index
        elif hasattr(row, 'fill_data'):
            # It's a RowAction
            expense_data = self._row_action_to_expense_data(row)
            row_index = row.row_number - 1
        else:
            # Try dictionary access
            if isinstance(row, dict):
                # We need to reconstruct or handle dict
                pass
            raise ValueError(f"Unsupported row type: {type(row)}")

        if not expense_data:
            return False
            
        success = await self.automation.process_row(row_index, expense_data)
        return success

    async def _create_dummy_transactions(self):
        """Create dummy transactions from receipt data for testing."""
        from .transaction_parser import Transaction, TransactionList
        
        transactions = []
        row_num = 1
        
        for path, receipt in self.receipts.items():
            if receipt.transaction:
                tx = receipt.transaction
                transactions.append(Transaction(
                    row_num=row_num,
                    date_time=f"{tx.date} {tx.time}",
                    merchant=receipt.vendor_info.name if receipt.vendor_info else "Unknown",
                    amount=tx.amount,
                    status="참석자 미입력",
                ))
                row_num += 1
        
        self.transactions = TransactionList(
            transactions=transactions,
            total_amount=sum(t.amount for t in transactions),
        )
        
        print(f"   Created {len(transactions)} dummy transactions from receipts")


# ============================================================================
# CONVENIENCE FUNCTION
# ============================================================================

async def run_mvp(
    user_name: str,
    memo_path: Optional[str] = None,
    receipts_path: Optional[str] = None,
    cdp_url: str = "http://localhost:9222",
    test_mode: bool = False,
    auto_approve: bool = False,
    stage1_cache_in: Optional[str] = None,
    stage1_cache_out: Optional[str] = None,
    stage1_report_out: Optional[str] = None,
    stage1_only: bool = False,
    receipts_only: bool = False,
    receipt_provider: str = "auto",
    receipt_cache: Optional[str] = None,
    max_rows: int = 0,
    stage2_cache_in: Optional[str] = None,
    stage2_cache_out: Optional[str] = None,
    stage2_only: bool = False,
    stage3_cache_in: Optional[str] = None,
    stage3_cache_out: Optional[str] = None,
    stage3_only: bool = False,
    review_only: bool = False,
    skip_post_verify: bool = False,
    lost_receipt_rows: Optional[Set[int]] = None,
    only_rows: Optional[Set[int]] = None,
    provider=None,
) -> Dict[str, Any]:
    """
    Run the MVP automation flow.

    Args:
        user_name: Default attendee name
        memo_path: Path to memo.txt
        receipts_path: Path to receipts folder
        cdp_url: Chrome DevTools URL
        test_mode: If True, skip browser interaction
        auto_approve: If True, skip interactive prompts
        stage1_cache_in: Load Stage 1 cache from this path (debug only)
        stage1_cache_out: Save Stage 1 cache to this path (debug only)
        stage1_report_out: Write Stage 1 report to this path (debug only)
        stage1_only: Stop after Stage 1 (debug only)
        receipts_only: Run receipt OCR only (debug only)
        receipt_provider: Receipt OCR provider override (debug only)
        receipt_cache: Path to receipt OCR cache file (load if exists, save after OCR)
        max_rows: Limit number of rows to process (0 = all)
        stage2_cache_in: Load Stage 2 matching results from this path (debug only)
        stage2_cache_out: Save Stage 2 matching results to this path (debug only)
        stage2_only: Stop after Stage 2 matching (debug only)
        stage3_cache_in: Load Stage 3 reviewed plan from this path (debug only)
        stage3_cache_out: Save Stage 3 reviewed plan to this path (debug only)
        stage3_only: Stop after Stage 3 review (debug only)
        review_only: Display review without prompts or execution
        skip_post_verify: Skip post-verification stage

    Returns:
        Result dictionary
    """
    if stage1_only and not stage1_cache_in and not stage1_cache_out:
        stage1_cache_out = DEFAULT_STAGE1_CACHE_PATH
    if stage1_only and not stage1_report_out:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stage1_report_out = os.path.join(DEFAULT_STAGE1_REPORT_DIR, f"stage1_report_{timestamp}.md")
    if receipts_only and not stage1_report_out:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stage1_report_out = os.path.join(DEFAULT_STAGE1_REPORT_DIR, f"receipt_ocr_report_{timestamp}.md")
    if stage3_only and not stage3_cache_out:
        stage3_cache_out = DEFAULT_STAGE3_CACHE_PATH

    orchestrator = MVPOrchestrator(
        user_name=user_name,
        memo_path=memo_path,
        receipts_path=receipts_path,
        cdp_url=cdp_url,
        auto_approve=auto_approve,
        stage1_cache_in=stage1_cache_in,
        stage1_cache_out=stage1_cache_out,
        stage1_report_out=stage1_report_out,
        stage1_only=stage1_only,
        receipts_only=receipts_only,
        receipt_provider=receipt_provider,
        receipt_cache=receipt_cache,
        max_rows=max_rows,
        stage2_cache_in=stage2_cache_in,
        stage2_cache_out=stage2_cache_out,
        stage3_cache_in=stage3_cache_in,
        stage3_cache_out=stage3_cache_out,
        provider=provider,
    )
    orchestrator.stage2_only = stage2_only
    orchestrator.stage3_only = stage3_only
    orchestrator.review_only = review_only
    orchestrator.skip_post_verify = skip_post_verify
    orchestrator.lost_receipt_rows = lost_receipt_rows or set()
    orchestrator.only_rows = only_rows if only_rows else None

    return await orchestrator.run(skip_transactions=test_mode)
