---
title: task_manager 任务分身
description: 多个独立子任务并行执行时用 task_manager 创建分身并汇总结果
category: tools
tags: [task_manager, parallel, subagent]
---
===================================================
 多个独立子任务（可并行）→ 用 task_manager 开分身
    ⚠️ 判断标准：
    - 任务可以拆成 2 个以上相互独立的子任务
    - 每个子任务不需要其他子任务的结果
    - 每个子任务耗时较长（适合并行）
    典型场景：
    - 验证多个独立猜想（A、B、C 各开一个分身）
    - 批量处理多个文件（每个文件一个分身）
    - 并行调研多个主题

    【核心流程 —— 必须遵守】

    Step 1：连续 create 登记分身（每次 1 个，分多轮）
    {
    "think": "猜想 A、B、C 相互独立，先登记 A",
    "call": "use_tool",
    "retain": 3,
    "args": {
        "target": "builtins/tools/task_manager",
        "action": "create",
        "params": {
        "description": "验证猜想 A",
        "task": "验证哥德巴赫猜想在 4-10000 范围内是否成立，输出反例或确认"
        }
    }
    }

    Step 2：全部 create 完后，直接 reply 用户
    「已提交 N 个分身任务，完成后通知你。」
    ❌ 不要立刻 list 查状态
    ❌ 不要立刻 collect
    ❌ 不要轮询

    Step 3：等回调（系统自动触发）
    分身跑完后，系统会自动回调你。你会在【信息流】里看到
    📨 回调 或 📋 task_summary 的消息。

    Step 4：收到回调后，调 collect 拿结果
    {
    "think": "分身都完成了，收集结果",
    "call": "use_tool",
    "retain": 3,
    "args": {
        "target": "builtins/tools/task_manager",
        "action": "collect",
        "params": {}
    }
    }

    Step 5：把结果整理后 reply 用户

    【★ 让分身"继续干" —— 用 send，不要用 create】

    如果你对某个分身的结果不满意，或者它没干完，或者想让它深入——**不要 create 新分身**，用 send 让原分身继续。

    为什么？原分身有记忆（它的信息流里有上次干了什么），新分身没有，要从头开始。

    正确做法：
    {
    "think": "sub_a3f8b2c1 没干完，让它带着记忆继续",
    "call": "use_tool",
    "retain": 3,
    "args": {
        "target": "builtins/tools/task_manager",
        "action": "send",
        "params": {
        "task_id": "sub_a3f8b2c1",
        "message": "上次你调研了 A 方向，但 X 部分没查完。请继续查 X 部分，重点看 Y 角度。"
        }
    }
    }

    错误做法：
    ❌ 重新 create 一个新分身重做 —— 丢失上次成果，重复劳动

    适用 send 的场景：
    - 分身返回"没干完" → send 让它继续
    - 分身返回结果但"不够深" → send 让它深化
    - 分身返回"方向偏了" → send 让它修正
    - 需要"在已有基础上加新要求" → send 让它带着记忆加

    铁律：
    🔴 让分身"继续/改进/深入" → 用 send(task_id, message)
    🔴 只有"全新任务，和之前分身无关" → 才用 create

    【★ 清理分身 —— delete vs clear】

    delete：永久删除指定的 1 个或多个分身（任务记录 + flow 文件）
    {
    "think": "sub_a3f8b2c1 已经跑偏了，不需要了",
    "call": "use_tool",
    "retain": 3,
    "args": {
        "target": "builtins/tools/task_manager",
        "action": "delete",
        "params": {
        "task_ids": ["sub_a3f8b2c1"]
        }
    }
    }
    适用：某个分身"不要了"、"跑偏了"、"不需要了"
    注意：不能删"正在运行"的——要先 cancel

    clear：批量清理所有"已结束"的分身（任务记录 + flow 文件）
    {
    "think": "任务周期结束，清理所有已结束的分身",
    "call": "use_tool",
    "retain": 2,
    "args": {
        "target": "builtins/tools/task_manager",
        "action": "clear",
        "params": {}
    }
    }
    适用：任务周期结束，统一清理

    【选择原则】
    - 单个/几个不要了 → delete
    - 全部结束，统一清理 → clear

    【铁律 —— 违反会浪费大量 token】
    🔴 create 是异步的：任务在后台跑，跑完会主动回调你。
    🔴 create 之后禁止 list / status / collect 轮询！
    系统会自动回调，你只需等。
    🔴 如果用户催「进度如何」，才用 list 查一次；
    查完立即回复用户，不要连续 list。
    🔴 收到回调后，collect 一次拿全部结果。
    🔴 禁止连续两次调用 list / collect 之间没有用户输入。
    🔴 send 之后不要立刻轮询——它也是异步的，跑完会回调你。

    【其他 action 说明】
    - task_manager.create：创建新分身（自动分配 session）
    - task_manager.send：给已有分身发消息（保留记忆，用于"继续/改进"）
    - task_manager.list：仅当用户问「进度如何」时查一次
    - task_manager.status：仅当用户问「某个任务怎么样」时查一次
    - task_manager.collect：收到回调后调用一次
    - task_manager.delete：删除指定的 1 个或多个分身（含 flow）
    - task_manager.clear：批量清理已结束的分身（含 flow）
    - task_manager.cancel：取消某个正在运行的分身

    ⚠️ 不要用 use_tool 直接调 builtins/agents/main_agent。
    要开分身，统一走 task_manager。
    ⚠️ 不要自己拼 session_id / callback_receiver / _task_id，task_manager 全权处理。
