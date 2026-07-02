# Douzonebot Plugin Package

This zip contains a self-contained Douzone expense automation plugin bundle for Codex/Claude users.

## Contents

- `douzonebot/.claude-plugin/plugin.json` - plugin metadata
- `douzonebot/skills/go/SKILL.md` - main workflow skill
- `douzonebot/skills/troubleshoot/SKILL.md` - troubleshooting skill
- `douzonebot/skills/uninstall/SKILL.md` - cleanup skill
- `douzonebot/bot/` - Python automation runtime
- `douzonebot/CLAUDE.md` - user-facing usage guide

Runtime files, logs, local caches, screenshots, virtualenvs, and `.env` files are intentionally excluded.

## Install For Claude/Codex

Unzip the package, then copy or move the `douzonebot` folder into your plugin/skill location.

Typical local plugin cache layout:

```bash
mkdir -p ~/.codex/plugins/cache/douzonebot-plugin/douzonebot
cp -R douzonebot ~/.codex/plugins/cache/douzonebot-plugin/douzonebot/0.4.2
```

If your environment uses Claude plugin folders instead, place the `douzonebot` folder wherever your Claude plugin loader expects plugin bundles.

## Use

In Codex/Claude, ask:

```text
더존 자동화 해줘
```

or invoke the skill directly if your client supports skill names:

```text
/douzonebot:go
```

The workflow is:

1. Check environment and install/use `uv`
2. Launch automation Chrome
3. Run preflight
4. Collect memo and receipt inputs
5. Review matches with the user before execution
6. Fill Douzone STEP 2
7. Verify warnings and cleanup

Meal rows must verify `용도` and `내용` by transaction or matched receipt time:

- `10:30-14:00` -> `100. 중식대 / 점심식사`
- `17:00-21:00` -> `110. 석식대 / 저녁식사`

For delivery/PG rows such as KCP, NICE, Inicis, Baemin, or Coupang Eats, use the actual restaurant/supplier from the receipt, not the payment platform name.

## Requirements

- macOS, Windows Git Bash, or Linux
- Chrome
- `uv` (the skill can guide installation)
- Access to Douzone/Amaranth expense STEP 2
- Memo text file and receipt folder for full mode

