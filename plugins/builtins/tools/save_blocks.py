import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import core

ALLOWED_PREFIXES = (
    'plugins/applications/',
    'www/',
)

BLOCK_RE = re.compile(
    r'===\s*(PLUGIN|HTML|CSS|JS|APP|YAML):\s*(\S+)\s*===\s*\n(.*?)=== END ===',
    re.DOTALL,
)
OPEN_RE = re.compile(r'^===\s*(PLUGIN|HTML|CSS|JS|APP|YAML):\s*(\S+)\s*===', re.M)

HARDCODED_HOST_RE = re.compile(r'https?://(?:127\.0\.0\.1|localhost)(?::\d+)?')
HARDCODED_API_RE = re.compile(r'/api/applications/([A-Za-z0-9_-]+)')


def _resolve_path(filepath: str, app_id: str) -> Path:
    filepath = filepath.replace('\\', '/').lstrip('./')
    return Path(filepath)


def _is_allowed(full: Path) -> bool:
    s = full.as_posix()
    return any(s.startswith(prefix) for prefix in ALLOWED_PREFIXES)


def _check_compile(filepath: str) -> bool:
    try:
        source = Path(filepath).read_text(encoding="utf-8")
        compile(source, filepath, "exec")
        return True
    except Exception:
        return False


def _project_of(full: Path):
    s = full.as_posix()
    parts = s.split('/')
    if s.startswith('plugins/applications/') and len(parts) >= 3:
        return parts[2]
    if s.startswith('www/apps/') and len(parts) >= 3:
        return parts[2]
    return None


def _add(bag, level, path, msg, hint=''):
    bag.append({'level': level, 'file': str(path), 'msg': msg, 'hint': hint})


# 硬编码项目名：const PLUGIN = 'fileshare' / const PROJECT = "fileshare"
JS_ASSIGN_RE = re.compile(
    r"""(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(['\"])([A-Za-z0-9_-]{2,40})\2"""
)
JS_FUNC_RE = re.compile(r'(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(')
JS_ARROW_RE = re.compile(
    r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]{0,80}\)\s*=>'
)
SCRIPT_RE = re.compile(r'<script\b[^>]*>(.*?)</script\s*>', re.S | re.I)
CHANNEL_RE = re.compile(r'channel=([A-Za-z0-9_-]+)')

# ── 前端变量解析：把 ${PLUGIN} / ${project} 还原成字面量 ─────────────
JS_VAR_TPL_RE = re.compile(
    r"""(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(['"`])(.+?)\2""",
    re.S,
)
TPL_VAR_RE = re.compile(r'\$\{([A-Za-z_$][\w$]*)\}')
API_TMPL_RE = re.compile(r'\$\{API\}/\$\{?([A-Za-z_$][\w$]*)\}?')
UNRESOLVED = '\x00'


def _extract_inline_js(code: str) -> str:
    """抽出所有内联 <script> 的内容（外链 src 的不算）。"""
    return '\n;\n'.join(SCRIPT_RE.findall(code or ''))


def _js_balance_check(js: str):
    """括号 / 引号 / 块注释配对检查（node 不可用时的兜底）。"""
    stack = []
    pairs = {')': '(', ']': '[', '}': '{'}
    i, n = 0, len(js)
    while i < n:
        c = js[i]
        if c in ('"', "'", '`'):
            quote = c
            start_line = js.count('\n', 0, i) + 1
            i += 1
            closed = False
            while i < n:
                ch = js[i]
                if ch == '\\':
                    i += 2
                    continue
                if ch == quote:
                    closed = True
                    break
                if quote != '`' and ch == '\n':
                    return False, '第 %d 行起的字符串没有闭合（%s）' % (start_line, quote)
                i += 1
            if not closed:
                return False, '第 %d 行起的字符串没有闭合（%s）' % (start_line, quote)
            i += 1
            continue
        if c == '/' and i + 1 < n:
            nxt = js[i + 1]
            if nxt == '/':
                j = js.find('\n', i)
                i = n if j < 0 else j
                continue
            if nxt == '*':
                j = js.find('*/', i + 2)
                if j < 0:
                    line = js.count('\n', 0, i) + 1
                    return False, (
                        '第 %d 行的块注释 /* 没有闭合（缺少 */），'
                        '它后面的代码全被当成注释、永远不会执行' % line
                    )
                i = j + 2
                continue
        if c in '([{':
            stack.append((c, js.count('\n', 0, i) + 1))
        elif c in ')]}':
            if not stack or stack[-1][0] != pairs[c]:
                line = js.count('\n', 0, i) + 1
                return False, '第 %d 行有多余或不匹配的 %s' % (line, c)
            stack.pop()
        i += 1
    if stack:
        c, line = stack[-1]
        return False, '第 %d 行的 %s 没有闭合' % (line, c)
    return True, ''


