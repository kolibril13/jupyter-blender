import subprocess
import sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "jupyterlab", "ipykernel", "jupyter-client", "pyxll-jupyter", "ipywidgets", "anywidget"])

--- and restart blender for dev setup
