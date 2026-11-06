"""mem_probe — 查看当前 AICP server 进程的内存与模块构成。

用法：
    use_tool(target="mytools/mem_probe", action="", plugin_args={})
"""
import sys

HEAVY = ["torch", "numpy", "pandas", "openai", "httpx", "requests",
         "aiohttp", "trio", "bs4", "lxml", "cv2", "PIL", "matplotlib",
         "skimage", "onnxruntime", "chromadb", "rapidocr_onnxruntime"]


def _collect():
    out = {}

    try:
        import psutil
        me = psutil.Process()
        m = me.memory_info()
        out["pid"] = me.pid
        out["rss_mb"] = round(m.rss / 1048576.0, 1)
        out["vms_mb"] = round(m.vms / 1048576.0, 1)
        try:
            out["num_threads"] = me.num_threads()
        except Exception:
            pass
    except Exception as e:
        out["mem_err"] = repr(e)[:200]

    out["nmods"] = len(sys.modules)
    tops = {}
    for name in sys.modules:
        top = name.split(".")[0]
        if top and not top.startswith("_"):
            tops[top] = tops.get(top, 0) + 1
    out["top_trees"] = sorted(tops.items(), key=lambda x: -x[1])[:15]
    out["heavy_present"] = [k for k in HEAVY if k in sys.modules]
    out["torch_submodules"] = len([x for x in sys.modules if x.split(".")[0] == "torch"])

    try:
        import gc
        gc.collect()
        import psutil
        m2 = psutil.Process().memory_info()
        out["rss_mb_after_gc"] = round(m2.rss / 1048576.0, 1)
    except Exception:
        pass

    return out


async def execute(envelop, agent):
    try:
        envelop.payload = {"ok": True, "data": _collect()}
    except Exception as e:
        envelop.payload = {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}
    return envelop