def _check_js_syntax(js: str):
    """优先 node --check 做真语法校验，没有 node 就做配对检查。"""
    if not js.strip():
        return True, ''
    node = shutil.which('node')
    if node:
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix='.mjs')
            os.close(fd)
            Path(tmp).write_text(js, encoding='utf-8')
            proc = subprocess.run([node, '--check', tmp], capture_output=True, timeout=25)
            if proc.returncode == 0:
                return True, ''
            err = (proc.stderr or b'').decode('utf-8', 'ignore').strip()
            if err:
                return False, err.replace(tmp, '<inline js>')[:800]
        except Exception:
            pass
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return _js_balance_check(js)


def _strip_js_comments(js: str) -> str:
    """剥离 JS 注释（不动字符串内容），用于判断函数是否真的被调用。"""
    out = []
    i, n = 0, len(js)
    while i < n:
        c = js[i]
        if c in ('"', "'", '`'):
            quote = c
            out.append(c)
            i += 1
            while i < n:
                ch = js[i]
                out.append(ch)
                if ch == '\\' and i + 1 < n:
                    out.append(js[i + 1])
                    i += 2
                    continue
                i += 1
                if ch == quote:
                    break
            continue
        if c == '/' and i + 1 < n and js[i + 1] == '/':
            j = js.find('\n', i)
            i = n if j < 0 else j
            continue
        if c == '/' and i + 1 < n and js[i + 1] == '*':
            j = js.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _check_js_entry(js: str, path, out):
    """定义了函数却从未被引用 → 入口大概率被注释掉或漏写。"""
    code = _strip_js_comments(js)
    names = []
    for rx in (JS_FUNC_RE, JS_ARROW_RE):
        for m in rx.finditer(code):
            nm = m.group(1)
            if nm not in names:
                names.append(nm)
    dead = [nm for nm in names
            if len(re.findall(r'\b%s\b' % re.escape(nm), code)) <= 1]
    if dead:
        _add(out, 'warning', path,
             '这些函数定义了但从未被引用：%s' % ', '.join(dead[:6]),
             '常见原因是入口 init()/loadXxx() 被注释掉或漏写；确认页面打开后逻辑真的跑了')


def _lint_python(code, path, out):
    """后端插件铁律（只看 applications 下的业务插件）。"""
    if not re.search(r'async def execute\s*\(', code):
        _add(out, 'error', path, '缺少 execute 函数',
             '签名必须是 async def execute(envelop, agent):')
    elif not re.search(r'async def execute\s*\(\s*envelop\s*,\s*agent\s*\)', code):
        _add(out, 'error', path, 'execute 签名不对', '必须是 async def execute(envelop, agent):')

    has_env = bool(re.search(r'return\s+envelop', code))
    has_data = bool(re.search(r'return\s*\{\s*["\']data["\']', code))
    if not has_env and not has_data:
        _add(out, 'error', path, '没有任何 return envelop',
             '每个 action 分支都要 return envelop，否则调用方拿到 None')

    if 'try' not in code or 'except' not in code:
        _add(out, 'warning', path, '没有 try/except 兜底', '异常应返回 {error: ...}，不要抛出')

    m = re.search(r'^PROJECT\s*=\s*["\']([A-Za-z0-9_-]+)["\']', code, re.M)
    if m:
        _add(out, 'error', path, 'PROJECT 硬编码为 "%s"' % m.group(1),
             '必须写 PROJECT = Path(__file__).parent.name')

    if 'agent.llm' in code and not re.search(r'if\s+not\s+agent\.llm', code):
        _add(out, 'error', path, '调用 agent.llm 前没有 guard', '加 if not agent.llm: 保护')

    if re.search(r'len\s*\([^)]*\)\s*\+\s*1', code) and re.search(r'["\']id["\']', code):
        _add(out, 'warning', path, 'ID 生成疑似 len(...)+1', '删除后 ID 会重复，改用 max(id)+1')

    if 'envelop.receiver' in code:
        _add(out, 'warning', path, '设置了 envelop.receiver', '除非明确转发，否则不要动')


