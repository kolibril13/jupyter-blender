# jupyter-blender

A Blender extension that runs a [Jupyter](https://jupyter.org) notebook server inside Blender so cells can `import bpy` and drive the running scene live.

Targets Blender 5.1+. Architecturally a mashup of:

- [`bpy_jupyter`](https://github.com/Octoframes/bpy_jupyter) (v2.1 branch) — the in-process IPython kernel + JupyterLab subprocess pattern.
- [`marimo-blender`](https://github.com/iplai/marimo-blender) — the N-panel UI, dependency installer flow, and GitHub Actions release pipeline.


## Quick start

1. Install the extension `.zip` via *Edit → Preferences → Get Extensions → Install from Disk*.
2. Open the **N** sidebar in the 3D viewport → **Jupyter** panel.
3. Click **Install Python Modules** once to pull `jupyterlab`, `ipykernel`, `pyxll-jupyter`, that's about 300 MB of package data, so this takes a short while.
4. Click **Start Notebook Server**. The browser opens to `http://127.0.0.1:10462/lab?token=…`.
5. Open a new notebook and try:

   ```python
   import bpy
   bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(1, 2, 0))
   ```

