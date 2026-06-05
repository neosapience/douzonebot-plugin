"""
Receipt OCR & Information Extraction Module

Uses AI Vision APIs to extract vendor information from receipt images.
Key distinction: Platform/PG info (shown on card statement) vs Real Vendor info (needed for tax filing).

Supported providers:
- Qwen2.5-VL OCR API (primary, on-premise)
- Google Gemini API (fallback, requires GOOGLE_API_KEY)
- Claude Code CLI (fallback - uses existing subscription, no API key needed)
- Anthropic API (fallback, requires ANTHROPIC_API_KEY)

Usage:
    from src.ocr import ClaudeCodeReceiptExtractor
    
    extractor = ClaudeCodeReceiptExtractor()
    result = await extractor.extract("path/to/receipt.jpg")
    
    print(f"Real Vendor: {result.vendor_info.name}")
    print(f"Business Number: {result.vendor_info.biz_num}")
"""

import asyncio
import base64
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Check if CLI tools are available
CLAUDE_CODE_AVAILABLE = shutil.which("claude") is not None
GEMINI_CLI_AVAILABLE = shutil.which("gemini") is not None

# Try to import AI SDKs
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

try:
    from ocr_api.client import OCRClient
    QWEN25VL_AVAILABLE = True
except Exception:
    QWEN25VL_AVAILABLE = False

OPENROUTER_AVAILABLE = True  # Always available if httpx is installed (checked at runtime)

# Pre-prepared OCR companion file extensions (checked in priority order)
PREOCR_EXTENSIONS = ['.ocr.md', '.ocr.txt', '.ocr.json']


def find_preocr_file(image_path: str) -> Optional[str]:
    """Check if a pre-prepared OCR text file exists alongside an image.

    Scans for companion files in two patterns (priority order):
      1. image.jpg.ocr.md  (extension appended to full filename)
      2. image.ocr.md      (image extension stripped first)

    Args:
        image_path: Path to the receipt image file.

    Returns:
        Path to the companion OCR file if found, None otherwise.
    """
    p = Path(image_path)
    stem_path = str(p.parent / p.stem)  # e.g., /path/to/receipt_a

    for ext in PREOCR_EXTENSIONS:
        # Pattern 1: receipt_a.jpg.ocr.md
        candidate = Path(str(image_path) + ext)
        if candidate.is_file():
            return str(candidate)
        # Pattern 2: receipt_a.ocr.md (image extension stripped)
        candidate = Path(stem_path + ext)
        if candidate.is_file():
            return str(candidate)
    return None


def _parse_json_response(response: str) -> dict:
    """Parse JSON from LLM response, handling formatting issues.

    Tries: direct parse → ```json block → raw {…} object.
    """
    try:
        return json.loads(response)
    except (json.JSONDecodeError, TypeError):
        pass

    if not response:
        return {"is_receipt": False, "confidence": "low"}

    # Try markdown code block
    json_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', response)
    if json_block_match:
        try:
            return json.loads(json_block_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try raw JSON object
    json_match = re.search(r'\{[\s\S]*\}', response)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    logger.warning("Could not parse JSON from response")
    return {"is_receipt": False, "confidence": "low"}


def _is_qwen_reachable(host: str = "200.168.0.41", port: int = 8810, timeout: float = 2.0) -> bool:
    """Quick check if Qwen2.5-VL OCR API server is reachable (fast fail for local mode)."""
    import socket
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


@dataclass
class BusinessInfo:
    """Business/vendor information extracted from receipt."""
    name: Optional[str] = None          # 상호명
    biz_num: Optional[str] = None       # 사업자등록번호 (XXX-XX-XXXXX format)
    address: Optional[str] = None       # 주소
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "biz_num": self.biz_num,
            "address": self.address,
        }


@dataclass
class TransactionInfo:
    """Transaction details extracted from receipt."""
    date: Optional[str] = None          # YYYY-MM-DD
    time: Optional[str] = None          # HH:MM:SS
    amount: Optional[int] = None        # Amount in KRW
    currency: str = "KRW"
    
    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "time": self.time,
            "amount": self.amount,
            "currency": self.currency,
        }


