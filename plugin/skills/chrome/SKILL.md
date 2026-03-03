---
name: chrome
description: Launch or restart the Chrome browser in automation mode (remote debugging enabled). Use when the CDP connection fails in preflight/run.
disable-model-invocation: false
user-invocable: true
allowed-tools: Bash, Read, AskUserQuestion
---

# 더존 자동화용 Chrome 실행

더존 지출 증빙 자동화를 위해 원격 디버깅 모드가 켜진 별도의 Chrome 창을 엽니다.

**중요: Claude Code는 Windows에서도 bash (Git Bash) 쉘을 사용합니다. PowerShell/cmd 문법이 아닌 bash 문법으로 실행하세요.**

## 경로 규칙

- **BOT_DIR**: 이 SKILL.md 파일에서 2단계 상위 디렉토리의 `bot/` 폴더 (플러그인에 내장된 봇 코드)
- **DATA_DIR**: `~/douzone-bot/` (사용자 설정 파일 — config.yaml)

모든 `uv run` 명령은 BOT_DIR에서 실행합니다.

## 단계 1: Chrome 실행

launcher.py가 자동으로 Chrome 경로를 탐색하고, 포트 충돌을 감지하여 빈 포트를 찾고, config.yaml을 업데이트합니다:

```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python ui/launcher.py --chrome
```

출력 예시:
```
[+] Launching automation Chrome → https://erp.neosapience.com
[+] Chrome listening on port 9444
CHROME_PORT=9444
CHROME_OK=true
CHROME_PATH=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome
```

포트 충돌이 있으면 자동으로 처리:
```
[+] Port 9444 occupied, using 9445 instead
[+] Updated chrome_debug_port: 9445 in ~/douzone-bot/config.yaml
[+] Launching automation Chrome → https://erp.neosapience.com
[+] Chrome listening on port 9445
CHROME_PORT=9445
CHROME_OK=true
```

## 단계 2: 실행 확인

출력에서 `CHROME_OK=true`이면 성공. CDP 연결을 추가 확인:

```bash
CDP_PORT=$(grep chrome_debug_port "$HOME/douzone-bot/config.yaml" 2>/dev/null | awk '{print $2}')
CDP_PORT=${CDP_PORT:-9444}
curl -s --connect-timeout 3 http://localhost:$CDP_PORT/json/version && echo "CDP_OK"
```

## 단계 3: 후속 안내

성공 시:
- "자동화 전용 Chrome 창이 열렸습니다."
- "더존 그룹웨어 페이지가 자동으로 열렸습니다. 로그인 후 경비 프로세스 절차에서 STEP 1까지 완료한 뒤 '지출정보등록 (STEP 2)' 화면으로 이동해 주세요."
- "준비가 완료되면 `/douzonebot:preflight`로 점검하거나 `/douzonebot:go`로 자동화를 시작하세요."

실패 시 (`CHROME_OK=false`):
- Chrome이 설치되어 있는지 확인
- 수동 실행: 아래 OS별 명령 참고

## 수동 실행 (launcher.py 실패 시 폴백)

**macOS:**
```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9444 --user-data-dir="$HOME/.douzone-chrome" "--remote-allow-origins=*" "https://erp.neosapience.com" &
```

**Windows (bash에서):**
```bash
"/c/Program Files/Google/Chrome/Application/chrome.exe" --remote-debugging-port=9444 --user-data-dir="$USERPROFILE/.douzone-chrome" --remote-allow-origins=* "https://erp.neosapience.com" &
```

**Linux:**
```bash
google-chrome --remote-debugging-port=9444 --user-data-dir="$HOME/.douzone-chrome" "--remote-allow-origins=*" "https://erp.neosapience.com" &
```

> 포트 번호는 config.yaml의 `chrome_debug_port` 값으로 변경 가능합니다 (기본값: 9444).
> URL은 config.yaml의 `douzone_url` 값으로 변경 가능합니다 (기본값: `https://erp.neosapience.com`).
