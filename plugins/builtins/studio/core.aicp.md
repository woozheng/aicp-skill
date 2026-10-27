ATOMIC_PLUGIN_GUIDE = """
## 原子插件协议 v1.1

### 0. 核心：一切围绕 Envelop
envelop 由引擎注入，插件只读写 envelop.payload。

### 1. 签名
async def execute(envelop, agent) -> Envelop:
    # envelop.payload = {"action": "xxx", "params": {...}}
    # 必须 return envelop

### 2. 统一格式
输入:  {"action": "xxx", "params": {...}}
输出:  {"ok": true, "data": {...}}
错误:  {"ok": false, "error": "失败原因"}

### 3. 能力声明
def help():
    return {
        "actions": {
            "动作名": {
                "params": {"参数": "类型和说明"},
                "returns": {"字段": "类型和说明"}
            }
        }
    }

### 4. Agent 可用能力
- agent.llm.chat / chat_json / chat_stream
- agent.system.call(envelop)  → 调其他插件
- agent.log / agent.base_url / agent.data_dir

### 5. 原子插件原则
- 只做一件事，不编排多个步骤
- 不调其他插件（除非是 OS 级基础能力）
- 需要编排 → 交给 workflow 或大插件

### 6. 缺什么
- 缺库 → # NEED: pip install xxx → 工厂自动装
- 优先用 Python 标准库，减少外部依赖
- 缺数据 → aiohttp / requests

### 7. ⚠️ 禁止行为（重要！）
- ❌ 不要定义 Envelop 类 → 引擎已注入
- ❌ 不要加 asyncio.wait_for / 手动超时 → 引擎统一管理
- ❌ 不要加 try-except 包整个函数 → 引擎统一捕获
- ❌ 不要检查 agent.llm 是否为 None 再加默认值
- ❌ 不要手动管理连接池/会话
- ❌ 不要加重试逻辑 → 工厂会重试整个插件
- ✅ 只在必要时对特定操作加 try-except（如文件读写、网络请求）
- ✅ 错误直接返回 {"ok": false, "error": "具体原因"}

### 8. 禁止过度引入包（重要！）
- ❌ 不要为了读文件引入 aiofiles → 标准库 open() 就够
- ❌ 不要为了发 HTTP 引入 httpx → aiohttp 或 requests 就够
- ❌ 不要为了简单计算引入 numpy/pandas → 标准库够用
- ✅ 优先标准库，非必要不引入外部依赖

### 9. 格式要求   用 ```text包裹
=== PLUGIN: plugins/xxx.py ===
代码
=== END ===
"""