import json
import asyncio
import time
import uuid
import zlib
import hashlib
import collections
from pathlib import Path

ACTIONS_SCHEMA = {
    "create_task": {
        "description": "创建DAG任务节点",
        "params": {
            "task_name": {"type": "string", "description": "任务名称"},
            "dependencies": {"type": "array", "description": "依赖的task_id列表"},
            "task_type": {"type": "string", "description": "任务类型"},
            "timeout": {"type": "number", "description": "超时秒数，默认300"},
            "max_retries": {"type": "number", "description": "最大重试次数，默认3"},
            "retry_on": {"type": "array", "description": "重试触发类型列表，如[timeout,error]"},
            "retry_delay": {"type": "number", "description": "重试延迟秒数，默认5"},
            "backoff_multiplier": {"type": "number", "description": "退避乘数，默认2.0"},
            "payload": {"type": "object", "description": "任务负载数据"},
            "priority": {"type": "number", "description": "优先级，数值越小优先级越高，默认0"},
            "condition": {"type": "object", "description": "条件表达式，dict或null，None表示无门控"}
        },
        "required": ["task_name"]
    },
    "update_task": {
        "description": "更新任务节点",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"},
            "task_name": {"type": "string", "description": "任务名称"},
            "dependencies": {"type": "array", "description": "依赖的task_id列表"},
            "task_type": {"type": "string", "description": "任务类型"},
            "timeout": {"type": "number", "description": "超时秒数"},
            "max_retries": {"type": "number", "description": "最大重试次数"},
            "retry_on": {"type": "array", "description": "重试触发类型列表"},
            "retry_delay": {"type": "number", "description": "重试延迟秒数"},
            "backoff_multiplier": {"type": "number", "description": "退避乘数"},
            "payload": {"type": "object", "description": "任务负载数据"},
            "priority": {"type": "number", "description": "优先级，数值越小优先级越高"},
            "condition": {"type": "object", "description": "条件表达式，dict或null"}
        },
        "required": ["task_id"]
    },
    "delete_task": {
        "description": "删除任务节点",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"}
        },
        "required": ["task_id"]
    },
    "get_task": {
        "description": "获取单个任务详情",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"}
        },
        "required": ["task_id"]
    },
    "list_tasks": {
        "description": "列出所有任务",
        "params": {
            "status": {"type": "string", "description": "按状态过滤：pending/running/completed/failed/cancelled/retrying/skipped"},
            "min_priority": {"type": "number", "description": "最小优先级（含），用于范围过滤"},
            "max_priority": {"type": "number", "description": "最大优先级（含），用于范围过滤"}
        },
        "required": []
    },
    "start_task": {
        "description": "启动任务（条件门控+状态变为running，启动超时计时器）",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"}
        },
        "required": ["task_id"]
    },
    "complete_task": {
        "description": "完成任务（记录执行历史，检查下游任务是否可启动）",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"},
            "result": {"type": "object", "description": "执行结果"}
        },
        "required": ["task_id"]
    },
    "fail_task": {
        "description": "标记任务失败（触发重试策略判断）",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"},
            "failure_type": {"type": "string", "description": "失败类型：timeout或error"},
            "error_message": {"type": "string", "description": "错误信息"}
        },
        "required": ["task_id", "failure_type"]
    },
    "cancel_task": {
        "description": "取消任务及其所有下游任务",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"}
        },
        "required": ["task_id"]
    },
    "check_timeouts": {
        "description": "检查所有超时节点",
        "params": {},
        "required": []
    },
    "retry_task": {
        "description": "手动触发任务重试",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"}
        },
        "required": ["task_id"]
    },
    "get_execution_history": {
        "description": "获取任务执行历史",
        "params": {
            "task_id": {"type": "string", "description": "任务ID，不传则返回全部"}
        },
        "required": []
    },
    "get_dag_status": {
        "description": "获取DAG整体状态摘要",
        "params": {},
        "required": []
    },
    "reset_task": {
        "description": "重置任务为pending状态",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"}
        },
        "required": ["task_id"]
    },
    "send_message": {
        "description": "发送消息给目标任务分身",
        "params": {
            "sender_task_id": {"type": "string", "description": "发送者task_id"},
            "recipient_task_id": {"type": "string", "description": "接收者task_id"},
            "message_type": {"type": "string", "description": "消息类型：data_request/data_share/status_update/signal"},
            "content": {"type": "string", "description": "消息内容字符串"},
            "payload": {"type": "object", "description": "附加数据对象"}
        },
        "required": ["sender_task_id", "recipient_task_id", "message_type", "content"]
    },
    "read_messages": {
        "description": "读取指定任务收到的消息",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"},
            "unread_only": {"type": "boolean", "description": "是否只返回未读消息，默认true"},
            "message_type": {"type": "string", "description": "按类型过滤消息"}
        },
        "required": ["task_id"]
    },
    "get_outbox": {
        "description": "查看指定任务发出的消息",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"}
        },
        "required": ["task_id"]
    },
    "mark_read": {
        "description": "标记消息为已读",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"},
            "message_id": {"type": "string", "description": "消息ID"}
        },
        "required": ["task_id", "message_id"]
    },
    "get_comm_stats": {
        "description": "获取通信统计",
        "params": {
            "task_id": {"type": "string", "description": "任务ID，不传则返回全局统计"}
        },
        "required": []
    },
    "report_heartbeat": {
        "description": "分身上报进度心跳",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"},
            "percentage": {"type": "number", "description": "进度百分比，0~1浮点数"},
            "current_step": {"type": "string", "description": "当前执行步骤描述"},
            "eta_seconds": {"type": "number", "description": "预估剩余秒数"},
            "extra": {"type": "object", "description": "附加数据"}
        },
        "required": ["task_id", "percentage", "current_step"]
    },
    "get_task_progress": {
        "description": "查询单个任务进度",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"}
        },
        "required": ["task_id"]
    },
    "get_dag_progress": {
        "description": "查询DAG整体进度",
        "params": {},
        "required": []
    },
    "list_heartbeats": {
        "description": "查看心跳历史记录",
        "params": {
            "task_id": {"type": "string", "description": "按任务ID过滤，不传返回所有"},
            "limit": {"type": "number", "description": "返回条数上限，默认50"}
        },
        "required": []
    },
    "save_checkpoint": {
        "description": "保存当前DAG状态检查点",
        "params": {
            "trigger_reason": {"type": "string", "description": "触发原因：manual/on_task_complete/on_interval，默认manual"}
        },
        "required": []
    },
    "set_checkpoint_config": {
        "description": "配置自动检查点策略",
        "params": {
            "auto_on_complete": {"type": "boolean", "description": "任务完成时自动保存，默认True"},
            "auto_interval_seconds": {"type": "number", "description": "定时自动保存间隔秒数，0表示禁用"},
            "max_checkpoints": {"type": "number", "description": "最大检查点保留数，默认5"}
        },
        "required": []
    },
    "get_checkpoint_config": {
        "description": "查看自动检查点配置",
        "params": {},
        "required": []
    },
    "list_checkpoints": {
        "description": "列出所有检查点",
        "params": {},
        "required": []
    },
    "restore_checkpoint": {
        "description": "从检查点恢复DAG状态",
        "params": {
            "checkpoint_id": {"type": "string", "description": "检查点ID，不传则恢复最新检查点"}
        },
        "required": []
    },
    "delete_checkpoint": {
        "description": "删除指定检查点",
        "params": {
            "checkpoint_id": {"type": "string", "description": "检查点ID"}
        },
        "required": ["checkpoint_id"]
    },
    "batch_create_tasks": {
        "description": "批量创建多个DAG任务节点，一次save_data",
        "params": {
            "tasks": {"type": "array", "description": "任务列表，每个元素包含create_task的所有参数（含condition）"}
        },
        "required": ["tasks"]
    },
    "get_metrics": {
        "description": "获取性能监控指标（计数器/计时器/延迟统计）",
        "params": {},
        "required": []
    },
    "reset_metrics": {
        "description": "重置所有性能监控计数器",
        "params": {},
        "required": []
    },
    "detect_zombie_tasks": {
        "description": "检测僵尸任务（running状态但长时间无心跳的节点）",
        "params": {
            "timeout_multiplier": {"type": "number", "description": "超时倍数，运行时间超过timeout*此倍数视为僵尸，默认2"}
        },
        "required": []
    },
    "recover_zombie_task": {
        "description": "恢复单个僵尸任务到pending状态，重置重试计数",
        "params": {
            "task_id": {"type": "string", "description": "要恢复的僵尸任务ID"}
        },
        "required": ["task_id"]
    },
    "batch_recover_zombies": {
        "description": "批量恢复所有僵尸任务",
        "params": {
            "timeout_multiplier": {"type": "number", "description": "超时倍数，默认2"}
        },
        "required": []
    },
    "get_error_summary": {
        "description": "获取DAG错误摘要（失败/超时/僵尸任务统计与最近错误）",
        "params": {},
        "required": []
    },
    "force_reset_stuck": {
        "description": "强制重置所有卡住的任务（running但无活跃watcher的节点）到pending",
        "params": {},
        "required": []
    },
    "set_task_priority": {
        "description": "修改任务优先级（仅允许pending状态的任务）",
        "params": {
            "task_id": {"type": "string", "description": "任务ID"},
            "priority": {"type": "number", "description": "新优先级，数值越小优先级越高"}
        },
        "required": ["task_id", "priority"]
    },
    "get_next_ready_tasks": {
        "description": "获取所有可立即启动的任务（pending且依赖已满足），按priority升序+created_at升序排列",
        "params": {},
        "required": []
    },
    "get_critical_path": {
        "description": "关键路径分析（CPM算法），返回最长路径、节点松弛时间和统计信息",
        "params": {
            "action_name": {"type": "string", "description": "权重字段名，从payload中读取耗时，默认 'duration'（即payload.duration/payload.estimated_seconds）"},
            "root_task_ids": {"type": "array", "description": "指定起点task_id列表，不传则从所有无上游依赖的根节点开始"},
            "include_completed": {"type": "boolean", "description": "是否把已完成的节点也纳入计算，默认true"}
        },
        "required": []
    },
    "batch_evaluate_conditions": {
        "description": "批量预评估任务条件，返回每个任务的evaluable/result/reason，不实际改变任务状态",
        "params": {
            "task_ids": {"type": "array", "description": "指定task_id列表，不传则评估所有pending任务"}
        },
        "required": []
    }
}

