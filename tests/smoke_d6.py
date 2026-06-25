"""D6 smoke: async runner active-cancellation + timeout + normal run.

Run: python tests/smoke_d6.py  (from repo root)
Exercises run_command_async without needing the MCP/transport stack.
"""
from __future__ import annotations

import asyncio
import sys
import time

from src.exec.runner import run_command_async

PY = "python" if sys.platform == "win32" else "python3"


async def test_normal() -> None:
    r = await run_command_async(PY, ["-c", "print('hello-d6')"], timeout=30)
    assert r["exit_code"] == 0, r
    assert "hello-d6" in r["stdout"], r
    assert r["timed_out"] is False, r
    print("OK normal:", r["stdout"].strip(), f'({r["duration_s"]}s)')


async def test_timeout() -> None:
    start = time.monotonic()
    r = await run_command_async(PY, ["-c", "import time; time.sleep(30)"], timeout=2)
    elapsed = time.monotonic() - start
    assert r["timed_out"] is True, r
    assert elapsed < 8, f"timeout did not kill promptly: {elapsed:.1f}s"
    print(f"OK timeout: killed after {elapsed:.2f}s (timeout=2, sleep=30)")


async def test_cancel() -> None:
    start = time.monotonic()
    task = asyncio.create_task(
        run_command_async(PY, ["-c", "import time; time.sleep(30)"], timeout=600)
    )
    await asyncio.sleep(0.7)  # let the child actually start
    task.cancel()
    try:
        await task
        raise AssertionError("expected CancelledError to propagate")
    except asyncio.CancelledError:
        pass
    elapsed = time.monotonic() - start
    assert elapsed < 8, f"cancel did not kill promptly: {elapsed:.1f}s"
    print(f"OK cancel: child killed + CancelledError re-raised after {elapsed:.2f}s")


async def main() -> None:
    await test_normal()
    await test_timeout()
    await test_cancel()
    print("\nD6 SMOKE: 3/3 PASS")


if __name__ == "__main__":
    asyncio.run(main())
