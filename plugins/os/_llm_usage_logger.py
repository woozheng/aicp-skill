"""LLM Usage Logger — 持久化每次 LLM 调用的 token 用量到磁盘

通过 _llm.py 的 set_usage_callback 机制注册回调，
每次 LLM 调用后追加写入按日期分片的 JSONL 文件。

存储路径: data/llm_usage/YYYY-MM-DD.jsonl
每行一条 JSON 记录，追加写入，无锁设计（单进程安全）。
"""
import json
import time
import os
from pathlib import Path

USAGE_DIR = Path("data/llm_usage")


def _ensure_dir():
    USAGE_DIR.mkdir(parents=True, exist_ok=True)


def _append_record(record: dict):
    """追加一条记录到当天的 JSONL 文件，原子写不锁。"""
    _ensure_dir()
    date_str = time.strftime("%Y-%m-%d")
    filepath = USAGE_DIR / f"{date_str}.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(line)


def usage_callback(usage: dict, info: dict):
    """被 _llm.py 的 _report_usage 调用的回调函数。

    Args:
        usage: {prompt_tokens, completion_tokens, total_tokens} 或 {}
        info: {session_id, method, model, usage, extra}
    """
    try:
        now = time.time()
        model = info.get("model", "unknown")
        # 从 model 中提取 provider（格式 provider/model_id）
        provider = ""
        model_name = model
        if isinstance(model, str) and "/" in model:
            parts = model.split("/", 1)
            provider = parts[0]
            model_name = parts[1]

        prompt_tokens = usage.get("prompt_tokens", 0) if usage else 0
        completion_tokens = usage.get("completion_tokens", 0) if usage else 0
        total_tokens = usage.get("total_tokens", 0) if usage else 0

        extra = info.get("extra", {}) or {}
        error_msg = ""
        # 如果 total_tokens 为 0 且 usage 为空，标记为可能的错误调用
        if not usage or total_tokens == 0:
            error_msg = "No token usage reported (possible error or empty response)"

        record = {
            "ts": now,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "date": time.strftime("%Y-%m-%d", time.localtime(now)),
            "session_id": info.get("session_id", ""),
            "method": info.get("method", ""),
            "provider": provider,
            "model": model_name,
            "model_full": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "text_len": extra.get("text_len", 0),
            "tool": extra.get("tool", ""),
            "note": error_msg,
        }
        _append_record(record)
    except Exception:
        pass  # logger 不能因为自身错误影响主流程


async def execute(envelop, agent):
    """插件入口 — 注册 usage callback 到 LLM 实例。

    action:
      - register: 注册回调（系统启动时调用一次）
      - status: 返回当前状态
    """
    raw = envelop.payload if isinstance(envelop.payload, dict) else {}
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    action = str(payload.get("action") or "register")

    if action == "register":
        if hasattr(agent, "llm") and agent.llm:
            agent.llm.set_usage_callback(usage_callback)
            _ensure_dir()
            envelop.payload = {"ok": True, "data": {"registered": True, "storage": str(USAGE_DIR)}}
        else:
            envelop.payload = {"ok": False, "error": "LLM not available"}
        return envelop

    if action == "status":
        _ensure_dir()
        files = sorted(USAGE_DIR.glob("*.jsonl"))
        total_files = len(files)
        total_size = sum(f.stat().st_size for f in files)
        envelop.payload = {
            "ok": True,
            "data": {
                "storage": str(USAGE_DIR),
                "files": total_files,
                "total_size_kb": round(total_size / 1024, 1),
                "registered": hasattr(agent, "llm") and agent.llm and agent.llm._usage_callback is not None,
            },
        }
        return envelop

    envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
    return envelop
