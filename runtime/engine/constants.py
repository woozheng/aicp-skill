"""引擎常量 —— 集中一处，方便调参。

与旧 main_agent 完全一致，保证行为对齐。
"""

MAX_RECURSION_DEPTH = 40
CONTEXT_PRESSURE_THRESHOLD = 300_000
BASE_ROUNDS = 8

# 信息流单条内容最大长度（字节）。超过则截断并标注。
FLOW_ENTRY_MAX_BYTES = 64 * 1024
# 压缩后信息流目标上限（字节）。按体积保留最近条目。
FLOW_COMPACT_TARGET_BYTES = 1024 * 1024
FLOW_LOAD_TAIL_BYTES = 4 * 1024 * 1024
# 会话归档阈值（flow.jsonl 字节数）
MAX_FLOW_BYTES = 10 * 1024 * 1024

# 内存中同时保留的活跃 flow 数
SESSION_FLOWS_MAX = 6

# 存储根目录
MEMORY_ROOT = "data/memories"

# 副作用工具（中断时提示已完成哪些写操作）
SIDE_EFFECT_TARGETS = (
    "builtins/tools/save_blocks",
    "os/_cron",
    "builtins/agents/engine",
    "builtins/agents/main_agent",
)

# LLM 错误前缀（判定是否为 LLM 调用失败）
LLM_ERROR_PREFIXES = (
    "[LLM stream error:",
    "[LLM请求失败:",
    "[系统错误:",
    "[服务请求超时",
    "[模型返回空响应",
    "[LLM 达到最大重试次数]",
    "[LLM 未配置]",
    "[空响应]",
)
