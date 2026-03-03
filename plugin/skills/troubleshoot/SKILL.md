---
name: troubleshoot
description: Automatically diagnose and fix common douzone-bot errors. Run this skill when the user reports an error (e.g., "it failed", "CDP error", "time out", "에러 났어", "안 돼") or when a previous douzonebot skill throws an unexpected error.
disable-model-invocation: false
user-invocable: true
allowed-tools: Bash, Read, Grep, AskUserQuestion
argument-hint: "[error_message_or_symptom]"
---

# 더존 봇 문제 해결

**중요: 사용자에게 응답할 때 반드시 한국어로 답변하세요.**

사용자가 보고한 오류 메시지나 증상을 바탕으로 원인을 진단하고 해결책을 제시합니다.

## 경로 규칙

- **BOT_DIR**: 이 SKILL.md 파일에서 2단계 상위 디렉토리의 `bot/` 폴더 (플러그인에 내장된 봇 코드)
- **DATA_DIR**: `~/douzone-bot/` (사용자 설정 파일 — config.yaml)

모든 `uv run` 명령은 BOT_DIR에서 실행합니다.

## 단계 1: 증상 분석

`$ARGUMENTS`에 오류 메시지가 있으면 읽습니다. 없으면 사용자에게 구체적으로 어떤 오류가 났는지 물어보거나 최근 로그를 달라고 요청하세요.

알려진 주요 문제(`known-issues.md`)를 확인합니다. 가장 흔한 문제들은 다음과 같습니다:

1. **CDP Connection Failed / 포트 연결 불가**
   - 원인: 자동화용 Chrome이 닫혔거나 실행되지 않음.
   - 해결: `/douzonebot:chrome` 스킬을 사용하여 Chrome을 (재)실행합니다.

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

6. **config.yaml이 없음**
   - 원인: `/douzonebot:setup`이 실행되지 않아 설정 파일이 생성되지 않음.
   - 해결: `/douzonebot:setup`을 실행하여 config.yaml을 생성합니다.

## 단계 2: 진단 명령어 실행

필요에 따라 다음 점검을 은밀히(백그라운드에서) 실행해 봅니다:

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python main.py --local --preflight-only --user dummy
```

또는 config.yaml 확인:
```bash
cat "$HOME/douzone-bot/config.yaml"
```

## 단계 3: 해결책 제시 및 실행

- 사용자에게 친절하고 명확한 원인을 설명합니다.
- 복잡한 해결책(예: Chrome 재시작)의 경우, "제가 해결해 드릴까요?" 라고 묻고 `/douzonebot:chrome` 등을 연계하여 바로 처리해 줍니다.
- 스스로 해결할 수 없는 외부 요인(더존 서버 점검, 사용자 인증 만료)인 경우 사용자가 직접 해야 할 행동을 명확히 안내합니다.
