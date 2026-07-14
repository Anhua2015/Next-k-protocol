"""Tiny JSON HTTP helper for Bitget when Node TLS is blocked (ECONNRESET).

Supports HTTPS_PROXY / HTTP_PROXY / BITGET_PROXY via urllib ProxyHandler.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    raw = sys.stdin.read()
    req = json.loads(raw)
    method = req.get("method", "GET").upper()
    url = req["url"]
    headers = req.get("headers") or {}
    body = req.get("body")
    data = None if body is None else body.encode("utf-8")
    timeout = float(req.get("timeoutSec") or 20)
    proxy = (req.get("proxy") or os.environ.get("BITGET_PROXY") or os.environ.get("HTTPS_PROXY")
             or os.environ.get("HTTP_PROXY") or os.environ.get("EXTENDED_PROXY") or "").strip()

    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            opener = urllib.request.build_opener(handler)
            resp_cm = opener.open(r, timeout=timeout)
        else:
            resp_cm = urllib.request.urlopen(r, timeout=timeout)
        with resp_cm as resp:
            text = resp.read().decode("utf-8", errors="replace")
            out = {"status": resp.status, "text": text}
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        out = {"status": e.code, "text": text, "error": str(e)}
    except Exception as e:
        out = {"status": 0, "text": "", "error": str(e)}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
