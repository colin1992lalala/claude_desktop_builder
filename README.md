# Claude Desktop Plugin

一个 Claude Code 插件：在本机浏览器里可视化浏览你的 Agent / Skill、发起 Claude 对话、查看任务进度与聊天
记录 —— **纯本地、零 token、零第三方依赖**。

## 包含什么

```
claude-desktop-plugin/
├── .claude-plugin/plugin.json          # 插件清单
├── skills/
│   └── claude-desktop/
│       ├── SKILL.md                    # 告诉 Claude 怎么用/维护仪表盘
│       └── dist/                       # 可运行工具本体
│           ├── server.py               #   后端（Python 标准库 http.server）
│           ├── index.html              #   前端（单文件 HTML/CSS/JS）
│           ├── 启动Claude桌面.bat       #   一键启动（自动起服务 + 开浏览器）
│           ├── README.md               #   工具使用文档
│           └── quick_launch.example.json
└── agents/
    └── claude-desktop-builder.md       # 维护 Agent 完整知识库
```

## 安装

**方式一：从目录加载（最稳）**
把本目录放到一个位置，然后在任意 Claude Code 会话里带 `--plugin-dir` 启动，或复制到 `~/.claude/skills/cl
aude-desktop/`：
```bash
# 放在 skills 目录下自动被发现（下次会话生效）
cp -r claude-desktop-plugin ~/.claude/skills/claude-desktop
# 或从磁盘加载
claude --plugin-dir /path/to/claude-desktop-plugin
```

**方式二：从 GitHub 安装（发布后）**
```bash
claude plugin install claude-desktop@<你的GitHub仓库>
```

安装后，Claude 会自动知道这个插件：当你说「打开 Claude 桌面仪表盘」「部署这个仪表盘」「仪表盘 XX 显示不
对」时，它会按 `SKILL.md` 操作。

## 使用

```bash
# 启动仪表盘（任选）
cd skills/claude-desktop/dist && python server.py --port 8723
# 或双击 dist\启动Claude桌面.bat
# 浏览器打开 http://127.0.0.1:8723
```

## 功能一览

- 🔵 正在运行任务面板（系统任务 + 对话框任务 + 对话会话三源合并）
- 🤖 / 🧩 浏览全部 Agent / Skill（详情、启动、以之作新对话）
- 💬 发起 Claude 对话（多标签、流式、切模型/执行方式/权限模式）
- 📊 token / 成本统计 + 趋势图 + 今日概览
- 🔍 会话全局全文搜索
- ⚡ 快捷启动 + 🧩 Prompt 模板库
- 🗑 站内确认弹窗、🚨 持久错误条、📱 触屏适配、🌗 深浅主题、🔔 桌面通知

## 开发 / 维护

改 `dist/` 里的 `server.py` / `index.html` 后，重启服务生效。改完跑：
- 后端：`python -m py_compile server.py` + `curl http://127.0.0.1:8723/api/overview` 看 counts
- 前端：浏览器打开确认无 JS 报错、卡片/会话数量正确
发布新版时递增 `.claude-plugin/plugin.json` 的 `version`。

## 许可证

仅供个人/团队内部参考使用。
