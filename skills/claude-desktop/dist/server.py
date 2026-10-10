# -*- coding: utf-8 -*-
"""
Claude Desktop Dashboard 后端服务
=================================
基于 Python 标准库（零第三方依赖），提供两个角色：

1. API 服务：读取 Claude 本地数据（agents/skills/jobs/sessions），
   以 JSON 形式提供给前端仪表盘轮询。
2. 静态文件服务：托管 index.html 等前端资源。

数据源:
  - ~/.claude/agents             全局 Agents (SKILL.md 说明)
  - ~/.claude/skills            全局 Skills (每个 skill 一个 SKILL.md)
  - <项目根>/.claude/agents       项目 Agents（PROJECT_ROOT 由上级目录推导）
  - <项目根>/.claude/skills       项目 Skills
  - ~/.claude/jobs/*/state.json  正在运行/历史的 job 状态 + timeline.jsonl 进度
  - ~/.claude/projects/          历史会话 jsonl

用法:
  python server.py [--port 8723]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

# ---------------------------------------------------------------------------
# 路径配置
# ---------------------------------------------------------------------------
HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
# 项目根目录（其中 .claude 下存放项目级 agents/skills）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # server.py 的上级目录
if os.environ.get("CLAUDE_DESKTOP_ROOT"):
    PROJECT_ROOT = os.environ["CLAUDE_DESKTOP_ROOT"]
PROJECT_CLAUDE_DIR = os.path.join(PROJECT_ROOT, ".claude")
# 当前项目的会话记录目录。
# Claude 按项目磁盘路径生成注册目录名（如 D:\AI -> D--AI，路径分隔符/冒号转成 -）。
# 优先用环境变量 CLAUDE_DESKTOP_SESSIONS 显式指定；
# 否则按 Claude 注册命名规则由 PROJECT_ROOT 推导（分隔符/冒号 -> '-'）；
# 目录不存在则扫描 ~/.claude/projects 逐一比对；仍无则回退 "D--AI"（兼容历史）。
PROJECT_SESSIONS_DIR = ""
if os.environ.get("CLAUDE_DESKTOP_SESSIONS"):
    PROJECT_SESSIONS_DIR = os.environ["CLAUDE_DESKTOP_SESSIONS"]
else:
    projects_dir = os.path.join(CLAUDE_DIR, "projects")
    # 规则还原：把注册名里的 '-' 尽量理解为 ['/','\\',':'] 之一，与 PROJECT_ROOT 比对
    def _mapped_name(root):
        # 生成可能的注册目录名变体
        variants = []
        base = root.replace("/", "-").replace("\\", "-").replace(":", "-")
        variants.append(base)
        variants.append(base.lstrip("-"))
        return variants
    try:
        for v in _mapped_name(PROJECT_ROOT):
            if os.path.isdir(os.path.join(projects_dir, v)):
                PROJECT_SESSIONS_DIR = os.path.join(projects_dir, v)
                break
        if not PROJECT_SESSIONS_DIR and os.path.isdir(projects_dir):
            for name in os.listdir(projects_dir):
                restored = name.replace("-", os.sep)
                if restored.lower().rstrip(os.sep) == PROJECT_ROOT.lower().rstrip(os.sep):
                    PROJECT_SESSIONS_DIR = os.path.join(projects_dir, name)
                    break
    except Exception:
        PROJECT_SESSIONS_DIR = ""
if not PROJECT_SESSIONS_DIR:
    PROJECT_SESSIONS_DIR = os.path.join(CLAUDE_DIR, "projects", "D--AI")
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# 回收站：完全删除对话时，对应的真实会话 jsonl 移到这里做兜底备份（绝不 os.remove）
TRASH_DIR = os.path.join(THIS_DIR, "tmp", "trash")

# 时区 (本地时间显示)
LOCAL_TZ = timezone(timedelta(hours=8))  # CST

PORT = 8723

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def local_iso(ts):
    """时间戳字符串 -> 本地时间 ISO 字符串（可读）"""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts


def now_local():
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def rel_time(ts):
    """相对时间：火星时间 3 分钟前"""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(LOCAL_TZ)
        now = datetime.now(LOCAL_TZ)
        delta = now - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return "刚刚"
        if secs < 60:
            return f"{secs}秒前"
        if secs < 3600:
            return f"{secs // 60}分钟前"
        if secs < 86400:
            return f"{secs // 3600}小时前"
        return f"{secs // 86400}天前"
    except Exception:
        return ""


def parse_frontmatter(text):
    """解析 markdown 的 YAML frontmatter (--- 分隔段)，返回 dict 和正文。"""
    text = text or ""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm = text[3:end]
    body = text[end + 4:]
    meta = {}
    for line in fm.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(("%", ">", "@", "&")):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        # 跳过 XML 类块（example 等）
        if val.startswith("<"):
            continue
        if key in ("name", "description", "version", "license", "metadata"):
            meta[key] = val
    return meta, body


def parse_yaml_lite(text):
    """解析 YAML 中的 name/description（含多行折叠）"""
    meta, body = parse_frontmatter(text)
    # 多行 description: '...' 展开
    # 简化：frontmatter 内直接提取
    return meta, body


# ---------------------------------------------------------------------------
# 数据采集
# ---------------------------------------------------------------------------

def file_mtime(path):
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _quick_launches_path():
    """快捷启动配置文件路径：server.py 同目录的 quick_launch.json（可移植）"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "quick_launch.json")