@dataclass
class ReceiptData:
    """Complete receipt data extracted by OCR."""
    is_receipt: bool = False            # Validation: is this actually a receipt?
    platform_info: BusinessInfo = field(default_factory=BusinessInfo)   # PG/Platform (shown on card statement)
    vendor_info: BusinessInfo = field(default_factory=BusinessInfo)     # Real vendor (for tax filing)
    transaction: TransactionInfo = field(default_factory=TransactionInfo)
    raw_text: Optional[str] = None      # Raw OCR text (for debugging)
    confidence: str = "low"             # Extraction confidence: low, medium, high
    provider: Optional[str] = None      # OCR provider identifier
    model: Optional[str] = None         # Model identifier (if known)
    source_path: Optional[str] = None   # Path to the source image file

    def to_dict(self) -> dict:
        return {
            "is_receipt": self.is_receipt,
            "platform_info": self.platform_info.to_dict(),
            "vendor_info": self.vendor_info.to_dict(),
            "transaction": self.transaction.to_dict(),
            "confidence": self.confidence,
            "provider": self.provider,
            "model": self.model,
            "source_path": self.source_path,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'ReceiptData':
        """Create ReceiptData from API response dict."""
        result = cls()
        result.is_receipt = data.get("is_receipt", False)
        result.confidence = data.get("confidence", "low")
        result.provider = data.get("provider")
        result.model = data.get("model")
        
        if "platform_info" in data and data["platform_info"]:
            p = data["platform_info"]
            result.platform_info = BusinessInfo(
                name=p.get("name"),
                biz_num=p.get("biz_num"),
                address=p.get("address"),
            )
        
        if "vendor_info" in data and data["vendor_info"]:
            v = data["vendor_info"]
            result.vendor_info = BusinessInfo(
                name=v.get("name"),
                biz_num=v.get("biz_num"),
                address=v.get("address"),
            )
        
        if "transaction" in data and data["transaction"]:
            t = data["transaction"]
            result.transaction = TransactionInfo(
                date=t.get("date"),
                time=t.get("time"),
                amount=t.get("amount"),
                currency=t.get("currency", "KRW"),
            )

        result.source_path = data.get("source_path")

        return result


# System prompt for receipt extraction
RECEIPT_EXTRACTION_PROMPT = """You are a receipt information extraction specialist. Your task is to extract vendor and transaction information from Korean receipt images.

CRITICAL DISTINCTION:
- "가맹점 정보" (Merchant/Platform Info): This is often the PAYMENT PLATFORM (e.g., 배달의민족, 카카오페이, NHN KCP). This appears on card statements.
- "판매자 정보" or "공급자 정보" (Vendor/Supplier Info): This is the ACTUAL SELLER who provided the goods/services. This is needed for tax filing.

Your job is to find the ACTUAL SELLER information, which is typically labeled as:
- "판매자" (Seller)
- "공급자" (Supplier)  
- "실제 판매자" (Actual Seller)
- "판매자 정보" (Seller Information)

Extract and return a JSON object with this exact structure:
{
  "is_receipt": true/false,  // Is this image actually a receipt?
  "platform_info": {         // The payment platform (if identifiable)
    "name": "string or null",
    "biz_num": "XXX-XX-XXXXX format or null",
    "address": "string or null"
  },
  "vendor_info": {           // The ACTUAL SELLER - most important!
    "name": "string or null",
    "biz_num": "XXX-XX-XXXXX format or null",  
    "address": "string or null"
  },
  "transaction": {
    "date": "YYYY-MM-DD or null",
    "time": "HH:MM:SS or null",
    "amount": number or null,
    "currency": "KRW"
  },
  "confidence": "low" | "medium" | "high"  // How confident are you in the extraction?
}

IMPORTANT RULES:
1. Business registration numbers (사업자등록번호) must be in XXX-XX-XXXXX format.
2. If you cannot find a field, set it to null.
3. For delivery app receipts (배달의민족, 쿠팡이츠, 요기요), the vendor_info should be the RESTAURANT, not the app company.
4. Set confidence to "high" only if you clearly found labeled sections for vendor info.
5. Return ONLY the JSON object, no additional text."""


# Prompt for text-based receipt parsing (pre-prepared OCR)
PREOCR_TEXT_EXTRACTION_PROMPT = """You are a receipt information extraction specialist. Your task is to extract vendor and transaction information from pre-extracted OCR text of a Korean receipt.

The following text was extracted from a receipt image by an OCR system. It may contain formatting artifacts, inconsistent spacing, or OCR errors.

--- BEGIN OCR TEXT ---
{ocr_text}
--- END OCR TEXT ---

CRITICAL DISTINCTION:
- "가맹점 정보" (Merchant/Platform Info): This is often the PAYMENT PLATFORM (e.g., 배달의민족, 카카오페이, NHN KCP). This appears on card statements.
- "판매자 정보" or "공급자 정보" (Vendor/Supplier Info): This is the ACTUAL SELLER who provided the goods/services. This is needed for tax filing.

Extract and return a JSON object with this exact structure:
{{
  "is_receipt": true/false,
  "platform_info": {{
    "name": "string or null",
    "biz_num": "XXX-XX-XXXXX format or null",
    "address": "string or null"
  }},
  "vendor_info": {{
    "name": "string or null",
    "biz_num": "XXX-XX-XXXXX format or null",
    "address": "string or null"
  }},
  "transaction": {{
    "date": "YYYY-MM-DD or null",
    "time": "HH:MM:SS or null",
    "amount": number or null,
    "currency": "KRW"
  }},
  "confidence": "low" | "medium" | "high"
}}

IMPORTANT RULES:
1. Business registration numbers (사업자등록번호) must be in XXX-XX-XXXXX format.
2. If you cannot find a field, set it to null.
3. For delivery app receipts (배달의민족, 쿠팡이츠, 요기요), the vendor_info should be the RESTAURANT, not the app company.
4. Set confidence to "high" only if the OCR text clearly contains labeled vendor information.
5. The "amount" should be the TOTAL amount (합계/총액), not individual item prices.
6. **Missing/unknown fields ≠ invalid receipt.** If fields are labeled "unknown", "N/A", "?", or left blank, treat them as null. As long as ANY recognizable receipt info (vendor name, amount, date, or biz number) is present, set is_receipt=true. Only set is_receipt=false if the text is clearly NOT a receipt (e.g., random notes, blank template with no real data).
7. Return ONLY the JSON object, no additional text."""


class ReceiptExtractor:
    """
    Extract vendor and transaction information from receipt images using Claude Vision.
    
    Usage:
        extractor = ReceiptExtractor(api_key="sk-ant-...")
        result = await extractor.extract("receipt.jpg")
        print(result.vendor_info.name)
    """
    
    def __init__(self, api_key: Optional[str] = None, model: str = "claude-sonnet-4-20250514"):
        """
        Initialize the extractor.
        
        Args:
            api_key: Anthropic API key. If not provided, reads from ANTHROPIC_API_KEY env var.
            model: Claude model to use. Default is claude-sonnet-4-20250514.
        """
        if not ANTHROPIC_AVAILABLE:
            raise ImportError("anthropic package is required. Install with: pip install anthropic")
        
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
    
    def _encode_image(self, image_path: str) -> Tuple[str, str]:
        """
        Encode image to base64 and detect media type.
        
        Returns:
            Tuple of (base64_data, media_type)
        """
        path = Path(image_path)
        
        # Detect media type
        suffix = path.suffix.lower()
        media_types = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
        }
        media_type = media_types.get(suffix, 'image/jpeg')
        
        # Read and encode
        with open(path, 'rb') as f:
            image_data = base64.standard_b64encode(f.read()).decode('utf-8')
        
        return image_data, media_type
    
    async def extract(self, image_path: str) -> ReceiptData:
        """
        Extract information from a receipt image.
        
        Args:
            image_path: Path to the receipt image file.
            
        Returns:
            ReceiptData with extracted information.
        """
        logger.info(f"Extracting receipt data from: {image_path}")
        
        # Encode image
        image_data, media_type = self._encode_image(image_path)
        
        # Call Claude Vision API
        try:
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
                                    "media_type": media_type,
                                    "data": image_data,
                                },
                            },
                            {
                                "type": "text",
                                "text": RECEIPT_EXTRACTION_PROMPT,
                            }
                        ],
                    }
                ],
            )
            
            # Parse response
            response_text = message.content[0].text
            logger.debug(f"API response: {response_text}")
            
            # Extract JSON from response
            result_data = self._parse_json_response(response_text)
            result = ReceiptData.from_dict(result_data)
            result.raw_text = response_text
            result.provider = "anthropic"
            result.model = self.model
            
            logger.info(f"Extraction complete. Vendor: {result.vendor_info.name}, Confidence: {result.confidence}")
            return result
            
        except Exception as e:
            logger.error(f"Failed to extract receipt data: {e}")
            raise
    
    def _parse_json_response(self, response: str) -> dict:
        """Parse JSON from Claude's response, handling potential formatting issues."""
        # Try direct parse first
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass
        
        # Try to find JSON block in response
        import re
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
        
        logger.warning("Could not parse JSON from response")
        return {"is_receipt": False, "confidence": "low"}
    
    def extract_sync(self, image_path: str) -> ReceiptData:
        """
        Synchronous version of extract() for convenience.
        
        Args:
            image_path: Path to the receipt image file.
            
        Returns:
            ReceiptData with extracted information.
        """
        import asyncio
        return asyncio.get_event_loop().run_until_complete(self.extract(image_path))


