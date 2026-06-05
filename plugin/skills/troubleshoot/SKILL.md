---
name: troubleshoot
description: Automatically diagnose and fix common douzone-bot errors, OR resolve unfixable post-verify issues (e.g., 미등록 PG 패턴) via the bounded agent-extension flow. Run this skill when the user reports an error ("it failed", "CDP error", "time out", "에러 났어", "안 돼") OR mentions post-verify findings ("리포트에 X 떴어", "verify에서 미등록 거래처", "STAGE 6에서 X").
disable-model-invocation: false
user-invocable: true
allowed-tools: Bash, Read, Edit, Grep, AskUserQuestion
argument-hint: "[error_message_or_symptom]"
---

# 더존 봇 문제 해결

**중요: 사용자에게 응답할 때 반드시 한국어로 답변하세요.**

사용자가 보고한 오류 메시지나 증상을 바탕으로 원인을 진단하고 해결책을 제시합니다.

## 경로 규칙

- **BOT_DIR**: 이 SKILL.md 파일의 grandparent 디렉토리 아래 `bot/` 폴더. 예: `plugin/skills/<skill>/SKILL.md` → `plugin/skills/` → `plugin/` → `plugin/bot/`
- **DATA_DIR**: `~/douzone-bot/` (사용자 데이터)

모든 `uv run` 명령은 BOT_DIR에서 실행합니다.

## 단계 1: 증상 분석

`$ARGUMENTS`에 오류 메시지가 있으면 읽습니다. 없으면 사용자에게 구체적으로 어떤 오류가 났는지 물어보거나 최근 로그를 달라고 요청하세요.

알려진 주요 문제(`known-issues.md`)를 확인합니다. 가장 흔한 문제들은 다음과 같습니다:

1. **CDP Connection Failed / 포트 연결 불가**
   - 원인: 자동화용 Chrome이 닫혔거나 실행되지 않음.
   - 해결: `/douzonebot:go`를 실행하면 Chrome을 자동으로 (재)실행합니다.

2. **팝업 요소를 찾을 수 없음 (Timeout Error)**
   - 원인: 더존 화면이 '지출정보등록 (STEP 2)' 탭이 아니거나, 로그인 세션이 만료됨.
   - 해결: 사용자에게 알맞은 화면(`STEP 2`)인지 확인시키고 재시도를 안내합니다.

3. **OCR 실패 또는 LLM Provider 인증 오류**
   - 원인: Claude Code CLI 로그인 세션이 만료됨 (영수증 OCR에 sonnet 모델 사용 중).
   - 해결: `claude /login`으로 재인증 안내.

4. **행 수 불일치 / 화면 스크롤 문제**
   - 원인: 그리드 API가 데이터를 다 못 읽음 (드문 경우).
   - 해결: 브라우저 캐시 문제일 수 있으므로 더존 탭을 새로고침(F5)하고 다시 시도하라고 안내.

5. **uv 관련 오류 (ModuleNotFoundError, "uv not found")**
   - 원인: uv가 설치되지 않았거나, PATH에 없거나, 의존성 해결 실패.
   - 해결: `uv --version` 확인 후 재설치 안내. 자세한 내용은 `known-issues.md` 참조.

6. **환경 설정 누락**
   - 원인: 초기 설정(uv 설치 등)이 완료되지 않음.
   - 해결: `/douzonebot:go`를 실행하면 자동으로 환경을 설정합니다.

## 단계 2: 진단 명령어 실행

필요에 따라 다음 점검을 은밀히(백그라운드에서) 실행해 봅니다:

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python main.py --local --preflight-only --user dummy
```

또는 Chrome CDP 연결 확인 (여러 포트 시도):
```bash
for port in 9444 9445 9446 9222; do
    curl -s --connect-timeout 2 http://localhost:$port/json/version 2>/dev/null && echo "CDP_OK port=$port" && break
done || echo "CDP_FAIL"
```

## 단계 3: 해결책 제시 및 실행

- 사용자에게 친절하고 명확한 원인을 설명합니다.
- 복잡한 해결책(예: Chrome 재시작)의 경우, "제가 해결해 드릴까요?" 라고 묻고 `/douzonebot:go`를 연계하여 바로 처리해 줍니다.
- 스스로 해결할 수 없는 외부 요인(더존 서버 점검, 사용자 인증 만료)인 경우 사용자가 직접 해야 할 행동을 명확히 안내합니다.

---

## 단계 4 (별도 트랙): 사후 검증 결과 해결 (Post-Verify Resolution)

파이프라인 STAGE 6 리포트가 `🆕 미등록 PG 패턴` 등 자동 처리되지 않은
이슈를 남겼거나, 사용자가 "verify에서 X 떴어", "리포트에 미등록 거래처가
나왔어"처럼 사후 검증 결과를 언급할 때 진입합니다.

### 4-1. 이슈 확인

```bash
python3 -c "
import json, sys
from pathlib import Path
p = Path.home() / 'douzone-bot' / 'cache' / 'execution_plan.json'
if not p.exists():
    sys.exit('no plan.json')