def collect_quick_launches():
    """读取快捷启动配置 quick_launch.json，返回列表；文件缺失/损坏返回 []。
    每条形如 {id, name, emoji, kind: claude|agent|skill, target, prompt, permission, modelGroup, model}"""
    ql_path = _quick_launches_path()
    try:
        with open(ql_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    items = data.get("quick_launches", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        return []
    # 手工配置文件可能没有 id：就地补一个（不写回文件，只用于前端标识）
    for it in items:
        if isinstance(it, dict) and not it.get("id"):
            it["id"] = "ql_" + (str(it.get("name", "")).strip() or "x")[:24].replace(" ", "_")
    return [i for i in items if isinstance(i, dict)]


def save_quick_launch(item, item_id=None):
    """新增/更新一条快捷启动：item_id=None 新增（自动生成 id），否则覆盖该 id 条目。
    返回 {ok, id, quick_launches: 最新列表}；非法输入返回 {ok:false, error}。"""
    if not isinstance(item, dict) or not isinstance(item.get("name", ""), str) or not item.get("name", "").strip():
        return {"ok": False, "error": "缺少 name"}
    if item.get("kind") not in ("claude", "agent", "skill"):
        item["kind"] = "claude"
    # 白名单校验：仅允许指定字段，防注入任意字段
    allowed = {"kind", "target", "prompt", "permission", "modelGroup", "model", "emoji"}
    clean = {k: item[k] for k in allowed if k in item}
    clean["name"] = str(item.get("name", "")).strip()
    clean["kind"] = item.get("kind") or "claude"
    clean["id"] = item_id or ("ql_" + datetime.now().strftime("%Y%m%d_%H%M%S"))

    items = collect_quick_launches()
    if item_id:
        for i, it in enumerate(items):
            if it.get("id") == item_id:
                items[i] = clean
                break
        else:
            return {"ok": False, "error": "未找到要更新的条目"}
    else:
        items.append(clean)
    try:
        with open(_quick_launches_path(), "w", encoding="utf-8") as f:
            json.dump({"quick_launches": items}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False, "error": "写入失败: " + str(e)}
    return {"ok": True, "id": clean["id"], "quick_launches": items}


def delete_quick_launch(item_id):
    """按 id 删除快捷启动；返回 {ok, quick_launches: 最新列表}"""
    items = collect_quick_launches()
    kept = [i for i in items if i.get("id") != item_id]
    try:
        with open(_quick_launches_path(), "w", encoding="utf-8") as f:
            json.dump({"quick_launches": kept}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False, "error": "写入失败: " + str(e)}
    return {"ok": True, "quick_launches": kept}


def _prompt_templates_path():
    """Prompt 模板库配置文件路径：server.py 同目录的 prompt_templates.json（可移植，与 quick_launch 同模式）"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_templates.json")


def collect_prompt_templates():
    """读取 Prompt 模板库 prompt_templates.json，返回列表；文件缺失/损坏返回 []。
    每条形如 {id, name, category, text, emoji}。category 为可选的场景归类（复盘/周报/邮件/简历等）。"""
    pt_path = _prompt_templates_path()
    try:
        with open(pt_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    items = data.get("prompt_templates", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        return []
    # 手工配置文件可能没有 id：就地补一个（不写回文件，只用于前端标识）
    for it in items:
        if isinstance(it, dict) and not it.get("id"):
            it["id"] = "pt_" + (str(it.get("name", "")).strip() or "x")[:24].replace(" ", "_")
    return [i for i in items if isinstance(i, dict)]


def save_prompt_template(item, item_id=None):
    """新增/更新一条 Prompt 模板：item_id=None 新增（自动生成 id），否则覆盖该 id 条目。
    返回 {ok, id, prompt_templates: 最新列表}；非法输入返回 {ok:false, error}。"""
    if not isinstance(item, dict) or not isinstance(item.get("name", ""), str) or not item.get("name", "").strip():
        return {"ok": False, "error": "缺少 name"}
    if not isinstance(item.get("text", ""), str) or not item.get("text", "").strip():
        return {"ok": False, "error": "缺少 text"}
    # 白名单校验：仅允许指定字段，防注入任意字段
    allowed = {"category", "text", "emoji"}
    clean = {k: str(item[k]).strip() for k in allowed if k in item}
    clean["name"] = str(item.get("name", "")).strip()
    clean["id"] = item_id or ("pt_" + datetime.now().strftime("%Y%m%d_%H%M%S"))

    items = collect_prompt_templates()
    if item_id:
        for i, it in enumerate(items):
            if it.get("id") == item_id:
                items[i] = clean
                break
        else:
            return {"ok": False, "error": "未找到要更新的条目"}
    else:
        items.append(clean)
    try:
        with open(_prompt_templates_path(), "w", encoding="utf-8") as f:
            json.dump({"prompt_templates": items}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False, "error": "写入失败: " + str(e)}
    return {"ok": True, "id": clean["id"], "prompt_templates": items}


def delete_prompt_template(item_id):
    """按 id 删除 Prompt 模板；返回 {ok, prompt_templates: 最新列表}"""
    items = collect_prompt_templates()
    kept = [i for i in items if i.get("id") != item_id]
    try:
        with open(_prompt_templates_path(), "w", encoding="utf-8") as f:
            json.dump({"prompt_templates": kept}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False, "error": "写入失败: " + str(e)}
    return {"ok": True, "prompt_templates": kept}


def collect_agents():
    """扫描全局与项目 agents 目录，返回 [{name, description, source, path, mtime}]"""
    out = []
    seen = set()
    for base, label in [(os.path.join(CLAUDE_DIR, "agents"), "全局"),
                        (os.path.join(PROJECT_CLAUDE_DIR, "agents"), "项目")]:
        if not os.path.isdir(base):
            continue
        for fn in sorted(os.listdir(base)):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(base, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
            except Exception:
                with open(path, "r", encoding="gbk", errors="ignore") as f:
                    text = f.read()
            meta, body = parse_frontmatter(text)
            name = meta.get("name") or fn[:-3]
            desc = meta.get("description") or (body[:120].strip() if body else "")
            if name in seen:
                continue
            seen.add(name)
            out.append({
                "name": name,
                "description": desc,
                "source": label,
                "path": path,
                "mtime": file_mtime(path),
                "content": body.strip()[:2000],   # flask 正文预览（详情弹窗用，与 skill 对齐）
            })
    return out


def collect_skills():
    """扫描全局与项目 skills 目录，读取每个 skill。

    Skill 有两种合法形态，均须采集：
      1. 目录形态：`skill-name/SKILL.md`（多数 skill，如 html-ppt/）
      2. 文件形态：`skill-name` 单个文件（frontmatter 即定义，如 skill-email-summary）
    """
    out = []
    seen = set()
    for base, label in [(os.path.join(CLAUDE_DIR, "skills"), "全局"),
                        (os.path.join(PROJECT_CLAUDE_DIR, "skills"), "项目")]:
        if not os.path.isdir(base):
            continue
        for fn in sorted(os.listdir(base)):
            item = os.path.join(base, fn)
            # 定位该 skill 的 markdown 源文件
            if os.path.isdir(item):
                skill_md = os.path.join(item, "SKILL.md")
                if not os.path.exists(skill_md):
                    continue
            else:
                # 文件形态：只认带 frontmatter 的 skill 定义（name/description）
                skill_md = item
            try:
                with open(skill_md, "r", encoding="utf-8") as f:
                    text = f.read()
            except Exception:
                with open(skill_md, "r", encoding="gbk", errors="ignore") as f:
                    text = f.read()
            meta, body = parse_frontmatter(text)
            # 无 name/description 的普通文件跳过（避免把杂项文件当 skill）
            if not (meta.get("name") or meta.get("description")):
                continue
            name = meta.get("name") or fn
            desc = meta.get("description") or ""
            # 描述可能需要从 "description: ..." 展开（skill SKILL.md 用 """）
            if name in seen:
                continue
            seen.add(name)
            out.append({
                "name": name,
                "description": desc,
                "source": label,
                "path": skill_md,
                "mtime": file_mtime(skill_md),
                # 正文（frontmatter 之后），供详情弹窗展示
                "content": body.strip()[:2000],
            })
    return out


def collect_jobs():
    """扫描 ~/.claude/jobs/*/state.json + timeline.jsonl，返回运行中任务"""
    out = []
    jobs_dir = os.path.join(CLAUDE_DIR, "jobs")
    if not os.path.isdir(jobs_dir):
        return out
    for d in sorted(os.listdir(jobs_dir)):
        state_path = os.path.join(jobs_dir, d, "state.json")
        timeline_path = os.path.join(jobs_dir, d, "timeline.jsonl")
        if not os.path.exists(state_path):
            continue
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            continue
        # 只显示有明确工作意图的任务（直接 job，非 daemon 内部）
        name = state.get("name") or state.get("intent") or state.get("sessionId") or d
        entry = {
            "id": d,
            "name": name,
            "state": state.get("state", "unknown"),
            "detail": state.get("detail", ""),
            "tempo": state.get("tempo", ""),
            "tokens": state.get("tokens", 0),
            "cwd": state.get("cwd", ""),
            "model": state.get("model", ""),
            "intent": state.get("intent", ""),
            "sessionId": state.get("sessionId", ""),
            "createdAt": state.get("createdAt", ""),
            "updatedAt": state.get("updatedAt", ""),
            "timeline": [],
        }
        # 读取 timeline
        if os.path.exists(timeline_path):
            try:
                with open(timeline_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except Exception:
                            continue
                        entry["timeline"].append({
                            "at": local_iso(ev.get("at")),
                            "state": ev.get("state", ""),
                            "detail": ev.get("detail", ""),
                            "text": (ev.get("text") or "")[:300],
                        })
            except Exception:
                pass
        out.append(entry)
    # 正在运行的优先
    order = {"working": 0, "idle": 1, "stopped": 2, "done": 3, "error": 4}
    out.sort(key=lambda x: (order.get(x["state"], 9), x.get("updatedAt", "")))
    return out


def collect_custom_tasks():
    """扫描 tmp/tasks/*.log ——「新任务」对话框启动的后台任务。

    这些任务用 `claude -p` 启动，不走 ~/.claude/jobs，因此必须单独采集，
    合并进顶部「正在运行/近期任务」面板。

    完成判定：`launch_new_task` 的 _run 在子进程结束（或异常）时向日志写入
    `# 结束:` 标记行；含该行即视为已完成。
    """
    out = []
    tasks_dir = os.path.join(THIS_DIR, "tmp", "tasks")
    if not os.path.isdir(tasks_dir):
        return out
    try:
        entries = sorted(os.listdir(tasks_dir), reverse=True)
    except Exception:
        return out
    import datetime as _dt
    now = _dt.datetime.now(LOCAL_TZ)

    for fn in entries:
        if not fn.endswith(".log"):
            continue
        path = os.path.join(tasks_dir, fn)
        try:
            st = os.stat(path)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            continue
        mtime = _dt.datetime.fromtimestamp(st.st_mtime, LOCAL_TZ)
        age = (now - mtime).total_seconds()

        # 解析启动头（索引可能因 stdio 缓冲落在文件末尾，故全文扫描）
        cmd = ""
        cwd = ""
        started = ""
        for line in text.splitlines():
            line = line.strip()
            if not cmd and line.startswith("$ ") and "claude" in line:
                cmd = line[2:]
            elif not cwd and line.startswith("# 工作目录:"):
                cwd = line.split(":", 1)[1].strip()
            elif not started and line.startswith("# 启动:"):
                started = line.split(":", 1)[1].strip()
            if cmd and cwd and started:
                break

        # 展示名：取 --agent 名；否则取命令第一段
        name = fn[:-4]
        if "--agent" in cmd:
            parts = cmd.split("--agent", 1)
            tok = parts[1].strip().split()
            name = tok[0].strip('"') if tok else name
        elif "-p" in cmd:
            # 默认 claude：提示词第一句
            p = cmd.split("-p", 1)[-1].strip()
            name = p[:30] if p else name

        # 完成判定：launch_new_task 结束时写入 "# 结束:" 标记
        done = "# 结束:" in text
        # 回退1：日志几乎为空且久未更新 → 视为完成（空跑/写入失败）
        if not done and age > 160 and len(text.strip()) <= 90:
            done = True
        # 回退2：claude -p 完成后日志 mtime 不再变化；>180 秒未更新视为已完成
        #        （真正运行中的任务会持续写日志，mtime 不断刷新，不会误杀）
        if not done and age > 180:
            done = True

        state = "done" if done else "working"
        # 详情：取正文部分（# 结束 之前，去掉头部命令区）
        show = text
        i_end = text.find("# 结束:")
        if i_end > 0:
            show = text[:i_end]
        # 去掉头标记与 update/模型噪音行
        noise = ("Applying", "warning:", "[claude-code:", "New version available",
                 "Update claude", "(y/N)")
        keep = []
        for ln in show.splitlines():
            s = ln.strip()
            if not s:
                continue
            if s.startswith("$ ") or s.startswith("# 工作目录") or s.startswith("# 启动"):
                continue
            if s.startswith(noise):
                continue
            keep.append(s)
        body = "\n".join(keep)
        detail = body[:400] or "（空输出）"

        out.append({
            "id": "task-" + fn[:-4],
            "name": name,
            "state": state,
            "detail": detail,
            "tempo": "",
            "tokens": 0,
            "cwd": cwd,
            "model": "",
            "intent": cmd,
            "sessionId": "",
            "createdAt": started,
            "updatedAt": local_iso(datetime.fromtimestamp(st.st_mtime).isoformat() if isinstance(st.st_mtime, float) else st.st_mtime.isoformat()),
            "source": "custom",   # 标记来源为对话框任务
            "log": fn,
            "timeline": [],
        })
    return out


def collect_models():
    """读取 RealCoder 模型清单（~/.config/realcoder/models.txt），按 `--` 分组解析。

    返回 {groups:[{id,label,models:[{name,desc}]}]}：
      - auto:   auto（自动）
      - onprem: fast/expert（地端）
      - cloud:  opus/sonnet/haiku/gpt/gemini 等（云端 IT contract）
    该文件由 claude --model-menu 维护；读不到时返回空组。
    """
    groups = [{"id": "auto", "label": "自动", "models": []},
              {"id": "onprem", "label": "地端", "models": []},
              {"id": "cloud", "label": "云端", "models": []}]
    cur = groups[0]          # 首个 -- 前为 auto
    sep_count = 0
    try:
        for fn in ("models.txt", "models-experimental.txt"):
            p = os.path.join(os.path.expanduser("~/.config/realcoder"), fn)
            if not os.path.exists(p):
                continue
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if line == "--":
                        sep_count += 1
                        cur = groups[1] if sep_count == 1 else groups[2]
                        continue
                    left, _, desc = line.partition("\t")
                    name = left.strip()
                    if name:
                        cur["models"].append({"name": name, "desc": desc.strip()})
        return [g for g in groups if g["models"] or g["id"] == "auto"]
    except Exception:
        return groups


def count_session_files():
    """会话目录下 jsonl 总数（只统计文件名，不读内容——轮询接口用，要快）"""
    if not os.path.isdir(PROJECT_SESSIONS_DIR):
        return 0
    try:
        return sum(1 for fn in os.listdir(PROJECT_SESSIONS_DIR) if fn.endswith(".jsonl"))
    except Exception:
        return 0


def collect_sessions(limit=30):
    """扫描项目目录下最近的 session jsonl 文件，提取标题/消息数/时间。

    limit<=0 表示不限制条数（返回全部——调用方需自行承担全量读取的耗时，
    仅供用户主动切换"全部"时调用，overview 轮询接口永远只用默认 30）。
    """
    out = []
    if not os.path.isdir(PROJECT_SESSIONS_DIR):
        return out
    files = []
    for fn in os.listdir(PROJECT_SESSIONS_DIR):
        if fn.endswith(".jsonl"):
            p = os.path.join(PROJECT_SESSIONS_DIR, fn)
            files.append((os.path.getmtime(p), p))
    files.sort(reverse=True)
    picked = files if limit is None or limit <= 0 else files[:limit]
    for _, p in picked:
        sid = os.path.basename(p)[:-6]  # 去掉 .jsonl
        # 粗略统计独立任务（按用户消息数）
        msg_count = 0
        title = ""
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get("type") == "user" and ev.get("userType") == "external":
                        msg_count += 1
                        if not title:
                            c = (ev.get("message") or {}).get("content")
                            if c:
                                if isinstance(c, str):
                                    title = c[:80]
                                elif isinstance(c, list):
                                    for it in c:
                                        if isinstance(it, dict) and it.get("type") == "text":
                                            title = it.get("text", "")[:80]
                                            break
                                elif isinstance(c, dict):
                                    title = c.get("content", "")[:80]
        except Exception:
            pass
        if not title:
            # 找文件名附近的信息
            title = sid[:8]
        out.append({
            "id": sid,
            "title": title,
            "messages": msg_count,
            "mtime": file_mtime(p),
            "size": os.path.getsize(p),
        })
    return out


# ---------------------------------------------------------------------------
# 附加功能：会话详情 / 目录打开 / 启动 Agent
# ---------------------------------------------------------------------------

def read_session_detail(sid):
    """读取会话 jsonl，返回标题、消息摘要、消息数。"""
    if not sid or not re.fullmatch(r"[0-9a-f-]{36}", sid):
        return {"error": "无效会话 ID"}
    path = os.path.join(PROJECT_SESSIONS_DIR, sid + ".jsonl")
    if not os.path.exists(path):
        return {"error": "会话文件不存在"}
    transcript = []
    title = ""
    ext_count = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                typ, ut, msg = ev.get("type"), ev.get("userType"), ev.get("message")
                if not msg:
                    continue
                role = ev.get("role") or (msg or {}).get("role")
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, list):
                    txt = []
                    for it in content:
                        if isinstance(it, dict) and it.get("type") == "text":
                            txt.append(it.get("text", ""))
                    content = "\n".join(txt)
                if not isinstance(content, str) or not content.strip():
                    continue
                if typ == "user" and ut == "external":
                    ext_count += 1
                    if not title:
                        title = content.strip()[:80]
                transcript.append({
                    "role": "user" if role == "user" else "assistant",
                    "content": content.strip()[:3000],
                    "at": local_iso(ev.get("timestamp") or ev.get("at")),
                })
    except Exception as e:
        return {"error": f"读取失败: {e}"}
    return {"id": sid, "title": title or sid[:8], "messages": ext_count,
            "mtime": file_mtime(path), "transcript": transcript[-40:]}


def launch_agent(name):
    """启动一个 Agent：在当前项目目录起一个 claude --agent 后台任务。
    返回命令行（无需密码权限）。
    """
    if not name or not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
        return {"ok": False, "error": "Agent 名称不合法"}
    # 构建 claude 命令
    claude = shutil.which("claude") or "claude"
    workdir = PROJECT_ROOT
    cmd = f'cd "{workdir}" && start "" cmd /c claude --agent "{name}"'
    # 实际执行：在项目目录启动新终端（弹窗式，让用户看到 CLI 界面）
    try:
        subprocess.Popen(
            f'cd /d "{workdir}" && start "Claude Agent: {name}" cmd /k claude --agent "{name}"',
            shell=True, cwd=workdir,
        )
    except Exception as e:
        return {"ok": False, "error": f"启动失败: {e}"}
    return {"ok": True, "name": name, "cmd": f'claude --agent "{name}"'}


def resume_job(kind, ident, session_id="", cwd="", cmd=""):
    """在新终端窗口重新发起一个任务/会话，复用上下文。

    kind:
      - "job"   系统任务：有 session_id 则 --resume 续上下文，否则按 cmd 重新发起
      - "task"  自定义任务（tmp/tasks）：claude -p 无内部会话，按 cmd 重跑
      - "chat"  聊天会话：用 chat_start 的 resume 机制（前端会再调 /api/chat/start? 否——直接开新窗口交互式续聊最简单）
    返回 {ok, cmd}。
    """
    if kind not in ("job", "task", "chat"):
        return {"ok": False, "error": f"未知 kind: {kind}"}
    claude = shutil.which("claude") or "claude"
    workdir = cwd.strip() or PROJECT_ROOT
    if not os.path.isdir(workdir):
        workdir = PROJECT_ROOT
    try:
        if kind == "chat":
            # 聊天会话：开交互式 claude 终端并 --resume 内部 session_id（若已知）
            if session_id:
                start_cmd = f'{claude} --resume "{session_id}"'
            else:
                start_cmd = claude
            subprocess.Popen(
                f'cd /d "{workdir}" && start "Claude: 续接聊天" cmd /k {start_cmd}',
                shell=True, cwd=workdir,
            )
            return {"ok": True, "cmd": start_cmd}
        # job / task：优先 --resume（有 session_id），否则按原 cmd 重新发起
        if session_id:
            start_cmd = f'{claude} --resume "{session_id}"'
        elif cmd.strip():
            start_cmd = cmd.strip()
        else:
            start_cmd = claude
        subprocess.Popen(
            f'cd /d "{workdir}" && start "Claude: 继续任务" cmd /k {start_cmd}',
            shell=True, cwd=workdir,
        )
        return {"ok": True, "cmd": start_cmd}
    except Exception as e:
        return {"ok": False, "error": f"启动失败: {e}"}


UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def move_session_to_trash(session_id, sessions_dir=None):
    """把指定 session_id 对应的 jsonl 移到回收站（tmp/trash/），绝不 os.remove。

    严格校验（安全红线，不允许放宽）：
      - session_id 必须是合法 UUID 格式（正则整段匹配，不接受子串/模糊匹配）；
      - 目标路径 os.path.abspath 后必须真实位于 sessions_dir 内（防路径穿越）；
      - 文件必须存在；
      - 只处理这一个文件，不遍历目录、不按时间/大小猜测、不批量操作。
    sessions_dir 缺省用全局 PROJECT_SESSIONS_DIR（测试时可传临时目录注入，避免碰真实数据）。

    返回 {ok, moved_to, error}。
    """
    sessions_dir = sessions_dir if sessions_dir is not None else PROJECT_SESSIONS_DIR
    sid = str(session_id or "").strip()
    if not UUID_RE.fullmatch(sid):
        return {"ok": False, "error": "session_id 不是合法 UUID，拒绝处理"}
    if not sessions_dir or not os.path.isdir(sessions_dir):
        return {"ok": False, "error": "会话目录不存在"}
    sessions_dir_abs = os.path.abspath(sessions_dir)
    target = os.path.abspath(os.path.join(sessions_dir, sid + ".jsonl"))
    # 防路径穿越：目标必须严格位于 sessions_dir 内部（父目录前缀 + 分隔符）
    if not (target == os.path.join(sessions_dir_abs, sid + ".jsonl")):
        return {"ok": False, "error": "路径校验失败（越界）"}
    if os.path.dirname(target) != sessions_dir_abs:
        return {"ok": False, "error": "路径校验失败（越界）"}
    if not os.path.isfile(target):
        return {"ok": False, "error": "该会话没有关联的 Claude 会话历史文件"}
    try:
        os.makedirs(TRASH_DIR, exist_ok=True)
        ts = datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(TRASH_DIR, f"{sid}.jsonl.{ts}.bak")
        # 时间戳精确到秒仍可能冲突（同秒多次操作），追加序号直到不重名
        n = 1
        while os.path.exists(dest):
            dest = os.path.join(TRASH_DIR, f"{sid}.jsonl.{ts}_{n}.bak")
            n += 1
        shutil.move(target, dest)   # 移动，不是复制+删除；绝不 os.remove 原文件
        return {"ok": True, "moved_to": dest}
    except Exception as e:
        return {"ok": False, "error": f"移动到回收站失败: {e}"}


def delete_entry(kind, ident, purge=False):
    """删除任务/会话的**状态记录**（默认不动任何真实会话/文件/进程）。

    kind:
      - "job"     删除 ~/.claude/jobs/<ident>/         （系统任务状态目录）
      - "task"    删除 tmp/tasks/<ident>.log           （自定义任务日志）
      - "chat"    删除 tmp/chats/<ident>.{json,log,md} （聊天会话记录）
      - "session" 只处理历史会话区的一条 jsonl（ident 就是 session_id/UUID），
                   等价于「完全删除」：移到回收站 + 若 tmp/chats 有同 session_id
                   的记录一并清理其仪表盘记录（jsonl 已被移走，留着就是僵尸记录）。

    purge: 仅对 kind="chat" 生效。False（默认）= 只删仪表盘记录（原行为）；
           True = 额外把该会话关联的 jsonl 移入 tmp/trash/ 回收站（不会 os.remove）。
           chat 的 session_id 从其自身 meta 里读取，绝不猜测/遍历。
    返回 {ok, deleted, file, purge_result?}。
    """
    try:
        if kind == "job":
            jobs_dir = os.path.join(CLAUDE_DIR, "jobs")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", str(ident)):
                return {"ok": False, "error": "任务 ID 不合法"}
            target = os.path.join(jobs_dir, str(ident))
            if not os.path.isdir(target) or os.path.abspath(target) == os.path.abspath(jobs_dir):
                return {"ok": False, "error": "目录不存在或越界"}
            shutil.rmtree(target)
            return {"ok": True, "deleted": ident, "file": str(ident)}
        elif kind == "task":
            tasks_dir = os.path.join(THIS_DIR, "tmp", "tasks")
            if not re.fullmatch(r"task_\d{8}_\d{6}\.log", str(ident)):
                return {"ok": False, "error": "任务日志名不合法"}
            path = os.path.join(tasks_dir, str(ident))
            if not os.path.exists(path):
                return {"ok": False, "error": "日志不存在"}
            os.remove(path)
            return {"ok": True, "deleted": ident, "file": path}
        elif kind == "chat":
            chats_dir = CHATS_DIR
            if not re.fullmatch(r"c\d{8}_\d{6}(\_\d+)?", str(ident)):
                return {"ok": False, "error": "会话 ID 不合法"}
            purge_result = None
            if purge:
                # 完全删除前先检查：该会话是否正在运行中（有活进程）——不允许对运行中的会话完全删除
                with _chat_procs_lock:
                    running = _chat_procs.get(str(ident))
                if running and running.poll() is None:
                    return {"ok": False, "error": "该会话正在回复中，请先中断（⏹）再完全删除"}
                cur = chat_get(str(ident))
                sid = (cur or {}).get("session_id", "")
                if not sid:
                    # 空会话骨架：没有关联的 Claude 会话历史，只能按①处理，不瞎找文件
                    purge_result = {"ok": True, "note": "该会话没有关联的 Claude 会话历史，将只删除仪表盘记录"}
                else:
                    purge_result = move_session_to_trash(sid)
                    if not purge_result.get("ok"):
                        # 移动失败：不继续删仪表盘记录，避免"jsonl 还在但仪表盘记录已丢"的不一致
                        return {"ok": False, "error": purge_result.get("error", "完全删除失败"), "purge_result": purge_result}
            removed = []
            for ext in (".json", ".log", ".md"):
                p = os.path.join(chats_dir, str(ident) + ext)
                if os.path.exists(p):
                    os.remove(p)
                    removed.append(os.path.basename(p))
            result = {"ok": True, "deleted": ident, "file": removed or "无文件"}
            if purge_result is not None:
                result["purge_result"] = purge_result
            return result
        elif kind == "session":
            # 历史会话区的一条 jsonl：只有"完全删除"这一种语义（移入回收站）
            sid = str(ident or "")
            purge_result = move_session_to_trash(sid)
            if not purge_result.get("ok"):
                return {"ok": False, "error": purge_result.get("error", "删除失败")}
            # 若 tmp/chats 里有同 session_id 的记录（该会话恰好也是仪表盘发起的），一并清理，
            # 避免仪表盘任务卡里留下指向已移除 jsonl 的僵尸记录。只按 session_id 精确匹配，不模糊。
            cleaned_chat = None
            try:
                for c in chat_list():
                    if c.get("session_id") == sid:
                        cleaned_chat = c.get("id")
                        for ext in (".json", ".log", ".md"):
                            p = os.path.join(CHATS_DIR, str(cleaned_chat) + ext)
                            if os.path.exists(p):
                                os.remove(p)
                        break
            except Exception:
                pass
            return {"ok": True, "deleted": sid, "moved_to": purge_result.get("moved_to"),
                    "cleaned_chat": cleaned_chat}
        return {"ok": False, "error": f"未知 kind: {kind}"}
    except Exception as e:
        return {"ok": False, "error": f"删除失败: {e}"}


def launch_new_task(agent, prompt, cwd="", permission="default", type="", skill=""):
    """通过对话框启动一个新的 Claude 任务（非交互式 -p 模式，后台执行）。

    - type:     "claude"(默认) / "agents" / "skills"
    - agent:    type=agents 时指定的 Agent 名；否则 --agent <name>
    - skill:    type=skills 时指定的 Skill 名；claude -p 不支持 --skill，
                改为在 prompt 前追加"先调用 /<skill> skill"指令让模型调用
    - prompt:   任务提示词（必填）
    - cwd:      工作目录，默认 PROJECT_ROOT
    - permission: default / auto / acceptEdits / plan / bypass

    返回 {ok, cmd, log 文件路径}。任务在独立 Python 子进程后台跑，
    输出写入 tmp 目录的日志文件（供前端轮询展示）。
    """
    if not prompt or not prompt.strip():
        return {"ok": False, "error": "提示词不能为空"}
    workdir = cwd.strip() or PROJECT_ROOT
    if not os.path.isdir(workdir):
        return {"ok": False, "error": f"工作目录不存在: {workdir}"}

    type = (type or "").strip() or "claude"
    # 校验 agent / skill 名称合法性
    if agent and not re.fullmatch(r"[A-Za-z0-9_\-\.]+", agent):
        return {"ok": False, "error": "Agent 名称不合法"}
    if skill and not re.fullmatch(r"[A-Za-z0-9_\-]+", skill):
        return {"ok": False, "error": "Skill 名称不合法"}

    # 组装真实提示词：skills 类型在 prompt 前加"调用 skill"指令
    real_prompt = prompt
    if type == "skills" and skill:
        real_prompt = f"请先调用 /{skill} skill 来处理本任务。\n\n{real_prompt}"

    # 权限模式白名单
    perm_map = {
        "default": "", "auto": "--permission-mode auto",
        "acceptEdits": "--permission-mode acceptEdits",
        "plan": "--permission-mode plan",
        "bypass": "--dangerously-skip-permissions",
    }
    perm_flag = perm_map.get(permission, "")

    claude = shutil.which("claude") or "claude"
    # 组装参数（用列表避免 shell 转义问题）
    args = [claude]
    if type == "agents" and agent:
        args += ["--agent", agent]
    if perm_flag:
        args += perm_flag.split(" ")
    args += ["-p", real_prompt]

    # 日志目录
    tmp_dir = os.path.join(THIS_DIR, "tmp", "tasks")
    os.makedirs(tmp_dir, exist_ok=True)
    task_id = datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(tmp_dir, f"task_{task_id}.log")

    # 后台线程跑 claude -p，输出落到 log
    def _run():
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"$ {' '.join(args)}\n")
                f.write(f"# 工作目录: {workdir}\n")
                f.write(f"# 启动: {now_local()}\n")
                f.flush()
                subprocess.run(args, cwd=workdir, stdout=f, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace")
            # 显式完成标记（供 collect_custom_tasks 可靠判定结束）
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n# 结束: %s\n" % now_local())
        except Exception as e:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[错误] {e}\n# 结束: {now_local()}\n")

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "task_id": task_id,
            "cmd": " ".join(args), "log": log_path}


def read_task_log(name):
    """读取新任务日志尾部（URL 白名单：只允许 task_xxx.log）。"""
    if not name or not re.fullmatch(r"task_\d{8}_\d{6}\.log", name):
        return {"error": "日志名不合法"}
    log_path = os.path.join(THIS_DIR, "tmp", "tasks", name)
    if not os.path.exists(log_path):
        return {"error": "日志不存在"}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return {"error": f"读取失败: {e}"}
    # 判断是否还在跑：即最后一个 subprocess 是否已写 [错误] 或命令完成
    tail = "".join(lines[-400:])  # 尾部 400 行
    running = True
    # 简单判断：日志末尾 10 行如果出现典型的完成标记则视为完成
    # （更可靠：由前端轮询判定，这里仅返回全文）
    return {"ok": True, "name": name, "running": running,
            "lines": len(lines), "tail": tail}


# ---------------------------------------------------------------------------
# 网页聊天：与交互式任务在网页内多轮对话
# ---------------------------------------------------------------------------
# 机制：每个"会话"数据落盘到 tmp/chats/<id>.json；
#      每轮对话 = 一个 `claude -p <消息> --output-format stream-json --verbose --resume <session_id>`
#      子进程，把 stdout 的 JSONL 事件写到 tmp/chats/<id>.log；
#      前端轮询该 log，解析 stream-json 事件还原聊天气泡。
#      子进程结束（result 事件）即本轮完成，session_id 保持不变以延续上下文。

CHATS_DIR = os.path.join(THIS_DIR, "tmp", "chats")
_chat_lock = threading.Lock()

# 运行中的聊天子进程注册表 { cid: Popen }，供「中断」操作终止本轮回复
_chat_procs = {}
_chat_procs_lock = threading.Lock()


def _chat_state_path(cid):
    return os.path.join(CHATS_DIR, cid + ".json")


def chat_list():
    """返回所有聊天会话元数据（最近的在最前），含 user_msgs（从 .md 还原用户消息）。"""
    out = []
    if not os.path.isdir(CHATS_DIR):
        return out
    for fn in sorted(os.listdir(CHATS_DIR), reverse=True):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(CHATS_DIR, fn), "r", encoding="utf-8") as f:
                st = json.load(f)
            st["user_msgs"] = chat_user_msgs(fn[:-5])
            out.append(st)
        except Exception:
            continue
    return out