DATA_DIR = Path("data/dag_scheduler")
STATE_FILE = DATA_DIR / "dag_state.json"

MAX_CONCURRENT_TASKS = 10
MAX_MESSAGES = 500
MAX_HEARTBEATS = 200
CHECKPOINT_DIR = Path('data/dag_scheduler/checkpoints')
MAX_CHECKPOINTS = 5
_active_watchers: dict[str, asyncio.Task] = {}

_data_cache = None
_dirty = False
_dirty_timer: asyncio.TimerHandle = None
_reverse_deps: dict[str, list[str]] = {}
_messages_by_recipient: dict[str, list] = {}
_messages_by_sender: dict[str, list] = {}
_last_checkpoint_time = 0

_metrics = {
    'total_tasks_created': 0,
    'tasks_completed': 0,
    'tasks_failed': 0,
    'tasks_retried': 0,
    'tasks_cancelled': 0,
    'tasks_skipped': 0,
    'tasks_started': 0,
    'messages_sent': 0,
    'checkpoints_saved': 0,
    'start_time': time.time(),
    'task_execution_times': [],
    'message_delivery_times': [],
    'heartbeat_intervals': [],
    '_last_heartbeat_times': {},
}

_TASK_TYPE_WEIGHTS = {
    'default': 1,
    'io_bound': 5,
    'cpu_bound': 10,
}

_VALID_OPERATORS = {'==', '!=', '>', '>=', '<', '<=', 'in', 'not_in', 'contains', 'exists', 'not_exists', 'truthy', 'falsy'}


def _safe_weight(value):
    try:
        v = float(value)
        if v != v or v < 0:
            return 1
        return v
    except (TypeError, ValueError):
        return 1


def _extract_node_weight(task, action_name):
    payload = task.get('payload') or {}
    if not isinstance(payload, dict):
        payload = {}
    est = payload.get('estimated_seconds')
    if est is not None:
        w = _safe_weight(est)
        if w != 1 or 'estimated_seconds' in payload:
            return w
    if action_name and action_name in payload:
        w = _safe_weight(payload.get(action_name))
        if w != 1 or action_name in payload:
            return w
    task_type = task.get('task_type', 'default')
    if task_type in _TASK_TYPE_WEIGHTS:
        return _TASK_TYPE_WEIGHTS[task_type]
    return 1


def _get_field_by_path(data_obj, path):
    if data_obj is None or not path:
        return None
    if not isinstance(path, str):
        return None
    parts = path.split('.')
    cur = data_obj
    for part in parts:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)):
            try:
                idx = int(part)
                if idx < 0 or idx >= len(cur):
                    return None
                cur = cur[idx]
            except (ValueError, TypeError):
                return None
        else:
            return None
    return cur


def _compare(actual, operator, expected):
    if actual is None:
        return False
    try:
        if operator == '==':
            return actual == expected
        elif operator == '!=':
            return actual != expected
        elif operator == '>':
            return float(actual) > float(expected)
        elif operator == '>=':
            return float(actual) >= float(expected)
        elif operator == '<':
            return float(actual) < float(expected)
        elif operator == '<=':
            return float(actual) <= float(expected)
        elif operator == 'in':
            if not isinstance(expected, (list, tuple, set)):
                return False
            return actual in expected
        elif operator == 'not_in':
            if not isinstance(expected, (list, tuple, set)):
                return True
            return actual not in expected
        elif operator == 'contains':
            if isinstance(actual, (str, list, tuple, set)):
                return expected in actual
            return False
        elif operator == 'exists':
            return True
        elif operator == 'not_exists':
            return False
        elif operator == 'truthy':
            return bool(actual)
        elif operator == 'falsy':
            return not bool(actual)
        else:
            return False
    except (TypeError, ValueError):
        return False


def _evaluate_single_condition(data, source_data, condition):
    if not isinstance(condition, dict):
        return False
    if 'op' in condition:
        return None
    source_task_id = condition.get('source_task_id')
    field = condition.get('field')
    operator = condition.get('operator')
    expected = condition.get('value')
    if source_task_id is None or field is None or operator is None:
        return False
    if operator not in _VALID_OPERATORS:
        return False
    if source_task_id not in data.get('tasks', {}):
        return False
    actual = _get_field_by_path(source_data, field)
    return _compare(actual, operator, expected)


def _evaluate_compound_condition(data, source_data, condition):
    if not isinstance(condition, dict):
        return False
    op = condition.get('op')
    subs = condition.get('conditions', [])
    if op == 'NOT':
        if not subs:
            return False
        first = subs[0]
        if isinstance(first, dict) and 'op' in first:
            inner = _evaluate_compound_condition(data, source_data, first)
        else:
            inner = _evaluate_single_condition(data, source_data, first)
        return not inner
    if op not in ('AND', 'OR'):
        return False
    if not isinstance(subs, list) or not subs:
        return False
    results = []
    for sub in subs:
        if isinstance(sub, dict) and 'op' in sub:
            r = _evaluate_compound_condition(data, source_data, sub)
        else:
            r = _evaluate_single_condition(data, source_data, sub)
        results.append(bool(r))
    if op == 'AND':
        return all(results)
    return any(results)


def evaluate_task_condition(data, task, condition):
    try:
        if not condition or not isinstance(condition, dict):
            return True
        source_task_id = condition.get('source_task_id')
        source_data = None
        if 'op' not in condition:
            if not source_task_id:
                return True
            if source_task_id not in data.get('tasks', {}):
                return False
            source_task = data['tasks'][source_task_id]
            if source_task.get('result') is not None:
                source_data = source_task.get('result')
            elif source_task.get('payload') is not None:
                source_data = source_task.get('payload')
            else:
                source_data = {}
        if 'op' in condition:
            return _evaluate_compound_condition(data, source_data, condition)
        else:
            return _evaluate_single_condition(data, source_data, condition)
    except Exception:
        return False


def _describe_condition(cond):
    try:
        if not isinstance(cond, dict):
            return str(cond)
        if 'op' in cond:
            op = cond.get('op', '')
            subs = cond.get('conditions', [])
            sub_descs = [_describe_condition(s) for s in subs]
            if op == 'NOT':
                return f"NOT({sub_descs[0] if sub_descs else ''})"
            joiner = ' AND ' if op == 'AND' else ' OR '
            return '(' + joiner.join(sub_descs) + ')'
        sid = cond.get('source_task_id', '?')
        field = cond.get('field', '?')
        op = cond.get('operator', '?')
        val = cond.get('value', '?')
        return f"{sid}.{field} {op} {val}"
    except Exception:
        return str(cond)


def _compute_critical_path(data, action_name, root_task_ids, include_completed):
    tasks = data.get('tasks', {})
    nodes = {}
    for tid, t in tasks.items():
        status = t.get('status', 'pending')
        if status in ('cancelled', 'skipped'):
            continue
        if not include_completed and status == 'completed':
            continue
        deps = [d for d in (t.get('dependencies') or []) if d in tasks]
        deps = [d for d in deps if tasks[d].get('status') not in ('cancelled', 'skipped')]
        nodes[tid] = {
            'task': t,
            'deps': deps,
            'weight': _extract_node_weight(t, action_name),
        }
    if not nodes:
        return {
            'critical_path': [],
            'critical_path_length': 0,
            'slack_map': {},
            'stats': {
                'total_nodes': 0,
                'critical_nodes': 0,
                'avg_slack': 0,
                'max_slack': 0,
                'min_slack': 0,
            }
        }
    if root_task_ids:
        roots = [r for r in root_task_ids if r in nodes]
    else:
        roots = [tid for tid, info in nodes.items() if not info['deps']]
        if not roots:
            roots = list(nodes.keys())
    successors = {tid: [] for tid in nodes}
    for tid, info in nodes.items():
        for d in info['deps']:
            if d in successors:
                successors[d].append(tid)
    in_degree = {tid: len(nodes[tid]['deps']) for tid in nodes}
    queue = collections.deque(roots)
    topo_order = []
    in_deg_local = dict(in_degree)
    while queue:
        nid = queue.popleft()
        topo_order.append(nid)
        for s in successors.get(nid, []):
            in_deg_local[s] -= 1
            if in_deg_local[s] <= 0:
                queue.append(s)
    reachable = set(topo_order)
    if len(reachable) < len(nodes):
        for tid in nodes:
            if tid not in reachable:
                in_deg_local2 = dict(in_degree)
                qq = collections.deque([tid])
                while qq:
                    n2 = qq.popleft()
                    if n2 in reachable:
                        continue
                    reachable.add(n2)
                    topo_order.append(n2)
                    in_deg_local2[n2] = 0
                    for s in successors.get(n2, []):
                        in_deg_local2[s] -= 1
                        if in_deg_local2[s] <= 0:
                            qq.append(s)
        if len(reachable) < len(nodes):
            return {'_cycle': True}
    earliest = {tid: 0.0 for tid in nodes}
    predecessors = {tid: [] for tid in nodes}
    for tid, info in nodes.items():
        for d in info['deps']:
            predecessors[tid].append(d)
    for nid in topo_order:
        w = nodes[nid]['weight']
        max_pred = 0.0
        for p in predecessors.get(nid, []):
            if p in earliest:
                if earliest[p] > max_pred:
                    max_pred = earliest[p]
        earliest[nid] = max_pred + w
    project_finish = max((earliest[nid] for nid in nodes), default=0)
    latest = {tid: project_finish for tid in nodes}
    for nid in reversed(topo_order):
        w = nodes[nid]['weight']
        succs = successors.get(nid, [])
        if not succs:
            latest[nid] = project_finish - w
        else:
            min_succ = float('inf')
            for s in succs:
                if s in latest and latest[s] < min_succ:
                    min_succ = latest[s]
            if min_succ == float('inf'):
                latest[nid] = project_finish - w
            else:
                latest[nid] = min_succ - w
    slack_map = {}
    for nid in nodes:
        slack_map[nid] = round(latest[nid] - earliest[nid], 6)
    critical_nodes = [nid for nid in nodes if abs(slack_map[nid]) < 1e-9]
    best_path = []
    best_len = -1.0
    for r in roots:
        if r not in nodes:
            continue
        path = [r]
        cur = r
        cur_len = nodes[r]['weight']
        while True:
            succs = [s for s in successors.get(cur, []) if s in nodes]
            if not succs:
                break
            best_s = None
            best_earliest = -1.0
            for s in succs:
                if slack_map.get(s, 0) < 1e-9 and earliest.get(s, -1) > best_earliest:
                    best_earliest = earliest[s]
                    best_s = s
            if best_s is None:
                break
            path.append(best_s)
            cur_len += nodes[best_s]['weight']
            cur = best_s
        if cur_len > best_len:
            best_len = cur_len
            best_path = path
    total_length = sum(nodes[nid]['weight'] for nid in best_path)
    slacks = list(slack_map.values())
    if slacks:
        avg_slack = sum(slacks) / len(slacks)
        max_slack = max(slacks)
        min_slack = min(slacks)
    else:
        avg_slack = max_slack = min_slack = 0
    return {
        'critical_path': best_path,
        'critical_path_length': round(total_length, 6),
        'slack_map': {k: round(v, 6) for k, v in slack_map.items()},
        'stats': {
            'total_nodes': len(nodes),
            'critical_nodes': len(critical_nodes),
            'avg_slack': round(avg_slack, 6),
            'max_slack': round(max_slack, 6),
            'min_slack': round(min_slack, 6),
        }
    }


