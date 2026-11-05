"""LLM Usage Query — 查询 token 用量日志

读取 data/llm_usage/*.jsonl，支持按日期范围、provider、model 过滤，
分页、汇总统计、导出。

性能设计：
- 只读取日期范围内的文件（按文件名过滤）
- 逐行流式解析 JSONL，不一次性加载全部
- 分页在内存中切片，避免大结果集
"""
import json
import time
import os
from pathlib import Path
from datetime import datetime, timedelta

USAGE_DIR = Path("data/llm_usage")


def _list_date_files(date_from: str = "", date_to: str = ""):
    """列出日期范围内的 JSONL 文件，按日期倒序。"""
    if not USAGE_DIR.exists():
        return []
    files = sorted(USAGE_DIR.glob("*.jsonl"), reverse=True)
    result = []
    for f in files:
        name = f.stem  # YYYY-MM-DD
        if date_from and name < date_from:
            continue
        if date_to and name > date_to:
            continue
        result.append(f)
    return result


def _read_records(files, provider: str = "", model: str = ""):
    """从文件列表中读取并过滤记录，返回倒序列表。"""
    records = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if provider and rec.get("provider", "") != provider:
                        continue
                    if model and rec.get("model", "") != model:
                        continue
                    records.append(rec)
        except (OSError, IOError):
            continue
    # 按时间戳倒序
    records.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return records


def _compute_summary(records):
    """计算汇总统计。"""
    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    total_calls = len(records)
    error_calls = 0
    by_model = {}
    by_provider = {}

    total_cache_creation = 0
    total_cache_read = 0

    for r in records:
        p = r.get("prompt_tokens", 0)
        c = r.get("completion_tokens", 0)
        t = r.get("total_tokens", 0)
        total_prompt += p
        total_completion += c
        total_tokens += t
        total_cache_creation += r.get("cache_creation_tokens", 0)
        total_cache_read += r.get("cache_read_tokens", 0)
        if r.get("note"):
            error_calls += 1

        m = r.get("model", "unknown")
        if m not in by_model:
            by_model[m] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        by_model[m]["calls"] += 1
        by_model[m]["prompt_tokens"] += p
        by_model[m]["completion_tokens"] += c
        by_model[m]["total_tokens"] += t

        prov = r.get("provider", "unknown") or "unknown"
        if prov not in by_provider:
            by_provider[prov] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        by_provider[prov]["calls"] += 1
        by_provider[prov]["prompt_tokens"] += p
        by_provider[prov]["completion_tokens"] += c
        by_provider[prov]["total_tokens"] += t

    return {
        "total_calls": total_calls,
        "error_calls": error_calls,
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "total_tokens": total_tokens,
        "cache_creation_tokens": total_cache_creation,
        "cache_read_tokens": total_cache_read,
        "by_model": by_model,
        "by_provider": by_provider,
    }


async def execute(envelop, agent):
    raw = envelop.payload if isinstance(envelop.payload, dict) else {}
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    action = str(payload.get("action") or "query")

    if action == "query":
        date_from = str(payload.get("date_from") or "")
        date_to = str(payload.get("date_to") or "")
        provider = str(payload.get("provider") or "")
        model = str(payload.get("model") or "")
        page = max(1, int(payload.get("page") or 1))
        page_size = min(100, max(1, int(payload.get("page_size") or 30)))

        files = _list_date_files(date_from, date_to)
        records = _read_records(files, provider, model)
        summary = _compute_summary(records)

        total = len(records)
        total_pages = max(1, (total + page_size - 1) // page_size)
        start = (page - 1) * page_size
        end = start + page_size
        page_records = records[start:end]

        envelop.payload = {
            "ok": True,
            "data": {
                "records": page_records,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": total_pages,
                },
                "summary": summary,
            },
        }
        return envelop

    if action == "today_summary":
        today = time.strftime("%Y-%m-%d")
        files = _list_date_files(today, today)
        records = _read_records(files)
        summary = _compute_summary(records)
        envelop.payload = {"ok": True, "data": {"date": today, "summary": summary}}
        return envelop

    if action == "history_summary":
        files = _list_date_files()
        records = _read_records(files)
        summary = _compute_summary(records)
        # 日期范围
        dates = sorted(set(r.get("date", "") for r in records if r.get("date")))
        summary["date_range"] = {"from": dates[0] if dates else "", "to": dates[-1] if dates else ""}
        summary["days"] = len(dates)
        envelop.payload = {"ok": True, "data": summary}
        return envelop

    if action == "filters":
        # 返回可用的 provider 和 model 列表
        files = _list_date_files()
        providers = set()
        models = set()
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            if rec.get("provider"):
                                providers.add(rec["provider"])
                            if rec.get("model"):
                                models.add(rec["model"])
                        except json.JSONDecodeError:
                            continue
            except (OSError, IOError):
                continue
        envelop.payload = {
            "ok": True,
            "data": {
                "providers": sorted(providers),
                "models": sorted(models),
            },
        }
        return envelop

    if action == "export":
        date_from = str(payload.get("date_from") or "")
        date_to = str(payload.get("date_to") or "")
        provider = str(payload.get("provider") or "")
        model = str(payload.get("model") or "")

        files = _list_date_files(date_from, date_to)
        records = _read_records(files, provider, model)

        # 返回 CSV 格式字符串
        header = "time,session_id,method,provider,model,prompt_tokens,completion_tokens,total_tokens,cache_creation_tokens,cache_read_tokens,text_len,tool,note"
        lines = [header]
        for r in records:
            row = [
                r.get("time", ""),
                r.get("session_id", ""),
                r.get("method", ""),
                r.get("provider", ""),
                r.get("model", ""),
                str(r.get("prompt_tokens", 0)),
                str(r.get("completion_tokens", 0)),
                str(r.get("total_tokens", 0)),
                str(r.get("cache_creation_tokens", 0)),
                str(r.get("cache_read_tokens", 0)),
                str(r.get("text_len", 0)),
                r.get("tool", ""),
                r.get("note", "").replace(",", ";"),
            ]
            lines.append(",".join(row))

        envelop.payload = {"ok": True, "data": {"csv": "\n".join(lines), "total": len(records)}}
        return envelop

    envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
    return envelop