class GeminiReceiptExtractor:
    """
    Extract vendor and transaction information from receipt images using Google Gemini.
    
    Usage:
        extractor = GeminiReceiptExtractor(api_key="AIza...")
        result = await extractor.extract("receipt.jpg")
        print(result.vendor_info.name)
    """
    
    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-3-flash-preview"):
        """
        Initialize the extractor.
        
        Args:
            api_key: Google API key. If not provided, reads from GOOGLE_API_KEY env var.
            model: Gemini model to use. Default is gemini-2.0-flash.
        """
        if not GEMINI_AVAILABLE:
            raise ImportError("google-generativeai package is required. Install with: pip install google-generativeai")
        
        import os
        api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("Google API key is required. Set GOOGLE_API_KEY env var or pass api_key parameter.")
        
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model)
    
    def _load_image(self, image_path: str):
        """Load image for Gemini API."""
        import PIL.Image
        return PIL.Image.open(image_path)
    
    async def extract(self, image_path: str) -> ReceiptData:
        """
        Extract information from a receipt image.
        
        Args:
            image_path: Path to the receipt image file.
            
        Returns:
            ReceiptData with extracted information.
        """
        logger.info(f"Extracting receipt data from: {image_path}")
        
        # Load image
        image = self._load_image(image_path)
        
        # Call Gemini API
        try:
            response = self.model.generate_content([
                RECEIPT_EXTRACTION_PROMPT,
                image
            ])
            
            # Parse response
            response_text = response.text
            logger.debug(f"API response: {response_text}")
            
            # Extract JSON from response
            result_data = self._parse_json_response(response_text)
            result = ReceiptData.from_dict(result_data)
            result.raw_text = response_text
            result.provider = "gemini_api"
            result.model = self.model
            
            logger.info(f"Extraction complete. Vendor: {result.vendor_info.name}, Confidence: {result.confidence}")
            return result
            
        except Exception as e:
            logger.error(f"Failed to extract receipt data: {e}")
            raise
    
    def _parse_json_response(self, response: str) -> dict:
        """Parse JSON from Gemini's response, handling potential formatting issues."""
        import re
        
        # Try direct parse first
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass
        
        # Try to find JSON block in response (might be wrapped in ```json ... ```)
        json_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', response)
        if json_block_match:
            try:
                return json.loads(json_block_match.group(1))
            except json.JSONDecodeError:
                pass
        
        # Try to find raw JSON object
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
        
        logger.warning("Could not parse JSON from response")
        return {"is_receipt": False, "confidence": "low"}
    
    def extract_sync(self, image_path: str) -> ReceiptData:
        """Synchronous version of extract()."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(self.extract(image_path))


# Prompt for Qwen2.5-VL OCR API
QWEN_RECEIPT_EXTRACTION_PROMPT = """This is a Korean receipt image. Extract the following information and return ONLY a JSON object:

{
  "is_receipt": true/false,
  "platform_info": {
    "name": "string or null",
    "biz_num": "XXX-XX-XXXXX format or null"
  },
  "vendor_info": {
    "name": "string or null",
    "biz_num": "XXX-XX-XXXXX format or null",
    "address": "string or null"
  },
  "transaction": {
    "date": "YYYY-MM-DD or null",
    "time": "HH:MM:SS or null",
    "amount": number or null,
    "currency": "KRW"
  },
  "confidence": "low" | "medium" | "high"
}

CRITICAL RULES:
1. For delivery app receipts (배달의민족, 쿠팡이츠, 요기요), vendor_info should be the RESTAURANT, not the app company.
2. platform_info is the payment platform (우아한형제들, 카카오페이, NHN KCP, etc.)
3. Business registration numbers (사업자등록번호) must be in XXX-XX-XXXXX format.
4. Return ONLY the JSON object, no markdown or extra text."""


GEMINI_CLI_RECEIPT_PROMPT = """Read the file __FILENAME__. This is a Korean receipt image. Extract the following information and return ONLY a JSON object:

{
  "is_receipt": true/false,
  "platform_info": {
    "name": "string or null",
    "biz_num": "XXX-XX-XXXXX format or null"
  },
  "vendor_info": {
    "name": "string or null",
    "biz_num": "XXX-XX-XXXXX format or null",
    "address": "string or null"
  },
  "transaction": {
    "date": "YYYY-MM-DD or null",
    "time": "HH:MM:SS or null",
    "amount": number or null,
    "currency": "KRW"
  },
  "confidence": "low" | "medium" | "high"
}

