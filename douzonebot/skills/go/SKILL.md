---
name: go
description: "End-to-end Douzone expense automation. Automatically handles setup, Chrome, preflight, and run in one command. ONLY trigger when the user's message contains '더존', 'douzone', 'douzonebot', or '경비' as a keyword. Example triggers: \"더존 해줘\", \"더존 자동화 해줘\", \"douzone 해줘\", \"경비 처리해줘\", \"경비청구 자동화 시작\", \"douzone go\". Do NOT trigger on generic phrases like \"해줘\" alone without a douzone/더존/경비 keyword."
disable-model-invocation: false
user-invocable: true
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, WebFetch, AskUserQuestion
---

# 더존 경비 자동화 — 원클릭 실행

이 스킬은 환경 설정부터 실행까지 전 과정을 자동으로 처리합니다. 각 단계를 확인하고, 이미 완료된 단계는 건너뜁니다.

진행 순서: **안내 → 환경 확인 → Chrome → 사전 점검 → 실행**

## Phase 0: 첫 사용자 안내

처음 사용하는 사용자인지 판단합니다 (uv 미설치, 또는 사용자가 "처음", "어떻게" 등의 질문을 하는 경우).

**처음 사용하는 사용자에게 안내할 내용:**

> 이 플러그인은 더존 경비보고서 STEP 2 (지출정보등록)를 자동으로 채워줍니다.
>
> **준비할 것:**
> 1. **메모 파일** (.txt) — 날짜별 참석자 정보. 예: `3/5 점심 홍길동 김철수 - 강남역 식당`
> 2. **영수증 폴더** (선택) — JPG, PNG, HEIC, PDF 파일을 한 폴더에 모아주세요
>
> **알아두면 좋은 규정:**
> - 배민/쿠팡이츠/카카오페이 등 PG 거래 → 영수증 첨부 필수 (실공급자 확인용)
> - 코엑스/백화점 등 대형 쇼핑몰 → 영수증 첨부 권장
> - 주차비 → 1건당 20만원 한도
> - 결제+취소가 같이 있으면 → 둘 다 건너뛰거나 둘 다 제출 (취소분만 제출 금지)
>
> **영수증 팁:**
> - 사진은 글씨가 잘 보이게 찍어주세요 (AI가 OCR로 읽습니다)
> - 영수증 옆에 `.ocr.md` 파일을 두면 OCR을 건너뛰고 바로 사용합니다
>   예: `receipt_0305.jpg` → `receipt_0305.ocr.md`

재사용자 (uv 이미 설치됨, 별도 질문 없음) → 이 단계를 건너뛰고 Phase 1로 진행합니다.

---

## 경로 규칙

- **BOT_DIR**: 이 SKILL.md 파일의 grandparent 디렉토리 아래 `bot/` 폴더. 예: `plugin/skills/go/SKILL.md` → `plugin/skills/` → `plugin/` → `plugin/bot/`
- **PLAN_FILE**: 실행 초반에 `mktemp`로 생성하는 임시 파일 (OS가 자동 정리)

모든 `uv run` 명령은 BOT_DIR에서 실행합니다.

## 중요: 실행 환경

Claude Code는 Windows에서도 bash (Git Bash/MSYS2) 쉘을 사용합니다.
- `$USERPROFILE` 또는 `$HOME`으로 홈 디렉토리 접근 (`$env:USERPROFILE` 사용 금지)
- `mkdir -p` 등 bash 명령어 사용
- Windows 전용 명령 필요 시 `cmd.exe /c "..."` 또는 `powershell -Command "..."` 래핑
- **각 단계를 순서대로 하나씩 실행** (병렬 실행 금지 — 앞 단계 실패 시 뒤 단계가 깨짐)

---

## Phase 1: 환경 확인

> **주의**: Phase 1의 각 단계를 **순서대로 하나씩** 실행하세요.

### 1-1. OS 감지

```bash
uname -s
```
- `Darwin` → macOS
- `MINGW*` 또는 `MSYS*` → Windows (Git Bash)
- `Linux` → Linux

결과를 기억하고 이후 OS별 분기에 사용합니다.

### 1-2. uv 확인

```bash
uv --version 2>/dev/null || echo "UV_NOT_FOUND"
```

