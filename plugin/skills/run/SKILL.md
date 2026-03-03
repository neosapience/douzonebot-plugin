---
name: run
description: Run the douzone-bot expense automation.
disable-model-invocation: false
user-invocable: true
allowed-tools: Bash, Read, Glob, Grep, AskUserQuestion
argument-hint: "[mode: simple|full|dashboard]"
---

# 더존 봇 자동화 실행

더존 경비 자동화를 실행합니다.

## 경로 규칙

- **BOT_DIR**: 이 SKILL.md 파일에서 2단계 상위 디렉토리의 `bot/` 폴더 (플러그인에 내장된 봇 코드)
- **DATA_DIR**: `~/douzone-bot/` (사용자 설정 파일 — config.yaml)
- **PLAN_FILE**: 실행 초반에 `mktemp`로 생성하는 임시 파일 (OS가 자동 정리)

모든 `uv run` 명령은 BOT_DIR에서 실행합니다.
config.yaml은 DATA_DIR에 있습니다.

**중요: Claude Code는 Windows에서도 bash (Git Bash/MSYS2) 쉘을 사용합니다.**
- `$USERPROFILE` 또는 `$HOME`으로 홈 디렉토리 접근 (`$env:USERPROFILE` 사용 금지)
- **각 단계를 순서대로 하나씩 실행** (병렬 실행 금지)

## 사전 확인

1. **config.yaml 확인**: `~/douzone-bot/config.yaml` 존재 여부 확인
   - 없으면: "config.yaml이 없습니다. `/douzonebot:setup`으로 생성해 주세요."
2. **포트 확인**: config.yaml에서 `chrome_debug_port` 읽기 (기본값: 9444)
   ```bash
   grep chrome_debug_port "$HOME/douzone-bot/config.yaml" 2>/dev/null
   ```
3. **Chrome 실행 확인**: 포트 소유 프로세스를 먼저 확인합니다 (중요: `curl`로 먼저 확인하지 마세요! Windows에서 비-HTTP 프로세스가 포트를 점유하면 `curl`이 영원히 멈춥니다).

   **Windows:**
   ```bash
   PID=$(netstat -ano 2>/dev/null | grep ":9444.*LISTENING" | awk '{print $5}' | head -1)
   [ -z "$PID" ] && echo "PORT_FREE" || powershell -Command "Get-Process -Id $PID -ErrorAction SilentlyContinue | Select-Object -ExpandProperty ProcessName" 2>/dev/null
   ```
   **macOS / Linux:**
   ```bash
   PID=$(lsof -ti :9444 2>/dev/null | head -1)
   [ -z "$PID" ] && echo "PORT_FREE" || ps -p $PID -o comm= 2>/dev/null
   ```

   - **PORT_FREE** 또는 **Chrome이 아닌 프로세스** → "자동화 Chrome이 실행되지 않고 있습니다. `/douzonebot:chrome`으로 Chrome을 실행해 주세요."
   - **Chrome 프로세스** → `curl`로 CDP 검증 가능 (Chrome이면 즉시 응답함)

## 실행 방식 선택

`$ARGUMENTS`에 방식이 지정되어 있으면 사용. 아니면 사용자에게 AskUserQuestion으로 질문:

- **대시보드 입력** (추천): 웹 대시보드에서 메모와 영수증을 입력
- **CLI — 전체 모드** (기본): 메모 파싱, 거래 매칭, 영수증 첨부까지 전부 처리
- **CLI — 간단 모드** (`--simple`): 모든 행에 본인 이름만 입력 (메모/영수증 불필요)

## 대시보드 입력 모드

1. **대시보드 서버 실행** — launcher.py가 포트 충돌 감지, 빈 포트 탐색, 서버 시작, health check를 자동 처리합니다:

   ```bash
   export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 NODE_NO_WARNINGS=1 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python ui/launcher.py --dashboard
   ```

   출력에서 `DASHBOARD_OK=true`와 `DASHBOARD_PORT=<포트>` 값을 확인합니다. 실패 시(`DASHBOARD_OK=false`) 에러 메시지를 사용자에게 안내합니다.

2. **브라우저 열기** (출력에서 확인한 포트로):
   - macOS: `open http://localhost:$PORT`
   - Windows: `powershell -Command "Start-Process 'http://localhost:$PORT'"`
   - Linux: `xdg-open http://localhost:$PORT`
   - 포트가 5000이 아니면 사용자에게 안내: "포트 5000이 사용 중이어서 $PORT로 대시보드를 열었습니다."

3. 사용자에게 안내:
   - "대시보드에서 메모와 영수증을 입력한 후 '저장' 버튼을 눌러주세요."
   - "저장이 완료되면 알려주세요."

4. 사용자가 저장 완료를 알리면, 저장된 데이터를 읽어서 변수에 저장:
   ```bash
   curl -s http://localhost:$PORT/data
   ```
   → 응답에서 `memo_path`, `receipts_path`, `user_name` 값을 추출

5. **전체 모드 실행** (아래 "전체 모드 — 단계별 실행" 섹션 참고)

## 입력 수집 (CLI 전체 모드)

CLI 전체 모드는 3가지 정보가 필요합니다. 순서대로 수집합니다:

1. **사용자 이름**: config.yaml의 `user_name` 값을 먼저 확인합니다:
   ```bash
   grep user_name "$HOME/douzone-bot/config.yaml" 2>/dev/null
   ```
   - 값이 있으면 사용
   - 없으면 사용자에게 직접 질문 (AskUserQuestion 사용 금지 — 자유 텍스트이므로 일반 대화로 질문). 입력받은 이름을 config.yaml에 저장:
   ```bash
   echo "user_name: <이름>" >> "$HOME/douzone-bot/config.yaml"
   ```

