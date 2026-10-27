"""签发 HMAC 自包含 token，与 os/_auth 共享 BIO_SECRET。

用法（Windows CMD）：
    set BIO_SECRET=your-secret
    python scripts\issue_token.py u_friend 30
    python scripts\issue_token.py u_friend 30 "{\"*\":true}"

用法（PowerShell / Linux / macOS）：
    $env:BIO_SECRET = "your-secret"          # PowerShell
    export BIO_SECRET=your-secret            # bash/zsh
    python scripts/issue_token.py u_friend 30
"""

import sys
import os
import time
import json
import hmac
import hashlib
import base64


def _secret() -> str:
    return os.environ.get("BIO_SECRET", "change-me-in-prod")


def issue_hmac_token(user_id: str, ttl_seconds: int = 30 * 86400, perms=None) -> str:
    """签发 HMAC 自包含 token。

    token 结构：base64url(payload).base64url(hmac_sha256(payload))
    payload: {"uid": "...", "exp": <unix_ts>, "perms": {...}?}
    """
    body = {"uid": user_id, "exp": int(time.time()) + ttl_seconds}
    if perms:
        body["perms"] = perms
    raw = base64.urlsafe_b64encode(
        json.dumps(body, separators=(",", ":")).encode()
    ).rstrip(b"=")
    sig = base64.urlsafe_b64encode(
        hmac.new(_secret().encode(), raw, hashlib.sha256).digest()
    ).rstrip(b"=")
    return f"{raw.decode()}.{sig.decode()}"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    uid = sys.argv[1]
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    perms = None
    if len(sys.argv) > 3:
        try:
            perms = json.loads(sys.argv[3])
        except json.JSONDecodeError as e:
            print(f"[ERROR] 第 3 个参数不是合法 JSON: {e}", file=sys.stderr)
            sys.exit(1)

    if not os.environ.get("BIO_SECRET"):
        print(
            "[WARN] BIO_SECRET 未设置，使用默认值 'change-me-in-prod'"
            "（仅本地调试可用，生产环境务必设置）",
            file=sys.stderr,
        )

    token = issue_hmac_token(uid, ttl_seconds=days * 86400, perms=perms)
    print(token)


if __name__ == "__main__":
    main()
