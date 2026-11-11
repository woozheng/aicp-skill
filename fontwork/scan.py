
import os, glob, json
root = r"E:\aicp-IDF\lcd_qemu_rgb_panel\main"
chars = set()
files = []
for ext in ("*.c", "*.h"):
    for p in glob.glob(os.path.join(root, "**", ext), recursive=True):
        files.append(p)
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            t = f.read()
        for ch in t:
            if ord(ch) > 127:
                chars.add(ch)
print(json.dumps({"files": len(files), "chars": "".join(sorted(chars)), "count": len(chars)}, ensure_ascii=False))
