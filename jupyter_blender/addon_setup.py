"""Runtime services: dependency installer + Jupyter kernel/server lifecycle.

Two singletons live here:

- ``installer`` — wraps ``pip`` to manage the Jupyter dependency set in
  Blender's extension site-packages. Modeled after marimo-blender's
  Installer.

- ``server`` — manages an in-process ``IPKernelApp`` plus a JupyterLab
  subprocess that connects to it through pyxll-jupyter's "existing
  kernel" provisioner. Architecture is ported from bpy_jupyter v2.1.

Running the kernel in-process is what makes ``import bpy`` work in
notebook cells: the ``_bpy`` C extension only exists inside the Blender
executable, so a subprocess kernel (Jupyter's default) cannot use it.
JupyterLab itself is still a subprocess — it doesn't need ``bpy``, only
the kernel does.
"""
from __future__ import annotations

import logging
import os
import pkgutil
import secrets
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional


def _invoke_callback(callback: Optional[Callable], *args: Any) -> None:
    if callback is None:
        return
    try:
        callback(*args)
    except Exception as exc:  # noqa: BLE001
        logging.exception("Callback failed:", exc_info=exc)


class Executor:
    """Run a function or a subprocess in a daemon thread, with line-by-line
    stdout callback and a finally callback."""

    def __init__(self) -> None:
        self._is_running = False
        self._return_value: Any = None
        self._exception: Optional[Exception] = None
        self._process: Optional[subprocess.Popen] = None
        self._exit_code = -1
        self._command_line = ""

    def exec_function(
        self,
        function: Callable[..., Any],
        *args: Any,
        line_callback: Optional[Callable[[str], None]] = None,
        finally_callback: Optional[Callable[["Executor"], Any]] = None,
    ) -> None:
        def _run_background() -> None:
            try:
                self._return_value = function(*args)
            except Exception as exception:  # noqa: BLE001
                self._exception = exception
                self.write_exception(exception, line_callback=line_callback)
            finally:
                self._is_running = False
                _invoke_callback(finally_callback, self)

        self._is_running = True
        self._return_value = None
        self._exception = None

        thread = threading.Thread(target=_run_background, daemon=True)
        thread.start()

    @staticmethod
    def write_exception(
        exception: Exception,
        line_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        if exception is None:
            return
        for line in (
            line
            for frame in traceback.format_exception(exception)
            for line in frame.splitlines()
        ):
            _invoke_callback(line_callback, line)

    def exec_command(
        self,
        *args: str,
        line_callback: Optional[Callable[[str], None]] = None,
        finally_callback: Optional[Callable[["Executor"], Any]] = None,
    ) -> None:
        if self.is_running:
            raise ValueError(f"Process is running: pid={self._process.pid}")

        self._exit_code = -1
        self._command_line = " ".join(args)
        self._process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )

        def _enqueue_output() -> None:
            encoding = sys.getdefaultencoding()
            assert self._process is not None
            input_text_io = self._process.stdout
            assert input_text_io is not None

            while self._process.poll() is None:
                for buffer in iter(input_text_io.readline, b""):
                    text = buffer.decode(encoding).rstrip()
                    _invoke_callback(line_callback, text)

            input_text_io.close()
            self._exit_code = self._process.poll()
            self._process = None

        self.exec_function(_enqueue_output, finally_callback=finally_callback)

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def return_value(self) -> Any:
        return self._return_value

    @property
    def exception(self) -> Optional[Exception]:
        return self._exception

    @property
    def command_line(self) -> str:
        return self._command_line

    @property
    def exit_code(self) -> int:
        return self._exit_code


