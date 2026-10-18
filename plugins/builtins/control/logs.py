"""控制面板 API — 日志查看"""
from pathlib import Path


async def execute(envelop, agent):
    action = envelop.payload.get("action", "tail")

    if action == "tail":
        lines_count = envelop.payload.get("lines", 100)
        log_file = Path("data/gateway.log")

        if not log_file.exists():
            envelop.payload = {"lines": ["日志文件不存在"], "total": 0}
            return envelop

        try:
            with open(log_file, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
                recent = all_lines[-lines_count:]
                envelop.payload = {
                    "lines": [line.strip() for line in recent],
                    "total": len(all_lines),
                }
        except Exception as e:
            envelop.payload = {"lines": [f"读取日志失败: {e}"], "total": 0}

        return envelop

    envelop.payload = {"error": f"Unknown action: {action}"}
    return envelop