def _record_task_execution(start_time, finish_time):
    duration = finish_time - start_time
    _metrics['task_execution_times'].append(duration)
    if len(_metrics['task_execution_times']) > 500:
        _metrics['task_execution_times'] = _metrics['task_execution_times'][-500:]


def _compute_metrics_summary():
    exec_times = _metrics['task_execution_times']
    avg_exec = sum(exec_times) / len(exec_times) if exec_times else 0
    msg_times = _metrics['message_delivery_times']
    avg_msg_delay = sum(msg_times) / len(msg_times) if msg_times else 0
    hb_intervals = _metrics['heartbeat_intervals']
    avg_hb_interval = sum(hb_intervals) / len(hb_intervals) if hb_intervals else 0
    uptime = time.time() - _metrics['start_time']
    return {
        'counters': {
            'total_tasks_created': _metrics['total_tasks_created'],
            'tasks_started': _metrics['tasks_started'],
            'tasks_completed': _metrics['tasks_completed'],
            'tasks_failed': _metrics['tasks_failed'],
            'tasks_retried': _metrics['tasks_retried'],
            'tasks_cancelled': _metrics['tasks_cancelled'],
            'tasks_skipped': _metrics['tasks_skipped'],
            'messages_sent': _metrics['messages_sent'],
            'checkpoints_saved': _metrics['checkpoints_saved'],
        },
        'timers': {
            'avg_task_execution_seconds': round(avg_exec, 3),
            'min_task_execution_seconds': round(min(exec_times), 3) if exec_times else 0,
            'max_task_execution_seconds': round(max(exec_times), 3) if exec_times else 0,
            'task_execution_samples': len(exec_times),
        },
        'latency': {
            'avg_message_delay_seconds': round(avg_msg_delay, 3),
            'message_delay_samples': len(msg_times),
            'avg_heartbeat_interval_seconds': round(avg_hb_interval, 3),
            'heartbeat_interval_samples': len(hb_intervals),
        },
        'uptime_seconds': round(uptime, 1),
    }


def load_data():
    global _data_cache
    if _data_cache is not None:
        return _data_cache
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        data = {"tasks": {}, "history": collections.deque(maxlen=1000), "timers": {}, "messages": [], "heartbeats": {}}
    else:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, ValueError):
            data = {"tasks": {}, "history": collections.deque(maxlen=1000), "timers": {}, "messages": [], "heartbeats": {}}
    if not isinstance(data.get("history"), collections.deque):
        data["history"] = collections.deque(data.get("history", [])[-1000:], maxlen=1000)
    if isinstance(data.get("heartbeats"), list):
        hb_dict = {}
        for hb in data["heartbeats"]:
            tid = hb.get("task_id")
            if tid:
                hb_dict[tid] = hb
        data["heartbeats"] = hb_dict
    elif not isinstance(data.get("heartbeats"), dict):
        data["heartbeats"] = {}
    if "messages" not in data:
        data["messages"] = []
    for tid, t in data.get("tasks", {}).items():
        if "condition" not in t:
            t["condition"] = None
    _data_cache = data
    _rebuild_all_indexes(data)
    return data


def save_data(data):
    global _dirty, _dirty_timer
    _dirty = True


def flush_now():
    global _dirty, _dirty_timer
    if not _dirty:
        return
    if _data_cache is None:
        return
    if _dirty_timer is not None:
        _dirty_timer.cancel()
        _dirty_timer = None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    serializable = _make_serializable(_data_cache)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)
    _dirty = False


def _make_serializable(data):
    out = {}
    for k, v in data.items():
        if isinstance(v, collections.deque):
            out[k] = list(v)
        elif isinstance(v, dict):
            out[k] = dict(v)
        else:
            out[k] = v
    return out


def _lazy_flush():
    global _dirty_timer
    if _dirty:
        flush_now()
    _dirty_timer = None


def _rebuild_reverse_deps(data):
    global _reverse_deps
    _reverse_deps = {}
    for tid, t in data["tasks"].items():
        for dep in (t.get("dependencies") or []):
            if dep not in _reverse_deps:
                _reverse_deps[dep] = []
            _reverse_deps[dep].append(tid)


def _rebuild_message_indexes(data):
    global _messages_by_recipient, _messages_by_sender
    _messages_by_recipient = {}
    _messages_by_sender = {}
    for msg in data.get("messages", []):
        rid = msg.get("recipient_task_id", "")
        sid = msg.get("sender_task_id", "")
        if rid not in _messages_by_recipient:
            _messages_by_recipient[rid] = []
        _messages_by_recipient[rid].append(msg)
        if sid not in _messages_by_sender:
            _messages_by_sender[sid] = []
        _messages_by_sender[sid].append(msg)


def _rebuild_all_indexes(data):
    _rebuild_reverse_deps(data)
    _rebuild_message_indexes(data)


def _compute_crc32(data_dict):
    raw = json.dumps(data_dict, sort_keys=True).encode('utf-8')
    return format(zlib.crc32(raw) & 0xFFFFFFFF, '08x')

def _build_checkpoint_snapshot(data):
    tasks_snap = []
    for tid, t in data['tasks'].items():
        tasks_snap.append({
            'task_id': tid,
            'task_name': t.get('task_name', ''),
            'status': t.get('status', 'pending'),
            'result': t.get('result'),
            'retry_count': t.get('current_retry', 0),
            'dependencies': t.get('dependencies', []),
            'payload': t.get('payload', {}),
            'condition': t.get('condition'),
            'created_at': t.get('created_at'),
            'started_at': t.get('started_at'),
            'finished_at': t.get('finished_at'),
            'priority': t.get('priority', 0)
        })
    heartbeats_snap = dict(data.get('heartbeats', {}))
    messages_snap = []
    for msg in data.get('messages', []):
        messages_snap.append({
            'message_id': msg.get('message_id'),
            'sender_task_id': msg.get('sender_task_id'),
            'recipient_task_id': msg.get('recipient_task_id'),
            'message_type': msg.get('message_type'),
            'content': msg.get('content'),
            'payload': msg.get('payload'),
            'sent_at': msg.get('sent_at'),
            'read': msg.get('read', False)
        })
    return {
        'tasks': tasks_snap,
        'heartbeats': heartbeats_snap,
        'messages': messages_snap,
        'history': list(data.get('history', []))[-200:]
    }

def _save_checkpoint(data, trigger_reason='manual'):
    global _last_checkpoint_time
    now = time.time()
    if trigger_reason == 'on_task_complete' and _last_checkpoint_time > 0:
        if now - _last_checkpoint_time < 10:
            return None, None
    _last_checkpoint_time = now
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = _build_checkpoint_snapshot(data)
    tasks = snapshot['tasks']
    stats = {'total': len(tasks), 'completed': 0, 'running': 0, 'pending': 0, 'failed': 0, 'cancelled': 0, 'retrying': 0, 'skipped': 0}
    for t in tasks:
        s = t.get('status', 'pending')
        if s in stats:
            stats[s] += 1
    ts_str = time.strftime('%Y%m%d_%H%M%S', time.localtime(now))
    cp_id = f'cp_{ts_str}_{uuid.uuid4().hex[:6]}'
    payload = {
        'checkpoint_id': cp_id,
        'dag_snapshot': snapshot,
        'created_at': now,
        'trigger_reason': trigger_reason,
        'task_stats': stats
    }
    crc = _compute_crc32(payload)
    wrapper = {'version': '8.4', 'crc32': crc, 'payload': payload}
    cp_file = CHECKPOINT_DIR / f'checkpoint_{cp_id}.json'
    with open(cp_file, 'w', encoding='utf-8') as f:
        json.dump(wrapper, f, ensure_ascii=False, indent=2)
    _metrics['checkpoints_saved'] += 1
    _enforce_checkpoint_retention()
    return cp_id, stats

def _enforce_checkpoint_retention():
    if not CHECKPOINT_DIR.exists():
        return
    files = sorted(CHECKPOINT_DIR.glob('checkpoint_cp_*.json'))
    if len(files) > MAX_CHECKPOINTS:
        for f in files[:len(files) - MAX_CHECKPOINTS]:
            f.unlink(missing_ok=True)