def _lint_html(code, path, project, out):
    if 'location.pathname.split' not in code:
        _add(out, 'warning', path, '没有从 URL 推导项目名',
             "建议 const project = window.location.pathname.split('/')[2]")

    for m in HARDCODED_API_RE.finditer(code):
        literal = m.group(1)
        if literal.startswith('$') or '${' in code[max(0, m.start() - 3):m.start() + 1]:
            continue
        _add(out, 'error', path, '硬编码 API 项目名 /api/applications/%s' % literal,
             '必须用 `${API}/<plugin>`，API = `/api/applications/${project}`')
        break

    m = HARDCODED_HOST_RE.search(code)
    if m:
        _add(out, 'warning', path, '硬编码地址 %s' % m.group(0),
             '用 /api/ws_config 和 /api/upload_config 取 url，不写死端口/host')

    if 'gradient(' in code or 'box-shadow' in code:
        _add(out, 'warning', path, '用了 CSS gradient / box-shadow', '禁令：不要装饰性渐变和阴影')

    if re.search(r'append\s*\(\s*["\']files["\']', code):
        _add(out, 'error', path, '上传字段名写成了 files', '必须是 file（单数）')

    if re.search(r'data:image/\w+;base64', code):
        _add(out, 'warning', path, '用了 base64 内嵌图片', '用上传返回的 url')

    if project and re.search(r'/api/applications/%s/' % re.escape(project), code):
        _add(out, 'error', path, '硬编码了本项目的 API 路径', '改用 ${API}')

    if '<script' not in code:
        _add(out, 'warning', path, '页面里没有 <script>', '确认前端逻辑是否没写')

    # ── JS 真语法校验（注释未闭合 / 括号不配对 / 引号不闭合） ──
    js = _extract_inline_js(code)
    if js.strip():
        ok, err = _check_js_syntax(js)
        if not ok:
            _add(out, 'error', path, 'JS 语法错误：%s' % err,
                 '页面会整段或局部不执行；修好语法再保存')
        else:
            _check_js_entry(js, path, out)

        # ── 硬编码项目名：const PLUGIN = 'fileshare' / const PROJECT = "fileshare" ──
        literal_names = {'project', 'api', 'pluginname', 'plugin_name'}
        for m in JS_ASSIGN_RE.finditer(js):
            var, literal = m.group(1), m.group(3)
            if var.lower() not in literal_names and var.lower() not in (
                    'plugin', 'proj', 'project', 'app', 'appid', 'app_id', 'projectname', 'project_name'):
                continue
            if var.lower() in ('plugin', 'pluginname', 'plugin_name'):
                # 插件名是全应用唯一手写名（const PLUGIN = "notes"），
                # 它对不对由项目级「对齐检查」判（磁盘上没有同名 .py 会报 error）
                continue
            if project and literal == project:
                _add(out, 'error', path,
                     '硬编码项目名：%s = \'%s\'' % (var, literal),
                     '换项目就坏；改成 const project = window.location.pathname.split(\'/\')[2]')
                break

    # ── 上传 / 鉴权下载专项（手册《文件上传与下载》章节） ──
    has_upload = ('upload_config' in code) or bool(re.search(r'append\s*\(\s*["\']file["\']', code))
    if has_upload:
        if 'XMLHttpRequest' not in code and 'upload.onprogress' not in code:
            _add(out, 'warning', path, '有上传但没有真实进度事件',
                 'fetch 无上传进度，必须用 XMLHttpRequest + xhr.upload.onprogress(e.loaded/e.total)')
        if 'setInterval' in code and 'onprogress' not in code:
            _add(out, 'warning', path, '上传进度疑似用 setInterval 伪造',
                 '用 xhr.upload.onprogress 的真实字节数，不要定时器假动画')
        if not re.search(r'file_path', code) and not re.search(r'files?\.json|filesave|files\b', code):
            _add(out, 'warning', path, '上传后没保存 file_path / 文件清单',
                 '落盘在 data/uploads/<8位子目录>/，只存 basename 会找不到；上传成功要自己存 file_path+原始文件名')

    # 分享/鉴权下载：裸 url 直连 = 绕过验证码
    _guarded = bool(re.search(r'验证码|过期|expire|token|passcode|secret', code, re.I))
    _code_param = bool(re.search(r'[?&]code=', code))
    if '/data/uploads/' in code and (_guarded or _code_param):
        _add(out, 'warning', path, '分享页里出现 /data/uploads/ 直连 url',
             '有验证码/过期的分享必须由插件校验后返回 meta.response_type=file 走网关直出，不要前端直连裸 url')


def _lint_js(code, path, project, out):
    """独立 .js 文件也要过语法校验 + 硬编码项目名检查（与内联 JS 同款判据）。"""
    ok, err = _check_js_syntax(code)
    if not ok:
        _add(out, 'error', path, 'JS 语法错误：%s' % err,
             '整个脚本不会执行；修好语法再保存')
        return

    _check_js_entry(code, path, out)

    literal_names = {'project', 'proj', 'projectname', 'project_name',
                     'plugin', 'pluginname', 'plugin_name', 'app', 'appid', 'app_id'}
    for m in JS_ASSIGN_RE.finditer(code):
        var, literal = m.group(1), m.group(3)
        if var.lower() not in literal_names:
            continue
        if project and literal == project:
            _add(out, 'error', path,
                 '硬编码项目名：%s = \'%s\'' % (var, literal),
                 '换项目就坏；改成 const project = window.location.pathname.split(\'/\')[2]')
            break
        if var.lower() in ('plugin', 'pluginname', 'plugin_name'):
            # 插件名是全应用唯一手写名（const PLUGIN = "notes"），
            # 它对不对由项目级「对齐检查」判（磁盘上没有同名 .py 会报 error）
            continue

    if 'gradient(' in code or 'box-shadow' in code:
        _add(out, 'warning', path, '用了 CSS gradient / box-shadow', '禁令：不要装饰性渐变和阴影')

    if re.search(r'append\s*\(\s*["\']files["\']', code):
        _add(out, 'error', path, '上传字段名写成了 files', '必须是 file（单数）')

    if 'location.pathname.split' not in code and '/api/applications' in code:
        _add(out, 'warning', path, '没有从 URL 推导项目名',
             "建议 const project = window.location.pathname.split('/')[2]")


