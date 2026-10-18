"""定时任务插件 v2.2 — 协议 v3.0 系统插件
接口对齐：os/_cron schedule/update/cancel/list
入参：interval(秒) / at_time("HH:MM")，同时提供at_time优先
特性：update无需重启即时生效；系统休眠时间跳变校正；重启自动恢复持久任务
"""
import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict
import core

DATA_DIR = Path("data/os_cron")
TASKS_FILE = DATA_DIR / "tasks.json"

_tasks: Dict[str, asyncio.Task] = {}
_persisted_tasks: Dict[str, dict] = {}
_stop_events: Dict[str, asyncio.Event] = {}


def load_persisted_tasks():
    global _persisted_tasks
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TASKS_FILE.exists():
        _persisted_tasks = {}
        return
    try:
        _persisted_tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError):
        _persisted_tasks = {}


def save_persisted_tasks():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.write_text(
        json.dumps(_persisted_tasks, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def _parse_at_time(at_time: str):
    """解析 "HH:MM" → (hour:int, minute:int)，失败抛ValueError"""
    hh, mm = at_time.strip().split(":")
    return int(hh), int(mm)


def _get_next_daily_ts(at_time: str) -> float:
    """at_time="HH:MM" 获取下一次本地时间戳，今日已过返回明天同一时刻"""
    hour, minute = _parse_at_time(at_time)
    now_dt = datetime.now()
    target_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_dt <= now_dt:
        target_dt = target_dt + timedelta(days=1)
    return target_dt.timestamp()


def _format_heartbeat_content(content: str) -> str:
    if not content:
        return content
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = f"【心跳唤醒 {timestamp}】"
    if content.startswith("【心跳唤醒"):
        end_idx = content.find("】")
        if end_idx != -1:
            return f"【心跳唤醒 {timestamp}】{content[end_idx+1:]}"
    return f"{prefix}{content}"


async def _start_cron_loop(task_id: str, config: dict, agent):
    interval = config.get("interval")
    at_time = config.get("at_time")
    target_receiver = config.get("target_receiver", "")
    target_payload = config.get("target_payload", {})
    immediate = config.get("immediate", False)

    # 调度模式判定：at_time存在优先daily，否则interval
    if at_time:
        schedule_mode = "daily"
    else:
        schedule_mode = "interval"

    stop_event = asyncio.Event()
    _stop_events[task_id] = stop_event

    async def cron_loop():
        next_run_time: float
        if schedule_mode == "daily":
            try:
                next_run_time = _get_next_daily_ts(at_time)
            except Exception as e:
                agent.log.error(f"Cron {task_id} parse at_time error: {e}")
                return
        else:
            next_run_time = time.time()

        if immediate:
            try:
                if "content" in target_payload:
                    target_payload["content"] = _format_heartbeat_content(target_payload["content"])
                await agent.system.call(core.Envelop(
                    sender="os/_cron",
                    receiver=target_receiver,
                    payload=target_payload,
                    meta={"callback_receiver": ""}
                ))
            except Exception as e:
                agent.log.error(f"Cron {task_id} immediate exec error: {e}")
            # immediate执行完计算下一轮
            if schedule_mode == "daily":
                try:
                    next_run_time = _get_next_daily_ts(at_time)
                except Exception:
                    return
            else:
                next_run_time += interval
        else:
            if schedule_mode == "interval":
                next_run_time += interval

        while task_id in _tasks and not stop_event.is_set():
            now = time.time()
            sleep_time = next_run_time - now

            # 休眠跳变阈值
            if schedule_mode == "interval":
                jump_threshold = interval * 2
            else:
                jump_threshold = 600

            if sleep_time < -jump_threshold:
                agent.log.warning(f"Cron {task_id}: 检测到系统休眠时间跳变，重新计算调度时刻")
                if schedule_mode == "daily":
                    try:
                        next_run_time = _get_next_daily_ts(at_time)
                    except Exception:
                        break
                else:
                    next_run_time = time.time() + interval
                sleep_time = next_run_time - time.time()

            if sleep_time > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=sleep_time)
                except asyncio.TimeoutError:
                    pass
                if stop_event.is_set():
                    break

            if task_id not in _tasks or stop_event.is_set():
                break

            # 执行任务
            try:
                payload_copy = dict(target_payload)
                if "content" in payload_copy:
                    payload_copy["content"] = _format_heartbeat_content(payload_copy["content"])
                await agent.system.call(core.Envelop(
                    sender="os/_cron",
                    receiver=target_receiver,
                    payload=payload_copy,
                    meta={"callback_receiver": ""}
                ))
            except Exception as e:
                agent.log.error(f"Cron {task_id}: {e}")

            # 下一次时间
            if schedule_mode == "daily":
                try:
                    next_run_time = _get_next_daily_ts(at_time)
                except Exception:
                    agent.log.error(f"Cron {task_id}: at_time解析失败，退出循环")
                    break
            else:
                next_run_time += interval

    _tasks[task_id] = asyncio.get_event_loop().create_task(cron_loop())


async def _restore_tasks(agent):
    load_persisted_tasks()
    for task_id, config in _persisted_tasks.items():
        if task_id not in _tasks:
            await _start_cron_loop(task_id, config, agent)
            agent.log.info(f"[os/_cron] 恢复任务: {task_id}")


async def execute(envelop, agent):
    action = envelop.payload.get("action", "")

    if not _persisted_tasks and TASKS_FILE.exists():
        await _restore_tasks(agent)

    if action == "schedule":
        task_id = envelop.payload.get("task_id", "")
        interval = envelop.payload.get("interval")
        at_time = envelop.payload.get("at_time")
        target_receiver = envelop.payload.get("target_receiver", "")
        target_payload = envelop.payload.get("target_payload", {})
        immediate = envelop.payload.get("immediate", False)
        persisted = envelop.payload.get("persisted", True)

        # ★★★ 参数校验 ★★★
        if not task_id:
            envelop.payload = {"ok": False, "error": "task_id is required"}
            return envelop

        if not target_receiver:
            envelop.payload = {"ok": False, "error": "target_receiver is required"}
            return envelop

        if interval is None and at_time is None:
            envelop.payload = {"ok": False, "error": "must provide interval (seconds) or at_time (HH:MM)"}
            return envelop

        # ★★★ 校验 target_payload 必须包含 content ★★★
        if not target_payload.get("content"):
            envelop.payload = {
                "ok": False,
                "error": "target_payload must contain 'content' field with execution instruction",
                "example": {
                    "session_id": "your_session_id",
                    "content": "【定时任务】执行具体操作描述"
                }
            }
            return envelop

        # ★★★ 校验 session_id 是否存在 ★★★
        if not target_payload.get("session_id"):
            envelop.payload = {
                "ok": False,
                "error": "target_payload must contain 'session_id' field",
                "example": {
                    "session_id": "your_session_id",
                    "content": "【定时任务】执行具体操作描述"
                }
            }
            return envelop

        # ★★★ 如果提供了 at_time，校验格式 ★★★
        if at_time is not None:
            try:
                _parse_at_time(at_time)
            except Exception:
                envelop.payload = {"ok": False, "error": f"invalid at_time format: {at_time}, expected HH:MM"}
                return envelop

        # ★★★ 如果提供了 interval，校验为正数 ★★★
        if interval is not None:
            try:
                interval = int(interval)
                if interval <= 0:
                    raise ValueError("interval must be positive")
            except Exception:
                envelop.payload = {"ok": False, "error": f"invalid interval: {interval}, must be positive integer (seconds)"}
                return envelop

        if task_id in _tasks:
            envelop.payload = {"ok": False, "error": f"task '{task_id}' already exists"}
            return envelop

        token = envelop.meta.get("session_id", "")
        if not token:
            token = envelop.meta.get("token", "")
        if "session_id" not in target_payload:
            target_payload["session_id"] = token

        if immediate and "content" in target_payload:
            target_payload["content"] = _format_heartbeat_content(target_payload["content"])

        task_config = {
            "interval": interval,
            "at_time": at_time,
            "target_receiver": target_receiver,
            "target_payload": target_payload,
            "token": token,
            "immediate": immediate,
        }

        if persisted:
            _persisted_tasks[task_id] = task_config
            save_persisted_tasks()

        await _start_cron_loop(task_id, task_config, agent)

        schedule_mode = "daily" if at_time else "interval"
        envelop.payload = {
            "ok": True,
            "task_id": task_id,
            "schedule_mode": schedule_mode,
            "interval": interval,
            "at_time": at_time,
            "immediate": immediate,
            "persisted": persisted,
            "target_receiver": target_receiver
        }
        return envelop

    elif action == "update":
        task_id = envelop.payload.get("task_id", "")
        interval = envelop.payload.get("interval")
        at_time = envelop.payload.get("at_time")
        target_receiver = envelop.payload.get("target_receiver")
        target_payload = envelop.payload.get("target_payload")
        immediate = envelop.payload.get("immediate")
        persisted = envelop.payload.get("persisted", True)

        # ★★★ 参数校验 ★★★
        if not task_id:
            envelop.payload = {"ok": False, "error": "task_id is required"}
            return envelop

        if task_id not in _persisted_tasks and task_id not in _tasks:
            envelop.payload = {"ok": False, "error": f"task '{task_id}' not found"}
            return envelop

        # ★★★ 如果提供了 at_time，校验格式 ★★★
        if at_time is not None:
            try:
                _parse_at_time(at_time)
            except Exception:
                envelop.payload = {"ok": False, "error": f"invalid at_time format: {at_time}, expected HH:MM"}
                return envelop

        # ★★★ 如果提供了 interval，校验为正数 ★★★
        if interval is not None:
            try:
                interval = int(interval)
                if interval <= 0:
                    raise ValueError("interval must be positive")
            except Exception:
                envelop.payload = {"ok": False, "error": f"invalid interval: {interval}, must be positive integer (seconds)"}
                return envelop

        # ★★★ 如果传了 target_payload，校验是否包含 content ★★★
        if target_payload is not None and not target_payload.get("content"):
            envelop.payload = {
                "ok": False,
                "error": "target_payload must contain 'content' field",
                "example": {
                    "session_id": "your_session_id",
                    "content": "【定时任务】执行具体操作描述"
                }
            }
            return envelop

        old_config = _persisted_tasks.get(task_id, {})
        new_config = dict(old_config)

        if interval is not None:
            new_config["interval"] = interval
        if at_time is not None:
            new_config["at_time"] = at_time
        if target_receiver is not None:
            new_config["target_receiver"] = target_receiver
        if target_payload is not None:
            merged_payload = dict(old_config.get("target_payload", {}))
            merged_payload.update(target_payload)
            new_config["target_payload"] = merged_payload
        if immediate is not None:
            new_config["immediate"] = immediate

        # 销毁旧协程，立刻生效
        if task_id in _stop_events:
            _stop_events[task_id].set()
        if task_id in _tasks:
            _tasks[task_id].cancel()
            del _tasks[task_id]
        if task_id in _stop_events:
            del _stop_events[task_id]

        if persisted:
            _persisted_tasks[task_id] = new_config
            save_persisted_tasks()
        else:
            if task_id in _persisted_tasks:
                del _persisted_tasks[task_id]
                save_persisted_tasks()

        await _start_cron_loop(task_id, new_config, agent)

        out_mode = "daily" if new_config.get("at_time") else "interval"
        envelop.payload = {
            "ok": True,
            "task_id": task_id,
            "updated": {
                "schedule_mode": out_mode,
                "interval": new_config.get("interval"),
                "at_time": new_config.get("at_time"),
                "target_receiver": new_config.get("target_receiver"),
                "immediate": new_config.get("immediate", False),
                "persisted": persisted,
            }
        }
        return envelop

    elif action == "cancel":
        task_id = envelop.payload.get("task_id", "")
        if task_id not in _tasks:
            envelop.payload = {"ok": False, "error": f"task '{task_id}' not found"}
            return envelop

        if task_id in _stop_events:
            _stop_events[task_id].set()
        _tasks[task_id].cancel()
        del _tasks[task_id]
        if task_id in _stop_events:
            del _stop_events[task_id]

        persisted_removed = False
        if task_id in _persisted_tasks:
            del _persisted_tasks[task_id]
            save_persisted_tasks()
            persisted_removed = True

        envelop.payload = {"ok": True, "task_id": task_id, "persisted_removed": persisted_removed}
        return envelop

    elif action == "list":
        tasks_info = {}
        for tid, task in _tasks.items():
            config = _persisted_tasks.get(tid, {})
            target_payload = config.get("target_payload", {})

            payload_preview = {}
            if "session_id" in target_payload:
                payload_preview["session_id"] = target_payload["session_id"]
            if "content" in target_payload:
                content = target_payload["content"]
                if len(content) > 100:
                    content = content[:100] + "..."
                payload_preview["content"] = content
            if "action" in target_payload:
                payload_preview["action"] = target_payload["action"]

            at_time_val = config.get("at_time")
            schedule_mode = "daily" if at_time_val else "interval"

            tasks_info[tid] = {
                "running": not task.done(),
                "schedule_mode": schedule_mode,
                "interval": config.get("interval"),
                "at_time": at_time_val,
                "target_receiver": config.get("target_receiver", "?"),
                "immediate": config.get("immediate", False),
                "persisted": tid in _persisted_tasks,
                "payload": payload_preview,
                "task_id": tid,
            }

        sorted_tasks = dict(sorted(tasks_info.items()))
        total = len(sorted_tasks)
        running_count = sum(1 for t in sorted_tasks.values() if t["running"])

        envelop.payload = {
            "ok": True,
            "data": {
                "tasks": sorted_tasks,
                "total": total,
                "running": running_count,
                "stopped": total - running_count,
            }
        }
        return envelop

    elif action == "restore":
        await _restore_tasks(agent)
        envelop.payload = {"ok": True, "restored": len(_tasks)}
        return envelop

    envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
    return envelop


def help():
    return {
        "route": "os/_cron",
        "description": "定时任务插件 v2.2 — 系统级定时调度",
        "actions": {
            "schedule": {
                "description": "创建定时任务",
                "required": ["task_id", "target_receiver", "target_payload.content", "target_payload.session_id"],
                "optional": ["interval", "at_time", "immediate", "persisted"],
                "note": "interval(秒) 或 at_time(HH:MM) 至少提供一个，同时提供时 at_time 优先"
            },
            "update": {
                "description": "更新已有任务（即时生效）",
                "required": ["task_id"],
                "optional": ["interval", "at_time", "target_receiver", "target_payload", "immediate", "persisted"]
            },
            "cancel": {
                "description": "取消并删除任务",
                "required": ["task_id"]
            },
            "list": {
                "description": "列出所有任务状态",
                "required": []
            },
            "restore": {
                "description": "手动恢复持久化任务",
                "required": []
            }
        },
        "input": {
            "task_id": "任务唯一标识",
            "interval": "间隔秒数",
            "at_time": "每日定时时间 HH:MM",
            "target_receiver": "目标插件路由",
            "target_payload": "发送给目标插件的 payload，必须包含 session_id 和 content",
            "immediate": "是否立即执行第一次",
            "persisted": "是否持久化（重启后恢复）"
        },
        "output": {
            "ok": "操作是否成功",
            "error": "错误信息",
            "data": "list 操作返回的任务列表"
        }
    }