"""Blender extension entry point.

Registers the N-panel, preferences and operators, plus a small button
in the 3D viewport header for quick access.
"""
from __future__ import annotations

import time

import bpy

from .preferences import (
    CopyJupyterURL,
    InstallPythonModule,
    InstallPythonModules,
    JupyterAddonPreferences,
    ListPythonModules,
    StartJupyterServer,
    StartJupyterServerHeadless,
    StartWithExample,
    StopJupyterServer,
    UninstallPythonModules,
    draw_preferences,
)


def _header_btn(self: bpy.types.Menu, context: bpy.types.Context) -> None:
    self.layout.operator(
        StartJupyterServer.bl_idname, icon="CONSOLE", text=""
    )


class JUPYTER_PT_main_panel(bpy.types.Panel):
    bl_label = "Notebook"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Jupyter"

    def draw(self, context: bpy.types.Context) -> None:
        prefs = context.preferences.addons[__package__].preferences
        draw_preferences(self.layout, prefs)


_CLASSES = (
    JUPYTER_PT_main_panel,
    JupyterAddonPreferences,
    InstallPythonModules,
    InstallPythonModule,
    UninstallPythonModules,
    ListPythonModules,
    StartJupyterServer,
    StartJupyterServerHeadless,
    StartWithExample,
    StopJupyterServer,
    CopyJupyterURL,
)

# Max seconds to wait for the JupyterLab subprocess to fully terminate
# before the addon unregister returns.
_SERVER_STOP_TIMEOUT = 5.0
_SERVER_DRAIN_INTERVAL = 0.05


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_HT_header.append(_header_btn)


def unregister() -> None:
    from . import addon_setup, main_thread

    try:
        bpy.types.VIEW3D_HT_header.remove(_header_btn)
    except (ValueError, RuntimeError):
        pass

    for cls in reversed(_CLASSES):
        if hasattr(cls, "bl_rna"):
            try:
                bpy.utils.unregister_class(cls)
            except RuntimeError:
                pass

    # If the server is running, stop it and give the subprocess a bounded
    # window to exit. Blender's main thread is blocked here so the
    # ``bpy.app.timers`` pump can't fire — but we don't strictly need it
    # for shutdown: subprocess wait + IPKernelApp.close() are both
    # synchronous.
    if addon_setup.server.is_running:
        addon_setup.server.stop()
        deadline = time.monotonic() + _SERVER_STOP_TIMEOUT
        while addon_setup.server.is_running and time.monotonic() < deadline:
            time.sleep(_SERVER_DRAIN_INTERVAL)

    main_thread.unregister()
