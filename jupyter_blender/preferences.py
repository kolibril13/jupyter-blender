"""Preferences, operators and the N-panel ``draw_preferences`` view.

UI is laid out the same way as marimo-blender:

- A *Launch* sub-panel (network options + start/stop / open-browser /
  URL copy actions).
- A *Dependencies* sub-panel (install / uninstall / list / per-module
  install + a collapsible log box).

The operators are thin wrappers around ``addon_setup.installer`` and
``addon_setup.server``.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import bpy

from . import addon_setup

_EXAMPLES_DIR = os.path.join(os.path.dirname(__file__), "examples")

# Lines streamed back from installer / server subprocesses for the log
# box. Carriage-return prefixed lines overwrite the previous entry, the
# same trick pip's progress bar relies on.
_LINES: list[str] = []


def _lines_append(line: str) -> None:
    if line.startswith("\r") and len(_LINES) > 0:
        del _LINES[-1]
        line = line[1:]
    _LINES.append(line)


def _connection_file_dir() -> Path:
    """Per-user, per-extension directory for the kernel connection file."""
    return Path(
        bpy.utils.extension_path_user(
            __package__,
            path="connection_cache",
            create=True,
        )
    )


def _resolve_notebook_dir(raw_dir: str) -> Path:
    """Expand a user-supplied notebook root to an absolute path.

    Falls back to the user home directory if ``raw_dir`` is empty,
    matching marimo-blender's behaviour when launched from the Dock with
    ``cwd=/``.
    """
    if not raw_dir:
        return Path(os.path.expanduser("~"))
    return Path(bpy.path.abspath(raw_dir)).resolve()


# ============================================================ #
# Operators: Dependencies                                      #
# ============================================================ #
class InstallPythonModules(bpy.types.Operator):
    """Install Python dependencies required by the Jupyter notebook server."""

    bl_idname = "jupyter_blender.install_python_modules"
    bl_label = "Install Python Modules"
    bl_options = {"REGISTER", "INTERNAL"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return not addon_setup.installer.is_running

    def execute(self, context: bpy.types.Context) -> set[str]:
        _LINES.clear()
        region = context.region
        addon_setup.installer.install_python_modules(
            line_callback=lambda line: _lines_append(line) or region.tag_redraw(),
            finally_callback=lambda e: region.tag_redraw(),
        )
        return {"FINISHED"}


class InstallPythonModule(bpy.types.Operator):
    """Install an arbitrary Python module into Blender's site-packages."""

    bl_idname = "jupyter_blender.install_python_module"
    bl_label = "Install Python Module"
    bl_options = {"REGISTER", "INTERNAL"}

    module_name: bpy.props.StringProperty(name="Module Name", default="")

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return not addon_setup.installer.is_running

    def execute(self, context: bpy.types.Context) -> set[str]:
        _LINES.clear()
        region = context.region
        addon_setup.installer.install_python_module(
            self.module_name,
            line_callback=lambda line: _lines_append(line) or region.tag_redraw(),
            finally_callback=lambda e: region.tag_redraw(),
        )
        return {"FINISHED"}