CRITICAL RULES:
1. For delivery app receipts (배달의민족, 쿠팡이츠, 요기요), vendor_info should be the RESTAURANT, not the app company.
2. platform_info is the payment platform (우아한형제들, 카카오페이, NHN KCP, etc.)
3. Business registration numbers (사업자등록번호) must be in XXX-XX-XXXXX format.
4. Return ONLY the JSON object, no markdown or extra text."""


class GeminiCliReceiptExtractor:
    """
    Extract vendor and transaction information from receipt images using Gemini CLI.
    """

    def __init__(self, model: str = "gemini-3-flash-preview", timeout: int = 60):
        if not GEMINI_CLI_AVAILABLE:
            raise RuntimeError("Gemini CLI not available.")
        self.model = model
        self.timeout = timeout

    async def extract(self, image_path: str) -> ReceiptData:
        logger.info(f"Extracting receipt data from: {image_path} (using Gemini CLI)")

        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image file not found: {image_path}")

        prompt = GEMINI_CLI_RECEIPT_PROMPT.replace("__FILENAME__", path.name)
        cmd = [
            "gemini",
            "-m", self.model,
            "-o", "json",
            prompt,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            cwd=str(path.parent),
        )

        if result.returncode != 0:
            raise RuntimeError(f"Gemini CLI error: {result.stderr}")

        cli_output = self._parse_cli_output(result.stdout)
        response_text = cli_output.get("response", "")
        data = self._parse_json_response(response_text)
        receipt = ReceiptData.from_dict(data)
        receipt.raw_text = response_text
        receipt.provider = "gemini_cli"
        receipt.model = self.model

        logger.info(f"Extraction complete. Vendor: {receipt.vendor_info.name}, Confidence: {receipt.confidence}")
        return receipt

    def _parse_json_response(self, response: str) -> dict:
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        json_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', response)
        if json_block_match:
            try:
                return json.loads(json_block_match.group(1))
            except json.JSONDecodeError:
                pass

        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        logger.warning("Could not parse JSON from Gemini CLI response")
        return {"is_receipt": False, "confidence": "low"}

    def _parse_cli_output(self, stdout: str) -> dict:
        """Parse Gemini CLI JSON output, skipping non-JSON prefixes."""
        json_start = stdout.find("{")
        if json_start == -1:
            raise RuntimeError(f"Gemini CLI returned non-JSON output: {stdout[:200]}")
        return json.loads(stdout[json_start:])


class QwenReceiptExtractor:
    """
    Extract vendor and transaction information from receipt images using Qwen2.5-VL OCR API.
    """

    def __init__(self, host: str = "200.168.0.41", port: int = 8810, timeout: int = 60):
        if not QWEN25VL_AVAILABLE:
            raise RuntimeError("Qwen2.5-VL OCR API client not available (ocr_api).")
        self.client = OCRClient(host=host, port=port, timeout=timeout)

    async def extract(self, image_path: str) -> ReceiptData:
        logger.info(f"Extracting receipt data from: {image_path} (using Qwen2.5-VL API)")

        result = await asyncio.to_thread(
            self.client.ocr,
            image_path,
            QWEN_RECEIPT_EXTRACTION_PROMPT,
        )

        if not result.get("success"):
            raise RuntimeError(f"Qwen2.5-VL OCR error: {result.get('error', 'Unknown error')}")

        response_text = result.get("text", "")
        logger.debug(f"Qwen2.5-VL response: {response_text}")

        data = self._parse_json_response(response_text)
        receipt = ReceiptData.from_dict(data)
        receipt.raw_text = response_text
        receipt.provider = "qwen25vl"
        receipt.model = result.get("model")

        logger.info(f"Extraction complete. Vendor: {receipt.vendor_info.name}, Confidence: {receipt.confidence}")
        return receipt

    def _parse_json_response(self, response: str) -> dict:
        """Parse JSON from Qwen2.5-VL response, handling potential formatting issues."""
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        json_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', response)
        if json_block_match:
            try:
                return json.loads(json_block_match.group(1))
            except json.JSONDecodeError:
                pass

        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        logger.warning("Could not parse JSON from Qwen2.5-VL response")
        return {"is_receipt": False, "confidence": "low"}

# Prompt for Claude Code (slightly simpler since we can reference file directly)
CLAUDE_CODE_EXTRACTION_PROMPT = """Read the file {image_path}. This is a receipt image. Extract the following information and return ONLY a JSON object (no other text):

{{
  "is_receipt": true/false,
  "platform_info": {{
    "name": "string or null",
    "biz_num": "XXX-XX-XXXXX format or null"
  }},
  "vendor_info": {{
    "name": "string or null",
    "biz_num": "XXX-XX-XXXXX format or null",
    "address": "string or null"
  }},
  "transaction": {{
    "date": "YYYY-MM-DD or null",
    "time": "HH:MM:SS or null",
    "amount": number or null,
    "currency": "KRW"
  }},
  "confidence": "low" | "medium" | "high"
}}

