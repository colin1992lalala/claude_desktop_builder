# Claude Desktop Dashboard

> 可视化 Claude 桌面：浏览全部 Agent / Skill，查看正在运行的任务与实时进度。
>
> **✨ 零 token 消耗**：仪表盘的扫描、轮询、统计（任务进度、token/成本、模型等）全部是**纯本地读文件 + 前端渲染**，数据来自 Claude 自带落盘的 state.json / 日志（我们只做被动聚合，不调用任何 LLM）。可以随时刷新、挂着看进度，完全不花 token。**唯一消耗 token 的场景**：你在仪表盘里真的向 Claude 发消息（对话 / 继续任务）。

## 快速开始

**方式一（推荐）**：双击 `启动Claude桌面.bat`，自动启动服务并在浏览器打开仪表盘。

**方式二**：手动启动
```bash
python server.py --port 8723
# 浏览器打开 http://127.0.0.1:8723
```

## 功能

| 区域 | 说明 |
|------|------|
| 🔵 正在运行 | 实时显示任务（运行中/停止/完成）：系统任务（`~/.claude/jobs/`）+ 对话框任务（`tmp/tasks/*.log`）+ **对话会话**（`tmp/chats/`，处理中的对话也在此显示）。每个任务卡有 **▶ 继续**（对话窗口续接上下文 / 终端重发）与 **🗑 删除**（仅删状态记录）、对话卡有 **💬 在对话中查看**；任务卡「实时日志」面板可点 **⤢ 全部展开** 看完整运行过程（不再只显示最近 15 条） |
| 🤖 Agents | 浏览全局 + 项目的全部 Agent（名称、描述、来源、更新、路径）。**单击卡片打开详情**、双击或 Alt+单击打开所在目录。卡片：「▶ 启动」打开新对话弹窗并预选该 Agent；「📖 详情」查看配置正文 + 一键以该 Agent 新建对话 |
| 🧩 Skills | 浏览全部 Skill 与其 SKILL.md 说明。**单击卡片打开详情**、双击打开所在目录。卡片：「▶ 启动」新对话预选该 Skill；「📖 详情」查看 SKILL.md 正文 |
| 💬 历史会话 | 最近会话列表，点击查看对话摘要（标题、消息数、时间、内容） |
| 🚀 对话/任务 | 顶部「对话」按钮 → 发起对话（多会话标签、流式回复、保持上下文）。新建弹窗：**模型三选一**（auto 自动 / 地端 on-prem / 云端，选地端或云端再从具体模型下拉选，模型清单来自 `~/.config/realcoder/models.txt`，用 `claude --model` 指定）、**执行方式三选一**（默认 Claude / 指定 Agent / 指定 Skill，Skill 自动注入调用指令）、权限勾选（5 项）、消息字体可调（A−/A+，10~24px 持久化）、**会话可重命名**（侧栏悬停 ✏️，存 meta.name，标签页/侧栏统一显示自定义名）、**会话可置顶**（侧栏悬停 📍/📌，置顶会话排最前并分组显示）。每轮用 `claude -p --output-format stream-json --verbose --resume <session>` 实现 |
| ⏹ 对话中断 | 对话进行中随时点「⏹ 中断」终止本轮回复（等价 CLI 的 Ctrl-C）：终止子进程、部分回复保留、回到待输入状态，可直接发下一条指令；已跑上下文不丢（下一轮自动 `--resume`） |
| 🧭 切换模型 | 对话状态条「🧭 模型」随时切换会话模型（auto/地端/云端），下一条消息用新模型（`--resume` 带新 `--model`，不打断当前回复）；状态条同时展示**实际使用模型**（auto 时从 result 事件提取真实模型） |
| 🤖 切换执行方式 | 对话状态条「🤖 执行方式」随时切换会话的执行方式 mode（默认 Claude / 指定 Agent / 指定 Skill，Skill 带 datalist 搜索选择），下一条消息用新模式（写入 meta 的 agent/skill 字段，不打断当前回复）；状态条按钮实时显示当前模式（如「🤖 pdf-to-md」「🧩 Skill tdd」） |
| 🛡 切换权限模式 | 对话状态条「🛡 权限」随时切换会话权限模式（default/auto/acceptEdits/plan/bypass，带说明文案；bypass 红色警示），下一条消息用新模式（写入 meta 的 permission 字段，`--resume` 时读新值，不打断当前回复）；按钮实时显示当前权限（bypass 标红）。默认=无 `--permission-mode`（-p 下工具确认会失败，仅对话用途），auto/acceptEdits/plan 对应 `--permission-mode`，bypass=`--dangerously-skip-permissions` |
| 📐 长文排版 | 消息气泡限宽（正文最多 96ch）优雅换行，超长代码块仍保持内部**横向滚动**；**AI 长回复（>600 字符）默认摘要折叠**：气泡顶部「📌 要点」条 + 自动提取的 1~3 条要点（标题/列表项/加粗段）+ 收起态渐变遮罩提示"还有更多"，点击要点条展开完整回复、再点收起；展开/收起状态按会话记忆（`localStorage`），轮询重渲染不丢失。用户消息/工具气泡不做摘要 |
| 🕐 消息时间戳+模型徽章 | 每条消息头部显示发送时间（HH:MM）；AI 消息头「Claude」旁显示该回复实际使用的模型徽章（未取到则隐藏） |
| 💬 新回复标题闪烁 | 页面不在当前标签页且对话**正在回复**时，AI 新消息到达后浏览器标题在正常标题与「💬 有来自 Claude 的新回复」间闪烁；切回页面立即恢复。不弹系统通知 |
| 📊 token/成本 | 对话状态条显示本会话累计消耗：`⧉ 会话 Xk 读 + Yk 写 · N 轮 · $0.xxx`（悬停看精确值与耗时），任务卡片同步显示 `🪙 tokens · 💰 成本 · 🔄 轮数· 🧭 模型`；数据来自每轮 `result` 事件体积小、纯本地统计 |
| ⚡ 快捷启动 | 把常用操作一步存成快捷卡片：新建对话弹窗勾选「💾 保存为快捷启动卡片」即可保存（含执行方式/agent/skill/提示词/权限/模型）；卡片 hover 可 **✏️ 编辑/重命名**（打开弹窗预填原配置）与 **🗑 删除**；配置存于 `quick_launch.json`（可编辑、可分享、可备份）。点击卡片：纯 prompt 的默认 Claude 直接发起对话，指定 agent/skill 的打开弹窗确认后发起 |
| 🧩 Prompt 模板 | 把常用 prompt 存成模板库：新建对话弹窗可直接从「🧩 模板」下拉选择（点击即填入首条消息），弹窗内勾选「🧩 存为 Prompt 模板」随手存档；首页「🧩 Prompt 模板」区展示全部模板卡片，点卡片打开新建弹窗预填，hover 可 **✏️ 编辑** / **🗑 删除**；配置存于 `prompt_templates.json`（可编辑、可分享、可备份）。零 token。模板库为空时自动隐藏 |
| 主题切换 | 顶部主题按钮一键切换深色/浅色主题，使用冷色调浅色面板，并通过 `localStorage`（`claude-desktop-theme`）记住选择；默认深色，刷新或重新打开后保持上次主题 |
| 🔔 桌面通知 | 顶部「通知」按钮（`localStorage` `claude-desktop-notify`，默认开）——对话/任务**状态变化时**弹 Windows 桌面通知：①对话回复完成（working→等待输入）②任务完成（working→done）③任务出错（working→error）。纯前端 Notification API，只在**页面处于后台**（`document.hidden`）时触发，避免前台刷屏；迁移瞬间只弹一次（幂等）；首次点击开关会请求浏览器通知权限，未授权则静默失效并显示"通知未授权"。零后端改动、零 token |
| 📅 今日概览 | 顶部统计区新增「今日」卡：今日活跃**会话数 + 任务数**、累计 **token/成本**、**最常用模型**。数据来自新接口 `/api/today`（纯本地读文件：按 `tmp/chats/<id>.log` 的 mtime 判"今天活跃"，聚合其全部 `result` 事件的用量；今日启动的对话框任务 `# 启动:` + 今日活跃的系统 job `createdAt/updatedAt` 计入任务数）。服务端 60s 内存缓存避免高频全扫，前端每 60s 拉一次、手动刷新按钮 `?force=1` 强制重算。零 token |
| 📈 token/成本趋势 | 统计区新增「趋势」卡（副文本显示 30 天累计摘要），点击开趋势弹窗：**7/14/30 天可切换**的每日 **token/成本双折线图**（SVG 手绘，蓝=token 绿=成本，hover 数据点看当天明细）+ 总览（累计 token/成本/轮数/活跃会话数）+ 每日明细表。数据来自 `/api/trend?days=N`：扫描 `tmp/chats/*.log`，把每轮 `result` 的用量按该轮 assistant 事件时间戳归到本地日期（result 无时间戳沿用最近已知时间，轮次粒度归因）。服务端 60s 缓存，前端弹窗打开时才拉取（不并 3s 轮询），天数选择存 localStorage。零 token |
| 🔍 会话全局搜索 | 统计区「搜索」卡 → 弹窗全文检索**全部对话**（用户提问 + AI 回复 + 工具调用参数）。**多关键词空格分隔（AND 且大小写不敏感）**，命中片段自动高亮并带语境（命中词前后 ~70 字符）；每个会话显示命中条数与来源徽章（👤 你问 / 🤖 AI 答）；**点击结果直达该会话**。数据来自 `/api/chat/search?q=`（纯本地扫描 `tmp/chats/*.log` + `.md`，流式读避免超大 log 卡死；每个会话最多 2 条片段、上限 30 个会话，保护性能）。输入即搜（260ms 防抖）、Enter 立即搜、Esc 关闭。零 token |
| ⚙️ 顶部统计 | 运行中任务、总任务、Agent、Skill、会话数量一览 |
| 🗑 站内确认弹窗 | 所有删除确认（快捷卡片/模板/任务记录/对话/历史会话）与**重命名**均使用站内 mini 弹窗（不再弹原生 confirm/prompt），样式与深浅主题一致；危险操作主键为红色；Esc/点遮罩取消 |
| 🚨 持久错误条 | 删除/保存/续接/重命名等操作网络失败或后端报错时，底部显示**可停留的红色错误条**（手动「知道了」关闭，不会被自动轮询刷掉），避免操作失败被静默吞掉 |
| 📱 触屏适配 | 触屏设备（`pointer:coarse`）下快捷卡片编辑/删除、会话删除、消息复制、侧栏操作等原本"悬停才显示"的按钮**默认常显**，保证触屏可达 |
| 🖥 大屏密度 | 组件库/任务网格列宽随视口自适应（2K/4K 下自动多列、卡片不过宽），消除大屏大片空边距 |
| 💬 历史会话续聊 | 历史会话详情弹窗底部新增「续聊」按钮——一键在对话面板中按原会话上下文续接继续 |