2. **메모 파일**: 사용자에게 메모 텍스트 파일 경로를 질문합니다 (예: `~/memo.txt`).
   - 파일 존재 확인: `test -f "<경로>" && echo "OK"`
   - 내용 미리보기 (첫 10줄): Read 도구로 확인
   - 메모가 없으면 `--memo` 플래그 생략 가능 (기본 참석자만 입력됨)

3. **영수증 폴더**: 사용자에게 영수증 이미지 폴더 경로를 질문합니다 (예: `~/receipts/`).
   - 폴더 존재 및 이미지 파일 개수 확인: `ls "<경로>"/*.{jpg,png,heic} 2>/dev/null | wc -l`
   - 영수증이 없으면 `--receipts` 플래그 생략 가능
   - **사전 OCR 지원**: 영수증 이미지 옆에 `.ocr.md` (또는 `.ocr.txt`, `.ocr.json`) 파일이 있으면 Vision AI를 건너뛰고 해당 텍스트를 직접 사용합니다. OCR이 미리 준비된 영수증 폴더도 그대로 사용 가능합니다.

## 간단 모드 실행

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 NODE_NO_WARNINGS=1 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python main.py --local -q --user "<이름>" --simple
```

## 전체 모드 — 단계별 실행

전체 모드는 3단계로 나눠서 실행합니다. 에이전트가 매칭 결과를 검토하고, 사용자에게 확인이 필요한 항목을 안내합니다.

모든 실행은 `uv run`을 사용합니다. BOT_DIR에서 실행합니다.
모든 명령에 PATH 설정을 포함합니다.

**먼저 임시 파일 경로를 생성합니다** (이후 단계에서 이 변수를 사용):

```bash
PLAN_FILE=$(mktemp "${TMPDIR:-/tmp}/douzone_plan_XXXXXX.json")
echo "PLAN_FILE=$PLAN_FILE"
```

### 1단계: 데이터 수집 및 매칭

더존 그리드 읽기, 메모 파싱, 영수증 OCR, 거래 매칭을 한 번에 실행하고 결과를 JSON으로 저장합니다:

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 NODE_NO_WARNINGS=1 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python main.py --local -q --user "<이름>" --memo <메모경로> --receipts <영수증경로> --stage2-cache-out "$PLAN_FILE" --stage2-only
```

- `--stage2-cache-out`: 매칭 결과를 JSON으로 저장
- `--stage2-only`: 매칭까지만 실행하고 중단 (자동 입력은 아직 안 함)
- 이 단계에서 오류가 나면 모니터링 섹션 참고하여 진단

### 2단계: 실행 계획 검토

매칭 결과를 로드하여 정리된 요약을 출력합니다:

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 NODE_NO_WARNINGS=1 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python main.py --local -q --user "<이름>" --stage2-cache-in "$PLAN_FILE" --review-only
```

- `--review-only`: 매칭 결과를 날짜별로 그룹화하여 보여주고, 확인이 필요한 항목을 ⚠️ 로 표시. 실행은 하지 않음.

터미널 출력을 읽고 사용자에게 결과를 안내합니다:

1. 요약 보고: 전체 행 수, 영수증 매칭 현황
2. **확인이 필요한 항목** (출력에서 ⚠️ 표시된 행):
   - 메모 할당이 필요한 항목 ("needs assignment" 메시지)
   - 영수증이 없는 항목 ("No receipt" 메시지)
   - 기타 낮은 신뢰도 항목
3. 확인이 필요한 항목이 있으면 사용자에게 대화형으로 안내합니다
4. 사용자가 승인하면 3단계로 진행합니다

### 3단계: 자동화 실행

매칭 결과를 로드하여 더존에 자동 입력합니다:

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 NODE_NO_WARNINGS=1 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python main.py --local -q --user "<이름>" --stage2-cache-in "$PLAN_FILE" --auto-approve
```

- `--stage2-cache-in`: 1단계에서 저장한 매칭 결과를 로드 (데이터 재수집 건너뜀)
- `--auto-approve`: 에이전트가 이미 2단계에서 검토했으므로 대화형 프롬프트 건너뜀

## 모니터링

자동화 실행 중 터미널 출력을 읽고 사용자에게 진행 상황을 한국어로 안내:
- 각 단계별 상태 (그리드 읽기, 메모 파싱, OCR, 매칭, 자동 입력)
- 오류 발생 시 진단:
  - CDP 오류: Chrome 연결 끊김 — 자동화 Chrome이 열려있는지 확인
  - LLM 오류: AI 제공자 문제 — `/douzonebot:preflight` 실행 제안
  - 그리드 오류: 더존 페이지가 STEP 2가 아닐 수 있음 — 사용자에게 확인 요청

## 완료 후 검토

자동화 완료 후 결과를 보고합니다:
- 처리된 거래 건수, 성공/실패 현황

다음 항목에 해당하는 행을 찾아 **사용자에게 수동 확인을 권장**합니다:
- 2단계에서 `needs_clarification: true`였던 행 (매칭이 불확실했던 항목)
- `pending_receipt: true`였던 행 (영수증 미첨부)
- `confidence: "LOW"`였던 행 (신뢰도가 낮았던 매칭)
- 자동화 실행 중 실패한 행

안내: "더존에서 위 항목들의 입력 내용을 한 번 확인해 주세요."

## 정리

대시보드 모드를 사용했으면 서버를 종료합니다:

```bash
curl -s -X POST http://localhost:$PORT/shutdown 2>/dev/null || true
```

대시보드를 사용하지 않은 경우 이 단계를 건너뜁니다.
