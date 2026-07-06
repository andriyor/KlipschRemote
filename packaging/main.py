"""Flet build / PyInstaller entry point — launches the Klipsch Remote desktop app.

`flet build` runs this module as the program (top-level `run()` -> `ft.run(...)`).
The Linux release instead freezes this module with PyInstaller (see build_app_linux.sh),
because `flet build linux` bundles a standalone CPython compiled without Bluetooth
headers, so `socket.AF_BLUETOOTH` is missing and the L2CAP backend cannot open the
Classic ATT channel. PyInstaller freezes with the runner's system CPython (built with
BlueZ), which keeps `AF_BLUETOOTH`.

The real app lives in the sibling `klipsch_remote` package; this only bootstraps it,
so the GUI source stays single-sourced with the `python -m klipsch_remote` dev path.
"""

from __future__ import annotations

import os
import sys


def _point_flet_at_bundled_client() -> None:
    """In a PyInstaller bundle, use the flet desktop client shipped inside it.

    The flet PyInstaller hook copies the desktop client into
    ``<bundle>/flet_desktop/app/flet``. Without a hint flet's
    ``ensure_client_cached()`` only finds it when a matching archive is present
    (it isn't — the hook ships the *extracted* client), so it would download the
    ~64 MB client from GitHub on first launch. Pointing ``FLET_VIEW_PATH`` at the
    bundled directory makes the app fully self-contained and offline-capable.

    Guarded on ``sys.frozen`` so a plain ``flet build`` / dev run is untouched.
    """
    if not getattr(sys, "frozen", False):
        return
    if os.environ.get("FLET_VIEW_PATH"):
        return
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    client_dir = os.path.join(base, "flet_desktop", "app", "flet")
    if os.path.isfile(os.path.join(client_dir, "flet")):
        os.environ["FLET_VIEW_PATH"] = client_dir


_point_flet_at_bundled_client()

from klipsch_remote.app import run  # noqa: E402  (must follow the env setup above)

run()
