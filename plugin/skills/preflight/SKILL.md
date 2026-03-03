---
name: preflight
description: Run pre-flight checks for douzone-bot. Use when the user wants to verify their setup is working, check API connections, or diagnose why automation isn't starting.
disable-model-invocation: false
user-invocable: true
allowed-tools: Bash, Read, Glob, Grep
---

# 더존 봇 사전 점검

**중요: 사용자에게 응답할 때 반드시 한국어로 답변하세요.**

사전 점검을 실행하여 필요한 API와 연결이 정상적으로 작동하는지 확인합니다.

## 경로 규칙

- **BOT_DIR**: 이 SKILL.md 파일에서 2단계 상위 디렉토리의 `bot/` 폴더 (플러그인에 내장된 봇 코드)
- **DATA_DIR**: `~/douzone-bot/` (사용자 설정 파일 — config.yaml)

모든 `uv run` 명령은 BOT_DIR에서 실행합니다.

## 단계

1. **config.yaml 확인**:
   - `~/douzone-bot/config.yaml` 찾기
   - 없으면: "config.yaml 파일이 없습니다. `/douzonebot:setup`으로 생성해 주세요."
   - 있으면 설정된 모드와 AI 제공자(provider)를 보고

2. **점검 실행**:
   ```bash
   export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python main.py --local --preflight-only --user dummy
   ```

3. **결과 해석**:
   - 출력에서 `[PASS]`, `[FAIL]`, `[WARN]` 라인을 파싱
   - 각 `[FAIL]`에 대해 원인과 해결 방법을 쉬운 한국어로 설명:
     - **CDP 연결 실패**: "Chrome 자동화 창이 실행되지 않고 있습니다. `/douzonebot:chrome`으로 실행하거나 Chrome 설정 안내를 확인하세요."
     - **Claude Code CLI 실패**: "Claude CLI 인증이 안 되어 있습니다. `claude /login`으로 로그인하세요."
   - `[PASS]`는 간단히 확인 메시지
   - `[WARN]`은 선택사항임을 설명

4. **요약**: 최종 결과를 명확히 전달:
   - 모두 통과: "모든 점검을 통과했습니다. `/douzonebot:run`으로 자동화를 실행할 수 있습니다."
   - 실패 있음: "N개의 점검이 실패했습니다. 위의 문제를 해결한 후 `/douzonebot:preflight`를 다시 실행하세요."
