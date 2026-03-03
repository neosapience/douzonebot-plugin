---
name: setup
description: Set up douzone-bot from scratch on the user's machine. Use when the user wants to install, configure, or set up Douzone expense automation for the first time.
disable-model-invocation: false
user-invocable: true
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, WebFetch, AskUserQuestion
---

# 더존 봇 설치 마법사

사용자의 환경에 맞게 douzone-bot을 설정합니다. 각 단계를 친절하게 설명합니다.

> **참고**: 봇 코드는 이 플러그인에 내장되어 있으며 플러그인 디렉토리에서 직접 실행됩니다. 별도의 설치나 복사가 필요 없습니다.

## 경로 규칙

- **BOT_DIR**: 이 SKILL.md 파일에서 2단계 상위 디렉토리의 `bot/` 폴더 (플러그인에 내장된 봇 코드)
- **DATA_DIR**: `~/douzone-bot/` (사용자 설정 파일 — config.yaml만 저장)

모든 `uv run` 명령은 BOT_DIR에서 실행합니다.
config.yaml은 DATA_DIR에 저장합니다.

## 사전 확인

1. **OS 감지**: `uname -s` 또는 환경변수를 확인하여 macOS, Windows, Linux 판별
   - Windows: `$env:OS` 또는 `[System.Environment]::OSVersion` 확인
2. **Python 확인**: Python 3.11+ 설치 여부 확인 (`python --version`, Windows에서는 `python3`이 없을 수 있으므로 `python` 사용)
   - 미설치 시 OS별 설치 방법 안내
   - macOS: `brew install python` 또는 python.org에서 다운로드
   - Windows: Microsoft Store 또는 python.org에서 다운로드

## 단계 1: uv 설치 확인

`uv`는 Python 패키지를 자동으로 관리하는 도구입니다. pip install이 필요 없습니다.

1. `uv --version` 으로 설치 여부 확인
2. 설치되지 않았으면:
   - **macOS / Linux:**
     ```bash
     curl -LsSf https://astral.sh/uv/install.sh | sh
     ```
   - **Windows (PowerShell):**
     ```powershell
     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
     ```
3. 설치 후 `uv --version`으로 확인

> "uv가 설치되면 Python 패키지를 자동으로 관리합니다. pip install을 따로 할 필요가 없어요."

## 단계 2: AI 제공자 선택

Claude Code CLI가 이미 설치되어 있으므로 (이 플러그인을 사용 중이면 당연히 있음), 별도의 AI 제공자 설정이 필요 없습니다.

- **영수증 OCR**: Claude Code CLI (sonnet 모델) — 자동 사용됨
- **메모 매칭/LLM**: Claude Code CLI — 자동 사용됨
- 추가 API 키나 설정 불필요

## 단계 3: config.yaml 생성

1. DATA_DIR (`~/douzone-bot/`) 디렉토리가 있는지 확인하고 없으면 생성:
   ```bash
   mkdir -p "$HOME/douzone-bot"
   ```

2. `~/douzone-bot/config.yaml`이 이미 있으면 내용을 표시하고 건너뛰기

3. 없으면 Write 도구로 `~/douzone-bot/config.yaml` 생성:
     ```yaml
     mode: local
     ```
   - `user_name`은 여기서 물어보지 않습니다 — 실행 시 웹 대시보드나 CLI에서 입력합니다.
   - Claude Code CLI가 기본 OCR/LLM 제공자이므로 providers 섹션은 선택사항

## 단계 4: Chrome 설정

config.yaml의 `chrome_debug_port` 값을 확인합니다 (기본값: 9444). 아래 명령의 포트 번호를 그에 맞게 변경합니다.

별도의 Chrome 인스턴스 방식을 설명합니다:

> "자동화 전용 Chrome 창을 별도로 실행합니다. 기존에 사용하시는 Chrome은 그대로 열려 있습니다."

OS별 실행 명령어 안내:

**macOS:**
```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --remote-debugging-port=9444 \
    --user-data-dir="$HOME/.douzone-chrome" \
    "--remote-allow-origins=*" &
```

**Windows (PowerShell):**
```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
    --remote-debugging-port=9444 `
    --user-data-dir="$env:USERPROFILE\.douzone-chrome" `
    --remote-allow-origins=*
```

**Windows (cmd.exe):**
```cmd
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
    --remote-debugging-port=9444 ^
    --user-data-dir="%USERPROFILE%\.douzone-chrome" ^
    --remote-allow-origins=*
```

**Linux:**
```bash
google-chrome \
    --remote-debugging-port=9444 \
    --user-data-dir="$HOME/.douzone-chrome" \
    "--remote-allow-origins=*" &
```

사용자에게 안내:
- 새 Chrome 창이 열립니다 - 이것이 자동화 전용 Chrome입니다
- 이 창에서 더존에 로그인하고 경비 프로세스 절차에서 STEP 1까지 완료한 뒤 '지출정보등록 (STEP 2)' 화면으로 이동하세요 (처음 한 번만)
- 프로필이 저장되므로 다음에는 로그인이 유지됩니다

## 단계 5: 점검

`/douzonebot:preflight`를 실행하여 모든 것이 정상인지 확인합니다.

## 단계 6: 대시보드 소개

설정이 완료되면 웹 대시보드를 소개합니다:

> "이제 웹 대시보드를 사용할 수 있습니다. 대시보드에서 메모와 영수증을 입력할 수 있습니다."

실행 방법 안내:
```bash
export PATH="$HOME/.local/bin:$USERPROFILE/.local/bin:$PATH" PYTHONIOENCODING=utf-8 && cd "<BOT_DIR>" && uv run --with-requirements requirements-local.txt python ui/launcher.py
```

설명:
- 대시보드 서버(Flask)와 자동화 Chrome이 함께 실행됩니다
- 브라우저에서 `http://localhost:5000` 이 자동으로 열립니다
- 대시보드에서 메모와 영수증을 입력하고 저장합니다
- 실제 자동화는 Claude가 터미널에서 실행하고 모니터링합니다
- 또는 CLI로도 실행 가능: `/douzonebot:run`

## 단계 7: 자동 업데이트 활성화

설정이 완료되면 플러그인 자동 업데이트를 안내합니다:

> "마지막으로 플러그인 자동 업데이트를 켜면 새 버전이 나올 때 자동으로 반영됩니다."

사용자에게 다음 단계를 안내합니다:
1. Claude Code에서 `/plugin` 입력
2. **Marketplaces** 탭 선택
3. `neosapience-douzonebot-plugin` 마켓플레이스 선택
4. **Enable auto-update** 클릭

> 이 설정은 한 번만 하면 됩니다. 이후에는 Claude Code 세션 시작 시 자동으로 최신 버전을 받습니다.

## 어조

- 격려하고 인내심 있게 안내
- 각 단계가 왜 필요한지 간단히 설명
- 문제가 발생하면 다음 단계로 넘어가기 전에 진단하고 해결책 제시
- 전문 용어는 최소화하고 쉬운 한국어로 설명