class Installer(Executor):
    """Install/uninstall Jupyter dependencies into Blender's site-packages.

    pip is invoked with ``--target=<site-packages>`` so wheels end up in
    a location already on Blender's ``sys.path``. The set of top-level
    deps is intentionally small; pip resolves the transitive closure
    (ipykernel, jupyter_client, tornado, …).
    """

    # Top-level pip distributions. Order matters only for display.
    dependencies: list[str] = [
        "jupyterlab",
        "ipykernel",
        "jupyter-client",
        "pyxll-jupyter",
        "ipywidgets",
        "anywidget",
    ]

    # pip dist name → importable module name (where they differ).
    _DIST_TO_MODULE: dict[str, str] = {
        "jupyter-client": "jupyter_client",
        "pyxll-jupyter": "pyxll_jupyter",
    }

    def _module_name(self, dist: str) -> str:
        return self._DIST_TO_MODULE.get(dist, dist)

    def get_required_modules(self) -> dict[str, bool]:
        modules = {d: False for d in self.dependencies}
        installed_top_levels = {m.name for m in pkgutil.iter_modules()}
        for dist in modules:
            if self._module_name(dist) in installed_top_levels:
                modules[dist] = True
        return modules

    @staticmethod
    def _site_packages_path() -> Optional[str]:
        return next((p for p in sys.path if p.endswith("site-packages")), None)

    def install_python_modules(
        self,
        line_callback: Optional[Callable[[str], None]] = None,
        finally_callback: Optional[Callable[["Executor"], Any]] = None,
    ) -> None:
        site_packages_path = self._site_packages_path()
        target_option = (
            ["--target", site_packages_path] if site_packages_path else []
        )

        missing = [
            name
            for name, installed in self.get_required_modules().items()
            if not installed
        ]
        if not missing:
            # Still run pip so the user gets feedback in the log box.
            missing = list(self.dependencies)

        self.exec_command(
            sys.executable, "-m", "ensurepip",
            line_callback=line_callback,
            finally_callback=lambda e: e.exec_command(
                sys.executable, "-m", "pip", "install",
                *target_option,
                "--disable-pip-version-check",
                "--no-input",
                "--exists-action", "i",
                "--upgrade",
                *missing,
                line_callback=line_callback,
                finally_callback=finally_callback,
            ),
        )

    def install_python_module(
        self,
        module_name: str,
        line_callback: Optional[Callable[[str], None]] = None,
        finally_callback: Optional[Callable[["Executor"], Any]] = None,
    ) -> None:
        site_packages_path = self._site_packages_path()
        target_option = (
            ["--target", site_packages_path] if site_packages_path else []
        )
        self.exec_command(
            sys.executable, "-m", "pip", "install",
            *target_option,
            "--disable-pip-version-check",
            "--no-input",
            "--exists-action", "i",
            *module_name.split(),
            line_callback=line_callback,
            finally_callback=finally_callback,
        )

    def uninstall_python_modules(
        self,
        line_callback: Optional[Callable[[str], None]] = None,
        finally_callback: Optional[Callable[["Executor"], Any]] = None,
    ) -> None:
        installed = [
            name
            for name, is_installed in self.get_required_modules().items()
            if is_installed
        ]
        if not installed:
            _invoke_callback(line_callback, "No installed dependencies to remove.")
            _invoke_callback(finally_callback, self)
            return
        self.exec_command(
            sys.executable, "-m", "pip", "uninstall",
            "--yes",
            *installed,
            line_callback=line_callback,
            finally_callback=finally_callback,
        )

    def list_python_modules(
        self,
        line_callback: Optional[Callable[[str], None]] = None,
        finally_callback: Optional[Callable[["Executor"], Any]] = None,
    ) -> None:
        self.exec_command(
            sys.executable, "-m", "pip", "list", "-v",
            line_callback=line_callback,
            finally_callback=finally_callback,
        )