def _lint_yaml(code, path, project, out):
    if not re.search(r'^id\s*:', code, re.M):
        _add(out, 'warning', path, 'app.yaml 缺 id', '应用列表可能不显示')
    m = re.search(r'^static_root\s*:\s*(\S+)', code, re.M)
    if not m:
        _add(out, 'warning', path, 'app.yaml 缺 static_root', '建议 www/apps/%s' % project)
    else:
        root = m.group(1).strip().strip('"\'').rstrip('/')
        if project and root != 'www/apps/%s' % project:
            _add(out, 'error', path, 'static_root=%s 与项目路径不一致' % root,
                 '应为 www/apps/%s' % project)


def _lint_block(filepath, code, out):
    full = Path(filepath)
    project = _project_of(full) or ''
    name = full.name

    if name == '__init__.py':
        _add(out, 'error', filepath, '出现了 __init__.py', '整个目录会变成一个包插件，删掉它')
    if full.suffix == '.py':
        _lint_python(code, filepath, out)
    elif full.suffix in ('.html', '.htm'):
        _lint_html(code, filepath, project, out)
    elif full.suffix == '.js':
        _lint_js(code, filepath, project, out)
    elif name in ('app.yaml', 'app.yml'):
        _lint_yaml(code, filepath, project, out)


def _project_checks(project, base_url):
    """按磁盘真实状态聚合项目自检（分次保存不会让结果抖动）。"""
    app_dir = Path('plugins/applications') / project
    py_files = sorted(app_dir.glob('*.py')) if app_dir.is_dir() else []
    compile_ok = True
    bad = []
    for p in py_files:
        if p.name.startswith('__'):
            continue
        if not _check_compile(str(p)):
            compile_ok = False
            bad.append(p.name)

    return {
        'plugin_dir': app_dir.as_posix(),
        'plugin_files': [p.name for p in py_files],
        'app_yaml': (app_dir / 'app.yaml').exists(),
        'frontend': (Path('www') / 'apps' / project / 'index.html').exists(),
        'compile_ok': compile_ok,
        'compile_failed': bad,
        'url': '%s/apps/%s/' % (base_url.rstrip('/'), project),
    }


API_SUFFIX_RE = re.compile(r'\$\{API\}/([A-Za-z0-9_-]+)')
API_LITERAL_RE = re.compile(r'/api/applications/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)')

# const PLUGIN = project;  —— 变量指向另一个变量
JS_VAR_IDENT_RE = re.compile(
    r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\s*[;\n]'
)
# function request(pluginName, payload) { ... }
FUNC_DEF_RE = re.compile(
    r'(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)'
)
# const request = async (pluginName, payload) => { ... }
ARROW_DEF_RE = re.compile(
    r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>'
)

# 后端频道名：CHANNEL = f"{PROJECT}_{PLUGIN}" / "channel_id": f"{PROJECT}_dashboard"
CH_DEF_RE = re.compile(
    r'(?:CHANNEL\s*=\s*|channel_id["\']?\s*[:=]\s*)f?["\']([^"\']+)["\']'
)


def _js_vars(js: str) -> dict:
    """抽出前端 JS 里的字符串常量变量（只做字面量赋值，不追表达式）。

    `const PLUGIN = "notes"` → {"PLUGIN": "notes"}
    """
    out = {}
    for m in JS_VAR_TPL_RE.finditer(_strip_js_comments(js)):
        name, value = m.group(1), m.group(3)
        if name not in out:
            out[name] = value
    return out


