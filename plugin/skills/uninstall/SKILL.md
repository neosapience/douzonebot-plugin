---
name: uninstall
description: "Completely remove douzone-bot and revert all changes made by the plugin. Use when user says \"더존 삭제\", \"더존 제거\", \"uninstall douzone\", \"remove douzone\", \"원래대로\", \"초기화\", \"다 지워줘\", or \"클린업\"."
disable-model-invocation: false
user-invocable: true
allowed-tools: Bash, Read, AskUserQuestion
---

# 더존 봇 완전 제거

이 스킬은 더존 봇 설치 과정에서 생성된 모든 파일과 설정을 제거하여 PC를 원래 상태로 되돌립니다.

**중요: Claude Code는 Windows에서도 bash (Git Bash) 쉘을 사용합니다.**

## 제거 대상 확인

먼저 각 항목의 존재 여부를 **순서대로 하나씩** 확인합니다:

```bash
# 1. 사용자 데이터
test -d "$HOME/douzone-bot" && echo "FOUND: ~/douzone-bot" || echo "NOT_FOUND: ~/douzone-bot"
```

```bash
# 2. 자동화 Chrome 프로필
test -d "$HOME/.douzone-chrome" && echo "FOUND: ~/.douzone-chrome" || echo "NOT_FOUND: ~/.douzone-chrome"
```

```bash
# 3. uv 패키지 매니저
uv --version 2>/dev/null && echo "FOUND: uv" || echo "NOT_FOUND: uv"
```

```bash
# 4. uv 캐시 (Python 패키지 캐시)
test -d "$HOME/.cache/uv" && echo "FOUND: ~/.cache/uv" || echo "NOT_FOUND: ~/.cache/uv"
# Windows에서는:
test -d "$LOCALAPPDATA/uv" && echo "FOUND: $LOCALAPPDATA/uv" || echo "NOT_FOUND"
```

```bash
# 5. 실행 중인 더존 봇 프로세스 전체 확인
# macOS / Linux:
echo "--- 더존 봇 프로세스 ---"
pgrep -af "ui/server.py\|server\.py.*--port" 2>/dev/null && echo "  → Web UI 실행 중" || true
pgrep -af "main.py.*--user\|main.py.*--simple\|main.py.*--local" 2>/dev/null && echo "  → 파이프라인 실행 중" || true
pgrep -af "launcher.py" 2>/dev/null && echo "  → 런처 실행 중" || true
```

```bash
# Windows — 패턴 + 포트 기반 감지 (uv run 실행 시 CommandLine에 server.py가 안 나올 수 있음):
echo "--- 프로세스 패턴 감지 ---"
powershell -Command "
  Get-CimInstance Win32_Process |
  Where-Object { \$_.CommandLine -match 'server\.py|main\.py|launcher\.py' } |
  ForEach-Object { 'RUNNING: ' + \$_.Name + ' (PID: ' + \$_.ProcessId + ') — ' + \$_.CommandLine.Substring(0, [Math]::Min(100, \$_.CommandLine.Length)) }
" 2>/dev/null || true

echo "--- 대시보드 포트 감지 (5000-5010) ---"
for port in 5000 5001 5002 5003 5004 5005 5006 5007 5008 5009 5010; do
    PID=$(netstat -ano 2>/dev/null | grep ":$port.*LISTENING" | awk '{print $5}' | head -1)
    if [ -n "$PID" ]; then
        echo "RUNNING: 포트 $port 에서 프로세스 감지됨 (PID: $PID)"
    fi
done
```

```bash
# 6. 자동화 Chrome 프로세스 (기본 포트 9444 확인)
for CDP_PORT in 9444 9445 9446 9222; do
    curl -s --connect-timeout 2 http://localhost:$CDP_PORT/json/version 2>/dev/null && echo "RUNNING: Chrome :$CDP_PORT" && break
done || echo "NOT_RUNNING"
```

## 사용자 확인

발견된 항목을 목록으로 보여주고 AskUserQuestion으로 확인합니다:

> "다음 항목을 제거합니다:"
> - 실행 중인 봇 프로세스 종료 (웹 대시보드, 파이프라인, 런처)
> - 자동화 Chrome 프로세스 종료
> - ~/douzone-bot (세션 데이터)
> - ~/.douzone-chrome (자동화 Chrome 프로필 — 더존 로그인 정보 포함)
> - uv (Python 패키지 매니저)
> - uv 캐시
>
> "참고: 플러그인 자체(douzonebot)는 Claude Code settings에서 별도로 제거해야 합니다."
>
> "정말 모두 제거할까요?"

**사용자가 승인한 경우에만 제거를 진행합니다.**

## 제거 실행

### 1. 더존 봇 프로세스 전체 종료

웹 대시보드(`ui/server.py`), 파이프라인(`main.py`), 런처(`launcher.py`) 등 더존 봇이 생성한 모든 Python 프로세스를 종료합니다:

**macOS / Linux:**
```bash
# Step 1: 프로세스 패턴으로 종료
for pattern in "ui/server.py" "server.py.*--port" "main.py.*--user" "main.py.*--simple" "main.py.*--local" "launcher.py"; do
    PIDS=$(pgrep -f "$pattern" 2>/dev/null)
    if [ -n "$PIDS" ]; then
        kill $PIDS && echo "종료됨 ($pattern): PID $PIDS"
    fi
done

# Step 2: 대시보드 포트로 종료 (uv run 경유 시 패턴 매칭 누락 방지)
for port in $(seq 5000 5010); do
    PID=$(lsof -ti :$port 2>/dev/null)
    if [ -n "$PID" ]; then
        kill $PID && echo "종료됨: 포트 $port (PID: $PID)"
    fi
done
echo "✓ 봇 프로세스 정리 완료"
```