class Server:
    """In-process IPython kernel + JupyterLab subprocess.

    The kernel runs inside Blender so cells can ``import bpy``. JupyterLab
    is launched as a subprocess and pointed at the kernel's connection
    file via pyxll-jupyter's ``pyxll-provisioner`` — a Jupyter kernel
    provisioner that attaches to an already-running kernel instead of
    spawning a new one.
    """

    _LOCK = threading.Lock()

    def __init__(self) -> None:
        self._kernel: Any = None  # IPKernelApp
        self._jupyter_proc: Optional[subprocess.Popen] = None
        self._connection_file: Optional[Path] = None
        self._token: Optional[str] = None
        self._host: Optional[str] = None
        self._port: Optional[int] = None
        self._lines_thread: Optional[threading.Thread] = None
        self._launch_browser: bool = True

    # ------------------------------------------------------------------ #
    # State                                                              #
    # ------------------------------------------------------------------ #
    @property
    def is_running(self) -> bool:
        with self._LOCK:
            return self._kernel is not None

    @property
    def port(self) -> Optional[int]:
        return self._port

    @property
    def host(self) -> Optional[str]:
        return self._host

    @property
    def token(self) -> Optional[str]:
        return self._token

    # ------------------------------------------------------------------ #
    # URLs                                                               #
    # ------------------------------------------------------------------ #
    def jupyter_lab_url(self) -> Optional[str]:
        if self._token is None or self._host is None or self._port is None:
            return None
        return f"http://{self._host}:{self._port}/lab?token={self._token}"

    def jupyter_api_url(self) -> Optional[str]:
        if self._token is None or self._host is None or self._port is None:
            return None
        return f"http://{self._host}:{self._port}/?token={self._token}"

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    def start(
        self,
        host: str,
        port: int,
        notebook_dir: Path,
        connection_file_dir: Path,
        launch_browser: bool = True,
        line_callback: Optional[Callable[[str], None]] = None,
        finally_callback: Optional[Callable[["Server"], Any]] = None,
    ) -> None:
        """Start the in-process kernel and the JupyterLab subprocess.

        Must run on Blender's main thread (the event-loop pump timer is
        registered here, and ``bpy.app.timers.register`` requires the
        main thread).
        """
        # Lazy imports so missing deps don't break addon registration.
        from ipykernel.kernelapp import IPKernelApp

        from . import main_thread

        with self._LOCK:
            if self._kernel is not None:
                raise ValueError("Jupyter server is already running.")

            connection_file_dir.mkdir(parents=True, exist_ok=True)
            self._connection_file = (
                connection_file_dir / "jupyter-blender-kernel.json"
            )
            self._host = host
            self._port = port
            self._launch_browser = launch_browser
            self._token = secrets.token_urlsafe(32)

            # ------------------------------------------------------- #
            # 1. In-process kernel                                    #
            # ------------------------------------------------------- #
            # IPKernelApp is a Configurable singleton; passing only
            # ``[sys.executable]`` as argv mirrors bpy_jupyter and
            # avoids it parsing Blender's CLI args.
            self._kernel = IPKernelApp.instance(
                connection_file=str(self._connection_file)
            )
            self._kernel.initialize([sys.executable])
            self._kernel.kernel.start()

            # ------------------------------------------------------- #
            # 2. JupyterLab subprocess                                #
            # ------------------------------------------------------- #
            cmd, env = self._build_jupyterlab_cmd(
                notebook_dir=notebook_dir,
                token=self._token,
                host=host,
                port=port,
                launch_browser=launch_browser,
            )

            self._jupyter_proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )

            # Stream JupyterLab logs back to the caller in a daemon thread.
            if line_callback is not None:
                self._lines_thread = threading.Thread(
                    target=self._drain_subprocess_lines,
                    args=(self._jupyter_proc, line_callback),
                    daemon=True,
                )
                self._lines_thread.start()

        # 3. Start the event-loop pump *after* we drop the lock; the
        # callback only reads state that's already published.
        main_thread.ensure_registered()

        if finally_callback is not None:
            _invoke_callback(finally_callback, self)

    def _build_jupyterlab_cmd(
        self,
        *,
        notebook_dir: Path,
        token: str,
        host: str,
        port: int,
        launch_browser: bool,
    ) -> tuple[list[str], dict[str, str]]:
        # JupyterLab is a subprocess; it needs Blender's extension
        # site-packages on its PYTHONPATH so it can find both
        # ``jupyterlab`` and ``pyxll_jupyter`` (whose entry point
        # registers the "pyxll-provisioner" kernel provisioner).
        env = os.environ.copy()
        env["PYXLL_IPYTHON_CONNECTION_FILE"] = str(self._connection_file)

        extra_path_entries: list[str] = []
        for entry in sys.path:
            if entry and entry.endswith("site-packages"):
                extra_path_entries.append(entry)

        # Also include the directory containing the bundled ``jupyter``
        # package so ``python -m jupyterlab`` resolves it.
        try:
            import jupyter

            jupyter_pkg_dir = Path(jupyter.__file__).resolve().parent.parent
            extra_path_entries.append(str(jupyter_pkg_dir))
        except Exception:  # noqa: BLE001
            pass

        if extra_path_entries:
            current = env.get("PYTHONPATH", "")
            joined = os.pathsep.join(extra_path_entries)
            env["PYTHONPATH"] = (
                joined + (os.pathsep + current if current else "")
            )

        # ----- JupyterLab app-dir ------------------------------------
        # JupyterLab's default app-dir is ``<sys.prefix>/share/jupyter/lab``,
        # which in Blender resolves to a directory inside the Blender
        # app bundle that doesn't contain the Lab UI assets. Because we
        # installed JupyterLab with ``pip --target=<site-packages>``,
        # the assets actually live in one of two places:
        #
        #   1. ``<site-packages>/jupyterlab/`` — the wheel bundles
        #      ``static/``, ``schemas/`` and ``themes/`` inside the
        #      package itself.
        #   2. ``<site-packages>/share/jupyter/lab/`` — pip extracts the
        #      wheel's ``*.data/data/share/jupyter/lab/`` payload here.
        #
        # We prefer (1) because it always exists wherever ``jupyterlab``
        # was importable from; (2) is a fallback.
        app_dir = self._discover_lab_app_dir(extra_path_entries)

        # ----- JUPYTER_PATH ------------------------------------------
        # Point jupyter_core at the per-target ``share/`` directories so
        # kernelspecs, lab extensions, etc. are discovered.
        jupyter_path_entries: list[str] = []
        for entry in extra_path_entries:
            share = Path(entry) / "share"
            if share.is_dir():
                jupyter_path_entries.append(str(share))
        if jupyter_path_entries:
            existing = env.get("JUPYTER_PATH", "")
            joined = os.pathsep.join(jupyter_path_entries)
            env["JUPYTER_PATH"] = (
                joined + (os.pathsep + existing if existing else "")
            )

        cmd = [
            sys.executable,
            "-m",
            "jupyterlab",
            f"--ip={host}",
            f"--port={port}",
            f"--notebook-dir={notebook_dir}",
            f"--IdentityProvider.token={token}",
            "--KernelProvisionerFactory.default_provisioner_name=pyxll-provisioner",
        ]
        if app_dir is not None:
            cmd.append(f"--app-dir={app_dir}")
        if not launch_browser:
            cmd.append("--no-browser")
        return cmd, env

    @staticmethod
    def _discover_lab_app_dir(site_packages_paths: list[str]) -> Optional[str]:
        """Locate a JupyterLab app directory that contains ``static/``.

        Tries (in order):
          1. The ``jupyterlab`` package directory (wheels ship the UI
             assets bundled inside the package).
          2. ``<site-packages>/share/jupyter/lab`` for each candidate
             site-packages, which is where pip --target places the
             wheel's data files.
        Returns ``None`` if nothing usable is found — the caller will
        then omit ``--app-dir`` and let JupyterLab error out as before.
        """
        try:
            import jupyterlab as _jl  # type: ignore[import-not-found]

            pkg_dir = Path(_jl.__file__).resolve().parent
            if (pkg_dir / "static").is_dir():
                return str(pkg_dir)
        except ImportError:
            pass

        for entry in site_packages_paths:
            candidate = Path(entry) / "share" / "jupyter" / "lab"
            if (candidate / "static").is_dir():
                return str(candidate)
        return None

    @staticmethod
    def _drain_subprocess_lines(
        proc: subprocess.Popen,
        line_callback: Callable[[str], None],
    ) -> None:
        encoding = sys.getdefaultencoding()
        stream = proc.stdout
        if stream is None:
            return
        while proc.poll() is None:
            for buffer in iter(stream.readline, b""):
                try:
                    text = buffer.decode(encoding).rstrip()
                except UnicodeDecodeError:
                    text = buffer.decode(encoding, errors="replace").rstrip()
                _invoke_callback(line_callback, text)

    def open_browser(self) -> None:
        url = self.jupyter_lab_url()
        if not url:
            return
        import webbrowser

        webbrowser.open(url)

    def stop(self) -> None:
        """Tear down the JupyterLab subprocess and the in-process kernel.

        Order matters: we ask the kernel to shut down through a Jupyter
        client first (so the IO loop can drain message replies), then
        kill JupyterLab, then close the kernel app and clear its
        singleton state. Without ``clear_instance`` a follow-up start
        gets the same dead kernel back.
        """
        from . import main_thread

        with self._LOCK:
            connection_file = self._connection_file
            kernel = self._kernel
            jupyter_proc = self._jupyter_proc

        # Best-effort: ask the kernel to shut down cleanly.
        if connection_file is not None and connection_file.exists():
            try:
                _shutdown_kernel_via_client(connection_file)
            except Exception as exc:  # noqa: BLE001
                logging.warning("Kernel client shutdown failed: %s", exc)

        # JupyterLab subprocess.
        if jupyter_proc is not None:
            try:
                jupyter_proc.terminate()
                try:
                    jupyter_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    jupyter_proc.kill()
                    jupyter_proc.wait(timeout=2)
            except Exception as exc:  # noqa: BLE001
                logging.warning("JupyterLab subprocess shutdown failed: %s", exc)

        # IPKernelApp.
        if kernel is not None:
            try:
                # The kernel's ZMQ streams are not closed by .close();
                # without explicit closes the file descriptors leak
                # until GC. See ipykernel kernelapp.py around line 547.
                for attr in ("shell_stream", "control_stream", "debugpy_stream"):
                    stream = getattr(kernel.kernel, attr, None)
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:  # noqa: BLE001
                            pass
                kernel.close()
                try:
                    kernel.cleanup_connection_file()
                except Exception:  # noqa: BLE001
                    pass
                kernel.kernel.clear_instance()
                kernel.clear_instance()
            except Exception as exc:  # noqa: BLE001
                logging.warning("IPKernelApp shutdown failed: %s", exc)

        # Stop the event-loop pump and reset state.
        main_thread.unregister()

        with self._LOCK:
            self._kernel = None
            self._jupyter_proc = None
            self._connection_file = None
            self._token = None
            self._host = None
            self._port = None
            self._lines_thread = None


# ----------------------------------------------------------------------- #
# Helpers                                                                 #
# ----------------------------------------------------------------------- #
def _shutdown_kernel_via_client(connection_file: Path) -> None:
    """Connect a transient client and send a kernel shutdown request.

    This runs in a background thread (the parent process can't block on
    a kernel reply when the kernel lives in the same process). Errors
    are surfaced via ``RuntimeError`` if the kernel stays alive for too
    long.
    """
    from jupyter_client.blocking.client import BlockingKernelClient

    def _do_shutdown() -> None:
        kc = BlockingKernelClient(connection_file=str(connection_file))
        kc.load_connection_file()
        kc.start_channels()
        try:
            kc.shutdown(restart=False)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if not kc.is_alive():
                    return
                time.sleep(0.05)
        finally:
            try:
                kc.stop_channels()
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=_do_shutdown, daemon=True)
    t.start()
    t.join(timeout=6.0)


# Module-level singletons (mirrors marimo-blender's API).
installer = Installer()
server = Server()
