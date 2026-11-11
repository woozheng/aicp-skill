"""旁白型 reply 检测 —— 模型把「我要去做什么」当成了最终回复。

原样搬自旧 main_agent，行为一致。
"""
import re

_EN_ACT = (r"read|check|look|open|search|inspect|analyz|analys|explor|locate|"
           r"review|examin|start|proceed|continue|implement|fix|update|replace|"
           r"wire|add|remove|refactor|mark|write|run|test|verify|collapse|"
           r"finish|apply|create|delete|move|copy|edit|scan|trace|debug|"
           r"plan|dive|dig|digging|go|try|attempt|begin|"
           r"list|enumerate|show|display|dump|output|print|summarize|summarise|"
           r"compile|collect|gather|aggregate|distinguish|separate|classify|"
           r"sort|group|filter|extract|identify")
_ZH_ACT = (r"读|读一?下|看|查|查看|打开|检查|分析|探索|搜索|定位|检索|扫|列|列出|"
           r"改|修改|修复|写|写入|创建|新建|生成|实现|执行|运行|跑|测试|验证|确认|"
           r"梳理|整理|处理|解决|尝试|着手|进行|推进|实施|替换|接入|继续|收起|折叠|"
           r"计划|开始|动手|动笔|深入|研究")
_NARRATION_PATTERNS = (
    r"\b(?:i'?ll|i will|i'?m going to|now i'?ll)\s+(?:\w+\s+){0,2}(?:" + _EN_ACT + r")\w*\b",
    r"\blet\s+me\s+(?:\w+\s+){0,2}(?:" + _EN_ACT + r")\w*\b",
    r"^\s*i\s+(?:" + _EN_ACT + r")\w*\b",
    r"(?:^|[。！？；\s])(?:我(?:要|将|来)?|接下来|下面|现在|下一步)(?:我)?"
    r"(?:来|去|先|再|接着|继续|开始|准备)?\s*(?:" + _ZH_ACT + r")",
    r"^\s*(?:我要|我先|我继续|我接着|我接下来|接下来|下面我|现在我|下一步我|我准备)\s*(?:" + _ZH_ACT + r")",
    r"^\s*no\s+\w+\s+(?:requested|needed|required)\b",
)
_NARRATION_ALLOW_EN = (
    "done", "completed", "fixed", "implemented", "verified", "summary",
    "conclusion", "findings", "results", "issues", "changes made",
    "what i did", "i've ", "i have ", "here's", "here is",
)
_NARRATION_ALLOW_ZH = (
    "已完成", "完成了", "已改", "已实现", "已修复", "已新增", "已删除",
    "已验证", "已通过", "已测试", "已生成", "已写入", "已保存", "已创建",
    "改动清单", "总结", "结论", "结果如下", "如下：", "问题清单", "缺陷",
    "建议如下", "原因如下", "已修好",
)
_DELIVERY_SIGNALS = ("\n#", "\n##", "\n- ", "\n* ", "\n1.", "\n|", "```")


def is_narration_reply(content: str) -> bool:
    if not content:
        return False
    text = content.strip()
    if not text:
        return False
    if len(text) > 400:
        return False
    for sig in _DELIVERY_SIGNALS:
        if sig in text:
            return False
    low = text.lower()
    for w in _NARRATION_ALLOW_ZH:
        if w in text:
            return False
    for w in _NARRATION_ALLOW_EN:
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            return False
    for pat in _NARRATION_PATTERNS:
        try:
            if re.search(pat, text, re.IGNORECASE | re.MULTILINE):
                return True
        except re.error:
            continue
    return False
