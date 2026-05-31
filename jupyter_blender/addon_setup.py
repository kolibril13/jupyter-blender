"""Runtime services: dependency installer + Jupyter kernel/server lifecycle.

Two singletons live here:

- ``installer`` — wraps ``pip`` to manage the Jupyter dependency set in
  Blender's extension site-packages. Modeled after marimo-blender's
  Installer.

- ``server`` — manages an in-process ``IPKernelApp`` plus a JupyterLab
  subprocess that connects to it through a small vendored "existing
  kernel" provisioner (see ``_jupyterlab_launcher``). Architecture is
  ported from bpy_jupyter v2.1.

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
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

# Kept in sync with ``_jupyterlab_launcher``: the name the vendored
# provisioner is registered under, and the env var carrying the kernel's
# connection file path to the JupyterLab subprocess.
_PROVISIONER_NAME = "jupyter-blender-existing"
_CONNECTION_FILE_ENV = "JUPYTER_BLENDER_CONNECTION_FILE"


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
        "ipywidgets",
        "anywidget",
    ]

    # pip dist name → importable module name (where they differ).
    _DIST_TO_MODULE: dict[str, str] = {
        "jupyter-client": "jupyter_client",
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
    is launched as a subprocess (via ``_jupyterlab_launcher``) and pointed
    at the kernel's connection file through a vendored kernel provisioner
    that attaches to the already-running kernel instead of spawning a new
    one.
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
        # Set True while ``stop`` is tearing things down, so the subprocess
        # monitor can tell an intentional shutdown from a crash.
        self._stopping: bool = False
        # Exit code of the JupyterLab subprocess if it died on its own
        # (e.g. the port was already in use). ``None`` while healthy.
        self._server_exit_code: Optional[int] = None

    # ------------------------------------------------------------------ #
    # State                                                              #
    # ------------------------------------------------------------------ #
    @property
    def is_running(self) -> bool:
        """True only when the kernel *and* the JupyterLab subprocess are up.

        The kernel lives in-process and stays alive even if JupyterLab
        dies, so checking the kernel alone would report a dead server as
        running. The monitor thread is the sole owner of ``proc.poll()``
        and records an unexpected exit in ``_server_exit_code``; reading
        that flag here (instead of polling again) avoids a concurrent
        ``poll()`` race while still flipping the UI back to Stopped when
        JupyterLab crashes (e.g. port in use).
        """
        with self._LOCK:
            return self._kernel is not None and self._server_exit_code is None

    @property
    def is_active(self) -> bool:
        """True if there is anything to tear down (kernel created).

        Unlike ``is_running`` this stays True even after the JupyterLab
        subprocess has crashed, so shutdown paths still clean up the
        leftover in-process kernel.
        """
        with self._LOCK:
            return self._kernel is not None

    @property
    def server_exit_code(self) -> Optional[int]:
        return self._server_exit_code

    @property
    def is_headless(self) -> bool:
        return not self._launch_browser

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
        default_url: Optional[str] = None,
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

        # Self-heal: a previously crashed JupyterLab (e.g. port in use)
        # leaves the in-process kernel running. ``is_running`` reports such
        # a half-dead server as stopped, so the UI offers Start again —
        # tear the leftover kernel down here before starting fresh rather
        # than raising "already running".
        if self.is_active and not self.is_running:
            self.stop()

        with self._LOCK:
            if self._kernel is not None:
                raise ValueError("Jupyter server is already running.")

            self._stopping = False
            self._server_exit_code = None
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
            # JupyterLab's built-in browser launch always lands on
            # ``LabApp.default_url`` (``/lab``); CLI overrides like
            # ``--ServerApp.default_url`` get clobbered. So when the
            # caller wants to land on a specific page (e.g. a notebook
            # file), we pass ``--no-browser`` and open the targeted URL
            # ourselves once the server prints its ready banner.
            handle_browser_ourselves = (
                launch_browser and default_url is not None
            )
            effective_launch_browser = (
                launch_browser and not handle_browser_ourselves
            )

            cmd, env = self._build_jupyterlab_cmd(
                notebook_dir=notebook_dir,
                token=self._token,
                host=host,
                port=port,
                launch_browser=effective_launch_browser,
            )

            self._jupyter_proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )

            effective_line_callback = line_callback
            if handle_browser_ourselves:
                target_url = (
                    f"http://{host}:{port}{default_url}"
                    f"?token={self._token}"
                )
                effective_line_callback = _make_ready_browser_opener(
                    target_url, line_callback
                )

            # Always monitor the subprocess in a daemon thread: it streams
            # JupyterLab logs back to the caller *and* notices if the
            # process dies on its own (draining stdout also keeps the pipe
            # from filling and blocking JupyterLab).
            self._lines_thread = threading.Thread(
                target=self._monitor_subprocess,
                args=(self._jupyter_proc, effective_line_callback),
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
        # site-packages on its PYTHONPATH so it can find ``jupyterlab``
        # and ``jupyter_client``. The connection file is passed via env so
        # the vendored provisioner (see ``_jupyterlab_launcher``) can
        # attach JupyterLab to the in-process kernel.
        env = os.environ.copy()
        env[_CONNECTION_FILE_ENV] = str(self._connection_file)

        extra_path_entries: list[str] = []
        for entry in sys.path:
            if entry and entry.endswith("site-packages"):
                extra_path_entries.append(entry)

        # Also include the directory containing the bundled ``jupyter``
        # package so the launcher's JupyterLab import resolves it.
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

        launcher = str(Path(__file__).resolve().parent / "_jupyterlab_launcher.py")
        cmd = [
            sys.executable,
            launcher,
            f"--ip={host}",
            f"--port={port}",
            f"--notebook-dir={notebook_dir}",
            f"--IdentityProvider.token={token}",
            f"--KernelProvisionerFactory.default_provisioner_name={_PROVISIONER_NAME}",
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

    def _monitor_subprocess(
        self,
        proc: subprocess.Popen,
        line_callback: Optional[Callable[[str], None]],
    ) -> None:
        """Drain the JupyterLab subprocess and watch for an early exit.

        Runs in a daemon thread. Streams stdout line-by-line to
        ``line_callback`` until the process exits, then — unless ``stop``
        initiated the shutdown — records the exit code and emits a final
        line so the UI surfaces the failure (the common cause is the port
        already being in use).
        """
        encoding = sys.getdefaultencoding()
        stream = proc.stdout
        if stream is not None:
            while proc.poll() is None:
                for buffer in iter(stream.readline, b""):
                    try:
                        text = buffer.decode(encoding).rstrip()
                    except UnicodeDecodeError:
                        text = buffer.decode(encoding, errors="replace").rstrip()
                    _invoke_callback(line_callback, text)

        exit_code = proc.poll()
        if self._stopping:
            return

        # Unexpected exit — the kernel is still alive but JupyterLab is
        # gone. Record it; ``is_running`` now reports False, and the line
        # below redraws the panel back to its Start state.
        self._server_exit_code = exit_code
        logging.warning("JupyterLab exited unexpectedly (code %s)", exit_code)
        _invoke_callback(
            line_callback,
            f"JupyterLab server exited unexpectedly (exit code {exit_code}). "
            f"Port {self._port} may already be in use — try a different port.",
        )

    def open_browser(self) -> None:
        url = self.jupyter_lab_url()
        if not url:
            return
        import webbrowser

        webbrowser.open(url)

    def stop(self) -> None:
        """Tear down the JupyterLab subprocess and the in-process kernel.

        The kernel lives in this process and its asyncio loop is pumped by
        the Blender main thread (see ``main_thread``). ``stop`` is itself
        called on the main thread, so a graceful client-driven shutdown
        can't work — the kernel could never process the shutdown request
        while the only thread that pumps its loop is blocked waiting for
        the reply. We close the kernel directly instead. We kill
        JupyterLab, then close the kernel app and clear its singleton
        state. Without ``clear_instance`` a follow-up start gets the same
        dead kernel back.
        """
        from . import main_thread

        with self._LOCK:
            # Tell the monitor thread this exit is intentional so it
            # doesn't report a crash.
            self._stopping = True
            kernel = self._kernel
            jupyter_proc = self._jupyter_proc

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
def _make_ready_browser_opener(
    target_url: str,
    inner_callback: Optional[Callable[[str], None]],
) -> Callable[[str], None]:
    """Return a line-callback that opens ``target_url`` once the
    JupyterLab subprocess prints its "is running at" banner.

    Forwards every line to ``inner_callback`` first (so logs still
    stream to the UI) and uses an Event to ensure the browser is only
    opened once.
    """
    import webbrowser

    opened = threading.Event()

    def _callback(text: str) -> None:
        if inner_callback is not None:
            try:
                inner_callback(text)
            except Exception:  # noqa: BLE001
                pass
        if opened.is_set():
            return
        if "is running at" in text.lower():
            opened.set()
            try:
                webbrowser.open(target_url)
            except Exception as exc:  # noqa: BLE001
                logging.warning("Failed to open browser: %s", exc)

    return _callback


# Module-level singletons (mirrors marimo-blender's API).
installer = Installer()
server = Server()
