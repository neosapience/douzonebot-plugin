# Douzone Expense Automation Package
from .automation import DouzoneAutomation
from .models import ExpenseData, GridSnapshot, RowInfo, ProcessingResult, RowStatus
from .proxy import TunnelCDPProxy
from .pipeline import (
    process_receipt_for_expense,
    enrich_expense_with_receipt,
    extract_supplier_info,
    is_pg_merchant,
    is_ocr_available,
    parse_memo,
    MemoData,
    match_items_to_transactions,
    filter_matches_for_review,
    CardTransaction,
    MatchResult,
)
from .ocr import extract_receipt, ReceiptData, ClaudeCodeReceiptExtractor

__all__ = [
    # Automation
    'DouzoneAutomation',
    'TunnelCDPProxy',
    
    # Data Models
    'ExpenseData',
    'GridSnapshot',
    'RowInfo',
    'RowStatus',
    'ProcessingResult',
    
    # OCR
    'extract_receipt',
    'ReceiptData',
    'ClaudeCodeReceiptExtractor',
    
    # Pipeline (OCR + NLP + Automation integration)
    'process_receipt_for_expense',
    'enrich_expense_with_receipt',
    'extract_supplier_info',
    'is_pg_merchant',
    'is_ocr_available',
    'parse_memo',
    'MemoData',
    'match_items_to_transactions',
    'filter_matches_for_review',
    'CardTransaction',
    'MatchResult',
]
