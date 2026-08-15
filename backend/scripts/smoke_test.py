"""Smoke test: boots the backend and hits the core endpoints.

Run from the backend/ directory with the venv active:
    python scripts/smoke_test.py
"""
from __future__ import annotations

import subprocess
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"


def main() -> None:
    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_ready()
        print("health        :", _get("/api/health"))

        s = _get("/api/system/status")
        print("system        : cpu%=", s.get("cpu_percent"), " mem%=", s["memory"]["percent"], " disks=", len(s["disk"]))

        p = _get("/api/system/processes", params={"sort": "cpu"})
        print("processes     :", len(p), "rows")

        q = _get("/api/quant/status")
        print("quant         : overall=", q.get("overall"), " red_lines=", len(q.get("red_lines", [])))

        f = _get("/api/files/search", params={"query": "test"})
        print("files search  : ok=", f.get("ok"), " error=", f.get("error"))

        a = _get("/api/apps/list")
        print("apps          :", type(a).__name__, "entries=", len(a) if isinstance(a, list) else "?")

        st = _get("/api/settings")
        print("settings      :", len(st), "keys")

        t = _get("/api/ai/tools")
        print("ai tools      :", len(t), "registered")

        qp = _get("/api/quant/processes")
        print("quant procs   :", len(qp), "matched")

        qc = _get("/api/quant/commands")
        print("quant cmds    :", len(qc), "commands")

        cs = _get("/api/claude/status")
        print("claude status :", cs)

        rs = _get("/api/research/search", params={"query": "test"})
        print("research      : ok=", rs.get("ok"), " results=", len(rs.get("results", [])))

        # confirmation flow
        r = httpx.post(f"{BASE}/api/confirmations", json={"action": "test", "title": "test op", "details": {"x": 1}}).json()
        cid = r["confirmation_id"]
        appr = httpx.post(f"{BASE}/api/confirmations/{cid}/approve").json()
        print("confirm flow  : created=%s approved=%s" % (cid, appr.get("ok")))

        # ai chat without a configured key should degrade gracefully
        try:
            ai = httpx.post(f"{BASE}/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]}, timeout=30).json()
            print("ai chat       : content_len=%d needs_confirm=%s" % (len(ai.get("content", "")), ai.get("needs_confirmation")))
        except Exception as exc:  # noqa: BLE001
            print("ai chat       : error", exc)

        print("SMOKE_TEST_OK")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _wait_ready() -> None:
    for _ in range(40):
        try:
            if httpx.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("backend did not become ready")


def _get(path: str, params: dict | None = None) -> dict | list:
    r = httpx.get(f"{BASE}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    main()