class UninstallPythonModules(bpy.types.Operator):
    """Uninstall the Python dependencies installed by this addon."""

    bl_idname = "jupyter_blender.uninstall_python_modules"
    bl_label = "Uninstall Python Modules"
    bl_options = {"REGISTER", "INTERNAL"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return not addon_setup.installer.is_running

    def execute(self, context: bpy.types.Context) -> set[str]:
        _LINES.clear()
        region = context.region
        addon_setup.installer.uninstall_python_modules(
            line_callback=lambda line: _lines_append(line) or region.tag_redraw(),
            finally_callback=lambda e: region.tag_redraw(),
        )
        return {"FINISHED"}


class ListPythonModules(bpy.types.Operator):
    """List all Python modules currently visible to Blender's interpreter."""

    bl_idname = "jupyter_blender.list_python_modules"
    bl_label = "List Python Modules"
    bl_options = {"REGISTER", "INTERNAL"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return not addon_setup.installer.is_running

    def execute(self, context: bpy.types.Context) -> set[str]:
        _LINES.clear()
        region = context.region
        addon_setup.installer.list_python_modules(
            line_callback=lambda line: _lines_append(line) or region.tag_redraw(),
            finally_callback=lambda e: region.tag_redraw(),
        )
        return {"FINISHED"}


# ============================================================ #
# Operators: Server                                            #
# ============================================================ #
class StartJupyterServer(bpy.types.Operator):
    """Start the Jupyter notebook server and open it in a browser."""

    bl_idname = "jupyter_blender.start_server_or_open_browser"
    bl_label = "Start Notebook Server"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return not addon_setup.installer.is_running

    def execute(self, context: bpy.types.Context) -> set[str]:
        prefs = context.preferences.addons[__package__].preferences

        if addon_setup.server.is_running:
            addon_setup.server.open_browser()
            return {"FINISHED"}

        _LINES.clear()
        region = context.region
        notebook_dir = _resolve_notebook_dir(prefs.notebook_dir)

        try:
            addon_setup.server.start(
                host=prefs.host,
                port=prefs.port,
                notebook_dir=notebook_dir,
                connection_file_dir=_connection_file_dir(),
                launch_browser=True,
                line_callback=lambda line: _lines_append(line)
                or region.tag_redraw(),
                finally_callback=lambda s: region.tag_redraw(),
            )
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Failed to start Jupyter: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class StartJupyterServerHeadless(bpy.types.Operator):
    """Start the Jupyter server without opening a browser.

    Copies the API URL (``http://host:port/?token=...``) to the
    clipboard so it can be pasted into VS Code's "Jupyter: Specify
    Jupyter Server for Connections" prompt.
    """

    bl_idname = "jupyter_blender.start_server_headless"
    bl_label = "Start Headless"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return (
            not addon_setup.installer.is_running
            and not addon_setup.server.is_running
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        prefs = context.preferences.addons[__package__].preferences

        _LINES.clear()
        region = context.region
        notebook_dir = _resolve_notebook_dir(prefs.notebook_dir)

        try:
            addon_setup.server.start(
                host=prefs.host,
                port=prefs.port,
                notebook_dir=notebook_dir,
                connection_file_dir=_connection_file_dir(),
                launch_browser=False,
                line_callback=lambda line: _lines_append(line)
                or region.tag_redraw(),
                finally_callback=lambda s: region.tag_redraw(),
            )
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Failed to start Jupyter: {exc}")
            return {"CANCELLED"}

        url = addon_setup.server.jupyter_api_url()
        if url:
            context.window_manager.clipboard = url
            self.report({"INFO"}, f"Jupyter server URL copied to clipboard: {url}")
        else:
            self.report({"WARNING"}, "Server started but URL is not available.")
        return {"FINISHED"}


class StartWithExample(bpy.types.Operator):
    """Start the server with a bundled example notebook's directory as root."""

    bl_idname = "jupyter_blender.start_with_example"
    bl_label = "Start with Example"
    bl_options = {"REGISTER", "INTERNAL"}

    filepath: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return (
            not addon_setup.installer.is_running
            and not addon_setup.server.is_running
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        prefs = context.preferences.addons[__package__].preferences
        example_path = Path(self.filepath).resolve()
        # JupyterLab's notebook-dir is a directory, so we point it at the
        # examples folder and use ``--ServerApp.default_url`` to land the
        # browser directly on the example .ipynb.
        prefs.notebook_dir = str(example_path.parent)
        default_url = f"/lab/tree/{quote(example_path.name)}"

        _LINES.clear()
        region = context.region
        try:
            addon_setup.server.start(
                host=prefs.host,
                port=prefs.port,
                notebook_dir=example_path.parent,
                connection_file_dir=_connection_file_dir(),
                launch_browser=True,
                default_url=default_url,
                line_callback=lambda line: _lines_append(line)
                or region.tag_redraw(),
                finally_callback=lambda s: region.tag_redraw(),
            )
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Failed to start Jupyter: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class StopJupyterServer(bpy.types.Operator):
    """Stop the running Jupyter notebook server and kernel."""

    bl_idname = "jupyter_blender.stop_server"
    bl_label = "Stop Notebook Server"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return addon_setup.server.is_running

    def execute(self, context: bpy.types.Context) -> set[str]:
        addon_setup.server.stop()
        return {"FINISHED"}


class CopyJupyterURL(bpy.types.Operator):
    """Copy a Jupyter server URL (Lab UI or REST API root) to the clipboard."""

    bl_idname = "jupyter_blender.copy_url"
    bl_label = "Copy URL"
    bl_options = {"REGISTER", "INTERNAL"}

    url_type: bpy.props.EnumProperty(
        name="URL Type",
        items=[
            ("LAB", "Lab", "JupyterLab UI URL"),
            ("API", "API", "Jupyter REST API root URL"),
        ],
        default="LAB",
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return addon_setup.server.is_running

    def execute(self, context: bpy.types.Context) -> set[str]:
        url = (
            addon_setup.server.jupyter_lab_url()
            if self.url_type == "LAB"
            else addon_setup.server.jupyter_api_url()
        )
        if not url:
            self.report({"WARNING"}, "Server URL is not available.")
            return {"CANCELLED"}
        context.window_manager.clipboard = url
        self.report({"INFO"}, f"Copied {self.url_type} URL to clipboard.")
        return {"FINISHED"}


# ============================================================ #
# Draw                                                         #
# ============================================================ #
def draw_preferences(
    layout: bpy.types.UILayout, prefs: "JupyterAddonPreferences"
) -> None:
    modules = addon_setup.installer.get_required_modules()
    all_installed = all(modules.values())
    is_running = addon_setup.server.is_running

    # ── Launch ─────────────────────────────────────────────── #
    launch_header, launch_body = layout.panel(
        "jupyter_blender_launch", default_closed=False
    )
    launch_header.label(text="Launch", icon="PLAY")
    if launch_body is not None:
        dir_row = launch_body.row(align=True)
        dir_row.prop(prefs, "notebook_dir", text="", icon="FILE_FOLDER")

        if is_running:
            launch_body.label(
                text=(
                    f"Server running on "
                    f"http://{addon_setup.server.host}:{addon_setup.server.port}"
                ),
                icon="RADIOBUT_ON",
            )
            actions = launch_body.row(align=True)
            actions.scale_y = 1.4
            if not addon_setup.server.is_headless:
                actions.operator(
                    StartJupyterServer.bl_idname, icon="URL", text="Open in Browser"
                )
            actions.operator(
                StopJupyterServer.bl_idname, icon="X", text="Stop"
            )

            if addon_setup.server.is_headless:
                url_row = launch_body.row(align=True)
                op = url_row.operator(
                    CopyJupyterURL.bl_idname, icon="COPYDOWN", text="Copy Server URL"
                )
                op.url_type = "API"
        else:
            big = launch_body.row(align=True)
            big.scale_y = 1.4
            big.enabled = all_installed
            big.operator(
                StartJupyterServer.bl_idname,
                icon="URL",
                text="Start Notebook Server",
            )
            big.operator(
                StartJupyterServerHeadless.bl_idname,
                icon="CONSOLE",
                text="Start Headless",
            )

            empty_path = os.path.join(_EXAMPLES_DIR, "empty.ipynb")
            if os.path.exists(empty_path):
                empty_row = launch_body.row(align=True)
                empty_row.scale_y = 1.4
                empty_row.enabled = all_installed
                op = empty_row.operator(
                    StartWithExample.bl_idname,
                    icon="FILE",
                    text="Open Empty Notebook",
                )
                op.filepath = empty_path

            example_path = os.path.join(_EXAMPLES_DIR, "data_to_geometry.ipynb")
            if os.path.exists(example_path):
                example_row = launch_body.row(align=True)
                example_row.enabled = all_installed
                op = example_row.operator(
                    StartWithExample.bl_idname,
                    icon="FILE_SCRIPT",
                    text="Example: Data to Geometry",
                )
                op.filepath = example_path

            if not all_installed:
                launch_body.label(text="Install dependencies first ↓", icon="INFO")

    # ── Dependencies ───────────────────────────────────────── #
    deps_header, deps_body = layout.panel(
        "jupyter_blender_dependencies", default_closed=all_installed
    )
    deps_header.label(
        text="Dependencies",
        icon="CHECKMARK" if all_installed else "ERROR",
    )
    if deps_body is not None:
        install_row = deps_body.row()
        install_row.alert = not all_installed
        install_row.operator(InstallPythonModules.bl_idname, icon="PREFERENCES")

        deps_body.label(text="Required Python Modules:")
        flow = deps_body.row(align=True).grid_flow(align=True)
        for name, is_installed in modules.items():
            flow.row().label(
                text=name, icon="CHECKMARK" if is_installed else "ERROR"
            )

        row = deps_body.row()
        row.operator(UninstallPythonModules.bl_idname, text="Uninstall")
        row.operator(ListPythonModules.bl_idname, text="List Modules")

        custom_row = deps_body.row(align=True)
        custom_row.operator(
            InstallPythonModule.bl_idname,
            icon="PLUS",
            text="pip install",
        ).module_name = prefs.module_name
        custom_row.prop(prefs, "module_name", text="")

        # Logs (collapsible)
        col = deps_body.column(align=False)
        log_row = col.row(align=True)
        log_row.prop(
            prefs,
            "show_logs",
            icon="TRIA_DOWN" if prefs.show_logs else "TRIA_RIGHT",
            icon_only=True,
            emboss=False,
        )
        log_row.label(text="Logs")
        exit_code = addon_setup.installer.exit_code
        if addon_setup.installer.is_running:
            log_row.label(text="Processing ...", icon="SORTTIME")
        elif exit_code >= 0:
            log_row.label(
                text=f"Done with code: {exit_code}",
                icon="CHECKMARK" if exit_code == 0 else "ERROR",
            )

        if prefs.show_logs:
            box = col.box().column(align=True)
            for line in _LINES:
                box.label(text=line)


# ============================================================ #
# Preferences                                                  #
# ============================================================ #
class JupyterAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    host: bpy.props.StringProperty(
        name="Host",
        description="IP address JupyterLab binds to (use 127.0.0.1 to keep it local)",
        default="127.0.0.1",
    )
    port: bpy.props.IntProperty(
        name="Port",
        description="Port the JupyterLab server listens on",
        default=10462,
        min=1024,
        max=49151,
    )
    notebook_dir: bpy.props.StringProperty(
        name="Notebook Root",
        description="Folder JupyterLab opens as its root. Leave empty for the home directory.",
        default="",
        subtype="DIR_PATH",
    )
    show_logs: bpy.props.BoolProperty(default=False)
    module_name: bpy.props.StringProperty(name="Module Name", default="")

    def draw(self, context: bpy.types.Context) -> None:
        self.layout.label(
            text="Settings available in 3D View > Sidebar (N) > Jupyter",
            icon="INFO",
        )
