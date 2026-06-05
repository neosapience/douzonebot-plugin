"""
Data models for Douzone Expense Automation.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum


class RowStatus(Enum):
    """Status of a row in the expense grid."""
    PENDING = "pending"
    COMPLETED = "완료"
    UNKNOWN = "unknown"


@dataclass
class RowInfo:
    """Information about a single row in the expense grid (from Vision analysis)."""
    y: int  # Y coordinate of the row (for clicking '+' button)
    status: RowStatus
    merchant: str  # 사용처 (가맹점명)
    amount: Optional[str] = None  # 청구금액

    @classmethod
    def from_dict(cls, data: dict) -> 'RowInfo':
        """Create RowInfo from Vision API response dict."""
        status_str = data.get('status', 'unknown')
        if status_str in ('pending', '미처리'):
            status = RowStatus.PENDING
        elif status_str in ('completed', '완료'):
            status = RowStatus.COMPLETED
        else:
            status = RowStatus.UNKNOWN

        return cls(
            y=data.get('y', 0),
            status=status,
            merchant=data.get('merchant', ''),
            amount=data.get('amount'),
        )


@dataclass
class GridSnapshot:
    """Snapshot of the visible grid from Vision analysis."""
    plus_button_x: int  # X coordinate of '+' buttons (same for all rows)
    rows: List[RowInfo]
    has_scroll_above: bool = False
    has_scroll_below: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> 'GridSnapshot':
        """Create GridSnapshot from Vision API response dict."""
        rows = [RowInfo.from_dict(r) for r in data.get('rows', [])]
        scroll = data.get('scroll', {})

        return cls(
            plus_button_x=data.get('plus_button_x', 0),
            rows=rows,
            has_scroll_above=scroll.get('has_above', False),
            has_scroll_below=scroll.get('has_below', False),
        )

    @property
    def pending_rows(self) -> List[RowInfo]:
        """Get only pending (unprocessed) rows."""
        return [r for r in self.rows if r.status == RowStatus.PENDING]


@dataclass
class ExpenseData:
    """
    User-provided data for filling an expense row.
    Matched to grid rows by merchant name.
    """
    merchant: str  # 사용처 - used for matching
    yongdo: str  # 용도 code or name (e.g., "중식대", "간식/음료")
    content: str  # 내용
    attendees: str  # 참석자

    # Optional: for 배민/PG transactions
    supplier_name: Optional[str] = None  # 실공급자상호
    supplier_biz_no: Optional[str] = None  # 실공급자 사업자등록번호

    # Optional: for receipt upload (supports multiple receipts per transaction)
    receipt_paths: List[str] = field(default_factory=list)

    # Optional: for pending receipts (when receipt is missing)
    pending_reason: Optional[str] = None  # Reason for missing receipt (e.g., "영수증 분실")

    # Optional: 비고 notes to fill (from Stage 2 matching)
    bigo_notes: List[str] = field(default_factory=list)

    # Flags for automation - whether to fill 용도/내용 in grid (before popup)
    needs_yongdo: bool = False  # True if 용도 column needs to be filled
    needs_content: bool = False  # True if 내용 column needs to be filled

    # True when merchant is in SUPPLIER_REQUIRED_MERCHANTS (배민/백화점/PG 등).
    # Set by orchestrator._build_execution_plan based on merchant rules —
    # independent of whether supplier data was actually extracted.
    requires_supplier_info: bool = False

    @property
    def has_supplier_data(self) -> bool:
        """Whether OCR/matching produced any supplier info for this row."""
        return bool(self.supplier_name or self.supplier_biz_no)

    @property
    def needs_supplier_info(self) -> bool:
        """Deprecated alias — historically checked data presence, not requirement.
        Kept for backward compat; new code should use has_supplier_data or
        requires_supplier_info explicitly."""
        return self.has_supplier_data

    @property
    def has_receipt(self) -> bool:
        """Check if this expense has receipt file(s)."""
        import os
        return bool(self.receipt_paths and any(os.path.exists(p) for p in self.receipt_paths))


@dataclass
class ProcessingResult:
    """Result of processing expense rows."""
    total_rows: int = 0
    processed_rows: int = 0
    skipped_rows: int = 0
    failed_rows: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        """Calculate success rate as percentage."""
        if self.total_rows == 0:
            return 0.0
        return (self.processed_rows / self.total_rows) * 100


# Column order in the Douzone grid (for keyboard navigation)
# Based on actual screenshot analysis
COLUMN_ORDER = [
    '선택',      # 0: Checkbox
    '사용일시',   # 1: Date/Time
    '사용처',    # 2: Merchant
    '결제방법',   # 3: Payment method
    '청구금액',   # 4: Amount
    '용도',      # 5: Purpose/Category
    '내용',      # 6: Content/Description
    '검증결과',   # 7: Validation result
    '추가항목',   # 8: Additional items (folder icon)
    '첨부파일',   # 9: Attachments
]

def get_column_index(column_name: str) -> int:
    """Get the index of a column by name."""
    try:
        return COLUMN_ORDER.index(column_name)
    except ValueError:
        raise ValueError(f"Unknown column: {column_name}. Valid columns: {COLUMN_ORDER}")


# ============================================================================
# STAGE 3 Output Models (Execution Plan)
# ============================================================================

class VerificationIssueType(Enum):
    """Type of issue detected during post-verification."""
    PG_MISSING_RECEIPT = "pg_missing_receipt"
    PG_MISSING_SUPPLIER = "pg_missing_supplier"
    CHARGE_CANCEL_PAIR = "charge_cancel_pair"
    PARKING_OVER_CAP = "parking_over_cap"
    UNKNOWN_PATTERN = "unknown_pattern"
    MISSING_ATTENDEE = "missing_attendee"
    CHARGE_CANCEL_UNEQUAL = "charge_cancel_unequal"
    ENTERTAINMENT_FORMAT = "entertainment_format"
    CANCEL_ONLY = "cancel_only"


@dataclass
class VerificationIssue:
    """A single issue detected during post-verification."""
    issue_type: str              # VerificationIssueType value
    row_numbers: List[int]       # Affected row(s) (1-based)
    merchant: str
    amount: int                  # Transaction amount (won)
    description: str             # Human-readable description
    resolved: bool = False
    resolution: Optional[str] = None  # What user chose (e.g., "skipped", "fixed", "acknowledged")
    paired_row_number: Optional[int] = None  # For charge+cancel pairs

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_type": self.issue_type,
            "row_numbers": self.row_numbers,
            "merchant": self.merchant,
            "amount": self.amount,
            "description": self.description,
            "resolved": self.resolved,
            "resolution": self.resolution,
            "paired_row_number": self.paired_row_number,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'VerificationIssue':
        return cls(
            issue_type=data["issue_type"],
            row_numbers=data["row_numbers"],
            merchant=data["merchant"],
            amount=data.get("amount", 0),
            description=data["description"],
            resolved=data.get("resolved", False),
            resolution=data.get("resolution"),
            paired_row_number=data.get("paired_row_number"),
        )


@dataclass
class PostVerificationResult:
    """Result of the post-verification scan."""
    started_at: str
    completed_at: Optional[str] = None
    total_issues: int = 0
    resolved_issues: int = 0
    issues: List[VerificationIssue] = field(default_factory=list)
    passed: bool = True  # True if no issues found

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "total_issues": self.total_issues,
            "resolved_issues": self.resolved_issues,
            "issues": [i.to_dict() for i in self.issues],
            "passed": self.passed,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PostVerificationResult':
        return cls(
            started_at=data["started_at"],
            completed_at=data.get("completed_at"),
            total_issues=data.get("total_issues", 0),
            resolved_issues=data.get("resolved_issues", 0),
            issues=[VerificationIssue.from_dict(i) for i in data.get("issues", [])],
            passed=data.get("passed", True),
        )


class ActionType(Enum):
    """Type of action to take for a row in STAGE 4."""
    AUTO_FILL = "auto_fill"           # High confidence match, auto-process
    USER_CONFIRMED = "user_confirmed"  # Low confidence but user selected
    SKIP = "skip"                      # No match, user chose to skip
    ALREADY_FILLED = "already_filled"  # Row status was '완료' in STAGE 1


class ExecutionStatus(Enum):
    """Execution status for a row during STAGE 4."""
    PENDING = "pending"       # Not yet processed
    PROCESSING = "processing" # Currently being processed
    SUCCESS = "success"       # Successfully filled
    FAILED = "failed"         # Failed to fill (error)
    SKIPPED = "skipped"       # Intentionally skipped (action=SKIP or ALREADY_FILLED)


@dataclass
class RowAction:
    """Complete action plan for a single transaction row."""

    # ============ Core Identification ============
    row_number: int  # 1-30 (Douzone row index, 1-based)

    # ============ Transaction Info (from STAGE 1) ============
    transaction: Dict[str, Any]  # CardTransaction as dict

    # ============ Match Results (from STAGE 2) ============
    matched_receipts: List[Dict[str, Any]] = field(default_factory=list)  # MatchResult as dict
    matched_memo: Optional[Dict[str, Any]] = None  # MatchResult as dict

    # ============ Action Decision (from STAGE 3 user review) ============
    action: str = ActionType.SKIP.value  # ActionType value

    # ============ Fill Data (combined from matches + user input) ============
    fill_data: Optional[Dict[str, Any]] = None  # ExpenseData as dict

    # ============ Additional Notes ============
    bigo_notes: List[str] = field(default_factory=list)
    skip_reason: Optional[str] = None

    # ============ Execution Tracking (updated during STAGE 4) ============
    execution_status: str = ExecutionStatus.PENDING.value
    execution_timestamp: Optional[str] = None  # ISO format
    execution_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "row_number": self.row_number,
            "transaction": self.transaction,
            "matched_receipts": self.matched_receipts,
            "matched_memo": self.matched_memo,
            "action": self.action,
            "fill_data": self.fill_data,
            "bigo_notes": self.bigo_notes,
            "skip_reason": self.skip_reason,
            "execution_status": self.execution_status,
            "execution_timestamp": self.execution_timestamp,
            "execution_error": self.execution_error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RowAction':
        """Create RowAction from dictionary."""
        return cls(
            row_number=data["row_number"],
            transaction=data["transaction"],
            matched_receipts=data.get("matched_receipts") or ([data["matched_receipt"]] if data.get("matched_receipt") else []),
            matched_memo=data.get("matched_memo"),
            action=data.get("action", ActionType.SKIP.value),
            fill_data=data.get("fill_data"),
            bigo_notes=data.get("bigo_notes", []),
            skip_reason=data.get("skip_reason"),
            execution_status=data.get("execution_status", ExecutionStatus.PENDING.value),
            execution_timestamp=data.get("execution_timestamp"),
            execution_error=data.get("execution_error"),
        )


@dataclass
class ExecutionPlan:
    """Complete execution plan after STAGE 3 user review."""

    # ============ Metadata ============
    user_name: str
    created_at: str  # ISO format
    stage1_cache_path: str

    # ============ Statistics ============
    total_rows: int
    rows_to_process: int
    rows_to_skip: int
    rows_already_filled: int

    # ============ Row Actions ============
    rows: List[RowAction] = field(default_factory=list)

    # ============ STAGE 4 Progress Tracking ============
    stage4_started_at: Optional[str] = None
    stage4_completed_at: Optional[str] = None
    stage4_success_count: int = 0
    stage4_failed_count: int = 0
    stage4_skipped_count: int = 0

    # ============ Post-Verification (Stage 5) ============
    post_verification: Optional[Dict[str, Any]] = None

    @property
    def is_complete(self) -> bool:
        """Check if all processable rows have been executed."""
        return self.stage4_success_count + self.stage4_failed_count >= self.rows_to_process

    @property
    def success_rate(self) -> float:
        """Calculate success rate percentage."""
        if self.rows_to_process == 0:
            return 100.0
        return (self.stage4_success_count / self.rows_to_process) * 100

    @property
    def rows_pending(self) -> List[RowAction]:
        """Get rows that are still pending."""
        return [r for r in self.rows if r.execution_status == ExecutionStatus.PENDING.value]

    @property
    def rows_successful(self) -> List[RowAction]:
        """Get rows that were successfully processed."""
        return [r for r in self.rows if r.execution_status == ExecutionStatus.SUCCESS.value]

    @property
    def rows_failed(self) -> List[RowAction]:
        """Get rows that failed processing."""
        return [r for r in self.rows if r.execution_status == ExecutionStatus.FAILED.value]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "user_name": self.user_name,
            "created_at": self.created_at,
            "stage1_cache_path": self.stage1_cache_path,
            "total_rows": self.total_rows,
            "rows_to_process": self.rows_to_process,
            "rows_to_skip": self.rows_to_skip,
            "rows_already_filled": self.rows_already_filled,
            "rows": [r.to_dict() for r in self.rows],
            "stage4_started_at": self.stage4_started_at,
            "stage4_completed_at": self.stage4_completed_at,
            "stage4_success_count": self.stage4_success_count,
            "stage4_failed_count": self.stage4_failed_count,
            "stage4_skipped_count": self.stage4_skipped_count,
            "post_verification": self.post_verification,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ExecutionPlan':
        """Create ExecutionPlan from dictionary."""
        return cls(
            user_name=data["user_name"],
            created_at=data["created_at"],
            stage1_cache_path=data["stage1_cache_path"],
            total_rows=data["total_rows"],
            rows_to_process=data["rows_to_process"],
            rows_to_skip=data["rows_to_skip"],
            rows_already_filled=data["rows_already_filled"],
            rows=[RowAction.from_dict(r) for r in data.get("rows", [])],
            stage4_started_at=data.get("stage4_started_at"),
            stage4_completed_at=data.get("stage4_completed_at"),
            stage4_success_count=data.get("stage4_success_count", 0),
            stage4_failed_count=data.get("stage4_failed_count", 0),
            stage4_skipped_count=data.get("stage4_skipped_count", 0),
            post_verification=data.get("post_verification"),
        )