def chat_user_msgs(cid):
    """从 tmp/chats/<cid>.md 解析历史用户消息（## 👤 用户 段），按顺序返回列表。

    用户消息是纯前端内存记录（s.userMsgs），刷新后即丢失；md 每条用户输入前都有
    `## 👤 用户` 标题，可据此恢复，保证刷新/重开面板后用户气泡仍显示。
    """
    md_path = os.path.join(CHATS_DIR, cid + ".md")
    if not os.path.exists(md_path):
        return []
    try:
        with open(md_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return []
    msgs = []
    parts = re.split(r"^##\s*👤\s*用户\s*$", text, flags=re.MULTILINE)
    for seg in parts[1:]:
        m = seg.strip()
        if m:
            msgs.append(m)
    return msgs


def chat_start(agent, prompt, cwd="", permission="default", resume_id=None, skill="", model=""):
    """开启（或沿用）一个聊天会话，并发出第一轮消息。

    resume_id: 已有聊天的 id（其 internal session_id 会被 --resume 复用）。
    skill:     指定 skill 名称，将在首条消息前注入 /skill-name 前缀（用默认 Claude 执行）。
    prompt 为空时：仅创建一个空会话（不执行 claude），等用户稍后发消息。
    返回 {ok, id, agent, session_id, skill}。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        # 空消息：建会话骨架（后续 /api/chat/start 带消息时再跑）
        cid = datetime.now(LOCAL_TZ).strftime("c%Y%m%d_%H%M%S")
        while os.path.exists(_chat_state_path(cid)):
            cid = datetime.now(LOCAL_TZ).strftime("c%Y%m%d_%H%M%S_%f")
        os.makedirs(CHATS_DIR, exist_ok=True)
        meta = {
            "id": cid, "agent": agent or "claude", "cwd": cwd.strip() or PROJECT_ROOT,
            "permission": permission, "session_id": "", "skill": skill or "",
            "model": model or "auto",
            "prompt": "", "log": cid + ".log", "created": now_local(),
            "updated": now_local(), "status": "waiting",
        }
        with _chat_lock:
            with open(_chat_state_path(cid), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        return {"ok": True, "id": cid, "agent": meta["agent"], "session_id": "", "log": cid + ".log"}

    workdir = cwd.strip() or PROJECT_ROOT
    if not os.path.isdir(workdir):
        return {"ok": False, "error": f"工作目录不存在: {workdir}"}
    if agent and not re.fullmatch(r"[A-Za-z0-9_\-]+", agent):
        return {"ok": False, "error": "Agent 名称不合法"}
    if skill and not re.fullmatch(r"[A-Za-z0-9_\-]+", skill):
        return {"ok": False, "error": "Skill 名称不合法"}

    perm_map = {
        "default": "", "auto": "--permission-mode auto",
        "acceptEdits": "--permission-mode acceptEdits",
        "plan": "--permission-mode plan",
        "bypass": "--dangerously-skip-permissions",
    }
    perm_flag = perm_map.get(permission, "")

    os.makedirs(CHATS_DIR, exist_ok=True)
    claude = shutil.which("claude") or "claude"

    # 复用已有会话（resume）
    internal_sid = None
    existing = None
    if resume_id:
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", resume_id):
            # 直接传外部 sessionId（如系统任务 job 的 sessionId）——在对话窗口续接该任务上下文
            internal_sid = resume_id
        else:
            existing = chat_get(resume_id)
            if existing and existing.get("session_id"):
                internal_sid = existing["session_id"]
                agent = existing.get("agent") or agent
                workdir = existing.get("cwd") or workdir
                permission = existing.get("permission") or permission
                perm_flag = perm_map.get(permission, "")
                skill = existing.get("skill") or skill
            # 若该会话正在回复中（_chat_procs 有运行进程），先自动中断再发新消息
            # （等价 CLI：回复中继续输入会打断当前回复、立即开启新一轮）
            if existing:
                with _chat_procs_lock:
                    running = _chat_procs.get(resume_id)
                if running and running.poll() is None:
                    chat_stop(resume_id)

    if existing:
        cid = resume_id                       # 续接已有 chat 会话：沿用原 cid
    else:
        cid = datetime.now(LOCAL_TZ).strftime("c%Y%m%d_%H%M%S")
        # 避免并发同名（毫秒级重试）
        while os.path.exists(_chat_state_path(cid)):
            cid = datetime.now(LOCAL_TZ).strftime("c%Y%m%d_%H%M%S_%f")

    log_path = os.path.join(CHATS_DIR, cid + ".log")
    markdown_log = os.path.join(CHATS_DIR, cid + ".md")  # 渲染后的聊天记录

    # 指定 skill 时：仅在【新建会话首轮】注入"调用 skill"指令（用默认 Claude 会话执行；
    # CLI 无独立 --skill 参数，靠自然语言让模型调用；与 launch_new_task 的 skills 类型一致）。
    # 续接已有会话（existing 非空）时不再注入——否则每轮回复前都重复出现"请先调用 /xxx skill"。
    if skill and not existing:
        prompt = f"请先调用 /{skill} skill 来处理本任务。\n\n{prompt}"

    # 组装命令
    args = [claude]
    if agent:
        args += ["--agent", agent]
    if model and model != "auto":
        args += ["--model", model]
    if perm_flag:
        args += perm_flag.split(" ")
    args += ["-p", prompt, "--output-format", "stream-json", "--verbose"]
    if internal_sid:
        args += ["--resume", internal_sid]

    # 记录会话元数据
    meta = {
        "id": cid,
        "agent": agent or "claude",
        "cwd": workdir,
        "permission": permission,
        "session_id": internal_sid or "",
        "skill": skill or "",
        "model": model or "auto",
        "prompt": prompt,
        "log": cid + ".log",
        "created": now_local(),
        "updated": now_local(),
        "status": "working",   # working(处理中) / waiting(已回复等你继续) / error
    }
    with _chat_lock:
        if not existing:
            with open(_chat_state_path(cid), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        else:
            meta = existing
            meta["status"] = "working"
            meta["updated"] = now_local()
            with open(_chat_state_path(cid), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

    # 本轮消息追加到 md 记录
    def _write_md():
        try:
            with open(markdown_log, "a", encoding="utf-8") as f:
                f.write(f"\n## 👤 用户\n\n{prompt}\n")
        except Exception:
            pass
    _write_md()

    # 后台线程执行 claude，把事件 JSONL 落到 log
    def _run():
        proc = None
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n--- NEW TURN ---\n")
                f.flush()
                # 不能直接 stdout=f：需要实时解析 init 里的 session_id
                # （否则第二轮无从 --resume）。用逐行读取，边写文件边解析。
                proc = subprocess.Popen(
                    args, cwd=workdir, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    bufsize=1,  # 行缓冲
                )
                with _chat_procs_lock:
                    _chat_procs[cid] = proc   # 注册正在运行的本轮进程（供中断终止）
                got_sid = bool(internal_sid)
                for line in proc.stdout:
                    with open(log_path, "a", encoding="utf-8") as f2:
                        f2.write(line + "\n")
                        f2.flush()
                    if not got_sid and line.lstrip().startswith("{"):
                        try:
                            ev = json.loads(line)
                            if ev.get("type") == "system" and ev.get("subtype") == "init":
                                sid = ev.get("session_id")
                                if sid:
                                    got_sid = True
                                    try:
                                        with open(_chat_state_path(cid), "r", encoding="utf-8") as fst:
                                            cur = json.load(fst)
                                        cur["session_id"] = sid
                                        with open(_chat_state_path(cid), "w", encoding="utf-8") as fst:
                                            json.dump(cur, fst, ensure_ascii=False, indent=2)
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                if proc.stdout:
                    proc.stdout.close()
                rc = proc.wait()
                with _chat_procs_lock:
                    _chat_procs.pop(cid, None)   # 本轮结束，注销进程
            # 本轮完成后回写状态：waiting = 本轮已回复、正在等你输入下一步（多轮对话无"完成"态）
            try:
                with open(_chat_state_path(cid), "r", encoding="utf-8") as fst:
                    cur = json.load(fst)
                cur["status"] = "error" if rc else "waiting"
                cur["updated"] = now_local()
                with open(_chat_state_path(cid), "w", encoding="utf-8") as fst:
                    json.dump(cur, fst, ensure_ascii=False, indent=2)
            except Exception:
                pass
        except Exception as e:
            try:
                with open(_chat_state_path(cid), "r", encoding="utf-8") as fst:
                    cur = json.load(fst)
                cur["status"] = "error"
                cur["error"] = str(e)
                with open(_chat_state_path(cid), "w", encoding="utf-8") as fst:
                    json.dump(cur, fst, ensure_ascii=False, indent=2)
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "id": cid, "agent": meta["agent"],
            "session_id": internal_sid or "", "log": cid + ".log"}


def chat_stop(cid):
    """中断指定聊天会话的本轮回复（等价于 CLI 的 Ctrl-C）。

    终止正在运行的 claude 子进程；会话回到"待回复"状态（waiting），
    已生成的部分回复保留在 events 里，可直接继续输入下一轮。
    """
    if not cid or not re.fullmatch(r"c\d{8}_\d{6}(_\d+)?", cid):
        return {"ok": False, "error": "会话 id 不合法"}
    with _chat_procs_lock:
        proc = _chat_procs.get(cid)
        if not proc or proc.poll() is not None:
            # 没有在运行的进程：可能已结束/从未运行，若状态卡在 working 则复位
            cur = chat_get(cid)
            if cur and cur.get("status") == "working":
                cur["status"] = "waiting"
                cur["updated"] = now_local()
                with _chat_lock:
                    with open(_chat_state_path(cid), "w", encoding="utf-8") as f:
                        json.dump(cur, f, ensure_ascii=False, indent=2)
                return {"ok": True, "note": "会话已复位为待回复（无运行中进程）"}
            return {"ok": False, "error": "该会话当前没有正在运行的回复"}
    try:
        proc.terminate()          # 先发 SIGTERM
        try:
            proc.wait(timeout=3)  # 等它退出
        except subprocess.TimeoutExpired:
            proc.kill()           # 3 秒不退就强杀
            proc.wait(timeout=3)
    except Exception as e:
        return {"ok": False, "error": f"中断失败: {e}"}
    with _chat_procs_lock:
        _chat_procs.pop(cid, None)
    # 转 waiting：中断视为"本轮被截断"，等用户继续输入
    cur = chat_get(cid)
    if cur:
        cur["status"] = "waiting"
        cur["updated"] = now_local()
        with _chat_lock:
            with open(_chat_state_path(cid), "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
    return {"ok": True, "interrupted": True}


def chat_get(cid):
    """读取单个会话元数据。"""
    if not cid or not re.fullmatch(r"c\d{8}_\d{6}(_\d+)?", cid):
        return None
    p = _chat_state_path(cid)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def chat_set_model(cid, model):
    """切换会话模型（保存到 meta，下一轮 --resume 时带新 --model 生效）。

    切换不打断当前运行中的回复；已启动的本轮仍用原模型，下一条消息起用新模型。
    """
    if not cid or not re.fullmatch(r"c\d{8}_\d{6}(_\d+)?", cid):
        return {"ok": False, "error": "会话 id 不合法"}
    cur = chat_get(cid)
    if not cur:
        return {"ok": False, "error": "会话不存在"}
    model = (model or "auto").strip()
    if cur.get("model") == model:
        return {"ok": True, "nochange": True, "model": model}
    cur["model"] = model
    cur["updated"] = now_local()
    with _chat_lock:
        with open(_chat_state_path(cid), "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    return {"ok": True, "model": model}


def chat_set_mode(cid, mode, target=""):
    """切换会话执行方式 mode（claude|agent|skill），保存到 meta，下一条消息起生效。

    mode=claude：清除 agent/skill，用默认 Claude 执行
    mode=agent：meta.agent=target；mode=skill：meta.skill=target（沿用 --agent claude 执行 + 首条注入调用 skill）
    其他值 / 非法 target 一律拦截。不打断当前运行中的回复；已启动的本轮仍按原样式执行。
    """
    if not cid or not re.fullmatch(r"c\d{8}_\d{6}(_\d+)?", cid):
        return {"ok": False, "error": "会话 id 不合法"}
    cur = chat_get(cid)
    if not cur:
        return {"ok": False, "error": "会话不存在"}
    mode = (mode or "claude").strip()
    target = (target or "").strip()
    if mode not in ("claude", "agent", "skill"):
        return {"ok": False, "error": "mode 只支持 claude|agent|skill"}
    if mode == "claude":
        cur["agent"] = "claude"; cur["skill"] = ""
    elif mode == "agent":
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", target):
            return {"ok": False, "error": "Agent 名称不合法"}
        cur["agent"] = target; cur["skill"] = ""
    else:  # skill
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", target):
            return {"ok": False, "error": "Skill 名称不合法"}
        cur["skill"] = target; cur["agent"] = "claude"
    cur["updated"] = now_local()
    with _chat_lock:
        with open(_chat_state_path(cid), "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    return {"ok": True, "mode": mode, "agent": cur["agent"], "skill": cur["skill"]}


def chat_set_permission(cid, permission):
    """切换会话权限模式（default|auto|acceptEdits|plan|bypass），保存到 meta，下一条消息起生效。

    mode 语义同新建弹窗：default=无 --permission-mode（-p 下工具确认会失败）、
    auto=--permission-mode auto、acceptEdits=--permission-mode acceptEdits、
    plan=--permission-mode plan、bypass=--dangerously-skip-permissions。
    不打断当前运行中的回复；已启动的本轮仍按原权限执行，下一条消息起用新权限。
    """
    if not cid or not re.fullmatch(r"c\d{8}_\d{6}(_\d+)?", cid):
        return {"ok": False, "error": "会话 id 不合法"}
    cur = chat_get(cid)
    if not cur:
        return {"ok": False, "error": "会话不存在"}
    permission = (permission or "default").strip()
    if permission not in ("default", "auto", "acceptEdits", "plan", "bypass"):
        return {"ok": False, "error": "permission 只支持 default|auto|acceptEdits|plan|bypass"}
    if cur.get("permission") == permission:
        return {"ok": True, "nochange": True, "permission": permission}
    cur["permission"] = permission
    cur["updated"] = now_local()
    with _chat_lock:
        with open(_chat_state_path(cid), "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    return {"ok": True, "permission": permission}


def chat_rename(cid, name):
    """重命名会话（自定义名存 meta.name；空名则清除回退到首条消息）。"""
    if not cid or not re.fullmatch(r"c\d{8}_\d{6}(_\d+)?", cid):
        return {"ok": False, "error": "会话 id 不合法"}
    cur = chat_get(cid)
    if not cur:
        return {"ok": False, "error": "会话不存在"}
    name = (name or "").strip()[:50]
    cur["name"] = name
    cur["updated"] = now_local()
    with _chat_lock:
        with open(_chat_state_path(cid), "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    return {"ok": True, "name": name}


def chat_pin(cid, pinned):
    """置顶/取消置顶会话（存 meta.pinned；置顶的排序时排最前）。"""
    if not cid or not re.fullmatch(r"c\d{8}_\d{6}(_\d+)?", cid):
        return {"ok": False, "error": "会话 id 不合法"}
    cur = chat_get(cid)
    if not cur:
        return {"ok": False, "error": "会话不存在"}
    pinned = 1 if pinned else 0
    if cur.get("pinned") == pinned:
        return {"ok": True, "nochange": True, "pinned": pinned}
    cur["pinned"] = pinned
    cur["updated"] = now_local()
    with _chat_lock:
        with open(_chat_state_path(cid), "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    return {"ok": True, "pinned": pinned}


# 单条工具参数/结果下发给前端的字符上限。折叠气泡展开后也只需看开头一段；
# 实测单条 tool_result 可达 623KB（Read 大文件/Grep 大结果），不截断会让轮询 payload 爆到 8MB。
# 超出部分不下发，前端用 full_len 显示"原始 N 字符，已截断"。
_SLIM_MAX = 4000

# tool_use.input 里最能代表"这次在操作什么"的字段，按优先级取第一个命中的
_TOOL_ARG_KEYS = ("file_path", "path", "notebook_path", "pattern", "command", "skill",
                  "prompt", "url", "query", "description")

# 只有这几个字段是"纯路径"，可以安全地只保留 basename。
# 教训（2026-09-23）：早先对所有字段都做 split("[\\/]")[-1]，Bash 的 command 里
# 往往含 / 或 \，结果标签取到了命令中段（实测 "wiki_table_to_gantt.py',doraise=True); pri…"），
# 完全看不出在干什么。命令/查询类一律取开头一段。
_TOOL_PATH_KEYS = ("file_path", "path", "notebook_path")


def _tool_arg_label(inp):
    """从 tool_use.input 提炼一个简短标签（如文件名/命令开头），供折叠气泡标题行显示。"""
    if not isinstance(inp, dict):
        return ""
    key = arg = ""
    for k in _TOOL_ARG_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            key, arg = k, v.strip()
            break
    if not arg:
        return ""
    if key in _TOOL_PATH_KEYS and ("\\" in arg or "/" in arg):
        arg = re.split(r"[\\/]", arg)[-1] or arg
    # 多行命令/提示词只取第一行有内容的，再压掉连续空白
    arg = next((s for s in arg.splitlines() if s.strip()), arg)
    arg = re.sub(r"\s+", " ", arg).strip()
    return arg[:42] + "…" if len(arg) > 42 else arg


def _slim_event(ev):
    """把一条 stream-json 事件裁剪成前端渲染真正需要的最小结构。

    动机（2026-09-23）：真实长会话的 log 里 `system/thinking_tokens` 心跳事件能占到
    26MB / 13.9 万条（实测 c20260922_151604.log 共 44.5MB），而前端一条都不用。
    原实现把整份 log 全量解析后原样返回，浏览器每 1.5 秒轮询一次就要收下几十 MB
    无用 JSON。这里按类型白名单裁剪：
      - assistant：只留 content 里的 text/thinking/tool_use（tool_use 的 input 保留，
        前端折叠气泡要显示完整参数）
      - user：只留 tool_result / text（tool_result 在 user 事件里，不在 assistant 里）
      - result：只留 usage/cost/duration/modelUsage 等元数据字段，丢掉重复的正文
        （result.result 与 assistant 的 text 重合，前端本就跳过不渲染）
      - system / tool_progress 等：整类丢弃（心跳噪音，不参与渲染）
    返回 None 表示该事件无需下发。
    """
    if not isinstance(ev, dict):
        return None
    t = ev.get("type")
    if t == "_user":
        return ev
    if t == "assistant":
        msg = ev.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            return None
        keep = []
        for c in content:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "text" and c.get("text"):
                keep.append({"type": "text", "text": c["text"]})
            elif ct == "thinking" and c.get("thinking"):
                keep.append({"type": "thinking", "thinking": c["thinking"]})
            elif ct == "tool_use":
                inp = c.get("input", {})
                try:
                    inp_txt = json.dumps(inp, ensure_ascii=False, indent=2)
                except Exception:
                    inp_txt = str(inp)
                full = len(inp_txt)
                keep.append({"type": "tool_use", "id": c.get("id", ""),
                             "name": c.get("name", ""),
                             "arg": _tool_arg_label(inp),
                             "input_text": inp_txt[:_SLIM_MAX] ,
                             "full_len": full})
        if not keep:
            return None
        return {"type": "assistant", "message": {"content": keep}}
    if t == "user":
        msg = ev.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            return None
        keep = []
        for c in content:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "tool_result":
                # content 可能是 str，也可能是 list（如 tool_reference 列表）
                body = c.get("content")
                if not isinstance(body, str):
                    try:
                        body = json.dumps(body, ensure_ascii=False)
                    except Exception:
                        body = str(body)
                keep.append({"type": "tool_result", "tool_use_id": c.get("tool_use_id", ""),
                             "content": body[:_SLIM_MAX], "full_len": len(body),
                             "is_error": bool(c.get("is_error"))})
            elif ct == "text" and c.get("text"):
                keep.append({"type": "text", "text": c["text"]})
        if not keep:
            return None
        return {"type": "user", "message": {"content": keep}}
    if t == "result":
        out = {"type": "result"}
        for k in ("usage", "total_cost_usd", "duration_ms", "modelUsage", "subtype", "is_error"):
            if k in ev:
                out[k] = ev[k]
        return out
    return None


def _accum_live(ev, live):
    """就地累计"本轮实时进度"（最后一次工具调用 / 已输出字数 / 思考段数）。

    传入的是**裁剪前**的原始事件（信息比 slim 版更全）。live 在每个 NEW TURN
    分隔处被调用方重置，所以这里累计的始终是"当前这一轮"。
    """
    if not isinstance(ev, dict):
        return
    t = ev.get("type")
    if t == "assistant":
        msg = ev.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            return
        for c in content:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "text" and c.get("text"):
                live["out_chars"] += len(c["text"])
            elif ct == "thinking" and c.get("thinking"):
                live["thinking"] += 1
            elif ct == "tool_use":
                live["tool"] = str(c.get("name") or "")
                live["tool_arg"] = _tool_arg_label(c.get("input"))
    elif t == "user":
        # 工具已返回结果 → 该工具不再"进行中"，清掉提示（避免一直显示最后一个工具名）
        msg = ev.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list) and any(
                isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            live["tool"] = ""
            live["tool_arg"] = ""


def chat_log(cid, since=0):
    """读取会话 log 的 JSONL 事件（已裁剪）+ 元数据。

    返回 {ok, meta, events[], status, used_model, stats, live}。
    events 经 `_slim_event` 裁剪，只含前端渲染所需字段（详见该函数说明）。
    live 描述"处理中"会话的实时进度（当前工具/已输出字数/本轮耗时），供前端替代
    静态的"正在思考…"提示。
    """
    meta = chat_get(cid)
    if not meta:
        return {"error": "会话不存在"}
    log_path = os.path.join(CHATS_DIR, cid + ".log")
    events = []
    used_model = ""   # 本轮实际使用的模型（从 result 事件的 modelUsage.canonicalModel 提取）
    # 会话 token/成本/时长汇总（跨全部轮次累加，data 来自每轮 result 事件的 usage/total_cost_usd）
    stats = {"turns": 0, "input_tokens": 0, "cache_read_tokens": 0, "cache_create_tokens": 0,
             "output_tokens": 0, "cost_usd": 0.0, "duration_ms": 0}
    # 本轮实时进度（仅 working 时有意义）：最后一次工具调用、本轮输出字数、本轮起始时间
    live = {"tool": "", "tool_arg": "", "out_chars": 0, "turn_started": "", "thinking": 0}
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            lines = []
        # 每条用户消息插入到其对应轮次（NEW TURN 分隔行）之前，恢复"你→Claude"真实顺序
        user_msgs = chat_user_msgs(cid)
        ui = 0
        # 时间戳：assistant/user 事件自带 timestamp（UTC ISO），注入 ts；result 等无时间戳
        # 事件沿用最近一次已知时间。_user 事件无自身时间，用其所在轮首个带时间的事件回填。
        last_ts = ""
        pending_user = None
        # 解析全部 JSONL 事件（跨多轮；NEW TURN 分隔行会被 json.loads 忽略）
        for ln in lines:
            ln = ln.strip()
            if not ln.startswith("{"):
                if ln.startswith("--- NEW TURN"):
                    if ui < len(user_msgs):
                        # 这一轮的用户消息插在其 AI 输出之前
                        uev = {"type": "_user", "text": user_msgs[ui]}
                        events.append(uev)
                        pending_user = uev       # 等该轮首个带时间的事件来回填 ts
                        ui += 1
                    live = {"tool": "", "tool_arg": "", "out_chars": 0,
                            "turn_started": "", "thinking": 0}   # 新一轮：实时进度归零
                continue
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            slim = _slim_event(ev)
            if slim is not None:
                ts = (ev.get("timestamp") if isinstance(ev, dict) else "") or ""
                if ts:
                    slim["ts"] = ts
                    last_ts = ts
                    if pending_user is not None:
                        pending_user["ts"] = ts
                        pending_user = None
                elif last_ts:
                    slim["ts"] = last_ts          # 无时间戳事件（如 result）沿用最近时间
                events.append(slim)
            # 累计本轮实时进度（用裁剪前的原事件，信息更全）
            _accum_live(ev, live)
            # 累加 result 事件里的用量（每轮一个 result）
            if isinstance(ev, dict) and ev.get("type") == "result":
                u = ev.get("usage")
                if isinstance(u, dict):
                    stats["input_tokens"] += u.get("input_tokens", 0)
                    stats["cache_read_tokens"] += u.get("cache_read_input_tokens", 0)
                    stats["cache_create_tokens"] += u.get("cache_creation_input_tokens", 0)
                    stats["output_tokens"] += u.get("output_tokens", 0)
                cost = ev.get("total_cost_usd")
                if isinstance(cost, (int, float)):
                    stats["cost_usd"] += cost
                dur = ev.get("duration_ms")
                if isinstance(dur, (int, float)):
                    stats["duration_ms"] += dur
                stats["turns"] += 1
        # 从最近的 result 事件提取实际使用模型（auto 时也能显示真实模型）
        for ev in reversed(events):
            if ev.get("type") != "result":
                continue
            mu = ev.get("modelUsage")
            if isinstance(mu, dict):
                names = [v.get("canonicalModel") for v in mu.values()
                         if isinstance(v, dict) and v.get("canonicalModel")]
                names = [n for n in names if n]
                if names:
                    used_model = "/".join(sorted(set(names)))
            if not used_model:
                usage = ev.get("usage")
                if isinstance(usage, dict) and usage.get("model"):
                    used_model = str(usage["model"])
            if used_model:
                break
        # 首轮用户消息（首个 NEW TURN 前没有任何事件时）以及用户多于分隔时兜底
        if ui == 0 and user_msgs:
            events.insert(0, {"type": "_user", "text": user_msgs[0], "ts": last_ts})
    # 本轮已耗时：meta.updated 在每轮开始时被刷新，正好是本轮起点（仅 working 时有意义）
    if meta.get("status") == "working":
        try:
            t0 = datetime.strptime(meta.get("updated", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)
            live["elapsed_ms"] = int((datetime.now(LOCAL_TZ) - t0).total_seconds() * 1000)
        except Exception:
            live["elapsed_ms"] = 0
    else:
        live["elapsed_ms"] = 0
    return {"ok": True, "meta": meta, "events": events, "status": meta.get("status"),
            "agent": meta.get("agent", "claude"), "skill": meta.get("skill", ""),
            "permission": meta.get("permission", "default"),
            "used_model": used_model, "stats": stats, "live": live}


def chat_idle_info(cid, meta):
    """计算会话"最后活动时间"距今的天数，供任务卡片显示未更新提醒。

    最后活动时间优先取 tmp/chats/<cid>.log 的 mtime（最后一次有事件写入的时间，
    包括 AI 输出/中断等任何活动）；log 不存在则退回 meta.updated；都没有则 None。
    额外返回 session_exists：session_id 对应的 jsonl 是否还存在（仅只读 os.path.isfile，
    不做内容解析，用于判断"是否已被 Claude 清理、无法再续聊"）。
    """
    log_path = os.path.join(CHATS_DIR, cid + ".log")
    last_ts = None
    if os.path.exists(log_path):
        try:
            last_ts = datetime.fromtimestamp(os.path.getmtime(log_path), LOCAL_TZ)
        except Exception:
            last_ts = None
    if last_ts is None:
        upd = (meta or {}).get("updated", "")
        if upd:
            try:
                last_ts = datetime.strptime(upd, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)
            except Exception:
                last_ts = None
    idle_days = None
    last_active_str = ""
    if last_ts is not None:
        idle_days = (datetime.now(LOCAL_TZ) - last_ts).total_seconds() / 86400.0
        last_active_str = last_ts.strftime("%Y-%m-%d %H:%M:%S")
    sid = (meta or {}).get("session_id", "")
    session_exists = None
    if sid and UUID_RE.fullmatch(sid):
        session_exists = os.path.isfile(os.path.join(PROJECT_SESSIONS_DIR, sid + ".jsonl"))
    return {"idle_days": idle_days, "last_active": last_active_str, "session_exists": session_exists}


def chat_stats_summary(cid):
    """轻量提取会话 token/成本/模型（不解析全部事件，供任务卡片 overview 用）。

    返回 {tokens, cost_usd, turns, model}；无 result 时 tokens=0。
    """
    out = {"tokens": 0, "cost_usd": 0.0, "turns": 0, "model": "", "duration_ms": 0}
    if not cid or not re.fullmatch(r"c\d{8}_\d{6}(_\d+)?", cid):
        return out
    log_path = os.path.join(CHATS_DIR, cid + ".log")
    if not os.path.exists(log_path):
        return out
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return out
    tok = used_model = ""
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        if ev.get("type") != "result":
            continue
        u = ev.get("usage")
        if isinstance(u, dict):
            out["tokens"] += (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                              + u.get("cache_creation_input_tokens", 0) + u.get("output_tokens", 0))
        cost = ev.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            out["cost_usd"] += cost
        dur = ev.get("duration_ms")
        if isinstance(dur, (int, float)):
            out["duration_ms"] += dur
        out["turns"] += 1
        if not used_model:
            mu = ev.get("modelUsage")
            if isinstance(mu, dict):
                names = [v.get("canonicalModel") for v in mu.values()
                         if isinstance(v, dict) and v.get("canonicalModel")]
                names = [n for n in names if n]
                if names:
                    used_model = "/".join(sorted(set(names)))
            if not used_model and isinstance(u, dict) and u.get("model"):
                used_model = str(u["model"])
        if not tok:
            tok = used_model
    out["model"] = used_model
    return out


# ---------------------------------------------------------------------------
# 会话全局搜索（/api/chat/search）—— 纯本地读文件，零 token
# ---------------------------------------------------------------------------
# 搜索源：tmp/chats/<cid>.log（assistant 事件的文本回复 + 思考 + 工具参数）
#         与 tmp/chats/<cid>.md（## 👤 用户 段 = 用户消息，恢复上下文后前端可见）。
# 语义：多关键词用空格分词，全部命中才算（AND）；大小写不敏感（英文）；中文直接子串匹配。
# 每个会话最多返回 MAX_MATCHES_PER_CHAT 条命中片段，片段带前后语境（~SNIPPET_CTX 字符）。
# 性能：逐行读 log（不 json.loads 整文件），超大 log（几十 MB）也只流式扫一遍；用户消息 md
#       通常很小直接读。搜索是手动触发的低频操作，不做缓存。
_CHAT_SEARCH_CTX = 70            # 命中片段前后语境字符数
_CHAT_SEARCH_MAX_PER_CHAT = 2    # 每个会话最多返回几条命中片段
_CHAT_SEARCH_MAX_CHATS = 30      # 最多返回多少个会话
_CHAT_SEARCH_TOTAL_HITS = 200    # 最多累计命中多少个文本块（防止超大 log 返回海量片段）


def _search_snippet(text, lower_terms, term_lens):
    """在单个文本块里找第一个命中词，返回其前后语境的片段展示文本（len 指字符），
    找不到返回 None。lower_terms 已小写；term_lens 为各词原始长度（用于截取还原显示）。"""
    lo = text.lower()
    first = None
    for t in lower_terms:
        i = lo.find(t)
        if i < 0:
            return None
        if first is None or i < first[0]:
            first = (i, t)
    if first is None:
        return None
    i, _t = first
    s = max(0, i - _CHAT_SEARCH_CTX)
    e = min(len(text), i + len(_t) + _CHAT_SEARCH_CTX)
    return text[s:e]


def chat_search(q):
    """全局搜索对话内容（AI 回复 + 用户消息）。

    返回 {ok, q, results[]}，每条 result：
      {cid, agent, skill, prompt, title, status, created, updated,
       hits: 命中片段列表（已含语境）, total: 该会话命中文本块总数, kind: user|ai}
    覆盖 AI 回复与用户消息；会话无 log 或 md 则自动跳过。
    """
    q = (q or "").strip()
    if not q:
        return {"ok": False, "error": "关键词为空"}
    if len(q) > 200:       # 超长输入防护（避免无意义全文件扫描）
        return {"ok": False, "error": "关键词过长（≤200 字符）"}
    # 分词：空白分隔，过滤过短/纯符号词；保留中文（单字也有意义）与英文/数字词
    raw_terms = [t for t in q.split() if t]
    if not raw_terms:
        return {"ok": False, "error": "关键词为空"}
    terms = [t for t in raw_terms if len([c for c in t if c.isalnum()]) >= 1]
    if not terms:
        return {"ok": False, "error": "关键词需包含字母或数字"}
    lower_terms = [t.lower() for t in terms]
    term_lens = [len(t) for t in terms]

    results = []
    if not os.path.isdir(CHATS_DIR):
        return {"ok": True, "q": q, "results": results}
    total_hits = 0
    for fn in sorted(os.listdir(CHATS_DIR), reverse=True):   # 最近的在最前
        if not fn.endswith(".json"):
            continue
        cid = fn[:-5]
        meta = chat_get(cid)
        if not meta:
            continue
        hits = []                 # 本会话命中片段
        per_kind = {"user": 0, "ai": 0}

        # ① 用户消息（.md 的 ## 👤 用户 段）—— 优先且轻量
        for um in chat_user_msgs(cid):
            snip = _search_snippet(um, lower_terms, term_lens)
            if snip is not None:
                hits.append({"kind": "user", "text": snip})
                per_kind["user"] += 1
                total_hits += 1
                if len(hits) >= _CHAT_SEARCH_MAX_PER_CHAT:
                    break

        # ② AI 回复（.log 的 assistant 事件文本）—— 流式扫
        if len(hits) < _CHAT_SEARCH_MAX_PER_CHAT:
            log_path = os.path.join(CHATS_DIR, cid + ".log")
            if os.path.exists(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        for ln in f:
                            ln = ln.strip()
                            if not ln.startswith("{"):
                                continue
                            try:
                                ev = json.loads(ln)
                            except Exception:
                                continue
                            if ev.get("type") != "assistant":
                                continue
                            for c in (ev.get("message") or {}).get("content") or []:
                                if not isinstance(c, dict):
                                    continue
                                text = None
                                if c.get("type") == "text" and c.get("text"):
                                    text = c["text"]
                                elif c.get("type") == "tool_use" and isinstance(c.get("input"), dict):
                                    text = json.dumps(c.get("input"), ensure_ascii=False)
                                if not text:
                                    continue
                                snip = _search_snippet(text, lower_terms, term_lens)
                                if snip is None:
                                    continue
                                hits.append({"kind": "ai", "text": snip,
                                             "tool": c.get("name", "") if c.get("type") == "tool_use" else ""})
                                per_kind["ai"] += 1
                                total_hits += 1
                                if len(hits) >= _CHAT_SEARCH_MAX_PER_CHAT or total_hits >= _CHAT_SEARCH_TOTAL_HITS:
                                    break
                    # 内层循环里 break 只跳一层，这里做双重判定兜底
                except Exception:
                    pass

        if not hits:
            continue
        prompt = (meta.get("prompt") or "").strip()
        results.append({
            "cid": cid,
            "agent": meta.get("agent") or "claude",
            "skill": meta.get("skill") or "",
            "prompt": prompt[:120],
            "title": (meta.get("name") or prompt or meta.get("agent") or "对话")[:120],
            "status": meta.get("status") or "",
            "created": meta.get("created", ""),
            "updated": meta.get("updated", ""),
            "hits": hits,
            "total": per_kind["user"] + per_kind["ai"],
            "user_hits": per_kind["user"],
            "ai_hits": per_kind["ai"],
        })
        if len(results) >= _CHAT_SEARCH_MAX_CHATS or total_hits >= _CHAT_SEARCH_TOTAL_HITS:
            break
    return {"ok": True, "q": q, "results": results}


# ---------------------------------------------------------------------------
# 今日概览（/api/today）—— 纯本地读文件聚合，零 token
# ---------------------------------------------------------------------------
# 语义约定（与现有对话统计一致）：
#   - 一个会话只要"今日活跃"（其 tmp/chats/<cid>.log 的 mtime 落在今天），就把该会话
#     的全部 result 用量（token/成本/轮数/模型）计入"今日"。理由：result 事件不自带
#     时间戳，跨天精确归因代价高；对话绝大多数当天开始、当天结束，log mtime 是可靠且
#     轻量的"今天活跃"判据（与任务卡 idle_days 用 log mtime 同源）。
#   - 任务数 = 今日启动的 custom 任务（tmp/tasks 的"# 启动:"）+ 今日活跃的系统 job
#     （~/.claude/jobs 的 state.json createdAt/updatedAt 落在今天）。
#   - 性能：轮询频繁，服务端 60s 内存缓存一次重算，不每次全量扫文件。
_TODAY_CACHE = {"t": 0.0, "data": None}
_TODAY_CACHE_LOCK = threading.Lock()
_TODAY_TTL = 60.0


def _iso_date_local(ts_str):
    """把时间字符串转成本地日期 'YYYY-MM-DD'，失败返回 ''。"""
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
    except Exception:
        return ""


def scan_chat_today(cid):
    """扫描单个会话日志：若 log mtime 落在今天，聚合其全部 result 用量。
    返回 {today, tokens, cost_usd, turns, models:{模型名:轮次}}；非今日返回 today=False。"""
    out = {"today": False, "tokens": 0, "cost_usd": 0.0, "turns": 0, "models": {}}
    if not cid or not re.fullmatch(r"c\d{8}_\d{6}(_\d+)?", cid):
        return out
    log_path = os.path.join(CHATS_DIR, cid + ".log")
    try:
        mt = datetime.fromtimestamp(os.path.getmtime(log_path), LOCAL_TZ)
    except Exception:
        return out
    if mt.strftime("%Y-%m-%d") != datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"):
        return out
    out["today"] = True
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return out
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        if ev.get("type") != "result":
            continue
        u = ev.get("usage")
        if isinstance(u, dict):
            out["tokens"] += (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                              + u.get("cache_creation_input_tokens", 0) + u.get("output_tokens", 0))
        cost = ev.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            out["cost_usd"] += cost
        out["turns"] += 1
        mu = ev.get("modelUsage")
        if isinstance(mu, dict):
            for v in mu.values():
                if isinstance(v, dict) and v.get("canonicalModel"):
                    m = str(v["canonicalModel"])
                    out["models"][m] = out["models"].get(m, 0) + 1
        elif isinstance(u, dict) and u.get("model"):
            m = str(u["model"])
            out["models"][m] = out["models"].get(m, 0) + 1
    return out


def collect_today(force=False):
    """聚合今日概览（服务端 60s 缓存）。
    返回 {date, sessions, tasks, tokens, cost_usd, turns, top_model, model_counts}。"""
    now = datetime.now(LOCAL_TZ)
    today = now.strftime("%Y-%m-%d")
    with _TODAY_CACHE_LOCK:
        if not force and _TODAY_CACHE["data"] is not None and (now - _TODAY_CACHE["t"]).total_seconds() < _TODAY_TTL:
            return _TODAY_CACHE["data"]
    acc = {"tokens": 0, "cost_usd": 0.0, "turns": 0, "models": {}}
    sessions = tasks = 0
    # 1) 对话会话（tmp/chats/<cid>.log，mtime 今日=今日活跃）
    if os.path.isdir(CHATS_DIR):
        for fn in os.listdir(CHATS_DIR):
            if not fn.endswith(".log"):
                continue
            cid = fn[:-4]
            if not re.fullmatch(r"c\d{8}_\d{6}(_\d+)?", cid):
                continue
            r = scan_chat_today(cid)
            if not r["today"]:
                continue
            sessions += 1
            acc["tokens"] += r["tokens"]
            acc["cost_usd"] += r["cost_usd"]
            acc["turns"] += r["turns"]
            for m, c in r["models"].items():
                acc["models"][m] = acc["models"].get(m, 0) + c
    # 2) custom 任务（tmp/tasks 的 "# 启动: <date ...>"，启动时间落在今天）
    tasks_dir = os.path.join(THIS_DIR, "tmp", "tasks")
    if os.path.isdir(tasks_dir):
        for fn in os.listdir(tasks_dir):
            if not fn.endswith(".log"):
                continue
            try:
                with open(os.path.join(tasks_dir, fn), "r", encoding="utf-8",
                          errors="replace") as f:
                    head = f.read(2048)
            except Exception:
                continue
            for line in head.splitlines():
                if line.startswith("# 启动:"):
                    if line.split(":", 1)[1].strip()[:10] == today:
                        tasks += 1
                    break
    # 3) 系统 job（~/.claude/jobs 的 state.json，createdAt/updatedAt 落在今天）
    jobs_dir = os.path.join(CLAUDE_DIR, "jobs")
    if os.path.isdir(jobs_dir):
        for d in os.listdir(jobs_dir):
            state_path = os.path.join(jobs_dir, d, "state.json")
            if not os.path.exists(state_path):
                continue
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    st = json.load(f)
            except Exception:
                continue
            if (_iso_date_local(st.get("createdAt")) == today
                    or _iso_date_local(st.get("updatedAt")) == today):
                tasks += 1
    # 最常用模型：canonicalModel 计数取 top1（模型名排序保证稳定输出）
    top_model = ""
    top_n = 0
    for m in sorted(acc["models"]):
        if acc["models"][m] > top_n:
            top_n, top_model = acc["models"][m], m
    data = {
        "date": today,
        "sessions": sessions,
        "tasks": tasks,
        "tokens": acc["tokens"],
        "cost_usd": round(acc["cost_usd"], 4),
        "turns": acc["turns"],
        "top_model": top_model,
        "model_counts": acc["models"],
    }
    with _TODAY_CACHE_LOCK:
        _TODAY_CACHE["t"] = now
        _TODAY_CACHE["data"] = data
    return data


# ---------------------------------------------------------------------------
# token/成本趋势（/api/trend）—— 纯本地读文件聚合，零 token
# ---------------------------------------------------------------------------
# 归因语义（与 chat_log 时间戳机制一致）：
#   - result 事件不自带时间戳，但其所在轮次的前置 assistant 事件带 timestamp（UTC ISO）。
#   - 遍历每行时维护 last_ts：assistant/user 事件更新它；result 事件沿用最近一次
#     last_ts（同一轮内时间近似，跨天归因为轮次粒度，误差仅在"午夜恰好在回复中"时出现）。
#   - 每个 result 一轮，其 usage（input+cache_read+cache_create+output）与 total_cost_usd
#     归到该轮 assistant 时间戳对应的本地日期。
#   - 性能：/api/trend 走 60s 内存缓存（与 /api/today 同构），不随 overview 高频轮询；
#     前端手动刷新时 ?force=1 强制重算。
_TREND_CACHE = {"t": 0.0, "data": None}
_TREND_CACHE_LOCK = threading.Lock()
_TREND_TTL = 60.0


def collect_trend(days=30, force=False):
    """聚合最近 N 天每日 token/成本/轮数/活跃会话数趋势。
    days 限制 1..90。返回 {days, start, end, daily[]}：
      daily = [{date, tokens, cost_usd, turns, sessions}]，按日期升序，
      无数据的日期也占位（tokens=0/cost=0），保证折线图连续。"""
    days = max(1, min(90, int(days)))
    now = datetime.now(LOCAL_TZ)
    today = now.strftime("%Y-%m-%d")
    with _TREND_CACHE_LOCK:
        if not force and _TREND_CACHE["data"] is not None and (now - _TREND_CACHE["t"]).total_seconds() < _TREND_TTL:
            return _TREND_CACHE["data"]
    # 日期桶：今天往前 days-1 天（含今天）
    by_date = {}
    for i in range(days - 1, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        by_date[d] = {"tokens": 0, "cost_usd": 0.0, "turns": 0, "sessions": 0}
    start_date = datetime.strptime(min(by_date), "%Y-%m-%d")
    if os.path.isdir(CHATS_DIR):
        for fn in sorted(os.listdir(CHATS_DIR)):
            if not fn.endswith(".log"):
                continue
            cid = fn[:-4]
            if not re.fullmatch(r"c\d{8}_\d{6}(_\d+)?", cid):
                continue
            log_path = os.path.join(CHATS_DIR, fn)
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except Exception:
                continue
            last_ts = ""          # 最近一次带时间戳事件（assistant/user），result 沿用
            chat_days = set()     # 该会话有 result 归因到的日期（算活跃会话数）
            for ln in lines:
                ln = ln.strip()
                if not ln.startswith("{"):
                    continue
                try:
                    ev = json.loads(ln)
                except Exception:
                    continue
                if not isinstance(ev, dict):
                    continue
                ts = ev.get("timestamp") or ""
                if ts:
                    last_ts = ts
                if ev.get("type") != "result":
                    continue
                day = _iso_date_local(last_ts)   # result 无时间戳，沿用最近已知时间
                if day not in by_date:
                    continue
                u = ev.get("usage")
                if isinstance(u, dict):
                    by_date[day]["tokens"] += (u.get("input_tokens", 0)
                                               + u.get("cache_read_input_tokens", 0)
                                               + u.get("cache_creation_input_tokens", 0)
                                               + u.get("output_tokens", 0))
                cost = ev.get("total_cost_usd")
                if isinstance(cost, (int, float)):
                    by_date[day]["cost_usd"] += cost
                by_date[day]["turns"] += 1
                chat_days.add(day)
            for day in chat_days:
                by_date[day]["sessions"] += 1
    daily = []
    for d in sorted(by_date):
        daily.append({"date": d, "tokens": by_date[d]["tokens"],
                      "cost_usd": round(by_date[d]["cost_usd"], 4),
                      "turns": by_date[d]["turns"], "sessions": by_date[d]["sessions"]})
    data = {"days": days, "start": start_date.strftime("%Y-%m-%d"), "end": today,
            "tokens_total": sum(x["tokens"] for x in daily),
            "cost_total": round(sum(x["cost_usd"] for x in daily), 4),
            "turns_total": sum(x["turns"] for x in daily),
            "sessions_total": sum(x["sessions"] for x in daily),
            "daily": daily}
    with _TREND_CACHE_LOCK:
        _TREND_CACHE["t"] = now
        _TREND_CACHE["data"] = data
    return data


def open_in_explorer(path):
    """在资源管理器中打开文件所在目录（并选中该文件）。"""
    if not path or not os.path.exists(path):
        return {"ok": False, "error": "路径不存在"}
    norm = os.path.normpath(path)
    folder = norm if os.path.isdir(norm) else os.path.dirname(norm)
    # 用 cmd start 异步打开目录（explorer 作为单例进程，返回码不可靠）
    try:
        subprocess.Popen(f'cmd /c start "" "{folder}"', shell=True)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": f"打开失败: {e}"}


def clean_jobs(ids):
    """删除指定的 job 状态目录（~/.claude/jobs/<id>/ 下的 state.json + timeline.jsonl）。

    仅删除任务**状态快照**，不影响任何真实会话/进程/文件。
    id 必须是 jobs 目录下的合法目录名（防路径穿越）。
    返回 {ok, deleted[], failed[]}。
    """
    jobs_dir = os.path.join(CLAUDE_DIR, "jobs")
    deleted, failed = [], []
    if not isinstance(ids, list):
        return {"ok": False, "error": "ids 需为数组"}
    for jid in ids:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(jid)):
            failed.append(jid)
            continue
        target = os.path.join(jobs_dir, str(jid))
        # 防意外：必须是 jobs 下的子目录，绝不能是 jobs 本身或越界
        if not os.path.isdir(target) or os.path.abspath(target) == os.path.abspath(jobs_dir):
            failed.append(jid)
            continue
        try:
            shutil.rmtree(target)
            deleted.append(jid)
        except Exception:
            failed.append(jid)
    return {"ok": True, "deleted": deleted, "failed": failed}


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeDesktop/1.0"

    def log_message(self, fmt, *args):
        # 静默访问日志
        pass

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path == "/" or path == "/index.html":
            idx = os.path.join(THIS_DIR, "index.html")
            if not os.path.exists(idx):
                self._send_json({"error": "index.html missing"}, 500)
            else:
                self._send_file(idx, "text/html; charset=utf-8")
        elif path == "/api/overview":
            agents = collect_agents()
            skills = collect_skills()
            jobs = collect_jobs()
            custom = collect_custom_tasks()
            sessions = collect_sessions()
            # 合并系统任务 + 对话框任务（custom）
            jobs = jobs + custom
            # 合并「对话」会话（tmp/chats）—— 处理中的对话也是任务，应显示在正在运行面板
            # 对话卡片的 token/成本/模型来自日志 result 事件统计（chat_stats_summary 轻量提取）
            for c in chat_list():
                _cs = chat_stats_summary(c.get("id", ""))
                _idle = chat_idle_info(c.get("id", ""), c)
                jobs.append({
                    "id": "chat-" + c["id"],
                    "name": (c.get("agent") or "claude") + ((" / " + str(c.get("skill") or "")) if c.get("skill") else ""),
                    # 对话无真正"完成"态：working=处理中，其余（waiting/done/error）都展示为待回复
                    "state": "working" if c.get("status") == "working" else ("error" if c.get("status") == "error" else "waiting"),
                    "detail": (c.get("prompt") or "").strip()[:200] or "（对话）",
                    "tempo": "", "tokens": _cs["tokens"], "cwd": c.get("cwd", ""),
                    "model": _cs["model"] or c.get("model", ""),
                    "intent": "对话会话", "sessionId": c.get("session_id", ""),
                    "createdAt": c.get("created", ""), "updatedAt": c.get("updated", ""),
                    "timeline": [], "source": "chat", "chat_id": c.get("id"),
                    "cost_usd": _cs["cost_usd"], "turns": _cs["turns"], "duration_ms": _cs["duration_ms"],
                    "pinned": c.get("pinned", 0),
                    "idle_days": _idle["idle_days"], "last_active": _idle["last_active"],
                    "session_exists": _idle["session_exists"],
                })
            self._send_json({
                "now": now_local(),
                "agents": agents,
                "skills": skills,
                "jobs": jobs,
                "sessions": sessions,
                "sessionsTotal": count_session_files(),
                "models": collect_models(),
                "root": PROJECT_ROOT,
                "quickLaunches": collect_quick_launches(),
                "promptTemplates": collect_prompt_templates(),
                "counts": {
                    "agents": len(agents),
                    "skills": len(skills),
                    "jobs": len(jobs),
                    "running": sum(1 for j in jobs if j["state"] in ("working", "idle", "temporary")),
                    "sessions": len(sessions),
                },
            })
        elif path == "/api/jobs":
            self._send_json({"jobs": collect_jobs()})
        elif path == "/api/sessions":
            qs = url.query or ""
            limit_raw = ""
            for part in qs.split("&"):
                if part.startswith("limit="):
                    limit_raw = unquote(part[6:])
                    break
            # 参数白名单校验：非法值/负数一律回退默认 30；"all"/"0" 表示不限制
            if limit_raw in ("all", "0"):
                lim = 0
            else:
                try:
                    lim = int(limit_raw)
                    if lim < 0:
                        lim = 30
                    if lim > 500:   # 上限保护，避免恶意/误传超大值拖垮一次请求
                        lim = 500
                except Exception:
                    lim = 30
            self._send_json({"sessions": collect_sessions(lim if lim else 0),
                              "total": count_session_files()})
        elif path == "/api/session":
            qs = url.query or ""
            sid = ""
            for part in qs.split("&"):
                if part.startswith("id="):
                    sid = unquote(part[3:])
                    break
            self._send_json(read_session_detail(sid))
        elif path == "/api/tasklog":
            qs = url.query or ""
            name = ""
            for part in qs.split("&"):
                if part.startswith("name="):
                    name = unquote(part[5:])
                    break
            self._send_json(read_task_log(name))
        elif path == "/api/today":
            # ?force=1 时忽略 60s 缓存强制重算（前端手动刷新按钮使用）
            force = "force=1" in (url.query or "")
            self._send_json(collect_today(force=force))
        elif path == "/api/trend":
            # ?days=N&force=1 —— token/成本/轮数/活跃会话数趋势（60s 缓存）
            qs = url.query or ""
            days = 30
            force = "force=1" in qs
            for part in qs.split("&"):
                if part.startswith("days="):
                    try:
                        days = int(unquote(part[5:]))
                    except Exception:
                        days = 30
                    break
            self._send_json(collect_trend(days=days, force=force))
        elif path == "/api/chat/list":
            self._send_json({"chats": chat_list()})
        elif path == "/api/chat/search":
            # 会话全局全文搜索：?q=<关键词>（空格分词，AND）
            qs = url.query or ""
            q = ""
            for part in qs.split("&"):
                if part.startswith("q="):
                    q = unquote(part[2:])
                    break
            self._send_json(chat_search(q))
        elif path == "/api/chat/log":
            qs = url.query or ""
            cid = ""
            for part in qs.split("&"):
                if part.startswith("id="):
                    cid = unquote(part[3:])
                    break
            self._send_json(chat_log(cid))
        else:
            self.send_error(404)

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln) if ln else b""
        except Exception:
            body = b""
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}

        if path == "/api/launch":
            self._send_json(launch_agent(payload.get("agent", "")))
        elif path == "/api/open":
            self._send_json(open_in_explorer(payload.get("path", "")))
        elif path == "/api/newtask":
            self._send_json(launch_new_task(
                agent=payload.get("agent", ""),
                prompt=payload.get("prompt", ""),
                cwd=payload.get("cwd", ""),
                permission=payload.get("permission", "default"),
                type=payload.get("type", "claude"),
                skill=payload.get("skill", ""),
            ))
        elif path == "/api/chat/start":
            self._send_json(chat_start(
                agent=payload.get("agent", ""),
                prompt=payload.get("prompt", ""),
                cwd=payload.get("cwd", ""),
                permission=payload.get("permission", "default"),
                resume_id=payload.get("id", ""),
                skill=payload.get("skill", ""),
                model=payload.get("model", ""),
            ))
        elif path == "/api/chat/stop":
            self._send_json(chat_stop(payload.get("id", "")))
        elif path == "/api/chat/model":
            self._send_json(chat_set_model(payload.get("id", ""), payload.get("model", "auto")))
        elif path == "/api/chat/mode":
            self._send_json(chat_set_mode(payload.get("id", ""), payload.get("mode", "claude"), payload.get("target", "")))
        elif path == "/api/chat/perm":
            self._send_json(chat_set_permission(payload.get("id", ""), payload.get("permission", "default")))
        elif path == "/api/chat/rename":
            self._send_json(chat_rename(payload.get("id", ""), payload.get("name", "")))
        elif path == "/api/chat/pin":
            self._send_json(chat_pin(payload.get("id", ""), payload.get("pinned", 0)))
        elif path == "/api/jobs/clean":
            self._send_json(clean_jobs(payload.get("ids", [])))
        elif path == "/api/jobs/resume":
            self._send_json(resume_job(
                kind=payload.get("kind", "job"),
                ident=payload.get("id", ""),
                session_id=payload.get("sessionId", ""),
                cwd=payload.get("cwd", ""),
                cmd=payload.get("cmd", ""),
            ))
        elif path == "/api/jobs/delete":
            self._send_json(delete_entry(
                kind=payload.get("kind", "job"),
                ident=payload.get("id", ""),
                purge=bool(payload.get("purge", False)),
            ))
        elif path == "/api/quick_launch":
            item = payload.get("item") or payload
            self._send_json(save_quick_launch(
                item if isinstance(item, dict) else {},
                item_id=payload.get("id", "") or None,
            ))
        elif path == "/api/quick_launch/delete":
            self._send_json(delete_quick_launch(payload.get("id", "")))
        elif path == "/api/prompt_template":
            item = payload.get("item") or payload
            self._send_json(save_prompt_template(
                item if isinstance(item, dict) else {},
                item_id=payload.get("id", "") or None,
            ))
        elif path == "/api/prompt_template/delete":
            self._send_json(delete_prompt_template(payload.get("id", "")))
        else:
            self.send_error(404)


def main():
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT, help="监听端口")
    args = ap.parse_args()
    PORT = args.port

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Claude Desktop Dashboard 已启动: http://127.0.0.1:{PORT}")
    print(f"  Agents/Skills 数据源: {CLAUDE_DIR}、{PROJECT_CLAUDE_DIR}")
    print("  按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
