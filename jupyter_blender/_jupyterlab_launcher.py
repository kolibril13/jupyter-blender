"""Standalone entry point for the JupyterLab subprocess.

Run by ``Server._build_jupyterlab_cmd`` as ``python _jupyterlab_launcher.py
<jupyterlab-args>``. It registers an in-process kernel provisioner, then
hands off to JupyterLab.

The provisioner makes JupyterLab attach to the kernel already running
inside Blender (whose connection file is named in the
``JUPYTER_BLENDER_CONNECTION_FILE`` env var) instead of spawning its own.
This is the one piece we used to depend on ``pyxll-jupyter`` for — vendoring
it (~30 lines) drops that dependency and the ~300 MB of Qt/pywin32/notebook
it pulled in but we never used. Adapted from pyxll-jupyter's
``ExistingProvisioner`` (MIT).

This module is executed as ``__main__`` in a plain Python subprocess. It
must NOT import ``bpy`` or the ``jupyter_blender`` package (whose
``__init__`` imports ``bpy``) — neither exists in that interpreter.
"""
import json
import os

from jupyter_client import KernelProvisionerBase

# Name we register the provisioner under and select via
# ``--KernelProvisionerFactory.default_provisioner_name``. Kept in sync with
# ``addon_setup._PROVISIONER_NAME``.
PROVISIONER_NAME = "jupyter-blender-existing"
CONNECTION_FILE_ENV = "JUPYTER_BLENDER_CONNECTION_FILE"


class ExistingKernelProvisioner(KernelProvisionerBase):
    """Attach to a kernel that is already running, instead of launching one.

    The kernel lives in the Blender process; its connection file path is
    passed through the environment. ``launch_kernel`` just reads that file
    and returns the connection info, so jupyter_server connects to the
    existing kernel. The lifecycle methods are no-ops — Blender owns the
    kernel, so JupyterLab must never poll, signal or kill it.
    """

    @property
    def has_process(self) -> bool:
        return True

    async def pre_launch(self, **kwargs):
        kwargs = await super().pre_launch(**kwargs)
        # No command to run — there is no kernel subprocess to spawn.
        kwargs.setdefault("cmd", None)
        return kwargs

    async def launch_kernel(self, cmd, **kwargs):
        connection_file = os.environ[CONNECTION_FILE_ENV]
        with open(connection_file) as f:
            connection_info = json.load(f)
        # jupyter_client expects the HMAC key as bytes.
        connection_info["key"] = connection_info["key"].encode()
        return connection_info

    async def poll(self):
        return None

    async def wait(self):
        return None

    async def send_signal(self, signum: int) -> None:
        return None

    async def kill(self, restart: bool = False) -> None:
        return None

    async def terminate(self, restart: bool = False) -> None:
        return None

    async def cleanup(self, restart: bool = False) -> None:
        return None


def _register_provisioner() -> None:
    """Inject the provisioner into jupyter_client's factory cache.

    Provisioners are normally discovered through the
    ``jupyter_client.kernel_provisioners`` entry-point group, which requires
    installed package metadata. We have no such metadata (this ships inside
    a Blender extension), so we pre-populate the factory's class-level
    ``provisioners`` dict instead. ``KernelProvisionerFactory`` checks that
    cache before falling back to entry-point discovery, so the name resolves
    without any installed distribution. The entry point loads
    ``__main__:ExistingKernelProvisioner`` — this module, run as a script.
    """
    from importlib.metadata import EntryPoint

    from jupyter_client.provisioning.factory import KernelProvisionerFactory

    KernelProvisionerFactory.provisioners.setdefault(
        PROVISIONER_NAME,
        EntryPoint(
            PROVISIONER_NAME,
            "__main__:ExistingKernelProvisioner",
            KernelProvisionerFactory.GROUP_NAME,
        ),
    )


def main() -> None:
    _register_provisioner()
    from jupyterlab.labapp import LabApp

    LabApp.launch_instance()


if __name__ == "__main__":
    main()
