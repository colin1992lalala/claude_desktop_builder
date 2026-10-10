---
name: claude-desktop
description: |
  当用户想使用/部署/维护「Claude 桌面仪表盘」（一个本地浏览器工具，可浏览 Agent/Skill、发起对话、查看任务进度与聊天记录）时使用。触发场景：用户想看 Agent/Skill 清单、报仪表盘显示不对/没数据/打不开、想加新功能、想了解它怎么运行、想在新机器上部署它。
---

# Claude 桌面仪表盘（claude-desktop）

一个**纯本地、零第三方依赖**的网页仪表盘：在浏览器里直观浏览本机与项目的 Agent/Skill、发起 Claude 对话（多标签、流式）、查看正在运行的任务与实时进度、历史会话、token/成本统计与趋势、全局搜索、快捷启动、Prompt 模板、深浅主题。

**核心卖点：零 token 消耗** —— 所有扫描/轮询/统计都是纯本地读文件（`~/.claude/` 与项目 `.claude/`）+ Python 标准库解析 + 浏览器渲染，不调用任何 LLM。唯一花 token 的场景是你在仪表盘里真正向 Claude 发消息。

## 本插件提供了什么

| 路径 | 说明 |
|---|---|
| `skills/claude-desktop/dist/` | **可运行的工具本体**：`server.py`（后端，Python 标准库 http.server）+ `index.html`（前端单文件）+ `启动Claude桌面.bat`（一键启动）+ `README.md` + `quick_launch.example.json`（快捷启动示例配置） |
| `agents/claude-desktop-builder.md` | 维护 Agent 完整知识库（架构、功能清单、踩坑记录、验证清单） |

## 用户想用这个仪表盘（首次部署）

1. **复制或原地使用**：最简单是把 `skills/claude-desktop/dist/` 整个目录放到一个工作位置（如 `D:\YourProject\claude-desktop\`）。或直接原地启动也可用。
2. **启动**：双击 `启动Claude桌面.bat`（自动起服务 + 开浏览器 http://127.0.0.1:8723）；或命令行 `python server.py --port 8723`。
3. **前置依赖**：Python 3.9+（仅标准库，无需 pip 安装任何包）；对话/任务功能需要 `claude` 在 PATH。
4. **路径自适应**：`PROJECT_ROOT` 由 `server.py` 所在目录的上一级自动推导，一般无需改；特殊路径可用环境变量 `CLAUDE_DESKTOP_ROOT` / `CLAUDE_DESKTOP_SESSIONS` 覆盖。
5. **快捷启动配置**（可选）：把 `quick_launch.example.json` 复制为 `quick_launch.json` 并修改。

## 用户报仪表盘出问题 / 想加功能（维护）

此时你是仪表盘的维护者，按优先级：

1. **先确认服务活着**：`netstat -ano | grep ":8723" | grep LISTEN`；没活就 `python server.py --port 8723` 看日志。
2. **健康检查**：`curl http://127.0.0.1:8723/api/overview`（存成文件再解析），看 `counts` 是否正常（agents/skills/jobs/sessions 非 0）。
3. **前端**：`Runtime.exceptionThrown=0` 是硬指标——JS 出错（尤其 `SyntaxError`）会让整个功能失效。用户真实浏览器可能缓存旧 `index.html`，先 `curl <url>/ | grep <新代码标记>` 确认服务端返回的已是新版。
4. **改代码**：后端 `server.py`（新增 API 遵循既有 `do_GET`/`do_POST` elif 链 + 参数白名单校验），前端 `index.html`（深色卡片式、保持零依赖、禁用原生 `confirm()/prompt()`，统一用站内 `confirmDlg/promptDlg`）。
5. **验证清单**：后端 `python -m py_compile server.py` + curl 接口；前端括号配平 + 无 JS 异常 + 卡片/会话渲染数量正确。改完同步更新 `dist/README.md`。

## 踩坑速查（浓缩）

- **bat 编码**：含中文的 `启动Claude桌面.bat` 必须 **UTF-8 with BOM + CRLF**，否则 Windows cmd 按 GBK 预解析会把中文注释劈进英文命令，症状是"双击没反应"但手动 `python server.py` 正常。
- **路径**：项目级 agents/skills 在 `<项目根>\.claude\agents`、`<项目根>\.claude\skills`（不是 `~/.claude/projects/...`）；`PROJECT_CLAUDE_DIR` 已含 `.claude`，别再拼一次。会话目录按磁盘路径自动映射（`D:\AI` → `D--AI`）。
- **两类任务数据源**：系统任务写 `~/.claude/jobs/`；对话用的 `claude -p` 不写 jobs，只落 `tmp/tasks`、`tmp/chats`。顶部面板三源合并，只改一方会漏。
- **Agent 详情/技能弹窗**：「详情」用通用 `showDetailModal`；「以该 Agent 新建对话」用 `openNewChatDlg({kind,name})` 预选机制。
- **前端只改 `index.html` 单文件**：调试用 URL `?debug=1`（onerror 带文件名/堆栈）。

## 编辑本插件开发注意

- 改 `dist/` 里任何文件后，若本机已启动仪表盘，需重启 `server.py` 才生效（bat 检测端口冲突会自动跳过启动）。
- 发布新版本时更新 `.claude-plugin/plugin.json` 的 `version`。