- 설치되어 있으면 → 건너뛰기
- 없으면 (처음 사용하는 사용자) OS별 설치:
  - **macOS / Linux:**
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```
  - **Windows (bash에서):**
    ```bash
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    ```
  - 설치 후 PATH에 추가하고 재확인:
    ```bash
    export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH"
    uv --version
    ```
  - uv를 **새로 설치한 경우**, 플러그인 자동 업데이트를 안내합니다:
    > "플러그인 자동 업데이트를 켜면 새 버전이 나올 때 자동으로 반영됩니다. `/plugin` 입력 → Marketplaces 탭 → neosapience-douzone-bot → Enable auto-update를 클릭해주세요. 한 번만 설정하면 됩니다."

**중요**: 이후 모든 `uv` 명령 앞에 PATH + 인코딩 설정을 포함합니다:
```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 && uv ...
```

> **참고**: 처음 실행 시 `uv`가 필요한 패키지를 자동 다운로드합니다 (약 20개, 1~2분 소요). 이후 실행에서는 캐시를 사용하므로 빠르게 시작됩니다.

**Phase 1 완료**: "✓ 환경 준비 완료"

---

## Phase 2: Chrome 확인

### 2-1. Chrome 실행

launcher.py가 자동으로 처리합니다:
- Chrome 경로 탐색 (OS별 자동 감지)
- 포트 충돌 감지 및 자동 해결 (빈 포트 탐색)
- CDP 연결 확인 (이미 실행 중이면 재사용)

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 NODE_NO_WARNINGS=1 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python ui/launcher.py --chrome
```

출력에서 `CHROME_OK=true`와 `CHROME_PORT=<포트>`를 파싱합니다. **이 포트 값을 이후 모든 단계에서 `CDP_PORT`로 사용합니다.**

> ⚠️ **중요**: 자동화 Chrome 창을 **전체 화면(최대화)** 상태로 유지하세요. 창이 작으면 더존 UI 요소가 보이지 않아 자동화가 실패할 수 있습니다. 사용자에게 반드시 안내하세요.

### 2-2. CDP 연결 확인

Phase 2-1에서 파싱한 포트를 사용합니다:

```bash
curl -s --connect-timeout 3 http://localhost:$CDP_PORT/json/version && echo "CDP_OK"
```

- CDP_OK → Phase 3로 진행
- 실패 시 → `CHROME_PATH` 출력에서 Chrome 경로를 확인하고, 수동 실행을 안내. `/douzonebot:troubleshoot` 참고.
- Chrome 재시작이 필요하면 `--force-restart` 플래그 추가: `python ui/launcher.py --chrome --force-restart`

### 2-3. 사용자 안내

Chrome 실행 확인 후 사용자에게 안내:
- "자동화 전용 Chrome 창이 열렸습니다. 더존 그룹웨어 페이지가 자동으로 로드됩니다."
- "이 Chrome은 별도 프로필(`~/.douzone-chrome`)을 사용하므로 기존 Chrome과 독립적입니다."
- "**로그인 후 경비 프로세스 절차에서 STEP 1까지 완료한 뒤 지출정보등록 (STEP 2) 화면으로 이동해 주세요.**"
- "더존 로그인은 처음 한 번만 하면 됩니다."
- "준비되면 알려주세요."

**Phase 2 완료**: "✓ Chrome 준비 완료"

---

## Phase 3: 사전 점검

### 3-1. Preflight 실행

Phase 2에서 파싱한 `CDP_PORT`를 사용합니다:

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 NODE_NO_WARNINGS=1 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python main.py --local --preflight-only --user dummy --cdp-url http://localhost:$CDP_PORT
```

### 3-2. 결과 파싱

출력에서 `[PASS]`, `[FAIL]`, `[WARN]`, `[SKIP]` 라인을 파싱합니다.

- **모두 PASS** → "✓ 모든 점검 통과" 표시 후 Phase 4로
- **FAIL 있음** → 각 실패에 대해 원인과 해결 방법 설명:
  - **CDP 연결 실패**: "Chrome 자동화 창이 연결되지 않습니다. Chrome이 열려있는지 확인하세요."
    - CDP 실패 시 `curl -s http://localhost:$CDP_PORT/json/version`으로 수동 확인. 응답이 오면 preflight 내부 문제이므로 `--skip-preflight`로 우회 가능
  - **Claude Code CLI 실패**: "Claude CLI 인증이 필요합니다. `claude /login`으로 로그인하세요."
  - 문제 해결 후 "다시 점검해 볼까요?" → 재실행
  - curl로 CDP가 확인되면 `--skip-preflight` 플래그를 추가하여 Phase 4로 진행 가능
- **WARN 있음** → "선택사항이므로 무시해도 됩니다" 안내

**Phase 3 완료**: "✓ 사전 점검 통과"

---

## Phase 4: 실행

### 4-1. 실행 방식 선택

AskUserQuestion으로 질문:

- **대시보드 입력** (추천): 웹 대시보드에서 메모와 영수증을 입력
- **CLI — 전체 모드**: 메모 파싱 + 거래 매칭 + 영수증 첨부까지 전부 처리
- **CLI — 간단 모드**: 모든 행에 본인 이름만 입력 (메모/영수증 불필요)