def _resolve_tpl(value: str, variables: dict) -> str:
    """把模板里的 ${var} 用已知变量替换掉；解不出来的留原样（表示动态）。"""
    for _ in range(3):
        new = TPL_VAR_RE.sub(lambda m: variables.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def _resolved_vars(js: str, project: str) -> dict:
    """变量表 + 注入 project + 变量值里的 ${...} 递归解掉。

    额外处理「变量指向变量」：`const PLUGIN = project;` → PLUGIN = 项目名。
    """
    variables = _js_vars(js)
    if project:
        variables.setdefault('project', project)
    for key in list(variables):
        variables[key] = _resolve_tpl(variables[key], variables)
    # const PLUGIN = project;  /  const PLUGIN = OTHER;
    for m in JS_VAR_IDENT_RE.finditer(_strip_js_comments(js)):
        name, target = m.group(1), m.group(2)
        if name in variables:
            continue
        if target == 'project' and project:
            variables[name] = project
        elif target in variables:
            variables[name] = variables[target]
    for key in list(variables):
        variables[key] = _resolve_tpl(variables[key], variables)
    return variables


# 调用实参字面量：request('notes', {...})
LITERAL_ARG_RE = re.compile(r"""['"]([A-Za-z0-9_-]+)['"]""")


def _resolve_arg(arg: str, variables: dict):
    """把调用实参解析成字面量；解不出来返回 None。"""
    arg = (arg or '').strip()
    if not arg:
        return None
    m = LITERAL_ARG_RE.fullmatch(arg)
    if m:
        return m.group(1)
    if arg in variables:
        return variables[arg]
    return None


def _param_values(js: str, variables: dict) -> dict:
    """形参名 → 所有调用点传入的字面量集合。

    前端常把插件名当函数参数传：fetch(`${API}/${pluginName}`)，
    真正决定路由的是调用处 request('notes', {...})。这里反查调用点，
    把「看起来动态」还原成静态可校验的插件名。
    """
    funcs = {}
    for rx in (FUNC_DEF_RE, ARROW_DEF_RE):
        for m in rx.finditer(js):
            params = [p.strip() for p in m.group(2).split(',') if p.strip()]
            funcs.setdefault(m.group(1), []).append(params)

    out = {}
    for fname, plists in funcs.items():
        pattern = r'(?<![A-Za-z0-9_$])' + re.escape(fname) + r'\s*\('
        for m in re.finditer(pattern, js):
            tail = js[m.end():]
            depth = 1
            brace = 0
            bracket = 0
            args = []
            cur = ''
            for ch in tail:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        break
                elif ch == '{':
                    brace += 1
                elif ch == '}':
                    brace -= 1
                elif ch == '[':
                    bracket += 1
                elif ch == ']':
                    bracket -= 1
                if ch == ',' and depth == 1 and brace == 0 and bracket == 0:
                    args.append(cur)
                    cur = ''
                else:
                    cur += ch
            args.append(cur)
            for pl in plists:
                for i, pname in enumerate(pl):
                    if i >= len(args):
                        break
                    val = _resolve_arg(args[i], variables)
                    if val:
                        out.setdefault(pname, set()).add(val)
    return out


def _backend_channels(app_dir, project: str) -> set:
    """抽后端插件里静态可见的 WS 频道名。

    认这几种写法，模板里的 {PROJECT} / {PLUGIN} / %s 会代入真名：
      CHANNEL = f"{PROJECT}_{PLUGIN}"       → {project}_{插件文件名}
      CHANNEL = f"{PROJECT}_proc"            → {project}_proc
      "channel_id": f"{PROJECT}_dashboard"   → {project}_dashboard
      "channel_id": '%s_dashboard' % PROJECT  → {project}_dashboard
    """
    out = set()
    if not app_dir.is_dir():
        return out
    for p in sorted(app_dir.glob('*.py')):
        if p.name.startswith('__'):
            continue
        try:
            src = p.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        for m in CH_DEF_RE.finditer(src):
            tpl = m.group(1)
            resolved = tpl.replace('{PROJECT}', project).replace('{PLUGIN}', p.stem)
            resolved = resolved.replace('%s', project)
            if not resolved or '{' in resolved or '%' in resolved:
                continue          # 模板里还有解不出来的变量，不猜
            out.add(resolved)
    return out


def _frontend_routes(js: str, project: str) -> tuple:
    """返回 (静态可解析的插件名列表, 是否存在无法静态解析的动态端点)。"""
    variables = _resolved_vars(js, project)
    param_values = _param_values(js, variables)
    names = []
    dynamic = False

    # `${API}/notes` —— 字面量段
    for m in re.finditer(r'\$\{API\}/([A-Za-z0-9_-]+)', js):
        if m.group(1) not in names:
            names.append(m.group(1))

    # `${API}/${PLUGIN}` —— 变量段，能解出字面量就收，解不出算动态
    for m in API_TMPL_RE.finditer(js):
        var = m.group(1)
        value = variables.get(var)
        if not value and var in param_values:
            # 插件名当函数参数传：request('notes', {...}) → 反查调用点还原
            cands = sorted(param_values[var])
            if len(cands) == 1:
                value = cands[0]
        if value and re.fullmatch(r'[A-Za-z0-9_-]+', value):
            if value not in names:
                names.append(value)
        else:
            dynamic = True

    # `/api/applications/<proj>/<plugin>` 字面量（先把 ${project} 解成真名）
    resolved = _resolve_tpl(js, variables)
    for m in API_LITERAL_RE.finditer(resolved):
        if m.group(1) == project and m.group(2) not in names:
            names.append(m.group(2))

    return names, dynamic


def _frontend_channels(js: str, project: str) -> list:
    """抽前端 WS 频道名（${project} 已解成真名）。"""
    variables = _resolved_vars(js, project)
    out = []
    for m in re.finditer(r'channel=([A-Za-z0-9_${}.-]+)', js):
        resolved = _resolve_tpl(m.group(1), variables)
        if resolved and resolved not in out:
            out.append(resolved)
    return out


def _lint_route_alignment(js, path, project, out):
    """前端引用的插件名 vs 磁盘上真实存在的 .py 文件，双向差集。"""
    if not project:
        return
    app_dir = Path('plugins/applications') / project
    on_disk = set()
    internal = set()
    if app_dir.is_dir():
        for p in app_dir.glob('*.py'):
            if p.name.startswith('__'):
                continue
            on_disk.add(p.stem)
            if p.stem.startswith('_'):
                # _init.py 这类内部插件：由系统/托盘调用，前端本来就不引用
                internal.add(p.stem)
    if not on_disk:
        return

    refs, dynamic = _frontend_routes(js, project)

    missing = [n for n in refs if n not in on_disk]
    for name in missing:
        _add(out, 'error', path,
             '前端调用了插件 "%s"，但 plugins/applications/%s/%s.py 不存在' % (name, project, name),
             '路由末段必须等于后端插件文件名（去 .py）；改前端或补后端文件')

    if dynamic:
        _add(out, 'warning', path,
             '有动态拼接的 API 端点（${API}/${变量} 且变量非字面量），静态检查覆盖不到',
             '改成 const PLUGIN = "字面量" + `${API}/${PLUGIN}`，静态 lint 才能对齐')

    # 有悬空端点时，「未被前端引用」是同一个错误的连带结果，不再重复报
    if refs and not dynamic and not missing:
        for name in sorted((on_disk - internal) - set(refs)):
            _add(out, 'warning', path,
                 '后端插件 %s.py 没有被前端引用' % name,
                 '被别的插件调用的 helper 可忽略；否则是改名后的残留文件，删掉它，否则网关会多注册一个 receiver')


def _lint_channel_alignment(js, path, project, out):
    """前端 WS 频道名必须与后端推送的频道名一致。"""
    if not project:
        return
    channels = _frontend_channels(js, project)
    if not channels:
        return
    backend = _backend_channels(Path('plugins/applications') / project, project)
    if not backend:
        return
    for ch in channels:
        if '$' in ch or '{' in ch:
            continue
        if ch in backend:
            continue
        _add(out, 'warning', path,
             'WS 频道名 "%s" 在后端找不到对应定义' % ch,
             '后端推送频道写成了别的名字；两端必须同规则：前端 `${project}_${topic}`，后端 f"{PROJECT}_{topic}"')


def _check_alignment(project, out):
    """项目级对齐检查：以前端最终落盘内容 vs 磁盘后端插件为准。"""
    html_path = Path('www') / 'apps' / project / 'index.html'
    if not html_path.exists():
        return
    try:
        html = html_path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return
    js = _extract_inline_js(html)
    if not js.strip():
        return
    _lint_route_alignment(js, html_path.as_posix(), project, out)
    _lint_channel_alignment(js, html_path.as_posix(), project, out)


def _extract_api_endpoints(html: str, project: str):
    """抽后端插件名（页面首屏会调的那几个），用于运行时探测。

    判据是「磁盘真实路由集」而不是「前端拼出来的字符串」：
      1. 前端静态可解析的引用（${API}/notes、${API}/${PLUGIN}，后者用 JS 变量表解）
      2. 磁盘上真实存在的插件文件（只认含 execute 的 .py）
    前端动态拼接时磁盘侧仍然完整，不会探到不存在的路由；
    反过来前端引用了磁盘上没有的名字，由对齐检查报 error。
    """
    names = []
    raw = html or ''

    js = _extract_inline_js(raw)
    if js.strip():
        try:
            refs, _dynamic = _frontend_routes(js, project)
        except Exception:
            refs = []
        for name in refs:
            if name not in names:
                names.append(name)

    app_dir = Path('plugins/applications') / project
    if app_dir.is_dir():
        for p in sorted(app_dir.glob('*.py')):
            if p.name.startswith('__') or p.stem in names:
                continue
            try:
                src = p.read_text(encoding='utf-8', errors='replace')
            except Exception:
                continue
            if 'async def execute' in src:
                names.append(p.stem)

    return names


async def _runtime_probe(project, base_url, issues):
    """保存后运行时自检：真请求一次线上页面 + 首屏接口。

    lint 只能看磁盘上的字符串；页面能不能打开、接口通不通，只有真发
    一次请求才知道。宿主连不上（没起 / 端口不同）只记 warning，
    页面 404 或线上内容不是刚存的那份才记 error。
    """
    import json as _json

    out = {
        'url': '%s/apps/%s/' % (base_url.rstrip('/'), project),
        'ran': False,
        'page_status': None,
        'page_len': 0,
        'local_len': 0,
        'endpoints': {},
        'problems': [],
    }

    local_html = Path('www') / 'apps' / project / 'index.html'
    if not local_html.exists():
        return out
    try:
        local_text = local_html.read_text(encoding='utf-8', errors='replace')
    except Exception:
        local_text = ''
    out['local_len'] = len(local_text)

    try:
        import aiohttp
    except ImportError:
        _add(issues, 'warning', project, '运行时自检跳过：aiohttp 不可用')
        return out

    timeout = aiohttp.ClientTimeout(total=10)
    html = ''
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(out['url']) as resp:
                out['page_status'] = resp.status
                html = await resp.text()
            out['ran'] = True

            if out['page_status'] == 200:
                for name in _extract_api_endpoints(html, project)[:3]:
                    api_url = '%s/api/applications/%s/%s' % (
                        base_url.rstrip('/'), project, name)
                    try:
                        async with sess.post(api_url, json={'action': 'list'}) as r2:
                            raw = await r2.text()
                            body = None
                            try:
                                body = _json.loads(raw)
                            except Exception:
                                body = None
                            info = {'status': r2.status}
                            if isinstance(body, dict):
                                info['ok'] = body.get('ok')
                                info['error'] = body.get('error')
                            out['endpoints'][name] = info
                    except Exception as e:
                        out['endpoints'][name] = {
                            'status': None,
                            'exception': '%s: %s' % (type(e).__name__, e),
                        }
    except Exception as e:
        _add(issues, 'warning', 'www/apps/%s/index.html' % project,
             '运行时自检未跑通：%s: %s' % (type(e).__name__, e),
             '宿主可能没起 / 端口不同；文件已保存，但页面能否打开未验证')
        return out

    out['page_len'] = len(html or '')

    if out['page_status'] != 200:
        out['problems'].append('page_status_%s' % out['page_status'])
        _add(issues, 'error', 'www/apps/%s/index.html' % project,
             '线上页面返回 %s（本地文件存在）：%s 打不开' % (out['page_status'], out['url']),
             '检查 app.yaml 的 static_root 是否指向 www/apps/%s' % project)
        return out

    if not out['page_len']:
        out['problems'].append('empty_page')
        _add(issues, 'error', 'www/apps/%s/index.html' % project,
             '线上页面返回 200 但内容为空', '检查静态目录与路由')
    elif out['local_len'] and out['page_len'] < out['local_len'] * 0.5:
        out['problems'].append('page_shorter_than_local')
        _add(issues, 'error', 'www/apps/%s/index.html' % project,
             '线上页面只有 %d 字节，本地文件 %d 字节：拿到的不是刚保存的页面' % (
                 out['page_len'], out['local_len']),
             '多半是 static_root 指错目录，或另一个同名页面在抢路由')
    else:
        lines = [ln.strip() for ln in local_text.splitlines() if ln.strip()]
        tail = lines[-1][:40] if lines else ''
        if tail and tail not in html:
            out['problems'].append('online_stale')
            _add(issues, 'warning', 'www/apps/%s/index.html' % project,
                 '线上页面里找不到本地最后一行内容：可能还没热加载或有缓存',
                 '让用户 Ctrl+F5 强刷；仍不对再查静态目录')

    for name, info in out['endpoints'].items():
        if info.get('exception'):
            out['problems'].append('api_%s_exception' % name)
            _add(issues, 'warning', 'api/%s' % name,
                 '首屏接口 %s 请求失败：%s' % (name, str(info['exception'])[:120]),
                 '确认该插件已加载（看 loaded 字段）')
        elif info.get('status') not in (200, None):
            out['problems'].append('api_%s_status_%s' % (name, info['status']))
            _add(issues, 'error', 'api/%s' % name,
                 '首屏接口 %s 返回 HTTP %s' % (name, info['status']),
                 '看该插件日志里的异常')
        elif info.get('ok') is False:
            out['problems'].append('api_%s_not_ok' % name)
            _add(issues, 'warning', 'api/%s' % name,
                 '首屏接口 %s 返回 ok:false：%s' % (name, str(info.get('error'))[:120]),
                 '该插件若没有 list action 属正常，否则是真错误')

    return out


async def execute(envelop, agent):
    raw = envelop.payload if isinstance(envelop.payload, dict) else {}
    payload = raw.get('payload') if isinstance(raw.get('payload'), dict) else raw

    text = payload.get('text', '') or ''
    app_id = payload.get('app_id', '') or ''

    if not isinstance(text, str) or not text.strip():
            envelop.payload = {'ok': False, 'error': 'text 为空，拒绝写入（可能是模型输出被截断）'}
            return envelop

    saved, rejected, projects = [], [], []
    issues = []

    matches = list(BLOCK_RE.finditer(text))
    opened = OPEN_RE.findall(text)

    if not matches:
        envelop.payload = {
            'ok': False,
            'error': '没有解析到任何代码块',
            'hint': '块头 === PLUGIN: plugins/applications/demo/demo.py === ，块尾单独一行 === END ===',
            'errors': ['块格式不完整，未写入任何文件'],
            'issues': [{'level': 'error', 'file': '', 'msg': '块格式不完整'}],
        }
        return envelop

    if len(opened) > len(matches):
        _add(issues, 'error', '',
             '有 %d 个块头没有对应 === END ===（内容被截断）' % (len(opened) - len(matches)),
             '把缺少 END 的块重新完整保存')

    for match in matches:
        filepath = match.group(2).strip()
        code = match.group(3).strip()
        full = _resolve_path(filepath, app_id)

        block_issues = []
        if not _is_allowed(full):
            _add(block_issues, 'error', filepath, '路径越权', '只允许 plugins/applications/ 和 www/')
        elif not code:
            _add(block_issues, 'error', filepath, '块内容为空', '检查是否漏写内容或提前写了 END')
        else:
            _lint_block(filepath, code, block_issues)

        issues.extend(block_issues)

        if any(i['level'] == 'error' for i in block_issues):
            rejected.append(filepath)
            continue

        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(code, encoding='utf-8')
            saved.append(full.as_posix())
        except Exception as e:
            _add(issues, 'error', filepath, '%s: %s' % (type(e).__name__, e))
            rejected.append(filepath)
            continue

        proj = _project_of(full)
        if proj and proj not in projects:
            projects.append(proj)

    # LLM 依赖提示：代码用到 agent.llm 但引擎没配模型时，显式告知（不拦截，guard 由 lint 管）
    if not getattr(agent, 'llm', None):
        for match in matches:
            if 'agent.llm' in match.group(3):
                _add(issues, 'warning', match.group(2).strip(),
                     '代码调用 agent.llm，但引擎未配置模型（aicp.yaml 缺 models.providers）',
                     '先在 aicp.yaml 配置 models 并重启，否则 LLM 功能不可用；请在回复中告知用户')
                break

    if saved:
        await asyncio.sleep(3)

    loaded = {}
    compiled = {}
    pending_reload = []
    for path in saved:
        p = Path(path)
        if p.suffix != '.py':
            continue
        route = p.relative_to('plugins').as_posix().replace('.py', '')
        compiled[route] = _check_compile(str(p))
        if not compiled[route]:
            _add(issues, 'error', path, 'Python 语法错误', '插件不会加载，改完重新保存')
            loaded[route] = False
            continue
        # 热加载有约 2 秒窗口：编译过了但还没注册，补等一次再判，避免误报 loaded:false
        try:
            is_loaded = route in core.plugins
        except Exception:
            is_loaded = False
        if not is_loaded:
            await asyncio.sleep(2)
            try:
                is_loaded = route in core.plugins
            except Exception:
                is_loaded = False
        loaded[route] = is_loaded
        if not is_loaded:
            pending_reload.append(route)

    if pending_reload:
        _add(issues, 'warning', ', '.join(pending_reload),
             '编译已通过（compiled:true），但返回时还没注册进 core.plugins',
             'loaded:false 不等于编译失败：多为热加载时延，等几秒直接调 action 验证；持续 false 才是 import 期异常，查引擎日志')

    base_url = getattr(agent, 'base_url', None) or 'http://127.0.0.1:9000'

    checks = {}
    for proj in projects:
        info = _project_checks(proj, base_url)
        checks[proj] = info
        if info['frontend']:
            try:
                info['runtime'] = await _runtime_probe(proj, base_url, issues)
            except Exception as e:
                _add(issues, 'warning', 'www/apps/%s/index.html' % proj,
                     '运行时自检异常：%s: %s' % (type(e).__name__, e))
        if not info['frontend']:
            _add(issues, 'warning', 'www/apps/%s/index.html' % proj,
                 '前端页面还没落盘，访问 %s 会 404' % info['url'],
                 '继续保存 HTML 块（本次只存后端属正常）')
        else:
            try:
                _check_alignment(proj, issues)
            except Exception as e:
                _add(issues, 'warning', 'www/apps/%s/index.html' % proj,
                     '对齐检查异常：%s: %s' % (type(e).__name__, e))
        if not info['app_yaml']:
            _add(issues, 'warning', 'plugins/applications/%s/app.yaml' % proj,
                 'app.yaml 缺失：应用列表不显示', '补 id / name / static_root')
        if not info['compile_ok']:
            _add(issues, 'error', 'plugins/applications/%s' % proj,
                 '编译失败：%s' % ', '.join(info['compile_failed']))

    errors = ['[%s] %s' % (i['file'], i['msg']) for i in issues if i['level'] == 'error']
    warnings = ['[%s] %s' % (i['file'], i['msg']) for i in issues if i['level'] == 'warning']

    ok = not errors and not rejected
    preview_url = checks[projects[0]]['url'] if projects else None

    envelop.payload = {
        'ok': ok,
        # Envelop.result() 只认 error 字段判断失败；没有 error 时会把 ok 覆盖成 True。
        # 这里显式带上 error，MCP 的 isError 才能正确置位。
        'error': (errors[0] if errors else None),
        'saved': saved,
        'count': len(saved),
        'rejected': rejected,
        'projects': projects,
        'preview_url': preview_url,
        'preview_links': [checks[p]['url'] for p in projects],
        'loaded': loaded,
        'compiled': compiled,
        'checks': checks,
        'errors': errors,
        'warnings': warnings,
        'issues': [i for i in issues if i['level'] == 'error'],
        'next': ('把 preview_url 原样作为 markdown 链接交付给用户'
                 if ok else '有 error / rejected：按 issues + hint 修完重新保存这一块，不要交付'),
    }
    return envelop