**Windows (bash에서):**
```bash
# Step 1: 프로세스 패턴으로 종료
powershell -Command "
  \$patterns = @('server\.py', 'main\.py', 'launcher\.py')
  \$killed = 0
  foreach (\$pat in \$patterns) {
    Get-CimInstance Win32_Process |
    Where-Object { \$_.CommandLine -match \$pat } |
    ForEach-Object {
      Stop-Process -Id \$_.ProcessId -Force
      Write-Output ('종료됨 (' + \$pat + '): PID ' + \$_.ProcessId)
      \$killed++
    }
  }
  if (\$killed -eq 0) { Write-Output '패턴 매칭 프로세스 없음' }
  else { Write-Output ('✓ ' + \$killed + '개 프로세스 종료됨') }
" 2>/dev/null

# Step 2: 대시보드 포트로 종료 (uv run 경유 시 패턴 매칭 누락 방지)
echo "--- 대시보드 포트 정리 (5000-5010) ---"
for port in 5000 5001 5002 5003 5004 5005 5006 5007 5008 5009 5010; do
    PID=$(netstat -ano 2>/dev/null | grep ":$port.*LISTENING" | awk '{print $5}' | head -1)
    if [ -n "$PID" ]; then
        taskkill //PID $PID //F 2>/dev/null && echo "종료됨: 포트 $port (PID: $PID)"
    fi
done
echo "✓ 포트 정리 완료"
```

### 2. 자동화 Chrome 종료

자동화 Chrome이 사용할 수 있는 포트들을 확인하고 종료합니다:

**macOS / Linux:**
```bash
KILLED=false
for CDP_PORT in 9444 9445 9446 9222; do
    CHROME_PID=$(lsof -ti :$CDP_PORT 2>/dev/null)
    if [ -n "$CHROME_PID" ]; then
        kill $CHROME_PID && echo "Chrome 프로세스 종료됨 (포트: $CDP_PORT, PID: $CHROME_PID)"
        KILLED=true
    fi
done
$KILLED || echo "실행 중인 자동화 Chrome 없음"
```

**Windows (bash에서):**
```bash
KILLED=false
for CDP_PORT in 9444 9445 9446 9222; do
    CHROME_PID=$(netstat -ano 2>/dev/null | grep ":$CDP_PORT" | grep "LISTENING" | awk '{print $5}' | head -1)
    if [ -n "$CHROME_PID" ]; then
        taskkill //PID $CHROME_PID //F 2>/dev/null && echo "Chrome 프로세스 종료됨 (포트: $CDP_PORT, PID: $CHROME_PID)"
        KILLED=true
    fi
done
$KILLED || echo "실행 중인 자동화 Chrome 없음"
```

### 3. 사용자 데이터 제거

```bash
rm -rf "$HOME/douzone-bot" && echo "✓ ~/douzone-bot 제거됨"
```

### 4. Chrome 프로필 제거

```bash
rm -rf "$HOME/.douzone-chrome" && echo "✓ ~/.douzone-chrome 제거됨"
```

### 5. uv 캐시 제거

**macOS / Linux:**
```bash
rm -rf "$HOME/.cache/uv" && echo "✓ uv 캐시 제거됨"
```

**Windows (bash에서):**
```bash
rm -rf "$LOCALAPPDATA/uv" 2>/dev/null; rm -rf "$HOME/.cache/uv" 2>/dev/null; echo "✓ uv 캐시 제거됨"
```

### 6. uv 제거 (선택)

AskUserQuestion으로 사용자에게 uv도 제거할지 질문합니다:
> "uv는 다른 Python 프로젝트에서도 사용할 수 있는 범용 도구입니다. uv도 제거할까요?"

사용자가 제거를 선택한 경우:

**macOS / Linux:**
```bash
rm -f "$HOME/.local/bin/uv" "$HOME/.local/bin/uvx" && echo "✓ uv 제거됨"
```

**Windows (bash에서):**
```bash
rm -f "$USERPROFILE/.local/bin/uv.exe" "$USERPROFILE/.local/bin/uvx.exe" 2>/dev/null
rm -f "$HOME/.local/bin/uv.exe" "$HOME/.local/bin/uvx.exe" 2>/dev/null
echo "✓ uv 제거됨"
```

## 완료 안내

제거 완료 후 사용자에게 안내:

> "✓ 더존 봇이 완전히 제거되었습니다."
>
> "제거된 항목:"
> - 봇 프로세스 종료 (웹 대시보드, 파이프라인, 런처)
> - 자동화 Chrome 프로세스 종료
> - ~/douzone-bot (세션 데이터)
> - ~/.douzone-chrome (자동화 Chrome 프로필)
> - uv 캐시
> - [uv (선택한 경우)]
>
> "PC가 설치 전 상태로 돌아갔습니다."
> "플러그인을 완전히 제거하려면 Claude Code settings에서 douzonebot 플러그인을 제거하세요."
> "다시 설치하려면 `/douzonebot:go`를 실행하세요."
