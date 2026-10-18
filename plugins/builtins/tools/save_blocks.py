import asyncio
import re
from pathlib import Path
import core

ALLOWED_PREFIXES = (
    'plugins/applications/',
    'www/',
)


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


async def execute(envelop, agent):
    text = envelop.payload.get('text', '')
    app_id = envelop.payload.get('app_id', '')

    if not text:
        envelop.payload = {'ok': False, 'error': '缺少 text'}
        return envelop

    saved = []
    errors = []
    pattern = r'===\s*(PLUGIN|HTML|CSS|JS|APP|YAML):\s*(\S+)\s*===\s*\n(.*?)=== END ==='

    for match in re.finditer(pattern, text, re.DOTALL):
        filepath = match.group(2).strip()
        code = match.group(3).strip()
        full = _resolve_path(filepath, app_id)
        if not _is_allowed(full):
            errors.append(f'路径越权: {filepath}')
            continue
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(code, encoding='utf-8')
            saved.append(str(full))
        except Exception as e:
            errors.append(f'{filepath}: {type(e).__name__}: {e}')

    await asyncio.sleep(3)

    loaded = {}
    checks = {
        'app_yaml': False,
        'compile_ok': True,
        'frontend_ok': False,
    }

    for path in saved:
        p = Path(path)
        if p.name == 'app.yaml':
            checks['app_yaml'] = True
        if p.suffix == '.py':
            route = str(p.relative_to('plugins')).replace('\\', '/').replace('.py', '')
            loaded[route] = route in core.plugins
            if not _check_compile(str(p)):
                checks['compile_ok'] = False
        if p.as_posix().startswith('www/') and p.name == 'index.html':
            checks['frontend_ok'] = True

    envelop.payload = {
        'ok': True,
        'saved': saved,
        'count': len(saved),
        'errors': errors if errors else [],
        'loaded': loaded,
        'checks': checks,
    }
    return envelop
