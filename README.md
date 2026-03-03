# Douzone Expense Automation Plugin

[Claude Code](https://claude.ai/claude-code) plugin that automates STEP 2 (지출정보등록) of Douzone's expense claim process.

- Reads your Douzone transaction grid automatically (Grid API)
- Parses memo files (참석자 정보) and receipt images (영수증 OCR)
- Matches memos/receipts to the correct transactions using AI
- Fills in attendees, purpose, content, and attaches receipts — row by row

## Install

```
/plugin marketplace add neosapience/douzonebot-plugin
/plugin install douzonebot
```

Then enable auto-update: `/plugin` → Marketplaces → `neosapience-douzonebot-plugin` → **Enable auto-update**

## Usage

```
/douzonebot:go
```

Or just ask in natural language:

```
더존 자동화 "해줘"
```

The agent handles everything: environment setup, Chrome launch, pre-flight checks, and automation execution.

## Skills

```
/douzonebot:go            End-to-end automation (recommended)
/douzonebot:setup         First-time setup wizard
/douzonebot:chrome        Launch automation Chrome
/douzonebot:preflight     Pre-flight health checks
/douzonebot:run           Run automation (standalone)
/douzonebot:troubleshoot  Diagnose errors
/douzonebot:uninstall     Clean removal
```

## Requirements

- [Claude Code](https://claude.ai/claude-code) installed
- Chrome browser (for Douzone web access)
- Active Douzone login session