def start_timer(data, node_id, timeout_seconds):
    data["timers"][node_id] = {"start": time.time(), "timeout": timeout_seconds}


def check_all_timeouts(data):
    now = time.time()
    timed_out = []
    for nid, timer in list(data["timers"].items()):
        if now - timer["start"] > timer["timeout"]:
            timed_out.append(nid)
    return timed_out


def stop_timer(data, node_id):
    data["timers"].pop(node_id, None)


def should_retry(task, failure_type):
    retry_on = task.get("retry_on", ["timeout", "error"])
    max_retries = task.get("max_retries", 3)
    current_retry = task.get("current_retry", 0)
    if failure_type not in retry_on:
        return False
    if current_retry >= max_retries:
        return False
    return True


def get_retry_delay(task):
    base = task.get("retry_delay", 5)
    multiplier = task.get("backoff_multiplier", 2.0)
    current = task.get("current_retry", 0)
    delay = base * (multiplier ** current)
    return min(delay, 300)


def get_downstream(data, task_id):
    return list(_reverse_deps.get(task_id, []))


def cancel_recursively(data, task_id, history):
    task = data["tasks"].get(task_id)
    if not task:
        return
    if task["status"] in ("pending", "running", "retrying"):
        task["status"] = "cancelled"
        task["updated_at"] = time.time()
        stop_timer(data, task_id)
        _metrics['tasks_cancelled'] += 1
        history.append({
            "task_id": task_id,
            "task_name": task.get("task_name", ""),
            "action": "cancelled",
            "timestamp": time.time()
        })
        for child_id in get_downstream(data, task_id):
            cancel_recursively(data, child_id, history)


def add_history(data, record):
    data["history"].append(record)


def make_task(task_name, **kwargs):
    now = time.time()
    return {
        "task_id": "task_" + uuid.uuid4().hex[:12],
        "task_name": task_name,
        "task_type": kwargs.get("task_type", "default"),
        "dependencies": kwargs.get("dependencies", []),
        "status": "pending",
        "timeout": kwargs.get("timeout", 300),
        "max_retries": kwargs.get("max_retries", 3),
        "retry_on": kwargs.get("retry_on", ["timeout", "error"]),
        "retry_delay": kwargs.get("retry_delay", 5),
        "backoff_multiplier": kwargs.get("backoff_multiplier", 2.0),
        "current_retry": 0,
        "payload": kwargs.get("payload", {}),
        "result": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "priority": kwargs.get("priority", 0),
        "condition": kwargs.get("condition", None)
    }


def task_to_dict(task):
    out = dict(task)
    if "condition" not in out:
        out["condition"] = None
    return out


def _cancel_watcher(task_id):
    watcher = _active_watchers.pop(task_id, None)
    if watcher and not watcher.done():
        watcher.cancel()


def _make_message(sender_task_id, recipient_task_id, message_type, content, payload=None):
    now = time.time()
    return {
        "message_id": "msg_" + uuid.uuid4().hex[:10],
        "sender_task_id": sender_task_id,
        "recipient_task_id": recipient_task_id,
        "message_type": message_type,
        "content": content,
        "payload": payload,
        "sent_at": now,
        "read": False,
        "read_at": None
    }


def _trim_messages(data):
    msgs = data.get("messages", [])
    if len(msgs) > MAX_MESSAGES:
        data["messages"] = msgs[-MAX_MESSAGES:]
        _rebuild_message_indexes(data)


def _upsert_heartbeat(data, hb):
    data["heartbeats"][hb["task_id"]] = hb


def _get_latest_heartbeat(data, task_id):
    return data.get("heartbeats", {}).get(task_id)


def detect_zombie_tasks(data, timeout_multiplier=2):
    now = time.time()
    zombies = []
    for tid, t in data["tasks"].items():
        if t["status"] != "running":
            continue
        started_at = t.get("started_at")
        if not started_at:
            zombies.append({"task_id": tid, "task_name": t.get("task_name", ""), "reason": "no_start_time", "running_seconds": 0})
            continue
        running_seconds = now - started_at
        timeout_val = t.get("timeout", 300)
        threshold = timeout_val * timeout_multiplier
        if running_seconds > threshold:
            hb = _get_latest_heartbeat(data, tid)
            last_hb_time = hb.get("reported_at") if hb else None
            no_heartbeat = last_hb_time is None or (now - last_hb_time > timeout_val * timeout_multiplier)
            zombies.append({
                "task_id": tid,
                "task_name": t.get("task_name", ""),
                "reason": "no_heartbeat" if no_heartbeat else "exceeded_time",
                "running_seconds": round(running_seconds, 1),
                "threshold_seconds": round(threshold, 1),
                "last_heartbeat_at": last_hb_time
            })
    return zombies


async def _timeout_guard(task_id, timeout_seconds):
    try:
        await asyncio.sleep(timeout_seconds)
        data = load_data()
        task = data["tasks"].get(task_id)
        if not task or task["status"] != "running":
            return
        stop_timer(data, task_id)
        task["error_message"] = "执行超时"
        task["updated_at"] = time.time()
        if should_retry(task, "timeout"):
            task["current_retry"] = task.get("current_retry", 0) + 1
            delay = get_retry_delay(task)
            task["status"] = "retrying"
            _metrics['tasks_retried'] += 1
            add_history(data, {"task_id": task_id, "task_name": task["task_name"], "action": "timeout_guard_retry", "retry_count": task["current_retry"], "timestamp": time.time()})
        else:
            task["status"] = "failed"
            task["finished_at"] = time.time()
            _metrics['tasks_failed'] += 1
            add_history(data, {"task_id": task_id, "task_name": task["task_name"], "action": "timeout_guard_failed", "timestamp": time.time()})
            downstream = get_downstream(data, task_id)
            for child_id in downstream:
                child = data["tasks"].get(child_id)
                if child and child["status"] in ("pending", "retrying"):
                    child["status"] = "cancelled"
                    child["updated_at"] = time.time()
                    stop_timer(data, child_id)
                    _metrics['tasks_cancelled'] += 1
                    add_history(data, {"task_id": child_id, "task_name": child["task_name"], "action": "cancelled", "reason": "upstream_failed", "timestamp": time.time()})
        save_data(data)
        flush_now()
    except asyncio.CancelledError:
        return


