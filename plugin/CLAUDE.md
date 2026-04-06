# Douzone Expense Automation Plugin

더존 그룹웨어 경비보고서 자동 작성 도우미.

## 처음 사용하시나요? (Getting Started)

이 플러그인은 더존 경비보고서의 **STEP 2 (지출정보등록)** 화면을 자동으로 채워줍니다.
사용자가 준비할 것은 **메모 파일**과 **영수증**뿐입니다.

### 준비물

**1. 메모 파일 (참석자 정보)**

텍스트 파일(.txt)에 식사/회의별 참석자를 작성합니다:

```
3/5 점심 홍길동 김철수 이영희 - 강남역 근처 식당
3/5 저녁 홍길동 박민수 - 회식 (배달의민족)
3/7 점심 홍길동 - 혼밥
```

형식은 자유롭습니다. 날짜, 참석자 이름, 장소/메모가 포함되면 AI가 알아서 파싱합니다.

**2. 영수증 (선택사항이지만 권장)**

영수증 이미지를 한 폴더에 모아둡니다:
- 지원 형식: JPG, PNG, HEIC, PDF
- 파일명은 자유 (날짜나 가게 이름으로 하면 매칭에 도움)
- AI가 영수증에서 가게명, 사업자번호, 금액, 날짜를 자동 추출 (OCR)

**OCR 결과 사전 준비 (선택):** 영수증 옆에 같은 이름의 `.ocr.md` 파일을 두면 Vision AI를 건너뛰고 해당 텍스트를 바로 사용합니다. 예: `receipt_0305.jpg` → `receipt_0305.ocr.md`

### 알아두면 좋은 경비 규정

에이전트가 알아서 사용자에게 안내해야 하는 규정입니다:

**배민/PG 거래 (배달의민족, 쿠팡이츠, 요기요, 카카오페이, 네이버페이 등)**
- 영수증 첨부 **필수** — 실제 공급자(실공급자) 확인용
- 실공급자상호 + 실공급자 사업자등록번호 입력 필수
- 영수증 없으면 비고란에 사유 자동 기입됨

**대형 쇼핑몰/백화점 (코엑스, 스타필드, 현대백화점, 롯데백화점 등)**
- 영수증 첨부 권장 — 실제 구매처 확인용

**주차비**
- 정기 주차 지원비 한도: **월 20만원** (1건당)
- 일회성 주차비, 회사에서 1km 이상 떨어진 곳의 주차비는 지원 대상이 아님

**결제+취소 (환불)**
- 같은 청구기간 내 결제와 취소가 모두 있으면: 둘 다 건너뛰거나 둘 다 제출
- **취소분만 제출하면 안 됨** (자동으로 감지하여 안내)

### 사용법

`/douzonebot:go` 한 마디면 됩니다. 또는 자연어로:

```
더존 자동화 해줘
경비 처리해줘
```

에이전트가 환경 설정, Chrome 실행, 사전 점검, 자동화 실행까지 전부 처리합니다.

### 실행 모드

- **대시보드 모드** (추천): 웹 화면에서 메모와 영수증을 업로드
- **CLI 전체 모드**: 터미널에서 메모 파일과 영수증 폴더 경로 지정
- **CLI 간단 모드**: 모든 행에 본인 이름만 입력 (메모/영수증 불필요)

### 자동화 전 더존 화면 준비

1. 자동화 Chrome에서 더존에 로그인 (처음 한 번만)
2. 경비 프로세스 절차에서 **STEP 1 완료**
3. **지출정보등록 (STEP 2)** 화면으로 이동
4. Chrome 창을 **전체 화면(최대화)** 유지

## Available Skills

- `/douzonebot:go` — **The main entry point.** Handles everything: setup → Chrome → preflight → run. Use this for all automation.
- `/douzonebot:troubleshoot` — Diagnose issues when things break
- `/douzonebot:uninstall` — Cleanup and remove

## Ad-hoc Operations (for targeted edits)

Beyond the full pipeline, you can perform **individual row edits** using the operations API.
This is useful when the user asks to fix a specific row, attach a receipt, or fill supplier info
without re-running the entire pipeline.

### How to use

The operations module is at `plugin/bot/src/operations.py`. Run operations inside the
appropriate Python environment (Docker container `douzone-bot` or local `uv run`).

### Available operations

```python
from src.operations import (
    connect,          # Connect to Douzone via CDP
    disconnect,       # Close connection
    read_grid,        # Read all grid rows (merchant, amount, status, etc.)
    get_row_status,   # Read single row status (yongdo, content, validation, attendee)
    get_row_fields,   # Read single row basic fields
    get_grid_info,    # Grid metadata (total rows, visible rows, top item)
    scroll_to_top,    # Scroll grid to top
    scroll_to_row,    # Navigate to specific row
    edit_row,         # Open popup, fill fields, save (attendee, supplier, bigo)
    attach_receipt,   # Open popup, attach receipt file, save
    fill_supplier,    # Open popup, fill 실공급자 info, save
)
```

### Examples

**Attach a receipt to row 5:**
```python
auto = await connect("http://localhost:9222")
await attach_receipt(auto, row=5, path="/path/to/receipt.jpg")
await disconnect(auto)
```

**Fill supplier info for row 3:**
```python
auto = await connect()
await fill_supplier(auto, row=3, name="맛나분식", biz_no="123-45-67890")
await disconnect(auto)
```

**Read what's in the grid:**
```python
auto = await connect()
rows = await read_grid(auto)
for r in rows:
    print(f"Row {r['row_number']}: {r.get('merchant','')} / {r.get('validation','')}")
await disconnect(auto)
```

**Edit multiple fields on a row:**
```python
auto = await connect()
await edit_row(auto, row=7, fields={
    'supplier_name': '진짜식당',
    'supplier_biz_no': '456-78-90123',
    'bigo': '실공급자 확인 완료',
})
await disconnect(auto)
```

### Important notes

- All row numbers are **1-based** (matching Douzone UI)
- Chrome must be running with debug mode (launched by `/douzonebot:go` Phase 2)
- CDP port is auto-detected by the launcher (default: 9444)
- Operations open the row's popup, edit, then save — existing data is preserved
- Receipt files must be JPG, PNG, or PDF
- For Docker: run inside `douzone-bot` container
- For local: run with `uv run --with-requirements requirements-local.txt`

## Post-Verification (Stage 5)

After the pipeline completes, a post-verification scan automatically checks for:
1. **PG 거래 영수증 누락** — 배민/코엑스/PG merchant transactions missing receipts
2. **실공급자 정보 누락** — Missing supplier name or business number for PG transactions
3. **결제+취소 쌍 불일치** — Charge+cancellation pairs where only one side was submitted
4. **주차비 한도 초과** — Parking transactions exceeding 200,000원 per-transaction cap

Issues are reported, then interactive CLI prompts offer row-by-row fixes.
Skip with `--skip-post-verify` flag.

Use the ad-hoc operations above to fix individual rows if the user prefers
manual corrections over the interactive flow.

## Session Report (on request)

When the user asks for a session report ("리포트 작성해줘", "what went wrong"), analyze the
conversation history and produce a structured report:

```
더존봇 세션 리포트

---
N. [Critical|Medium|Low] 제목

증상: 무엇이 발생했는지.
원인: 왜 발생했는지.
영향: 추가로 필요했던 작업.
제안: 개선 방법.
---
```

Focus on: skill/code issues (not user error or external outages).
Include an efficiency summary table by phase (호출 수, 불필요한 호출, 원인).
