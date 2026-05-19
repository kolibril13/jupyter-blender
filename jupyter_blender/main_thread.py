"""Pump the kernel's asyncio event loop on Blender's main thread.

`IPKernelApp.kernel.start()` doesn't block — the kernel's IO/ZMQ work
runs on an asyncio loop that needs to be iterated to make progress. We
drive that loop from a `bpy.app.timers` callback so messages from
JupyterLab (cell execution requests, etc.) get processed.

Pumping on the main thread means cell code also executes on the main
thread, which is what makes `bpy` calls safe inside notebook cells
without any extra cross-thread bridging.
"""
from __future__ import annotations

import asyncio
import threading

import bpy

_PUMP_INTERVAL = 0.016  # ~60 Hz — matches Blender's redraw cadence
_TIMER_REGISTERED = False
_LOCK = threading.Lock()


def _pump_callback() -> float | None:
    """Iterate the kernel's asyncio loop once.

    `loop.call_soon(loop.stop)` schedules a stop on the next iteration,
    then `run_forever` blocks until that stop fires — effectively a
    single tick. Errors are swallowed so a momentary loop hiccup doesn't
    take the timer down.
    """
    if not _TIMER_REGISTERED:
        return None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return _PUMP_INTERVAL

    try:
        loop.call_soon(loop.stop)
        loop.run_forever()
    except (RuntimeError, OSError):
        pass
    return _PUMP_INTERVAL


def ensure_registered() -> None:
    """Start the event-loop pump. Must be called from the main thread."""
    global _TIMER_REGISTERED
    with _LOCK:
        if _TIMER_REGISTERED:
            return
        bpy.app.timers.register(
            _pump_callback,
            first_interval=0.0,
            persistent=True,
        )
        _TIMER_REGISTERED = True


def unregister() -> None:
    """Stop the event-loop pump. Safe to call from the main thread."""
    global _TIMER_REGISTERED
    with _LOCK:
        if not _TIMER_REGISTERED:
            return
        try:
            bpy.app.timers.unregister(_pump_callback)
        except (ValueError, RuntimeError):
            pass
        _TIMER_REGISTERED = False
