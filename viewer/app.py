r"""laop_view as a desktop window: the same server as viewer.py, shown in a native window (pywebview).

    python app.py                  window on the repositories in ~/.laop/viewer.json
    python app.py repo=<path>      also watch this repository
    python app.py config=<file>    another list of repositories

Needs ``pip install pywebview``. viewer.py is unchanged and stays usable on its own; the server
listens on 127.0.0.1 and stops when the window closes.

The web view has no desktop notifications of its own: the page sends them through ``Api.notify``,
shown as Windows toasts (Windows PowerShell and its WinRT classes, nothing to install) under the
app's own name and icon: at every start the app registers its AppUserModelID for the current user
(HKCU\Software\Classes\AppUserModelId\laop.view) with the icon copied to ~/.laop/laop-view.png. The window
keeps its browser storage (theme, notifications on or off, the open tab) in ~/.laop/webview, and the
server takes the same port at every start when it is free, so that storage stays with the page.
"""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from xml.sax.saxutils import escape

import viewer

#: First port tried: the browser storage belongs to the page's origin, port included.
APP_PORT = 8775

APP_NAME = "laop view"
#: The app's AppUserModelID: the window's taskbar identity and the sender of its toasts.
APP_ID = "laop.view"
#: Fallback sender when the registration fails: Windows PowerShell, known to Windows without one.
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
TOAST_APP_ID = POWERSHELL_APP_ID
#: The icon, next to app.py or inside the packaged app.
ICON = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "laop.png"
TOAST_SCRIPT = """
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:LAOP_TOAST_XML)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:LAOP_TOAST_APP).Show($toast)
"""


def register_app_id() -> bool:
    """Make ``APP_ID`` known to Windows for the current user (name and icon of its toasts), and
    give this process that identity. An unpackaged app needs no Start menu shortcut for it."""
    global TOAST_APP_ID
    if sys.platform != "win32":
        return False
    import ctypes
    import winreg
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass
    try:
        icon = Path.home() / ".laop" / "laop-view.png"
        if ICON.is_file():
            icon.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ICON, icon)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            if icon.is_file():
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
    except OSError:
        return False
    TOAST_APP_ID = APP_ID
    return True


def toast(title: str, body: str) -> bool:
    """Show a Windows toast; False when it could not be shown (not Windows, PowerShell failed)."""
    if sys.platform != "win32":
        return False
    xml = (f'<toast><visual><binding template="ToastGeneric"><text>{escape(str(title)[:200])}</text>'
           f'<text>{escape(str(body)[:600])}</text>'
           "</binding></visual></toast>")
    env = {**os.environ, "LAOP_TOAST_XML": xml, "LAOP_TOAST_APP": TOAST_APP_ID}
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                              "-Command", TOAST_SCRIPT], env=env, capture_output=True, timeout=20,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


class Api:
    """Called by the page as ``window.pywebview.api.<name>``."""

    def notify(self, title: str, body: str) -> bool:
        return toast(title, body)


def bind(handler) -> http.server.ThreadingHTTPServer:
    for port in [*range(APP_PORT, APP_PORT + 10), 0]:
        try:
            return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            continue
    raise OSError("no free port")


def main(argv: list[str]) -> int:
    opts: dict[str, str] = {}
    for arg in argv:
        key, sep, value = arg.partition("=")
        if not sep or key not in ("repo", "config"):
            print(__doc__)
            return 2
        opts[key] = value

    try:
        import webview
    except ImportError:
        print("pywebview is missing: pip install pywebview")
        return 1

    register_app_id()
    registry = viewer.Registry(Path(opts["config"]) if "config" in opts else viewer.default_config())
    if "repo" in opts:
        rid, why = registry.add(str(Path(opts["repo"]).expanduser().resolve()))
        if rid is None:
            print(why)
            return 2

    handler = type("BoundHandler", (viewer.Handler,), {"registry": registry,
                                                       "convs": viewer.Convs(registry.config.parent / "chats")})
    server = bind(handler)
    registry.start_scheduler()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()

    url = f"http://127.0.0.1:{server.server_address[1]}/"
    # text_select: pywebview turns text selection off by default; the owner copies from the page
    webview.create_window(APP_NAME, url, width=1280, height=860, min_size=(800, 500), js_api=Api(),
                          text_select=True)
    storage = Path.home() / ".laop" / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    try:
        webview.start(private_mode=False, storage_path=str(storage))
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