CLI 모드(전체 또는 간단) 선택 시, 아래 정보를 수집합니다:

1. **사용자 이름**: 사용자에게 직접 질문 (자유 텍스트 — AskUserQuestion 사용 금지)
2. **메모 파일** (전체 모드만): 메모 텍스트 파일 경로 → 파일 존재 확인 후 내용 미리보기
3. **영수증 폴더** (전체 모드만): 영수증 폴더 경로 → 파일 개수 확인 (이미지 + PDF)
   - **PDF 영수증 지원**: 이미지(JPG, PNG, HEIC)뿐만 아니라 PDF 파일도 영수증으로 처리됩니다.
   - **사전 OCR 지원**: 영수증 파일 옆에 `.ocr.md` (또는 `.ocr.txt`, `.ocr.json`) 파일이 있으면 Vision AI를 건너뛰고 해당 텍스트를 직접 사용합니다. OCR이 미리 준비된 영수증 폴더도 그대로 사용 가능합니다.

메모/영수증이 없으면 해당 `--memo`/`--receipts` 플래그를 생략합니다 (기본 참석자만 입력됨).

**중요**: 모든 `python main.py` 명령에 `--cdp-url http://localhost:$CDP_PORT`를 포함합니다 (Phase 2에서 파싱한 포트).

### 4-2. 대시보드 입력 모드

1. **대시보드 서버 실행** — launcher.py가 포트 충돌 감지, 빈 포트 탐색, 서버 시작, health check를 자동 처리합니다:

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 NODE_NO_WARNINGS=1 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python ui/launcher.py --dashboard
```

출력에서 `DASHBOARD_OK=true`와 `DASHBOARD_PORT=<포트>` 값을 확인합니다. 실패 시(`DASHBOARD_OK=false`) 에러 메시지를 사용자에게 안내합니다.

2. **브라우저 열기** (출력에서 확인한 포트로):
- macOS: `open http://localhost:$DASHBOARD_PORT`
- Windows: `powershell -Command "Start-Process 'http://localhost:$DASHBOARD_PORT'"`
- Linux: `xdg-open http://localhost:$DASHBOARD_PORT`
- 포트가 5000이 아니면 사용자에게 안내: "포트 5000이 사용 중이어서 $DASHBOARD_PORT로 대시보드를 열었습니다."

3. 사용자에게 안내:
   - "대시보드에서 메모와 영수증을 입력한 후 '저장' 버튼을 눌러주세요."
   - "저장이 완료되면 알려주세요."

4. 사용자가 저장 완료를 알리면, 저장된 데이터를 읽어서 변수에 저장:
```bash
curl -s http://localhost:$DASHBOARD_PORT/data
```
→ 응답에서 `memo_path`, `receipts_path`, `user_name` 값을 추출

5. **전체 모드 단계별 실행** (아래 4-4 참고)

### 4-3. CLI 간단 모드

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 NODE_NO_WARNINGS=1 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python main.py --local -q --user "<이름>" --simple --cdp-url http://localhost:$CDP_PORT
```

> 실행 후 더존 화면에서 변화가 나타나기까지 수 초 걸릴 수 있습니다. 사용자에게 "더존 화면에서 자동 입력이 시작됩니다. 잠시 기다려 주세요."라고 안내하세요.

### 4-4. 전체 모드 — 단계별 실행

전체 모드는 3단계로 나눠서 실행합니다. 에이전트가 매칭 결과를 검토하고, 사용자에게 확인이 필요한 항목을 안내합니다.

**먼저 임시 파일 경로를 생성합니다** (이후 단계에서 이 변수를 사용):

```bash
PLAN_FILE=$(mktemp "${TMPDIR:-/tmp}/douzone_plan_XXXXXXXXXX")
echo "PLAN_FILE=$PLAN_FILE"
```

#### 4-4a. 데이터 수집 및 매칭

더존 그리드 읽기, 메모 파싱, 영수증 OCR, 거래 매칭을 한 번에 실행하고 결과를 JSON으로 저장합니다:

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 NODE_NO_WARNINGS=1 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python main.py --local -q --user "<이름>" --memo <메모경로> --receipts <영수증경로> --stage2-cache-out "$PLAN_FILE" --stage2-only --cdp-url http://localhost:$CDP_PORT
```

- `--stage2-cache-out`: 매칭 결과를 JSON으로 저장
- `--stage2-only`: 매칭까지만 실행하고 중단 (자동 입력은 아직 안 함)
- 오류 시 Phase 4-5 모니터링 참고

#### 4-4b. 실행 계획 검토

