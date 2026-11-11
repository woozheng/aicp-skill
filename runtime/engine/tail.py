"""动态上下文尾部 —— 按 components 列表拼装。

components 可选项：backpack / taskboard / sub_tasks / depth_hint / env /
                  datetime / base_url
"""
import json
from datetime import datetime
from pathlib import Path

from .backpack import load_failure_index
from .paths import session_taskboard_file, session_tasks_file


def _load_taskboard(agent_id, user_id, session_id):
    f = session_taskboard_file(user_id, agent_id, session_id)
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return ""


def _load_sub_tasks(agent_id, user_id, session_id):
    f = session_tasks_file(user_id, agent_id, session_id)
    if not f.exists():
        return ""
    try:
        tasks = json.loads(f.read_text(encoding="utf-8"))
        if not tasks:
            return ""
        lines = ["【分身任务】"]
        for tid, t in sorted(tasks.items(), key=lambda x: x[1].get("created_at", "")):
            status = t.get("status", "unknown")
            icon = {"running": "⏳", "done": "✅", "failed": "❌",
                    "timeout": "⏰", "cancelled": "🚫"}.get(status, "?")
            lines.append(f"{icon} {tid}: {t.get('description', '')} [{status}]")
        return "\n".join(lines)
    except Exception:
        return ""


def _build_depth_hint(depth, max_depth):
    if depth == 0:
        return "开始执行，综合分析，整理思路，逐步执行，一次只调用一次工具或者回复。"
    if depth <= 10:
        return ("执行中：预计执行深度，随时 `builtins/tools/add_task_board`"
                "（action=\"replace\"，plugin_args={\"content\": \"看板内容\"}）先添加看板、记录过程；"
                "确认完成后用 action=\"clear\" 清除。")
    if depth <= 20:
        return "检查：本次执行是否接近完成？是→reply，否→继续。"
    if depth <= 30:
        return ("注意：未完成内容必须建立任务看板！！！避免跨轮丢失内容，"
                "优先考虑是否该 reply 用户当前进展。")
    if depth <= max_depth - 3:
        return (f"⚠️ 警告：执行深度 {depth}/{max_depth}，即将达到上限。"
                f"**先整理当前进展，优先 reply 用户**；"
                f"确实必须继续时，最多再走 1~2 步就必须 reply。")
    if depth == max_depth - 2:
        return f"🚨 强警告：执行深度 {depth}/{max_depth}，这一步是最后一次能调工具的机会……"
    return f"🛑 最后机会：执行深度 {depth}/{max_depth}。本轮唯一允许的动作是 reply。"


def _build_env(flow):
    last_env = None
    for e in reversed(flow._flow if hasattr(flow, "_flow") else []):
        if e.get("from") == "system" and e.get("action") == "call_end":
            r = (e.get("detail") or {}).get("result") or {}
            if isinstance(r, dict) and isinstance(r.get("env"), dict):
                last_env = r["env"]
                break
    if not last_env:
        return ""
    bits = []
    if "gpu" in last_env:
        bits.append(f"gpu={last_env['gpu']}")
    if "disk_free_gb" in last_env:
        bits.append(f"disk_free={last_env['disk_free_gb']}GB")
    if "cpu_count" in last_env:
        bits.append(f"cpu={last_env['cpu_count']}")
    if "exec_timeout_s" in last_env:
        bits.append(f"exec_timeout={last_env['exec_timeout_s']}s")
    net = last_env.get("net") or {}
    if net:
        bits.append("net{" + " ".join(f"{k}={v}" for k, v in net.items()) + "}")
    if not bits:
        return ""
    return (f"【执行环境（code_runner 实测）】\n"
            f"{' | '.join(bits)}\n"
            f"决策时按此评估可行性：下载/安装超过 exec_timeout_s 的、"
            f"gpu=false 时不要装 CUDA 包、网络慢时不要下大文件。")


def _build_identity(ctx) -> str:
    """每轮注入的身份信息。不依赖 prompt，不依赖 md 配置。"""
    return (
        f"【你的身份】\n"
        f"agent_id：`{ctx.agent_id}`\n"
        f"user_id：`{ctx.user_id}`\n"
        f"session_id：`{ctx.session_id}`\n"
        f"调 engine 调自己时，payload 里不传 agent_id，meta 会自动兜底为 `{ctx.agent_id}`。\n"
        f"写用户级路径时用：`data/memories/users/{ctx.user_id}/...`\n"
        f"\n"
        f"【think 纪律】你上一轮 think 里的「我已读完/已确认/已具备/方案就绪」是想法，"
        f"不是事实。只有工具返回（call_end 的 result）里的内容才算事实。"
        f"think 只写「为什么现在调这个工具」，不写完成时陈述。"
    )


def build_tail(components):
    """返回 tail_builder(ctx) -> str。"""

    def build(ctx):
        parts = []
        # ★ 身份信息：默认加，不可关闭，不依赖 components
        parts.append(_build_identity(ctx))

        agent_id = ctx.agent_id
        user_id = ctx.user_id
        session_id = ctx.session_id

        if "backpack" in components:
            idx = load_failure_index(agent_id, user_id)
            if idx:
                parts.append(f"【经验背包 · 坑点，严禁反复踩坑】\n{idx}")

        if "taskboard" in components:
            tb = _load_taskboard(agent_id, user_id, session_id)
            if tb:
                parts.append(f"【任务看板】判断任务复杂度\n{tb}")

        if "sub_tasks" in components:
            st = _load_sub_tasks(agent_id, user_id, session_id)
            if st:
                parts.append(st)

        if "depth_hint" in components:
            parts.append(f"执行深度：{ctx.depth}/{ctx.max_depth} - "
                         f"{_build_depth_hint(ctx.depth, ctx.max_depth)}")

        if "env" in components:
            env_text = _build_env(ctx.flow)
            if env_text:
                parts.append(env_text)

        if "datetime" in components:
            now = datetime.now()
            week = "一二三四五六日"[now.weekday()]
            parts.append(f"【当前时间】{now:%Y-%m-%d} 周{week} {now:%H:%M:%S}（{now:%Z}）")

        if "base_url" in components:
            base = getattr(ctx.agent, "base_url", "") or ""
            if base:
                parts.append(f"【本机基址】{base}（给用户可点击链接时直接用这个）")

        return "\n\n".join(parts)

    return build