async def execute(envelop, agent):
    global _dirty_timer
    p = envelop.payload
    action = p.get("action", "default")
    data = load_data()

    if action == "create_task":
        task_name = p.get("task_name")
        if not task_name:
            envelop.payload = {"ok": False, "error": "缺少 task_name 参数"}
            return envelop
        deps = p.get("dependencies", [])
        if deps:
            for d in deps:
                if d not in data["tasks"]:
                    envelop.payload = {"ok": False, "error": "依赖任务不存在: " + d}
                    return envelop
        task = make_task(
            task_name,
            task_type=p.get("task_type", "default"),
            dependencies=deps,
            timeout=p.get("timeout", 300),
            max_retries=p.get("max_retries", 3),
            retry_on=p.get("retry_on", ["timeout", "error"]),
            retry_delay=p.get("retry_delay", 5),
            backoff_multiplier=p.get("backoff_multiplier", 2.0),
            payload=p.get("payload", {}),
            priority=p.get("priority", 0),
            condition=p.get("condition", None)
        )
        data["tasks"][task["task_id"]] = task
        for dep in deps:
            if dep not in _reverse_deps:
                _reverse_deps[dep] = []
            _reverse_deps[dep].append(task["task_id"])
        _metrics['total_tasks_created'] += 1
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": task_to_dict(task)}
        return envelop

    elif action == "update_task":
        task_id = p.get("task_id")
        if not task_id:
            envelop.payload = {"ok": False, "error": "缺少 task_id 参数"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        old_deps = set(task.get("dependencies") or [])
        for field in ["task_name", "task_type", "dependencies", "timeout", "max_retries",
                       "retry_on", "retry_delay", "backoff_multiplier", "payload", "priority", "condition"]:
            if field in p:
                task[field] = p[field]
        if "condition" not in task:
            task["condition"] = None
        task["updated_at"] = time.time()
        new_deps = set(task.get("dependencies") or [])
        removed_deps = old_deps - new_deps
        added_deps = new_deps - old_deps
        for dep in removed_deps:
            if dep in _reverse_deps:
                rlist = _reverse_deps[dep]
                if task_id in rlist:
                    rlist.remove(task_id)
        for dep in added_deps:
            if dep not in _reverse_deps:
                _reverse_deps[dep] = []
            _reverse_deps[dep].append(task_id)
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": task_to_dict(task)}
        return envelop

    elif action == "delete_task":
        task_id = p.get("task_id")
        if not task_id:
            envelop.payload = {"ok": False, "error": "缺少 task_id 参数"}
            return envelop
        if task_id not in data["tasks"]:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        dependent = [tid for tid, t in data["tasks"].items() if task_id in (t.get("dependencies") or [])]
        if dependent:
            envelop.payload = {"ok": False, "error": "任务被依赖，无法删除: " + ", ".join(dependent)}
            return envelop
        _cancel_watcher(task_id)
        stop_timer(data, task_id)
        del data["tasks"][task_id]
        _reverse_deps.pop(task_id, None)
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": {"deleted": task_id}}
        return envelop

    elif action == "get_task":
        task_id = p.get("task_id")
        if not task_id:
            envelop.payload = {"ok": False, "error": "缺少 task_id 参数"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        if "priority" not in task:
            task["priority"] = 0
        envelop.payload = {"ok": True, "data": task_to_dict(task)}
        return envelop

    elif action == "list_tasks":
        filter_status = p.get("status")
        min_priority = p.get("min_priority")
        max_priority = p.get("max_priority")
        all_tasks = []
        for t in data["tasks"].values():
            if "priority" not in t:
                t["priority"] = 0
            if filter_status and t.get("status") != filter_status:
                continue
            if min_priority is not None and t.get("priority", 0) < min_priority:
                continue
            if max_priority is not None and t.get("priority", 0) > max_priority:
                continue
            all_tasks.append(task_to_dict(t))
        envelop.payload = {"ok": True, "data": {"tasks": all_tasks, "count": len(all_tasks)}}
        return envelop

    elif action == "start_task":
        task_id = p.get("task_id")
        if not task_id:
            envelop.payload = {"ok": False, "error": "缺少 task_id 参数"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        if task["status"] not in ("pending", "retrying"):
            envelop.payload = {"ok": False, "error": "任务状态不允许启动: " + task["status"]}
            return envelop
        running_count = sum(1 for t in data["tasks"].values() if t["status"] == "running")
        if running_count >= MAX_CONCURRENT_TASKS:
            envelop.payload = {"ok": False, "error": "并发任务数已达上限: " + str(MAX_CONCURRENT_TASKS)}
            return envelop
        for dep_id in task.get("dependencies", []):
            dep = data["tasks"].get(dep_id)
            if dep and dep["status"] != "completed":
                envelop.payload = {"ok": False, "error": "依赖任务未完成: " + dep_id}
                return envelop
        cond = task.get("condition")
        if cond:
            result_ok = evaluate_task_condition(data, task, cond)
            if not result_ok:
                desc = _describe_condition(cond)
                task["status"] = "skipped"
                task["error_message"] = "条件不满足: " + desc
                task["finished_at"] = time.time()
                task["updated_at"] = time.time()
                _metrics['tasks_skipped'] += 1
                add_history(data, {
                    "task_id": task_id,
                    "task_name": task["task_name"],
                    "action": "skipped_by_condition",
                    "condition": cond,
                    "reason": desc,
                    "timestamp": time.time()
                })
                save_data(data)
                if _dirty_timer is None:
                    loop = asyncio.get_event_loop()
                    _dirty_timer = loop.call_later(5, _lazy_flush)
                result_dict = task_to_dict(task)
                result_dict["status"] = "skipped"
                result_dict["skip_reason"] = desc
                envelop.payload = {"ok": True, "data": result_dict}
                return envelop
        task["status"] = "running"
        task["started_at"] = time.time()
        task["updated_at"] = time.time()
        start_timer(data, task_id, task.get("timeout", 300))
        add_history(data, {"task_id": task_id, "task_name": task["task_name"], "action": "started", "timestamp": time.time()})
        _metrics['tasks_started'] += 1
        save_data(data)
        _cancel_watcher(task_id)
        _active_watchers[task_id] = asyncio.create_task(_timeout_guard(task_id, task.get("timeout", 300)))
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": task_to_dict(task)}
        return envelop

    elif action == "complete_task":
        task_id = p.get("task_id")
        if not task_id:
            envelop.payload = {"ok": False, "error": "缺少 task_id 参数"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        if task["status"] != "running":
            envelop.payload = {"ok": False, "error": "任务状态不允许完成: " + task["status"]}
            return envelop
        _cancel_watcher(task_id)
        task["status"] = "completed"
        task["result"] = p.get("result")
        task["finished_at"] = time.time()
        task["updated_at"] = time.time()
        stop_timer(data, task_id)
        add_history(data, {"task_id": task_id, "task_name": task["task_name"], "action": "completed", "result": p.get("result"), "timestamp": time.time()})
        _metrics['tasks_completed'] += 1
        if task.get('started_at'):
            _record_task_execution(task['started_at'], task['finished_at'])
        downstream = get_downstream(data, task_id)
        auto_cp = True
        config_file = DATA_DIR / 'checkpoint_config.json'
        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as cf:
                    cp_cfg = json.load(cf)
                auto_cp = cp_cfg.get('auto_on_complete', True)
            except Exception:
                pass
        if auto_cp:
            cp_result = _save_checkpoint(data, trigger_reason='on_task_complete')
            if cp_result[0] is None:
                auto_cp = False
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": task_to_dict(task), "downstream_ready": downstream}
        return envelop

    elif action == "fail_task":
        task_id = p.get("task_id")
        failure_type = p.get("failure_type")
        if not task_id:
            envelop.payload = {"ok": False, "error": "缺少 task_id 参数"}
            return envelop
        if not failure_type:
            envelop.payload = {"ok": False, "error": "缺少 failure_type 参数"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        _cancel_watcher(task_id)
        stop_timer(data, task_id)
        task["error_message"] = p.get("error_message", "")
        task["updated_at"] = time.time()
        if should_retry(task, failure_type):
            task["current_retry"] = task.get("current_retry", 0) + 1
            delay = get_retry_delay(task)
            task["status"] = "retrying"
            _metrics['tasks_retried'] += 1
            add_history(data, {"task_id": task_id, "task_name": task["task_name"], "action": "retry_scheduled", "retry_count": task["current_retry"], "delay": delay, "timestamp": time.time()})
            save_data(data)
            if _dirty_timer is None:
                loop = asyncio.get_event_loop()
                _dirty_timer = loop.call_later(5, _lazy_flush)
            envelop.payload = {"ok": True, "data": {"task": task_to_dict(task), "will_retry": True, "retry_count": task["current_retry"], "retry_delay": delay}}
            return envelop
        else:
            task["status"] = "failed"
            task["finished_at"] = time.time()
            _metrics['tasks_failed'] += 1
            add_history(data, {"task_id": task_id, "task_name": task["task_name"], "action": "failed", "failure_type": failure_type, "error_message": task["error_message"], "timestamp": time.time()})
            downstream = get_downstream(data, task_id)
            for child_id in downstream:
                child = data["tasks"].get(child_id)
                if child and child["status"] in ("pending", "retrying"):
                    child["status"] = "cancelled"
                    child["updated_at"] = time.time()
                    stop_timer(data, child_id)
                    _cancel_watcher(child_id)
                    add_history(data, {"task_id": child_id, "task_name": child["task_name"], "action": "cancelled", "reason": "upstream_failed", "timestamp": time.time()})
            save_data(data)
            if _dirty_timer is None:
                loop = asyncio.get_event_loop()
                _dirty_timer = loop.call_later(5, _lazy_flush)
            envelop.payload = {"ok": True, "data": {"task": task_to_dict(task), "will_retry": False, "cancelled_downstream": downstream}}
            return envelop

    elif action == "cancel_task":
        task_id = p.get("task_id")
        if not task_id:
            envelop.payload = {"ok": False, "error": "缺少 task_id 参数"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        if task["status"] in ("completed", "failed", "cancelled", "skipped"):
            envelop.payload = {"ok": False, "error": "任务已终结，无法取消: " + task["status"]}
            return envelop
        cancelled = []
        cancel_recursively(data, task_id, cancelled)
        _cancel_watcher(task_id)
        for rec in cancelled:
            _cancel_watcher(rec.get("task_id"))
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": {"cancelled_tasks": cancelled}}
        return envelop

    elif action == "check_timeouts":
        timed_out = check_all_timeouts(data)
        results = []
        for nid in timed_out:
            task = data["tasks"].get(nid)
            if task and task["status"] == "running":
                _cancel_watcher(nid)
                stop_timer(data, nid)
                if should_retry(task, "timeout"):
                    task["current_retry"] = task.get("current_retry", 0) + 1
                    delay = get_retry_delay(task)
                    task["status"] = "retrying"
                    task["updated_at"] = time.time()
                    task["error_message"] = "执行超时"
                    _metrics['tasks_retried'] += 1
                    results.append({"task_id": nid, "action": "retry", "retry_count": task["current_retry"], "retry_delay": delay})
                    add_history(data, {"task_id": nid, "task_name": task["task_name"], "action": "timeout_retry", "retry_count": task["current_retry"], "timestamp": time.time()})
                else:
                    task["status"] = "failed"
                    task["finished_at"] = time.time()
                    task["updated_at"] = time.time()
                    task["error_message"] = "执行超时且无法重试"
                    _metrics['tasks_failed'] += 1
                    results.append({"task_id": nid, "action": "failed"})
                    add_history(data, {"task_id": nid, "task_name": task["task_name"], "action": "timeout_failed", "timestamp": time.time()})
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": {"timed_out_nodes": results, "checked_count": len(data["timers"])}}
        return envelop

    elif action == "retry_task":
        task_id = p.get("task_id")
        if not task_id:
            envelop.payload = {"ok": False, "error": "缺少 task_id 参数"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        if task["status"] not in ("failed", "retrying", "skipped"):
            envelop.payload = {"ok": False, "error": "任务状态不允许重试: " + task["status"]}
            return envelop
        _cancel_watcher(task_id)
        task["status"] = "pending"
        task["current_retry"] = 0
        task["error_message"] = None
        task["result"] = None
        task["started_at"] = None
        task["finished_at"] = None
        task["updated_at"] = time.time()
        add_history(data, {"task_id": task_id, "task_name": task["task_name"], "action": "manual_retry", "timestamp": time.time()})
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": task_to_dict(task)}
        return envelop

    elif action == "get_execution_history":
        task_id = p.get("task_id")
        if task_id:
            history = [h for h in data["history"] if h.get("task_id") == task_id]
        else:
            history = list(data["history"])
        envelop.payload = {"ok": True, "data": {"history": history, "count": len(history)}}
        return envelop

    elif action == "get_dag_status":
        tasks = data["tasks"]
        summary = {"total": len(tasks), "pending": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0, "retrying": 0, "skipped": 0}
        for t in tasks.values():
            s = t.get("status", "pending")
            if s in summary:
                summary[s] += 1
        summary["timers_active"] = len(data["timers"])
        priority_dist = {"high": 0, "normal": 0, "low": 0}
        for t in tasks.values():
            prio = t.get("priority", 0)
            if prio < 0:
                priority_dist["high"] += 1
            elif prio == 0:
                priority_dist["normal"] += 1
            else:
                priority_dist["low"] += 1
        summary["priority_distribution"] = priority_dist
        envelop.payload = {"ok": True, "data": summary}
        return envelop

    elif action == "reset_task":
        task_id = p.get("task_id")
        if not task_id:
            envelop.payload = {"ok": False, "error": "缺少 task_id 参数"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        _cancel_watcher(task_id)
        task["status"] = "pending"
        task["current_retry"] = 0
        task["error_message"] = None
        task["result"] = None
        task["started_at"] = None
        task["finished_at"] = None
        task["updated_at"] = time.time()
        stop_timer(data, task_id)
        add_history(data, {"task_id": task_id, "task_name": task["task_name"], "action": "reset", "timestamp": time.time()})
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": task_to_dict(task)}
        return envelop

    elif action == "send_message":
        sender_id = p.get("sender_task_id")
        recipient_id = p.get("recipient_task_id")
        message_type = p.get("message_type")
        content = p.get("content")
        if not sender_id or not recipient_id or not message_type or content is None:
            envelop.payload = {"ok": False, "error": "缺少必填参数: sender_task_id, recipient_task_id, message_type, content"}
            return envelop
        valid_types = ("data_request", "data_share", "status_update", "signal")
        if message_type not in valid_types:
            envelop.payload = {"ok": False, "error": "message_type 无效，必须是: " + ", ".join(valid_types)}
            return envelop
        sender = data["tasks"].get(sender_id)
        if not sender:
            envelop.payload = {"ok": False, "error": "发送者任务不存在: " + sender_id}
            return envelop
        if sender["status"] != "running":
            envelop.payload = {"ok": False, "error": "发送者必须是running状态，当前状态: " + sender["status"]}
            return envelop
        recipient = data["tasks"].get(recipient_id)
        if not recipient:
            envelop.payload = {"ok": False, "error": "接收者任务不存在: " + recipient_id}
            return envelop
        msg = _make_message(sender_id, recipient_id, message_type, content, p.get("payload"))
        data["messages"].append(msg)
        if recipient_id not in _messages_by_recipient:
            _messages_by_recipient[recipient_id] = []
        _messages_by_recipient[recipient_id].append(msg)
        if sender_id not in _messages_by_sender:
            _messages_by_sender[sender_id] = []
        _messages_by_sender[sender_id].append(msg)
        _trim_messages(data)
        _metrics['messages_sent'] += 1
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": {"message_id": msg["message_id"], "sent_at": msg["sent_at"]}}
        return envelop

    elif action == "read_messages":
        task_id = p.get("task_id")
        if not task_id:
            envelop.payload = {"ok": False, "error": "缺少 task_id 参数"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        unread_only = p.get("unread_only", True)
        filter_type = p.get("message_type")
        now = time.time()
        msgs = _messages_by_recipient.get(task_id, [])
        results = []
        for msg in msgs:
            if unread_only and msg["read"]:
                continue
            if filter_type and msg["message_type"] != filter_type:
                continue
            if not msg["read"]:
                delivery_delay = now - msg.get("sent_at", now)
                _metrics['message_delivery_times'].append(delivery_delay)
                if len(_metrics['message_delivery_times']) > 500:
                    _metrics['message_delivery_times'] = _metrics['message_delivery_times'][-500:]
            msg["read"] = True
            if not msg["read_at"]:
                msg["read_at"] = now
            results.append(msg)
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": {"messages": results, "count": len(results)}}
        return envelop

    elif action == "get_outbox":
        task_id = p.get("task_id")
        if not task_id:
            envelop.payload = {"ok": False, "error": "缺少 task_id 参数"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        results = _messages_by_sender.get(task_id, [])
        envelop.payload = {"ok": True, "data": {"messages": results, "count": len(results)}}
        return envelop

    elif action == "mark_read":
        task_id = p.get("task_id")
        message_id = p.get("message_id")
        if not task_id or not message_id:
            envelop.payload = {"ok": False, "error": "缺少必填参数: task_id, message_id"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        found = None
        for msg in _messages_by_recipient.get(task_id, []):
            if msg["message_id"] == message_id:
                msg["read"] = True
                msg["read_at"] = time.time()
                found = msg
                break
        if not found:
            envelop.payload = {"ok": False, "error": "消息不存在或不属于该任务: " + message_id}
            return envelop
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": found}
        return envelop

    elif action == "get_comm_stats":
        task_id = p.get("task_id")
        if task_id:
            send_msgs = _messages_by_sender.get(task_id, [])
            recv_msgs = _messages_by_recipient.get(task_id, [])
            messages = send_msgs + recv_msgs
        else:
            messages = data["messages"]
        total = len(messages)
        unread = sum(1 for m in messages if not m["read"])
        by_type = {}
        for m in messages:
            mt = m["message_type"]
            by_type[mt] = by_type.get(mt, 0) + 1
        envelop.payload = {"ok": True, "data": {"total_messages": total, "unread_messages": unread, "messages_by_type": by_type}}
        return envelop

    elif action == "report_heartbeat":
        task_id = p.get("task_id")
        percentage = p.get("percentage")
        current_step = p.get("current_step")
        if not task_id or percentage is None or current_step is None:
            envelop.payload = {"ok": False, "error": "缺少必填参数: task_id, percentage, current_step"}
            return envelop
        if not isinstance(percentage, (int, float)) or percentage < 0 or percentage > 1:
            envelop.payload = {"ok": False, "error": "percentage 必须是 0~1 之间的浮点数"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        if task["status"] != "running":
            envelop.payload = {"ok": False, "error": "任务状态必须为 running，当前状态: " + task["status"]}
            return envelop
        now = time.time()
        last_hb_time = _metrics['_last_heartbeat_times'].get(task_id)
        if last_hb_time is not None:
            interval = now - last_hb_time
            _metrics['heartbeat_intervals'].append(interval)
            if len(_metrics['heartbeat_intervals']) > 500:
                _metrics['heartbeat_intervals'] = _metrics['heartbeat_intervals'][-500:]
        _metrics['_last_heartbeat_times'][task_id] = now
        hb = {
            "task_id": task_id,
            "percentage": float(percentage),
            "current_step": current_step,
            "eta_seconds": p.get("eta_seconds"),
            "extra": p.get("extra"),
            "reported_at": now
        }
        _upsert_heartbeat(data, hb)
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": {"task_id": task_id, "percentage": hb["percentage"], "current_step": current_step, "reported_at": now}}
        return envelop

    elif action == "get_task_progress":
        task_id = p.get("task_id")
        if not task_id:
            envelop.payload = {"ok": False, "error": "缺少 task_id 参数"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        hb = _get_latest_heartbeat(data, task_id)
        envelop.payload = {"ok": True, "data": {
            "task_id": task_id,
            "percentage": hb["percentage"] if hb else 0,
            "current_step": hb["current_step"] if hb else "",
            "eta_seconds": hb["eta_seconds"] if hb else None,
            "reported_at": hb["reported_at"] if hb else None,
            "status": task["status"]
        }}
        return envelop

    elif action == "get_dag_progress":
        tasks = data["tasks"]
        total = len(tasks)
        completed_count = 0
        running_count = 0
        task_list = []
        for tid, t in tasks.items():
            status = t.get("status", "pending")
            percentage = 0
            current_step = ""
            if status == "completed":
                percentage = 1.0
                completed_count += 1
            elif status == "running":
                running_count += 1
                hb = _get_latest_heartbeat(data, tid)
                if hb:
                    percentage = hb["percentage"]
                    current_step = hb["current_step"]
            task_list.append({
                "task_id": tid,
                "task_name": t.get("task_name", ""),
                "status": status,
                "percentage": percentage,
                "current_step": current_step
            })
        overall = sum(t["percentage"] for t in task_list) / total if total > 0 else 0
        envelop.payload = {"ok": True, "data": {
            "total_tasks": total,
            "completed_count": completed_count,
            "running_count": running_count,
            "overall_percentage": overall,
            "tasks": task_list
        }}
        return envelop

    elif action == "list_heartbeats":
        filter_task_id = p.get("task_id")
        limit = p.get("limit", 50)
        if filter_task_id:
            hb = data.get("heartbeats", {}).get(filter_task_id)
            beats = [hb] if hb else []
        else:
            beats = list(data.get("heartbeats", {}).values())
        beats = beats[-limit:]
        envelop.payload = {"ok": True, "data": {"heartbeats": beats, "count": len(beats)}}
        return envelop

    elif action == 'save_checkpoint':
        trigger_reason = p.get('trigger_reason', 'manual')
        cp_id, stats = _save_checkpoint(data, trigger_reason)
        if cp_id is None:
            envelop.payload = {'ok': True, 'data': {'checkpoint_id': None, 'task_stats': None, 'trigger_reason': trigger_reason, 'skipped': True, 'reason': 'cooldown not met'}}
            return envelop
        envelop.payload = {'ok': True, 'data': {'checkpoint_id': cp_id, 'task_stats': stats, 'trigger_reason': trigger_reason}}
        return envelop

    elif action == 'set_checkpoint_config':
        config_file = DATA_DIR / 'checkpointConfig.json'
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        existing = {}
        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as cf:
                    existing = json.load(cf)
            except Exception:
                pass
        if 'auto_on_complete' in p:
            existing['auto_on_complete'] = bool(p['auto_on_complete'])
        if 'auto_interval_seconds' in p:
            val = int(p['auto_interval_seconds'])
            existing['auto_interval_seconds'] = max(0, val)
        if 'max_checkpoints' in p:
            val = int(p['max_checkpoints'])
            existing['max_checkpoints'] = max(1, min(val, 50))
            global MAX_CHECKPOINTS
            MAX_CHECKPOINTS = existing['max_checkpoints']
        with open(config_file, 'w', encoding='utf-8') as cf:
            json.dump(existing, cf, ensure_ascii=False, indent=2)
        envelop.payload = {'ok': True, 'data': existing}
        return envelop

    elif action == 'get_checkpoint_config':
        config_file = DATA_DIR / 'checkpointConfig.json'
        config = {'auto_on_complete': True, 'auto_interval_seconds': 0, 'max_checkpoints': 5}
        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as cf:
                    stored = json.load(cf)
                    config.update(stored)
            except Exception:
                pass
        envelop.payload = {'ok': True, 'data': config}
        return envelop

    elif action == 'list_checkpoints':
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(CHECKPOINT_DIR.glob('checkpoint_cp_*.json'))
        checkpoints = []
        for f in files:
            try:
                with open(f, 'r', encoding='utf-8') as fh:
                    wrapper = json.load(fh)
                payload = wrapper.get('payload', {})
                checkpoints.append({
                    'checkpoint_id': payload.get('checkpoint_id'),
                    'created_at': payload.get('created_at'),
                    'trigger_reason': payload.get('trigger_reason'),
                    'task_stats': payload.get('task_stats', {})
                })
            except Exception:
                pass
        envelop.payload = {'ok': True, 'data': {'checkpoints': checkpoints, 'count': len(checkpoints)}}
        return envelop

    elif action == 'restore_checkpoint':
        global _data_cache
        cp_id = p.get('checkpoint_id')
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        if cp_id:
            cp_file = CHECKPOINT_DIR / f'checkpoint_{cp_id}.json'
            if not cp_file.exists():
                envelop.payload = {'ok': False, 'error': '检查点不存在: ' + cp_id}
                return envelop
            files_to_check = [cp_file]
        else:
            files_to_check = sorted(CHECKPOINT_DIR.glob('checkpoint_cp_*.json'), reverse=True)
        restored = False
        restored_cp_id = None
        restored_count = 0
        running_reset = 0
        for f in files_to_check:
            try:
                with open(f, 'r', encoding='utf-8') as fh:
                    wrapper = json.load(fh)
                stored_crc = wrapper.get('crc32')
                payload = wrapper.get('payload', {})
                computed_crc = _compute_crc32(payload)
                if stored_crc != computed_crc:
                    continue
                snapshot = payload.get('dag_snapshot', {})
                restored_cp_id = payload.get('checkpoint_id')
                snap_tasks = {}
                for t_snap in snapshot.get('tasks', []):
                    tid = t_snap.get('task_id')
                    full_task = make_task(
                        t_snap.get('task_name', ''),
                        task_type='restored',
                        dependencies=t_snap.get('dependencies', []),
                        payload=t_snap.get('payload', {}),
                        priority=t_snap.get('priority', 0),
                        condition=t_snap.get('condition', None)
                    )
                    full_task['task_id'] = tid
                    status = t_snap.get('status', 'pending')
                    if status in ('running', 'retrying'):
                        full_task['status'] = 'pending'
                        running_reset += 1
                    else:
                        full_task['status'] = status
                    full_task['result'] = t_snap.get('result')
                    full_task['current_retry'] = t_snap.get('retry_count', 0)
                    full_task['created_at'] = t_snap.get('created_at', time.time())
                    full_task['started_at'] = t_snap.get('started_at')
                    full_task['finished_at'] = t_snap.get('finished_at')
                    full_task['updated_at'] = time.time()
                    full_task['timeout'] = 300
                    full_task['max_retries'] = 3
                    full_task['retry_on'] = ['timeout', 'error']
                    full_task['retry_delay'] = 5
                    full_task['backoff_multiplier'] = 2.0
                    full_task['error_message'] = None
                    snap_tasks[tid] = full_task
                data['tasks'] = snap_tasks
                data['history'] = collections.deque(snapshot.get('history', [])[-1000:], maxlen=1000)
                data['messages'] = snapshot.get('messages', [])
                hb_snap = snapshot.get('heartbeats', {})
                if isinstance(hb_snap, dict):
                    data['heartbeats'] = dict(hb_snap)
                else:
                    data['heartbeats'] = {h.get('task_id'): h for h in hb_snap if h.get('task_id')}
                data['timers'] = {}
                _rebuild_all_indexes(data)
                for tid in list(_active_watchers.keys()):
                    _cancel_watcher(tid)
                restored_count = len(snap_tasks)
                add_history(data, {
                    'task_id': 'system', 'task_name': 'checkpoint_restore',
                    'action': 'restored', 'checkpoint_id': restored_cp_id,
                    'restored_task_count': restored_count, 'running_reset': running_reset,
                    'timestamp': time.time()
                })
                _data_cache = data
                save_data(data)
                flush_now()
                restored = True
                break
            except Exception:
                continue
        if not restored:
            envelop.payload = {'ok': False, 'error': '未找到有效检查点可恢复'}
            return envelop
        envelop.payload = {
            'ok': True,
            'data': {
                'checkpoint_id': restored_cp_id,
                'restored_task_count': restored_count,
                'running_reset_to_pending': running_reset
            }
        }
        return envelop

    elif action == 'delete_checkpoint':
        cp_id = p.get('checkpoint_id')
        if not cp_id:
            envelop.payload = {'ok': False, 'error': '缺少 checkpoint_id 参数'}
            return envelop
        cp_file = CHECKPOINT_DIR / f'checkpoint_{cp_id}.json'
        if not cp_file.exists():
            envelop.payload = {'ok': False, 'error': '检查点不存在: ' + cp_id}
            return envelop
        cp_file.unlink()
        envelop.payload = {'ok': True, 'data': {'deleted': cp_id}}
        return envelop

    elif action == 'batch_create_tasks':
        tasks_input = p.get("tasks", [])
        if not tasks_input:
            envelop.payload = {"ok": False, "error": "缺少 tasks 数组参数"}
            return envelop
        created = []
        for ti in tasks_input:
            task_name = ti.get("task_name")
            if not task_name:
                envelop.payload = {"ok": False, "error": "缺少 task_name 参数"}
                return envelop
            deps = ti.get("dependencies", [])
            if deps:
                for d in deps:
                    if d not in data["tasks"]:
                        envelop.payload = {"ok": False, "error": "依赖任务不存在: " + d}
                        return envelop
            task = make_task(
                task_name,
                task_type=ti.get("task_type", "default"),
                dependencies=deps,
                timeout=ti.get("timeout", 300),
                max_retries=ti.get("max_retries", 3),
                retry_on=ti.get("retry_on", ["timeout", "error"]),
                retry_delay=ti.get("retry_delay", 5),
                backoff_multiplier=ti.get("backoff_multiplier", 2.0),
                payload=ti.get("payload", {}),
                priority=ti.get("priority", 0),
                condition=ti.get("condition", None)
            )
            data["tasks"][task["task_id"]] = task
            for dep in deps:
                if dep not in _reverse_deps:
                    _reverse_deps[dep] = []
                _reverse_deps[dep].append(task["task_id"])
            created.append(task_to_dict(task))
        _metrics['total_tasks_created'] += len(created)
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": {"tasks": created, "count": len(created)}}
        return envelop

    elif action == 'get_metrics':
        summary = _compute_metrics_summary()
        envelop.payload = {'ok': True, 'data': summary}
        return envelop

    elif action == 'reset_metrics':
        _metrics['total_tasks_created'] = 0
        _metrics['tasks_started'] = 0
        _metrics['tasks_completed'] = 0
        _metrics['tasks_failed'] = 0
        _metrics['tasks_retried'] = 0
        _metrics['tasks_cancelled'] = 0
        _metrics['tasks_skipped'] = 0
        _metrics['messages_sent'] = 0
        _metrics['checkpoints_saved'] = 0
        _metrics['task_execution_times'] = []
        _metrics['message_delivery_times'] = []
        _metrics['heartbeat_intervals'] = []
        _metrics['_last_heartbeat_times'] = {}
        _metrics['start_time'] = time.time()
        envelop.payload = {'ok': True, 'data': {'message': '性能计数器已重置'}}
        return envelop

    elif action == 'detect_zombie_tasks':
        timeout_multiplier = p.get('timeout_multiplier', 2)
        zombies = detect_zombie_tasks(data, timeout_multiplier)
        for z in zombies:
            hb = _get_latest_heartbeat(data, z['task_id'])
            z['last_heartbeat_at'] = hb.get('reported_at') if hb else None
            z['last_heartbeat_step'] = hb.get('current_step') if hb else None
        envelop.payload = {'ok': True, 'data': {'zombies': zombies, 'count': len(zombies), 'timeout_multiplier': timeout_multiplier}}
        return envelop

    elif action == 'recover_zombie_task':
        task_id = p.get('task_id')
        if not task_id:
            envelop.payload = {'ok': False, 'error': '缺少 task_id 参数'}
            return envelop
        task = data['tasks'].get(task_id)
        if not task:
            envelop.payload = {'ok': False, 'error': '任务不存在: ' + task_id}
            return envelop
        if task['status'] != 'running':
            envelop.payload = {'ok': False, 'error': '任务状态不是running，无法恢复: ' + task['status']}
            return envelop
        _cancel_watcher(task_id)
        stop_timer(data, task_id)
        task['status'] = 'pending'
        task['current_retry'] = 0
        task['error_message'] = None
        task['result'] = None
        task['started_at'] = None
        task['finished_at'] = None
        task['updated_at'] = time.time()
        add_history(data, {'task_id': task_id, 'task_name': task['task_name'], 'action': 'zombie_recovered', 'timestamp': time.time()})
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {'ok': True, 'data': task_to_dict(task)}
        return envelop

    elif action == 'batch_recover_zombies':
        timeout_multiplier = p.get('timeout_multiplier', 2)
        zombies = detect_zombie_tasks(data, timeout_multiplier)
        recovered = []
        for z in zombies:
            tid = z['task_id']
            task = data['tasks'].get(tid)
            if not task or task['status'] != 'running':
                continue
            _cancel_watcher(tid)
            stop_timer(data, tid)
            task['status'] = 'pending'
            task['current_retry'] = 0
            task['error_message'] = None
            task['result'] = None
            task['started_at'] = None
            task['finished_at'] = None
            task['updated_at'] = time.time()
            recovered.append(tid)
            add_history(data, {'task_id': tid, 'task_name': task['task_name'], 'action': 'zombie_recovered', 'timestamp': time.time()})
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {'ok': True, 'data': {'recovered_tasks': recovered, 'count': len(recovered), 'total_zombies_found': len(zombies)}}
        return envelop

    elif action == 'get_error_summary':
        now = time.time()
        tasks = data['tasks']
        failed_tasks = []
        timeout_tasks = []
        retrying_tasks = []
        skipped_tasks = []
        for tid, t in tasks.items():
            status = t.get('status', 'pending')
            if status == 'failed':
                failed_tasks.append({'task_id': tid, 'task_name': t.get('task_name', ''), 'error_message': t.get('error_message', ''), 'finished_at': t.get('finished_at'), 'retry_count': t.get('current_retry', 0)})
            elif status == 'retrying':
                retrying_tasks.append({'task_id': tid, 'task_name': t.get('task_name', ''), 'error_message': t.get('error_message', ''), 'retry_count': t.get('current_retry', 0), 'max_retries': t.get('max_retries', 3)})
            elif status == 'skipped':
                skipped_tasks.append({'task_id': tid, 'task_name': t.get('task_name', ''), 'error_message': t.get('error_message', ''), 'condition': t.get('condition'), 'finished_at': t.get('finished_at')})
            elif status == 'running':
                started = t.get('started_at')
                if started and (now - started) > t.get('timeout', 300):
                    timeout_tasks.append({'task_id': tid, 'task_name': t.get('task_name', ''), 'running_seconds': round(now - started, 1), 'timeout': t.get('timeout', 300)})
        zombies = detect_zombie_tasks(data, 2)
        total_errors = len(failed_tasks) + len(timeout_tasks) + len(zombies)
        recent_errors = []
        for h in list(data['history'])[-50:]:
            if h.get('action') in ('failed', 'timeout_guard_failed', 'timeout_failed', 'cancelled', 'skipped_by_condition'):
                recent_errors.append(h)
        summary = {
            'total_tasks': len(tasks),
            'failed_count': len(failed_tasks),
            'timeout_risk_count': len(timeout_tasks),
            'retrying_count': len(retrying_tasks),
            'skipped_count': len(skipped_tasks),
            'zombie_count': len(zombies),
            'total_errors': total_errors,
            'failed_tasks': failed_tasks,
            'timeout_risk_tasks': timeout_tasks,
            'retrying_tasks': retrying_tasks,
            'skipped_tasks': skipped_tasks,
            'zombie_tasks': zombies,
            'recent_errors': recent_errors[-10:]
        }
        envelop.payload = {'ok': True, 'data': summary}
        return envelop

    elif action == 'force_reset_stuck':
        now = time.time()
        reset_tasks = []
        for tid, t in list(data['tasks'].items()):
            if t['status'] == 'running' and tid not in _active_watchers:
                _cancel_watcher(tid)
                stop_timer(data, tid)
                t['status'] = 'pending'
                t['current_retry'] = 0
                t['error_message'] = None
                t['result'] = None
                t['started_at'] = None
                t['finished_at'] = None
                t['updated_at'] = now
                reset_tasks.append(tid)
                add_history(data, {'task_id': tid, 'task_name': t.get('task_name', ''), 'action': 'force_reset_stuck', 'timestamp': now})
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {'ok': True, 'data': {'reset_tasks': reset_tasks, 'count': len(reset_tasks)}}
        return envelop

    elif action == "set_task_priority":
        task_id = p.get("task_id")
        priority = p.get("priority")
        if not task_id:
            envelop.payload = {"ok": False, "error": "缺少 task_id 参数"}
            return envelop
        if priority is None:
            envelop.payload = {"ok": False, "error": "缺少 priority 参数"}
            return envelop
        task = data["tasks"].get(task_id)
        if not task:
            envelop.payload = {"ok": False, "error": "任务不存在: " + task_id}
            return envelop
        if task["status"] != "pending":
            envelop.payload = {"ok": False, "error": "仅允许修改 pending 状态的任务，当前状态: " + task["status"]}
            return envelop
        old_priority = task.get("priority", 0)
        task["priority"] = priority
        task["updated_at"] = time.time()
        add_history(data, {"task_id": task_id, "task_name": task["task_name"], "action": "priority_changed", "old_priority": old_priority, "new_priority": priority, "timestamp": time.time()})
        save_data(data)
        if _dirty_timer is None:
            loop = asyncio.get_event_loop()
            _dirty_timer = loop.call_later(5, _lazy_flush)
        envelop.payload = {"ok": True, "data": task_to_dict(task)}
        return envelop

    elif action == "get_next_ready_tasks":
        ready_tasks = []
        for tid, t in data["tasks"].items():
            if t.get("status") != "pending":
                continue
            deps = t.get("dependencies") or []
            all_deps_done = True
            for dep_id in deps:
                dep = data["tasks"].get(dep_id)
                if not dep or dep.get("status") != "completed":
                    all_deps_done = False
                    break
            if all_deps_done:
                ready_tasks.append(task_to_dict(t))
        ready_tasks.sort(key=lambda x: (x.get("priority", 0), x.get("created_at", 0)))
        envelop.payload = {"ok": True, "data": {"ready_tasks": ready_tasks, "count": len(ready_tasks)}}
        return envelop

    elif action == "get_critical_path":
        action_name = p.get("action_name", "duration")
        if not action_name:
            action_name = "duration"
        root_task_ids = p.get("root_task_ids")
        include_completed = p.get("include_completed", True)
        if root_task_ids is not None and not isinstance(root_task_ids, list):
            envelop.payload = {"ok": False, "error": "root_task_ids 必须是数组"}
            return envelop
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _compute_critical_path(data, action_name, root_task_ids, include_completed)
        )
        if isinstance(result, dict) and result.get('_cycle'):
            envelop.payload = {"ok": False, "error": "检测到环路，无法计算关键路径"}
            return envelop
        envelop.payload = {"ok": True, "data": result}
        return envelop

    elif action == "batch_evaluate_conditions":
        task_ids_input = p.get("task_ids")
        if task_ids_input is None:
            target_tasks = [(tid, t) for tid, t in data["tasks"].items() if t.get("status") == "pending"]
        else:
            if not isinstance(task_ids_input, list):
                envelop.payload = {"ok": False, "error": "task_ids 必须是数组"}
                return envelop
            target_tasks = []
            for tid in task_ids_input:
                t = data["tasks"].get(tid)
                if t:
                    target_tasks.append((tid, t))
        results = []
        for tid, task in target_tasks:
            cond = task.get("condition")
            if not cond or not isinstance(cond, dict):
                results.append({
                    "task_id": tid,
                    "task_name": task.get("task_name", ""),
                    "condition": cond,
                    "evaluable": False,
                    "result": True,
                    "reason": "无条件表达式，无需门控"
                })
                continue
            try:
                source_task_id = cond.get("source_task_id")
                if 'op' not in cond and source_task_id is not None:
                    if source_task_id not in data["tasks"]:
                        results.append({
                            "task_id": tid,
                            "task_name": task.get("task_name", ""),
                            "condition": cond,
                            "evaluable": False,
                            "result": False,
                            "reason": "source_task_id 不存在"
                        })
                        continue
                evaluated = evaluate_task_condition(data, task, cond)
                results.append({
                    "task_id": tid,
                    "task_name": task.get("task_name", ""),
                    "condition": cond,
                    "evaluable": True,
                    "result": bool(evaluated),
                    "reason": "满足条件" if evaluated else "条件不满足: " + _describe_condition(cond)
                })
            except Exception as e:
                results.append({
                    "task_id": tid,
                    "task_name": task.get("task_name", ""),
                    "condition": cond,
                    "evaluable": False,
                    "result": False,
                    "reason": "评估异常: " + str(e)
                })
        envelop.payload = {"ok": True, "data": {"evaluations": results, "count": len(results)}}
        return envelop

    else:
        envelop.payload = {"ok": False, "error": "未知操作: " + action}
        return envelop

# @AICP_ALIGN: actions=create_task,update_task,delete_task,get_task,list_tasks,start_task,complete_task,fail_task,cancel_task,check_timeouts,retry_task,get_execution_history,get_dag_status,reset_task,send_message,read_messages,get_outbox,mark_read,get_comm_stats,report_heartbeat,get_task_progress,get_dag_progress,list_heartbeats,save_checkpoint,list_checkpoints,restore_checkpoint,delete_checkpoint,set_checkpoint_config,get_checkpoint_config,batch_create_tasks,get_metrics,reset_metrics,detect_zombie_tasks,recover_zombie_task,batch_recover_zombies,get_error_summary,force_reset_stuck,set_task_priority,get_next_ready_tasks,get_critical_path,batch_evaluate_conditions | output_fields=task,tasks,history,timed_out_nodes,count,downstream_ready,cancelled_tasks,summary,deleted,messages,message_id,sent_at,total_messages,unread_messages,messages_by_type,read_at,percentage,current_step,reported_at,eta_seconds,extra,task_name,status,total_tasks,completed_count,running_count,overall_percentage,heartbeats,checkpoint_id,task_stats,trigger_reason,checkpoints,restored_tasks,restored_task_count,running_reset_to_pending,version,skipped,skip_reason,condition,tasks_skipped,evaluable,result,reason,evaluations,ok,message,data,counters,timers,latency,uptime_seconds,zombies,timeout_multiplier,last_heartbeat_at,last_heartbeat_step,recovered_tasks,total_zombies_found,failed_count,timeout_risk_count,retrying_count,skipped_count,zombie_count,total_errors,failed_tasks,timeout_risk_tasks,retrying_tasks,skipped_tasks,zombie_tasks,recent_errors,reset_tasks,ready_tasks,priority,priority_distribution,critical_path,critical_path_length,slack_map,stats,total_nodes,critical_nodes,avg_slack,max_slack,min_slack | input_fields=task_id,task_name,dependencies,task_type,timeout,max_retries,retry_on,retry_delay,backoff_multiplier,payload,condition,result,failure_type,error_message,sender_task_id,recipient_task_id,message_type,content,unread_only,message_id,percentage,current_step,eta_seconds,extra,limit,trigger_reason,checkpoint_id,auto_on_complete,auto_interval_seconds,max_checkpoints,tasks,timeout_multiplier,priority,status,min_priority,max_priority,action_name,root_task_ids,include_completed,task_ids | type_values=running,completed,failed,cancelled,pending,retrying,skipped,timeout,error,data_request,data_share,status_update,signal,high,normal,low