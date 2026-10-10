# Claude Desktop Plugin

[中文版](README.zh-CN.md)

A **Claude Code plugin** that visualizes your local Agent / Skill library in a browser: browse agents & skills, start Claude conversations, watch running tasks and chat history — **100% local, zero token cost, zero third-party dependencies**.

## What's Inside

```
claude-desktop-plugin/
├── .claude-plugin/
│   ├── plugin.json               # Plugin manifest
│   └── marketplace.json          # Marketplace declaration (source: ./)
├── skills/
│   └── claude-desktop/
│       ├── SKILL.md              # Tells Claude how to use / maintain the dashboard
│       └── dist/                 # Runable tool itself
│           ├── server.py         #   Backend (Python stdlib http.server)
│           ├── index.html        #   Frontend (single-file HTML/CSS/JS)
│           ├── 启动Claude桌面.bat #   One-click launcher (start server + open browser)
│           ├── README.md         #   Tool usage doc
│           └── quick_launch.example.json
└── agents/
    └── claude-desktop-builder.md # Complete knowledge base for the maintenance agent
```

## Install

After install, Claude automatically knows this plugin. Say things like "open the Claude desktop dashboard", "deploy this dashboard", or "dashboard XX isn't showing correctly" and it will act per `SKILL.md`.

**Method 1 (recommended): from GitHub marketplace** ✅ verified end-to-end
```bash
# Add the repo as a marketplace, then install
claude plugin marketplace add colin1992lalala/claude_desktop_builder
claude plugin install claude-desktop
```

**Method 2: copy directory / load from disk**
```bash
# Copy into your user skills dir — auto-discovered next session
cp -r claude-desktop-plugin ~/.claude/skills/claude-desktop

# Or load from disk in any session (temporary)
claude --plugin-dir /path/to/claude-desktop-plugin
```

**Method 3: run from the released zip**
Download `claude-desktop.zip` from **Releases** (or clone the repo), then double-click `启动Claude桌面.bat` inside — no install needed.

## Usage

```bash
# Start the dashboard (either way)
cd skills/claude-desktop/dist && python server.py --port 8723
# or double-click dist\启动Claude桌面.bat
# open http://127.0.0.1:8723 in your browser
```

Requires Python 3.9+ (stdlib only, no pip installs). Conversation/task features need `claude` on your PATH.

## Features

- 🔵 Live task panel (system jobs + dialog tasks + chat sessions, merged from 3 sources)
- 🤖 / 🧩 Browse all Agents / Skills (details, launch, start a new chat with one)
- 💬 Start Claude conversations (multi-tab, streaming, switch model / execution mode / permission mode)
- 📊 Token & cost stats + trend chart + today overview
- 🔍 Global full-text search across all conversations
- ⚡ Quick Launch + 🧩 Prompt template library
- 🗑 In-app confirm dialogs, 🚨 persistent error bar, 📱 touch support, 🌗 light/dark theme, 🔔 desktop notifications

## Development / Maintenance

Edit `dist/server.py` or `dist/index.html`, restart the server to apply.

- Backend: `python -m py_compile server.py` + `curl http://127.0.0.1:8723/api/overview` to check counts
- Frontend: open in browser, confirm no JS errors and card/session counts are correct
- Bump the `version` in `.claude-plugin/plugin.json` for each new release

## License

For personal / internal team reference only.