data = json.loads(p.read_text(encoding='utf-8'))
issues = (data.get('post_verification') or {}).get('issues', [])
for i in issues:
    if (not i.get('resolved')) or i.get('issue_type') == 'unknown_pattern':
        print(f\"row {i['row_numbers']}: {i['issue_type']} — {i['merchant']} — {i['description']}\")
"
```

`merchant`, `row_numbers`, `description`을 사용자에게 보여주고 무엇을
처리할지 합의합니다.

### 4-2. 처리 방식 (둘 중 하나만)

**A. 데이터-only 추가 (제일 흔함)**
- 새 거래처를 `models.py`의 `SUPPLIER_REQUIRED_MERCHANTS` 또는
  `RECEIPT_REQUIRED_MERCHANTS` 리스트에 추가.
- 사용자에게 한국어로 한 번 확인: "X가 PG/대행사 거래처가 맞나요? 영수증 +
  실공급자 입력이 필요한 곳이라면 룰에 추가하겠습니다."
- 승인 시 단 한 줄 추가.

**B. 새 operation 추가 (드물지만 가능)**
- `operations.py`에 새 async 헬퍼 작성 (예: `split_attendee`,
  `fill_overseas_supplier`).
- **반드시 기존 함수 / 기존에 사용 중인 `auto.<method>`만 사용**.
  automation.py에 새 메서드 호출 또는 추가는 금지 — 이는 별도 PR로 분리.

### 4-3. 편집 허용 범위 (Hard Rule)

```
허용 (이 스킬에서 수정 가능):
  plugin/bot/src/operations.py
  plugin/bot/src/models.py    (상수 리스트 추가만)

금지 (절대 수정 금지):
  automation.py / orchestrator.py / pipeline.py / ocr.py / 그 외 *.py
```

이 범위를 벗어나는 변경이 필요해 보이면 사용자에게 보고하고 멈춥니다:
"이 변경은 자동 확장 범위 밖입니다 — 별도로 PR이 필요합니다."

### 4-4. 검증 게이트 (필수)

편집 전 백업 → 편집 → 검증. `REJECT` 시 절대 진행 금지, 반드시 백업 복원:

```bash
cd "<BOT_DIR>"
cp src/operations.py src/operations.py.before
# ... 여기서 편집 도구로 src/operations.py 수정 ...
python3 src/validate_extension.py src/operations.py \
  --baseline src/operations.py.before
```

검증기는 stdlib만 사용하므로 `uv run` 불필요 — 시스템 python3로 충분.

- `OK` → 진행 (백업은 단계 4-7 이후 삭제)
- `REJECT:` → 사유를 사용자에게 보여주고 `mv src/operations.py.before
  src/operations.py`로 복원

### 4-5. 플러그인↔루트 동기화

`<BOT_DIR>/../../src/`가 존재하면(개발 저장소), **같은 diff를 양쪽에 모두**
Edit 도구로 적용합니다. `cp`로 덮어쓰지 마세요 — 양쪽이 다른 이유로
달라져 있을 수 있고, 그 차이를 잃습니다.

```bash
ROOT_SRC="<BOT_DIR>/../../src"
test -d "$ROOT_SRC" && diff -q "<BOT_DIR>/src/operations.py" "$ROOT_SRC/operations.py"
test -d "$ROOT_SRC" && diff -q "<BOT_DIR>/src/models.py" "$ROOT_SRC/models.py"
```

차이가 있으면 사용자에게 보고하고 멈춥니다 (sync 손상 가능성).
사용자 환경(`~/douzone-bot/`만 있는 경우) `$ROOT_SRC`가 없으므로 이 단계를
건너뜁니다.

### 4-6. 한 행 dry-run

새 operation을 추가한 경우, 한 행에만 시범 적용해 사용자가 Chrome에서
확인하게 합니다 (데이터-only 추가는 dry-run 생략 가능, 다음 파이프라인
실행 때 자동 적용됨):

```bash
cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt \
  python -c "
import asyncio
from src.operations import connect, disconnect, <new_op>
async def main():
    auto = await connect('http://localhost:9444')
    ok = await <new_op>(auto, row=<N>, ...)
    print('OK' if ok else 'FAIL')
    await disconnect(auto)
asyncio.run(main())
"
```

사용자가 "맞다" 확인 → 진행. "아니다"면 백업 복원.

### 4-7. 정리 + 커밋 제안

성공 시 백업 삭제 후, 사용자에게 커밋 제안 (CLAUDE.md 트레일러 형식 따라):

```bash
rm -f "<BOT_DIR>/src/operations.py.before"
```

```
feat(merchants): add <merchant> to SUPPLIER_REQUIRED list

Constraint: User-confirmed during YYYY-MM-DD troubleshoot session
Confidence: medium
Scope-risk: narrow
```

사용자가 직접 커밋할지 또는 다음 릴리스 때 묶을지 선택하게 합니다.