顶部「自动刷新」默认开启（每 3 秒轮询），可关闭。

## 数据源

- Agents/Skills：`~/.claude/agents`、`~/.claude/skills`（全局）+ `<项目根>\.claude\agents`、`<项目根>\.claude\skills`（项目）
- 任务进度：`~/.claude/jobs/*/state.json` + `timeline.jsonl`
- 历史会话：`~/.claude/projects/<项目注册目录名>/*.jsonl`（Claude 按磁盘路径生成，如 `D:\AI` → `D--AI`）

### 可移植 / 换项目

项目根目录**默认自动取 server.py 所在目录的上级**（不再硬编码 `D:\AI`）；会话目录自动按 Claude 命名规则匹配。如需覆盖：

```bash
CLAUDE_DESKTOP_ROOT=D:\你的项目    # 覆盖项目根
CLAUDE_DESKTOP_SESSIONS=C:\...\D--AI  # 显式指定会话目录
python server.py --port 8723
```

打包给他人：把本目录拷给他即可；`启动Claude桌面.bat` 会自动找 python（上级 `python312` / 同目录 `python` / PATH）。对方需已安装 Claude CLI（`claude` 在 PATH）才能用「对话/新任务」启动会话。

## API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/overview` | GET | 总览：agents/skills/jobs/sessions + 统计 |
| `/api/jobs` | GET | 任务列表（含 timeline） |
| `/api/session?id=xxx` | GET | 某个会话详情（标题 + 消息摘要） |
| `/api/launch` | POST `{agent}` | 启动指定 Agent（新终端运行 claude --agent） |
| `/api/open` | POST `{path}` | 在资源管理器中打开文件所在目录 |
| `/api/newtask` | POST `{type,agent,skill,prompt,cwd,permission}` | （保留兼容）原"新任务"后台 claude -p；功能已并入对话模式 |
| `/api/tasklog?name=xxx` | GET | （保留兼容）读取原新任务日志尾部 |
| `/api/today` | GET | 今日概览：今日活跃会话/任务数、累计 token/成本、最常用模型（`?force=1` 忽略缓存强制重算；纯本地读文件零 token） |
| `/api/trend` | GET | token/成本趋势：`?days=N`（1–90，默认 30）返回每日 token/成本/轮数/活跃会话数 + 总计与日期范围（`?force=1` 忽略缓存；纯本地读文件零 token） |
| `/api/chat/list` | GET | 聊天会话列表 |
| `/api/chat/search?q=xxx` | GET | 会话全局全文搜索：`q` 多关键词空格分隔（AND、大小写不敏感），返回命中会话 + 片段；搜索源为 `tmp/chats/*.log`（AI 回复/工具参数）+ `.md`（用户消息） |
| `/api/chat/start` | POST `{agent,prompt,cwd,permission,id?,skill?,model?}` | 开启/续接聊天会话（id=复用已有会话；skill=指定 Skill 自动注入调用指令；model=claude --model 的模型名，auto 则不加参数） |
| `/api/chat/log?id=xxx` | GET | 读取聊天会话全部 stream-json 事件（前端轮询） |
| `/api/chat/model` | POST `{id,model}` | 切换会话模型（写入 meta，下一轮生效） |
| `/api/chat/mode` | POST `{id,mode,target?}` | 切换会话执行方式 mode（claude/agent/skill；agent/skill 需传 target，写入 meta 的 agent/skill 字段，下一轮生效） |
| `/api/chat/perm` | POST `{id,permission}` | 切换会话权限模式（default/auto/acceptEdits/plan/bypass，写入 meta 的 permission 字段，下一轮生效） |
| `/api/chat/stop` | POST `{id}` | 中断会话进行中的回复（等价 Ctrl-C） |
| `/api/chat/rename` | POST `{id,name}` | 重命名会话（meta.name，空名恢复默认标题） |
| `/api/chat/pin` | POST `{id,pinned}` | 置顶/取消置顶会话（meta.pinned，置顶排最前） |
| `/api/jobs/clean` | POST `{ids}` | 批量删除已完成/已停止等任务状态目录 |
| `/api/jobs/resume` | POST `{kind,id,sessionId,cwd,cmd}` | 在**新终端窗口**继续任务/会话（有 sessionId 用 `--resume`，否则按 cmd 重跑；kind=job/task/chat） |
| `/api/jobs/delete` | POST `{kind,id}` | 删除单条任务/会话的**状态记录**（job=状态目录 / task=日志 / chat=聊天记录；不影响真实会话与文件） |
| `/api/quick_launch` | POST `{item, id?}` | 新增/更新快捷启动（item 含 name/kind/target/prompt/permission/modelGroup/model/emoji；带 id=覆盖该条目） |
| `/api/quick_launch/delete` | POST `{id}` | 删除快捷启动 |
| `/api/prompt_template` | POST `{item, id?}` | 新增/更新 Prompt 模板（item 含 name/category/text/emoji；带 id=覆盖该条目） |
| `/api/prompt_template/delete` | POST `{id}` | 删除 Prompt 模板 |

## 技术说明

- 纯 Python 标准库（无需 `pip install`），Python 3.9+ 均可运行
- 前端单页 `index.html`，无任何外部依赖（单文件、离线可用）
- 深色/浅色仪表盘主题，自适应宽屏/窄屏；主题选择保存在浏览器 `localStorage`，默认深色
- **零 token 消耗**：所有展示数据来自本地文件（`~/.claude/jobs/`、`tmp/chats/<id>.log` 里的 `result` 事件等），仪表盘只做文本解析与前端渲染，不调用任何 LLM；随时刷新/轮询不产生任何费用（唯一例外：在对话里真正向 Claude 发消息）
- 对话渲染零后端重放：`chat_log` 解析 log 时给每个事件注入 `ts` 时间戳（来自事件自带 UTC `timestamp`，取不到则沿用该轮最近时间），前端据此显示每条消息的发送时间；摘要提取、时间戳格式、标题闪烁全部是前端纯本地实现