매칭 결과를 로드하여 정리된 요약을 출력합니다:

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 NODE_NO_WARNINGS=1 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python main.py --local -q --user "<이름>" --stage2-cache-in "$PLAN_FILE" --review-only --cdp-url http://localhost:$CDP_PORT
```

- `--review-only`: 매칭 결과를 날짜별로 그룹화하여 보여주고, 확인이 필요한 항목을 ⚠️ 로 표시. 실행은 하지 않음.

터미널 출력을 읽고 사용자에게 결과를 안내합니다:

1. 요약 보고: 전체 행 수, 영수증 매칭 현황
2. **확인이 필요한 항목** (출력에서 ⚠️ 표시된 행):
   - 메모 할당이 필요한 항목 ("needs assignment" 메시지)
   - 영수증이 없는 항목 ("No receipt" 메시지)
   - SaaS 구독 거래에 영수증이 없는 항목 (예: Claude, GitHub, AWS 등 — 구독 증빙 첨부 권장)
   - 기타 낮은 신뢰도 항목
3. 확인이 필요한 항목이 있으면 사용자에게 대화형으로 안내합니다
4. 사용자가 승인하면 4-4c로 진행합니다

**필수 검토: 용도/내용 시간대 확인**

- 모든 식대 행은 사용일시 또는 매칭된 영수증 시간을 기준으로 `용도`와 `내용`을 반드시 검토합니다.
- 점심 시간대(10:30-14:00)는 `용도=100. 중식대`, `내용=점심식사`입니다.
- 저녁 시간대(17:00-21:00)는 `용도=110. 석식대`, `내용=저녁식사`입니다.
- 더존에 이미 값이 있어도 시간이 맞지 않으면 수정 대상입니다. 예: 18:20 거래가 `100. 중식대 / 점심식사`이면 반드시 `110. 석식대 / 저녁식사`로 고칩니다.
- 배민/쿠팡이츠 등 PG 거래는 플랫폼 사용처(KCP/나이스/이니시스 등)가 아니라 영수증의 실제 음식점과 함께 이 시간대 검토를 수행합니다.

#### 4-4c. 자동화 실행

매칭 결과를 로드하여 더존에 자동 입력합니다:

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 NODE_NO_WARNINGS=1 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python main.py --local -q --user "<이름>" --stage2-cache-in "$PLAN_FILE" --auto-approve --cdp-url http://localhost:$CDP_PORT
```

- `--stage2-cache-in`: 4-4a에서 저장한 매칭 결과를 로드 (데이터 재수집 건너뜀)
- `--auto-approve`: 에이전트가 이미 4-4b에서 검토했으므로 대화형 프롬프트 건너뜀

자동 입력 시에도 위 시간대 규칙을 적용해 `용도`와 `내용`을 확인하고, 비어 있거나 잘못된 기존 값을 모두 수정합니다. 참석자/실공급자/첨부파일만 입력하고 끝내면 안 됩니다.

### 4-5. 모니터링

자동화 실행 중 터미널 출력을 읽고 사용자에게 진행 상황을 한국어로 안내합니다:
- 각 단계별 상태 (그리드 읽기, 메모 파싱, OCR, 매칭, 자동 입력)
- 오류 발생 시 원인 분석 및 해결 방법 안내

### 4-6. 완료 후 검토

자동화 완료 후 결과를 보고합니다:
- 처리된 거래 건수, 성공/실패 현황

다음 항목에 해당하는 행을 찾아 **사용자에게 수동 확인을 권장**합니다:
- 4-4b에서 `needs_clarification: true`였던 행 (매칭이 불확실했던 항목)
- `pending_receipt: true`였던 행 (영수증 미첨부 — PG 또는 SaaS 구독 거래)
- `confidence: "LOW"`였던 행 (신뢰도가 낮았던 매칭)
- 자동화 실행 중 실패한 행

안내: "더존에서 위 항목들의 입력 내용을 한 번 확인해 주세요."

### 4-7. 정리

대시보드 모드를 사용했으면 서버를 종료합니다:

```bash
curl -s -X POST http://localhost:$DASHBOARD_PORT/shutdown 2>/dev/null || true
```

대시보드를 사용하지 않은 경우 이 단계를 건너뜁니다.

---

## 규칙

- 각 Phase 시작 시 진행 상황을 명확히 표시 (예: "Phase 2/4: Chrome 확인")
- **각 단계를 순서대로 하나씩 실행** — 앞 단계가 성공해야 다음 단계로
- 이미 완료된 단계는 "✓" 로 간결하게 건너뛰기
- 문제 발생 시 차분하게 해결 방법 안내
- 전문 용어는 최소화하고 쉬운 한국어로 설명