CRITICAL RULES:
1. For delivery app receipts (배달의민족, 쿠팡이츠, 요기요), vendor_info should be the RESTAURANT, not the app company.
2. platform_info is the payment platform (우아한형제들, 카카오페이, NHN KCP, etc.)
3. Business registration numbers (사업자등록번호) must be in XXX-XX-XXXXX format.
4. Return ONLY the JSON object, no markdown code blocks or other text."""


class ClaudeCodeReceiptExtractor:
    """
    Extract vendor and transaction information from receipt images using Claude Code CLI.
    
    This extractor uses the Claude Code CLI (installed via `claude` command) which
    leverages your existing Claude subscription - no API key needed!
    
    Usage:
        extractor = ClaudeCodeReceiptExtractor()
        result = await extractor.extract("receipt.jpg")
        print(result.vendor_info.name)
    """
    
    def __init__(self, model: Optional[str] = "sonnet", timeout: int = 60):
        """
        Initialize the extractor.

        Args:
            model: Model to use (e.g., "sonnet", "opus"). Default "sonnet" — best
                   balance of accuracy and speed for receipt OCR (tested 5/5 accuracy,
                   ~12s avg vs opus which is slower and more expensive).
            timeout: Timeout in seconds for CLI calls. Default 60s.
        """
        if not CLAUDE_CODE_AVAILABLE:
            raise RuntimeError(
                "Claude Code CLI not found. Install from https://claude.ai/code or ensure 'claude' is in PATH."
            )
        
        self.model = model
        self.timeout = timeout
    
    async def extract(self, image_path: str) -> ReceiptData:
        """
        Extract information from a receipt image.
        
        Args:
            image_path: Path to the receipt image file.
            
        Returns:
            ReceiptData with extracted information.
        """
        logger.info(f"Extracting receipt data from: {image_path} (using Claude Code CLI)")
        
        # Verify file exists
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image file not found: {image_path}")
        
        # Build prompt — use filename only (cwd is set to image directory)
        prompt = CLAUDE_CODE_EXTRACTION_PROMPT.format(image_path=path.name)
        
        # Build command
        # Use --no-session-persistence to avoid cluttering the user's session history
        cmd = ["claude", "-p", prompt, "--output-format", "json", "--no-session-persistence"]
        if self.model:
            cmd.extend(["--model", self.model])
        
        # Run Claude Code CLI (strip ALL CLAUDE* env vars to avoid nested-session block)
        import os as _os
        _env = {k: v for k, v in _os.environ.items()
                if not k.startswith("CLAUDE")}
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                cwd=str(path.parent),  # Run from image directory
                env=_env,
            )
            
            if result.returncode != 0:
                # Interim failure — outer fallback loop will retry with another
                # provider before this becomes user-visible. Stay at WARNING so
                # successful retries don't leave ERROR lines in the log.
                logger.warning(f"Claude Code CLI failed: {result.stderr}")
                raise RuntimeError(f"Claude Code CLI error: {result.stderr}")

            if not result.stdout:
                logger.warning(f"Claude Code CLI returned empty stdout. stderr: {result.stderr}")
                raise RuntimeError(f"Claude CLI returned empty output (stdout=None). This may indicate too many concurrent processes. stderr: {result.stderr}")

            # Parse the JSON output from Claude Code
            cli_output = json.loads(result.stdout)
            
            if cli_output.get("is_error"):
                raise RuntimeError(f"Claude Code error: {cli_output.get('result', 'Unknown error')}")
            
            # Extract the actual result (which contains the receipt JSON)
            # Use `or ""` because .get() returns None when key exists but value is None
            response_text = cli_output.get("result") or ""
            logger.debug(f"Claude Code response: {response_text}")
            
            # Parse the nested JSON from the result
            result_data = self._parse_json_response(response_text)
            result = ReceiptData.from_dict(result_data)
            result.raw_text = response_text
            result.provider = "claude_code"
            result.model = self.model
            
            logger.info(f"Extraction complete. Vendor: {result.vendor_info.name}, Confidence: {result.confidence}")
            return result
            
        except subprocess.TimeoutExpired:
            logger.warning(f"Claude Code CLI timed out after {self.timeout}s")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude Code output: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to extract receipt data: {e}")
            raise
    
    def _parse_json_response(self, response: str) -> dict:
        """Parse JSON from Claude Code's response, handling potential formatting issues."""
        # Try direct parse first
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass
        
        # Try to find JSON block in response (might be wrapped in ```json ... ```)
        json_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', response)
        if json_block_match:
            try:
                return json.loads(json_block_match.group(1))
            except json.JSONDecodeError:
                pass
        
        # Try to find raw JSON object
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
        
        logger.warning("Could not parse JSON from response")
        return {"is_receipt": False, "confidence": "low"}
    
    def extract_sync(self, image_path: str) -> ReceiptData:
        """Synchronous version of extract()."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(self.extract(image_path))


class OpenRouterReceiptExtractor:
    """Extract receipt information using OpenRouter API (OpenAI-compatible)."""

    API_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "anthropic/claude-sonnet-4", timeout: int = 60):
        if not api_key:
            raise ValueError("OpenRouter API key required. Set in config.yaml or OPENROUTER_API_KEY env var.")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def extract(self, image_path: str) -> ReceiptData:
        """Extract information from a receipt image via OpenRouter vision API."""
        import httpx

        logger.info(f"Extracting receipt data from: {image_path} (using OpenRouter: {self.model})")

        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image file not found: {image_path}")

        # Encode image as base64
        with open(path, "rb") as f:
            image_bytes = f.read()
        b64_image = base64.b64encode(image_bytes).decode("utf-8")

        suffix = path.suffix.lower()
        mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                    ".gif": "image/gif", ".webp": "image/webp"}
        mime_type = mime_map.get(suffix, "image/jpeg")

        prompt_text = QWEN_RECEIPT_EXTRACTION_PROMPT  # Reuse the same extraction prompt

        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime_type};base64,{b64_image}"
                }},
            ],
        }]

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.API_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                },
            )
            response.raise_for_status()

        data = response.json()
        response_text = data["choices"][0]["message"]["content"]

        parsed = self._parse_json_response(response_text)
        receipt = ReceiptData.from_dict(parsed)
        receipt.raw_text = response_text
        receipt.provider = "openrouter"
        receipt.model = self.model

        logger.info(f"Extraction complete. Vendor: {receipt.vendor_info.name}, Confidence: {receipt.confidence}")
        return receipt

    def _parse_json_response(self, response: str) -> dict:
        """Parse JSON from OpenRouter response."""
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        json_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', response)
        if json_block_match:
            try:
                return json.loads(json_block_match.group(1))
            except json.JSONDecodeError:
                pass

        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        logger.warning("Could not parse JSON from OpenRouter response")
        return {"is_receipt": False, "confidence": "low"}


# Extract receipt data from pre-prepared OCR text (no vision needed)
async def extract_receipt_from_text(
    ocr_text: str,
    image_path: Optional[str] = None,
    provider: str = "auto",
) -> ReceiptData:
    """Extract receipt data from pre-prepared OCR text using a text-only LLM.

    Much cheaper/faster than vision OCR — no image encoding, smaller model works.

    Args:
        ocr_text: Raw OCR text content from the companion file.
        image_path: Original image path (for source_path/logging only).
        provider: Provider string ("auto", "claude_code", "gemini_cli", "openrouter", etc.)

    Returns:
        ReceiptData with extracted information.
    """
    import os

    logger.info(f"Extracting receipt from pre-prepared OCR text"
                f"{f' for {Path(image_path).name}' if image_path else ''}")

    prompt = PREOCR_TEXT_EXTRACTION_PROMPT.format(ocr_text=ocr_text)

    # Resolve provider — text-only, so skip vision-only providers like Qwen
    if provider in ("auto", "qwen25vl"):
        candidates = []
        if CLAUDE_CODE_AVAILABLE:
            candidates.append("claude_code")
        if GEMINI_CLI_AVAILABLE:
            candidates.append("gemini_cli")
        or_key = os.environ.get("OPENROUTER_API_KEY", "")
        if or_key:
            candidates.append("openrouter")
        if not candidates:
            raise ValueError("No LLM provider available for pre-OCR text parsing")
        provider = candidates[0]

    response_text = ""
    provider_name = provider

    if provider == "claude_code":
        if not CLAUDE_CODE_AVAILABLE:
            raise RuntimeError("Claude Code CLI not found")
        # Strip CLAUDE* env vars to avoid nested-session block
        import os as _os
        _env = {k: v for k, v in _os.environ.items() if not k.startswith("CLAUDE")}
        cmd = ["claude", "-p", prompt, "--output-format", "json",
               "--no-session-persistence", "--model", "sonnet"]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=30, env=_env
        )
        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI error: {result.stderr[:200]}")
        try:
            cli_output = json.loads(result.stdout)
            if cli_output.get("is_error"):
                raise RuntimeError(f"Claude error: {cli_output.get('result', '')[:200]}")
            response_text = cli_output.get("result", "")
        except json.JSONDecodeError:
            response_text = result.stdout.strip()

    elif provider == "gemini_cli":
        if not GEMINI_CLI_AVAILABLE:
            raise RuntimeError("Gemini CLI not found")
        cmd = ["gemini", "-o", "json", prompt]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(f"Gemini CLI error: {result.stderr[:200]}")
        response_text = result.stdout.strip()

    elif provider == "openrouter":
        import httpx
        or_key = os.environ.get("OPENROUTER_API_KEY", "")
        or_model = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {or_key}", "Content-Type": "application/json"},
                json={"model": or_model, "messages": [{"role": "user", "content": prompt}]},
            )
            resp.raise_for_status()
            data = resp.json()
            response_text = data["choices"][0]["message"]["content"]

    else:
        raise ValueError(f"Unsupported provider for pre-OCR text: {provider}")

    # Parse JSON response
    result_data = _parse_json_response(response_text)
    receipt = ReceiptData.from_dict(result_data)
    receipt.raw_text = ocr_text
    receipt.provider = f"preocr_{provider_name}"
    receipt.source_path = image_path

    # Safety net: if is_receipt=False but we have ANY identifiable receipt data,
    # override to True. Users may have .ocr.md files with partial/unknown fields
    # that should still be usable for matching.
    if not receipt.is_receipt:
        has_data = bool(
            (receipt.vendor_info and receipt.vendor_info.name)
            or (receipt.vendor_info and receipt.vendor_info.biz_num)
            or (receipt.transaction and receipt.transaction.amount)
            or (receipt.transaction and receipt.transaction.date)
        )
        if has_data:
            logger.info(
                f"Pre-OCR marked is_receipt=false but has data; overriding to true. "
                f"vendor={receipt.vendor_info.name}, amount={receipt.transaction.amount}"
            )
            receipt.is_receipt = True

    logger.info(f"Pre-OCR extraction complete. Vendor: {receipt.vendor_info.name}, "
                f"Confidence: {receipt.confidence}")
    return receipt


# Convenience function - auto-selects best available provider
async def extract_receipt(image_path: str, api_key: Optional[str] = None, provider: str = "auto") -> ReceiptData:
    """
    Quick function to extract receipt data. Auto-selects provider based on availability.

    Args:
        image_path: Path to receipt image.
        api_key: API key (optional, uses env var if not provided).
        provider: "auto", "qwen25vl", "gemini_cli", "claude_code", "openrouter", "gemini", or "anthropic"

    Returns:
        ReceiptData with extracted information.

    Provider priority (auto mode):
        Server mode: Qwen2.5-VL → Claude Code CLI (sonnet) → Gemini CLI → Gemini API → Anthropic API
        Local mode:  Claude Code CLI (sonnet) → Gemini CLI → OpenRouter → Gemini API → Anthropic API
    """
    import os

    if provider == "auto":
        providers = []
        local_mode = os.environ.get("DOUZONE_LOCAL_MODE") == "1"

        if QWEN25VL_AVAILABLE and not local_mode:
            # Only try Qwen in server mode and if reachable
            if _is_qwen_reachable():
                providers.append("qwen25vl")
            else:
                logger.info("Qwen2.5-VL server not reachable, skipping")

        if CLAUDE_CODE_AVAILABLE:
            providers.append("claude_code")
        if GEMINI_CLI_AVAILABLE:
            providers.append("gemini_cli")

        # OpenRouter if API key is set
        or_key = os.environ.get("OPENROUTER_API_KEY", "")
        if or_key:
            providers.append("openrouter")

        if GEMINI_AVAILABLE and (api_key or os.environ.get("GOOGLE_API_KEY")):
            providers.append("gemini")
        if ANTHROPIC_AVAILABLE and (api_key or os.environ.get("ANTHROPIC_API_KEY")):
            providers.append("anthropic")

        if not providers:
            raise ValueError(
                "No AI provider available. Options:\n"
                "  1. Install Claude Code CLI (recommended for local mode)\n"
                "  2. Install Gemini CLI\n"
                "  3. Set OPENROUTER_API_KEY environment variable\n"
                "  4. Ensure Qwen2.5-VL OCR API is reachable (server mode)\n"
                "  5. Set GOOGLE_API_KEY or ANTHROPIC_API_KEY environment variable"
            )

        last_error = None
        for candidate in providers:
            try:
                return await extract_receipt(image_path, api_key=api_key, provider=candidate)
            except Exception as e:
                last_error = e
                logger.warning(f"Receipt OCR failed with {candidate}, trying fallback: {str(e)[:100]}")
        if last_error:
            raise last_error

    if provider == "qwen25vl":
        extractor = QwenReceiptExtractor()
    elif provider == "gemini_cli":
        extractor = GeminiCliReceiptExtractor()
    elif provider == "claude_code":
        extractor = ClaudeCodeReceiptExtractor()
    elif provider == "openrouter":
        or_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        or_model = os.environ.get("OPENROUTER_VISION_MODEL", "anthropic/claude-sonnet-4")
        extractor = OpenRouterReceiptExtractor(api_key=or_key, model=or_model)
    elif provider == "gemini":
        extractor = GeminiReceiptExtractor(api_key=api_key)
    elif provider == "anthropic":
        extractor = ReceiptExtractor(api_key=api_key)
    else:
        raise ValueError(f"Unknown provider: {provider}. Choose from: auto, claude_code, gemini_cli, openrouter, gemini, anthropic")

    return await extractor.extract(image_path)
