# jupyter-blender

A Blender extension that runs a [Jupyter](https://jupyter.org) notebook server inside Blender so cells can `import bpy` and drive the running scene live.

Targets Blender 5.1+. Architecturally a mashup of:

- [`bpy_jupyter`](https://github.com/Octoframes/bpy_jupyter) (v2.1 branch) — the in-process IPython kernel + JupyterLab subprocess pattern.
- [`marimo-blender`](https://github.com/iplai/marimo-blender) — the N-panel UI, dependency installer flow, and GitHub Actions release pipeline.


## Quick start

1. Install the extension `.zip` via *Edit → Preferences → Get Extensions → Install from Disk*.
2. Open the **N** sidebar in the 3D viewport → **Jupyter** panel.
3. Click **Install Python Modules** once to pull `jupyterlab`, `ipykernel`, `pyxll-jupyter` and friends into Blender's extension site-packages via `pip --target`.
4. Click **Start Notebook Server**. The browser opens to `http://127.0.0.1:10462/lab?token=…`.
5. Open a new notebook and try:

   ```python
   import bpy
   bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(1, 2, 0))
   ```


## How it works

The hard part of embedding Jupyter in Blender isn't running the web server — it's getting `bpy` to be importable from notebook cells. This section explains why each piece exists.


### The fundamental constraint: `_bpy` only exists inside the Blender executable

`bpy` is a Python wrapper around `_bpy`, a C extension compiled into the Blender binary itself. A spawned Python subprocess — even one using Blender's bundled interpreter — cannot import `_bpy`, because the symbol isn't there outside the running Blender process. Anything that wants to call `bpy` must live in the Blender process.

Jupyter's default architecture conflicts with this. JupyterLab spawns each kernel as a child process via a kernel provisioner; that subprocess can't see `_bpy`. So we split the architecture in two:

1. The **IPython kernel** runs *inside* Blender as `IPKernelApp.instance(...)`.
2. **JupyterLab** runs as a subprocess and is told to attach to the already-running kernel via [`pyxll-jupyter`](https://github.com/pyxll/pyxll-jupyter)'s `pyxll-provisioner` (a Jupyter kernel provisioner that connects to an existing connection file instead of spawning a fresh interpreter).

Cells therefore execute inside Blender's Python, with full access to `bpy`. JupyterLab is just the UI shell + REST/WebSocket front-end and never needs `bpy` itself.


### 1. In-process kernel

`jupyter_blender/addon_setup.py` (`Server.start`):

```python
self._kernel = IPKernelApp.instance(connection_file=str(self._connection_file))
self._kernel.initialize([sys.executable])
self._kernel.kernel.start()
```

`IPKernelApp` is a `Configurable` singleton; we pass only `[sys.executable]` as argv so it doesn't try to parse Blender's CLI. `kernel.start()` doesn't block — it wires the kernel up to an asyncio event loop that needs to be iterated externally for messages from JupyterLab to make progress.


### 2. Pumping the kernel's asyncio loop

`jupyter_blender/main_thread.py` registers a `bpy.app.timers` callback that runs roughly every 16 ms:

```python
loop = asyncio.get_event_loop()
loop.call_soon(loop.stop)
loop.run_forever()
```

`call_soon(loop.stop)` + `run_forever()` is the standard trick to do exactly one iteration of an asyncio loop — pending tasks (kernel ZMQ message handlers, completion replies, …) get a chance to run, then control returns to Blender's main thread.

Because the pump runs on the main thread, cell bodies also execute on the main thread — so `bpy.ops.*` calls and depsgraph mutations are safe. The trade-off is that long-running cells block Blender's UI redraw; same caveat as `bpy_jupyter` and `marimo-blender`.


### 3. JupyterLab subprocess

JupyterLab is launched as `python -m jupyterlab` with:

- `--KernelProvisionerFactory.default_provisioner_name=pyxll-provisioner` — Jupyter consults `pyxll_jupyter`'s entry point instead of the built-in `local-provisioner` (which would spawn a new kernel).
- `PYXLL_IPYTHON_CONNECTION_FILE` env var — points the provisioner at the kernel's connection file.
- `--IdentityProvider.token=<random>` — a fresh per-launch token, exposed via the *Copy URL* buttons in the panel.

The subprocess's environment needs `PYTHONPATH` to include Blender's extension site-packages so it can find `jupyterlab` and `pyxll_jupyter` (entry points are only discovered for packages on `sys.path`).


### 4. Dependency installer

`Installer` wraps `pip --target=<site-packages>` to drop wheels into a directory already on Blender's `sys.path`. The set of top-level deps is small:

```
jupyterlab
ipykernel
jupyter-client
pyxll-jupyter
ipywidgets
anywidget
```

pip resolves the rest (tornado, zmq, traitlets, …). The same `Installer` class also exposes *uninstall*, *list* and *install &lt;module&gt;* operations, all streamed line-by-line into the panel's log box.


## Trade-offs and limitations

- **Cells block the Blender UI** while they run. Same caveat as `bpy_jupyter` and `marimo-blender`. Long renders or heavy numerical loops will freeze the viewport — the cost of giving cells safe access to `bpy`.
- **The kernel is a singleton.** Restarting from JupyterLab will not get you a fresh interpreter — the in-process `IPKernelApp.instance()` is shared with Blender's own process. Use the **Stop Notebook Server** button if you need to reset.
- **Only one server at a time.** The Server class refuses a second start until the first is stopped; the connection file path is fixed.


## Source map

| File | Role |
|---|---|
| `jupyter_blender/__init__.py` | Addon registration, N-panel and viewport-header button. |
| `jupyter_blender/preferences.py` | `AddonPreferences`, all operators, and the panel `draw_preferences` body. |
| `jupyter_blender/addon_setup.py` | `Installer` (pip wrapper) and `Server` (IPKernelApp + JupyterLab subprocess). |
| `jupyter_blender/main_thread.py` | `bpy.app.timers` callback that pumps the kernel's asyncio loop. |
| `jupyter_blender/examples/move_cube.ipynb` | Bundled "hello bpy" notebook. |
| `.github/workflows/release.yaml` | Tag-driven GitHub release: zips the addon folder and attaches it to the release. |


## Releasing

Pushing a tag of the form `v*` to the default branch triggers `.github/workflows/release.yaml`, which:

1. Copies `LICENSE` into the extension folder.
2. Zips `jupyter_blender/` into `jupyter-blender-<tag>.zip`.
3. Creates a GitHub Release with the zip attached and auto-generated notes.

```bash
git tag v0.1.0
git push origin v0.1.0
```


## License

AGPL-3.0-or-later. See `LICENSE`.
