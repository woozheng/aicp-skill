"""工具 schema —— reply / use_tool / manage_flow。"""


def reply_schema():
    return {
        "type": "function",
        "function": {
            "name": "reply",
            "description": (
                "向用户回复最终结果或澄清问题。任务完成、需要用户确认、或无法继续时使用。\n"
                "⚠️ 不要用 reply 说「我要读文件 / 我要查一下 / I'll now implement...」——"
                "这些是「下一步动作」，应该用 use_tool / manage_flow 直接执行。\n"
                "⚠️ 也不要用 reply 发「实施完毕 / 全部改动 / 断言全过 / 校验通过」这类收尾报告——"
                "除非最近确实有成功的 write_file / edit_file / code_runner 调用。"
                "没有写操作记录就说改完了，是谎报，会被拦下重试。\n"
                "reply 一旦发出，任务就结束了。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "给用户的回复内容 —— 必须是已经拿到的结果（结论、产物、清单、报错说明）。"
                            "不能是「我接下来要做什么」的宣告，也不能是「我已经做完了」的报告，"
                            "除非本回合确实有对应的写操作记录。"
                        ),
                    },
                },
                "required": ["content"],
            },
        },
    }


def use_tool_schema():
    return {
        "type": "function",
        "function": {
            "name": "use_tool",
            "description": (
                "调用系统内的一个插件：执行动作 / 读写文件 / 跑代码 / 造应用 / 推前端 / 查系统。"
                "一次只调一个，拿到结果后再决定下一步。\n"
                "⚠️ 文件内容（HTML/JS/CSS/代码）必须写在 plugin_args 里，不要写在 reasoning 里"
                "（写长内容会被 max_tokens 截断，导致 tool_call 丢失）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "think": {
                        "type": "string",
                        "description": (
                            "一句话说明【为什么现在调这个工具】——写意图，不写结论。\n"
                            "✅ 好：「要改 X，先定位 Y 的定义段」「上一步 0 命中，换关键词再搜」"
                            "「方案甲要改三个文件，先确认 _websocket.py 入站构造段」\n"
                            "❌ 坏：「我已读完三处文件」「确认 X 已加入」「方案就绪，开始实施」"
                            "「我已掌握全部信息」\n"
                            "完成时陈述（我已/已确认/已具备/已读完）不是事实，"
                            "下一轮会被当成工具返回，禁止写。\n"
                            "≤100 字，语种跟随用户。"
                        ),
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "插件路由，如 builtins/tools/file_utils_api、"
                            "builtins/tools/code_runner、builtins/tools/save_blocks。"
                            "skill/<rel> 用于读 data/skills 下的 skill 全文。"
                        ),
                    },
                    "action": {
                        "type": "string",
                        "description": (
                            "插件 action，必填。无 action 的插件传空字符串 \"\"，不要传 \"none\"。"
                            "不同 action 的必填 plugin_args 键不同；不确定时先用 "
                            "builtins/tools/contract_agent 查该插件的契约，再发调用。"
                        ),
                    },
                    "plugin_args": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": (
                            "插件的全部参数，直接平铺在这里，没有子层，不要再嵌套 params。\n"
                            "不同 action 必填键不同，例如：\n"
                            "file_utils_api.read_file 必填 {\"path\": \"...\"}；\n"
                            "file_utils_api.read_file_lines 必填 {\"path\": \"...\", "
                            "\"start_line\": 1, \"end_line\": 50}；\n"
                            "file_utils_api.edit_file 必填 {\"path\": \"...\", "
                            "\"find\": \"...\", \"replace\": \"...\"}；\n"
                            "file_utils_api.write_file 必填 {\"path\": \"...\", "
                            "\"content\": \"...\"}；\n"
                            "file_utils_api.search 必填 {\"path\": \"...\"}，"
                            "多关键词用 content_pattern + content_regex=true；\n"
                            "code_runner 必填 {\"code\": \"...\", \"packages\": []}，"
                            "可选 {\"timeout_s\": 240}（默认 60，最大 300）。\n"
                            "漏必填键会报「缺少 X 参数」——下一发必须补上 X 重发，禁止原样重试。"
                        ),
                    },
                    "retain": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": (
                            "信息流保留轮数。一次性操作 1，写文件 3，"
                            "读文件 5，任务收尾 3-5。"
                        ),
                    },
                },
                "required": ["target", "action", "plugin_args"],
            },
        },
    }


def manage_flow_schema():
    return {
        "type": "function",
        "function": {
            "name": "manage_flow",
            "description": (
                "【上下文超限时的唯一工具】去除信息流无关当前任务的噪音。\n"
                "编号 \"3\" 折整轮，\"3.2\" 折单条链，\"3.2.1\" 折单步。\n"
                "⚠️ 当前轮整轮不能折，只能折当前轮里不需要的单链。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ignore": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "要忽略的编号列表：\n"
                            "  \"3\"     → 折整轮（历史轮）\n"
                            "  \"3.2\"   → 折第 3 轮第 2 条链\n"
                            "  \"3.2.1\" → 折第 3 轮第 2 条链第 1 步\n"
                            "指定哪些折哪些，未指定的不动。"
                        ),
                    }
                },
                "required": ["ignore"],
            },
        },
    }


SCHEMAS = {
    "reply": reply_schema,
    "use_tool": use_tool_schema,
    "manage_flow": manage_flow_schema,
}
