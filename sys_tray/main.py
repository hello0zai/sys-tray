"""
Minimal Windows system-tray icon inspired by SABnzbd's sabtraywin.py
+ server status + background workers + process management
"""
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

import psutil
import pywintypes
import win32con
import win32gui
import win32gui_struct
from dotenv import load_dotenv


# ----------------------------------------------------------------------
# Paths / environment
# ----------------------------------------------------------------------
def get_running_path() -> str:
    """Folder of the exe (frozen) or of this script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


RUNNING_PATH = get_running_path()

# Load .env from next to the exe/script (not from the current working dir,
# which is unreliable when started from Startup/Task Scheduler/PyInstaller).
load_dotenv(os.path.join(RUNNING_PATH, ".env"))


def find_icon() -> str | None:
    candidates = [os.path.join(RUNNING_PATH, "icons/icon.ico")]
    bundle_dir = getattr(sys, "_MEIPASS", None)  # PyInstaller --onefile temp dir
    if bundle_dir:
        candidates.append(os.path.join(bundle_dir, "icons/icon.ico"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# ----------------------------------------------------------------------
# Logging (file + console). The file log matters for --noconsole builds,
# where there is no stdout to look at.
# ----------------------------------------------------------------------
handlers: list[logging.Handler] = []
try:
    handlers.append(logging.FileHandler(os.path.join(RUNNING_PATH, "tray.log"), encoding="utf-8"))
except OSError:
    pass
if sys.stderr is not None:
    handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
    handlers=handlers,
)


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
WINDOW_CLASS_NAME = "MyTrayIconDemo"
APP_NAME = "SABnzbd Demo"

SERVER=r"C:\Users\wan\AppData\Local\Programs\Sundial\sd-server.exe"
SERVER_ENDPOINT="http://127.0.0.1:7600/api/0/info"

# SERVER = os.getenv("SERVER")
SERVER_COMMAND = [SERVER] if SERVER else []
# SERVER_ENDPOINT = os.getenv("SERVER_ENDPOINT")
SETTINGS_ENDPOINT = "https://dummyjson.com/quotes"

SERVER_CHECK_INTERVAL = 60
SETTINGS_CHECK_INTERVAL = 60
REQUEST_TIMEOUT = 3
STARTUP_TIMEOUT = 30
RETRY_INTERVAL = 2
PROCESS_STOP_TIMEOUT = 5

# Custom window messages
WM_TRAY = win32con.WM_USER + 20
WM_STATUS_UPDATED = win32con.WM_APP + 1
WM_SETTINGS_UPDATED = win32con.WM_APP + 2

# Shared state
stop_event = threading.Event()
server_status = None  # None = Checking, True/False = Connected/Disconnected
server_status_lock = threading.Lock()
latest_settings = None
settings_lock = threading.Lock()
server_check_lock = threading.Lock()
settings_request_lock = threading.Lock()

tray_app = None  # assigned in main() via `global`


# ----------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------
def get_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "TrayDemo/1.0"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def check_server() -> bool:
    try:
        get_json(SERVER_ENDPOINT)
        return True
    except Exception as exc:
        logging.debug("Server check failed: %s", exc)
        return False


def fetch_settings():
    try:
        settings = get_json(SETTINGS_ENDPOINT)
        logging.info("Settings received successfully.")
        return settings
    except Exception as exc:
        logging.warning("Could not retrieve settings: %s", exc)
        return None


# ----------------------------------------------------------------------
# Server process management
# ----------------------------------------------------------------------
def get_server_executable() -> Path:
    if not SERVER_COMMAND:
        raise ValueError("SERVER is not set (SERVER_COMMAND is empty).")
    return Path(SERVER_COMMAND[0]).resolve()


def find_server_processes() -> list[psutil.Process]:
    executable = get_server_executable()
    matches = []
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            process_path = process.info.get("exe")
            if process_path and Path(process_path).resolve() == executable:
                matches.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return matches


def stop_server_processes() -> bool:
    """Terminate the server and its children. Returns True if nothing is left."""
    processes = find_server_processes()
    if not processes:
        return True

    # Include child processes, otherwise terminating only the parent can leave
    # orphans that keep the port busy.
    targets: dict[int, psutil.Process] = {}
    for proc in processes:
        targets[proc.pid] = proc
        try:
            for child in proc.children(recursive=True):
                targets[child.pid] = child
        except psutil.Error:
            pass

    for proc in targets.values():
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            logging.error("Access denied terminating PID %s.", proc.pid)

    _, alive = psutil.wait_procs(list(targets.values()), timeout=PROCESS_STOP_TIMEOUT)
    for proc in alive:
        try:
            logging.warning("Force-killing unresponsive PID %s.", proc.pid)
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    _, alive = psutil.wait_procs(alive, timeout=3)
    if alive:
        logging.error("Could not stop PIDs: %s", [p.pid for p in alive])
        return False
    return True


def wait_for_server(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not stop_event.is_set():
        if check_server():
            return True
        if time.monotonic() >= deadline:
            return False
        if stop_event.wait(RETRY_INTERVAL):
            return False
    return False


def start_server() -> bool:
    executable = get_server_executable()
    if not executable.is_file():
        logging.error("Server executable not found: %s", executable)
        return False
    if stop_event.is_set():
        return False
    if find_server_processes():
        logging.warning("Server process still exists but is not responding.")
        return False

    try:
        logging.info("Starting server: %s", executable)
        subprocess.Popen(
            SERVER_COMMAND,
            cwd=str(executable.parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
    except OSError:
        logging.exception("Failed to start the server.")
        return False

    if wait_for_server(STARTUP_TIMEOUT):
        logging.info("Server started and is responding.")
        return True

    logging.error("Server did not become healthy within %s seconds.", STARTUP_TIMEOUT)
    return False


def restart_server() -> bool:
    executable = get_server_executable()
    if not executable.is_file():
        logging.error("Server executable not found: %s", executable)
        return False
    if stop_event.is_set():
        return False

    if find_server_processes():
        logging.warning("Server endpoint failed. Restarting existing server process(es).")
        if not stop_server_processes():
            return False

    if stop_event.is_set():
        return False
    if check_server():
        logging.info("Server recovered without a new launch.")
        return True

    return start_server()


# ----------------------------------------------------------------------
# Background workers
# ----------------------------------------------------------------------
def publish_server_status(status: bool):
    global server_status
    with server_status_lock:
        server_status = status
    if tray_app is not None:
        tray_app.post_message(WM_STATUS_UPDATED)


def _check_and_recover():
    """Check the server; if it is down, try to recover. Caller holds server_check_lock."""
    status = check_server()
    publish_server_status(status)

    if status:
        logging.info("Server status: Connected")
        return

    logging.warning("Server status: Disconnected - attempting recovery")
    if stop_event.is_set():
        return
    recovered = restart_server()
    status = check_server() if recovered and not stop_event.is_set() else False
    publish_server_status(status)
    logging.info("Server status after recovery: %s", "Connected" if status else "Disconnected")


def server_worker():
    """Periodic health check + automatic recovery."""
    while not stop_event.is_set():
        if server_check_lock.acquire(blocking=False):
            try:
                _check_and_recover()
            except Exception:
                logging.exception("server_worker error")
                publish_server_status(False)
            finally:
                server_check_lock.release()

        if stop_event.wait(SERVER_CHECK_INTERVAL):
            break


def server_worker_once():
    """One-shot manual refresh (from menu)."""
    if stop_event.is_set():
        return
    if not server_check_lock.acquire(blocking=False):
        logging.info("Server check already running")
        return
    try:
        _check_and_recover()
    except Exception:
        logging.exception("Manual server check failed")
        publish_server_status(False)
    finally:
        server_check_lock.release()


def _refresh_settings_once():
    global latest_settings
    if not settings_request_lock.acquire(blocking=False):
        logging.info("Settings request already running")
        return
    try:
        settings = fetch_settings()
        if settings is not None:
            with settings_lock:
                latest_settings = settings
            if tray_app is not None:
                tray_app.post_message(WM_SETTINGS_UPDATED)
    except Exception:
        logging.exception("settings refresh error")
    finally:
        settings_request_lock.release()


def settings_worker():
    """Periodic settings refresh."""
    while not stop_event.is_set():
        _refresh_settings_once()
        if stop_event.wait(SETTINGS_CHECK_INTERVAL):
            break


def refresh_settings_async():
    threading.Thread(target=_refresh_settings_once, name="SettingsRefresh", daemon=True).start()


# ----------------------------------------------------------------------
# Menu actions
# ----------------------------------------------------------------------
def show_interface():
    logging.info("Show interface")


def open_complete_folder():
    logging.info("Open complete folder")


def pause_resume():
    logging.info("Pause / Resume toggled")


def pause_for(minutes: int):
    logging.info("Pause for %s minutes", minutes)


def read_rss():
    logging.info("Read all RSS feeds")


def restart():
    logging.info("Restart")


def restart_no_login():
    logging.info("Restart without login")


def restart_localhost():
    logging.info("Restart - 127.0.0.1:8080")


def shutdown():
    logging.info("Shutdown requested")
    if tray_app is not None:
        tray_app.stop()
    else:
        stop_event.set()


# ----------------------------------------------------------------------
# Tray implementation
# ----------------------------------------------------------------------
class TrayIcon(threading.Thread):
    def __init__(self):
        super().__init__(name="Tray", daemon=True)
        self.terminate = False
        self.hwnd = None
        self.menu = None
        self.notify_id = None
        self.hicon = None
        self.ready = threading.Event()  # set once the window/icon creation attempt is done
        self._stop_lock = threading.Lock()
        self._stopped = False

        raw_menu = [
            ("Server: Checking...", None),
            ("Refresh server status", server_worker_once),
            ("Refresh screenshot settings", refresh_settings_async),
            ("SEPARATOR", None),
            ("Show interface", show_interface),
            ("Open complete folder", open_complete_folder),
            ("SEPARATOR", None),
            ("Pause/Resume", pause_resume),
            (
                "Pause for",
                [
                    ("Pause for 5 minutes", lambda: pause_for(5)),
                    ("Pause for 15 minutes", lambda: pause_for(15)),
                    ("Pause for 30 minutes", lambda: pause_for(30)),
                    ("Pause for 1 hour", lambda: pause_for(60)),
                    ("Pause for 3 hours", lambda: pause_for(180)),
                    ("Pause for 6 hours", lambda: pause_for(360)),
                ],
            ),
            ("SEPARATOR", None),
            ("Read all RSS feeds", read_rss),
            ("SEPARATOR", None),
            (
                "Troubleshoot",
                [
                    ("Restart", restart),
                    ("Restart without login", restart_no_login),
                    ("Restart - 127.0.0.1:8080", restart_localhost),
                ],
            ),
            ("Shutdown", shutdown),
        ]

        self.menu_actions_by_id: dict[int, Callable] = {}
        self.menu_options = self._assign_ids(raw_menu, iter(range(1, 1000)))

        self.status_cmd_id = None
        for text, _action, cmd_id in self.menu_options:
            if text.startswith("Server:"):
                self.status_cmd_id = cmd_id
                break

        self.start()

    def _assign_ids(self, options, id_iter):
        result = []
        for text, action in options:
            cmd_id = next(id_iter)
            if callable(action):
                self.menu_actions_by_id[cmd_id] = action
                result.append((text, action, cmd_id))
            elif isinstance(action, (list, tuple)):
                submenu = self._assign_ids(action, id_iter)
                result.append((text, submenu, cmd_id))
            else:
                result.append((text, None, cmd_id))
        return result

    # ------------------------------------------------------------------
    # Thread body: everything touching the window lives on THIS thread
    # ------------------------------------------------------------------
    def run(self):
        try:
            self._create_window()
            self._create_menu()
            self._add_icon()
        except Exception:
            # Without this, a failure here kills the thread silently
            # and you just never see an icon.
            logging.exception("Tray setup failed")
            self.terminate = True
        finally:
            self.ready.set()

        try:
            while not self.terminate:
                win32gui.PumpWaitingMessages()
                time.sleep(0.05)
        except Exception:
            logging.exception("Tray message loop crashed")
        finally:
            self._cleanup()

    def _cleanup(self):
        if self.notify_id:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, self.notify_id)
            except Exception:
                pass
            self.notify_id = None
        if self.menu:
            try:
                win32gui.DestroyMenu(self.menu)
            except Exception:
                pass
            self.menu = None
        if self.hwnd and win32gui.IsWindow(self.hwnd):
            try:
                win32gui.DestroyWindow(self.hwnd)
            except Exception:
                pass
        self.hwnd = None

    def stop(self):
        """Safe to call from any thread, any number of times."""
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True

            logging.info("Stopping tray and server...")
            stop_event.set()  # tells workers not to start/restart anything

            # Wait for an in-flight check/restart to notice stop_event, so it
            # cannot spawn a new server behind our back.
            got_lock = server_check_lock.acquire(timeout=PROCESS_STOP_TIMEOUT + STARTUP_TIMEOUT)
            try:
                try:
                    stop_server_processes()
                except Exception:
                    logging.exception("Failed to stop server")
            finally:
                if got_lock:
                    server_check_lock.release()

            # Ask the tray thread to close its own window (DestroyWindow must
            # be called from the thread that created the window).
            self.terminate = True
            if self.hwnd:
                try:
                    win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
                except Exception:
                    pass

    def post_message(self, message):
        if self.hwnd and not self.terminate:
            try:
                win32gui.PostMessage(self.hwnd, message, 0, 0)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _create_window(self):
        message_map = {
            win32gui.RegisterWindowMessage("TaskbarCreated"): self._on_taskbar_created,
            win32con.WM_CLOSE: self._on_close,
            win32con.WM_DESTROY: self._on_destroy,
            win32con.WM_COMMAND: self._on_command,
            WM_TRAY: self._on_notify,
            WM_STATUS_UPDATED: self._on_status_updated,
            WM_SETTINGS_UPDATED: self._on_settings_updated,
        }

        wc = win32gui.WNDCLASS()
        hinst = wc.hInstance = win32gui.GetModuleHandle(None)
        wc.lpszClassName = WINDOW_CLASS_NAME
        wc.style = win32con.CS_VREDRAW | win32con.CS_HREDRAW
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc.hbrBackground = win32con.COLOR_WINDOW
        wc.lpfnWndProc = message_map

        try:
            class_atom = win32gui.RegisterClass(wc)
        except pywintypes.error as exc:
            if exc.winerror != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                raise
            class_atom = WINDOW_CLASS_NAME

        style = win32con.WS_OVERLAPPED | win32con.WS_SYSMENU
        self.hwnd = win32gui.CreateWindow(
            class_atom,
            WINDOW_CLASS_NAME,
            style,
            0, 0, win32con.CW_USEDEFAULT, win32con.CW_USEDEFAULT,
            0, 0, hinst, None,
        )
        win32gui.UpdateWindow(self.hwnd)

        self.hicon = self._load_icon()

    def _load_icon(self):
        icon_path = find_icon()
        if icon_path:
            try:
                return win32gui.LoadImage(
                    0, icon_path, win32con.IMAGE_ICON, 0, 0,
                    win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
                )
            except pywintypes.error:
                logging.exception("Could not load %s, using default icon", icon_path)
        else:
            logging.warning("icon.ico not found, using default icon")
        return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    def _add_icon(self):
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        self.notify_id = (
            self.hwnd,
            0,
            flags,
            WM_TRAY,
            self.hicon,
            APP_NAME,
        )
        # Let this raise: run() logs it, which tells us exactly why no icon appears.
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, self.notify_id)
        logging.info("Tray icon added.")

    def _create_menu(self):
        self.menu = win32gui.CreatePopupMenu()
        self._populate_menu(self.menu, self.menu_options)

    def _populate_menu(self, menu, options):
        for text, action, cmd_id in reversed(options):
            if text == "SEPARATOR":
                item, _ = win32gui_struct.PackMENUITEMINFO(fType=win32con.MFT_SEPARATOR)
                win32gui.InsertMenuItem(menu, 0, 1, item)
            elif isinstance(action, list):
                submenu = win32gui.CreatePopupMenu()
                self._populate_menu(submenu, action)
                item, _ = win32gui_struct.PackMENUITEMINFO(text=text, hSubMenu=submenu)
                win32gui.InsertMenuItem(menu, 0, 1, item)
            else:
                state = win32con.MFS_DISABLED if text.startswith("Server:") else 0
                item, _ = win32gui_struct.PackMENUITEMINFO(text=text, wID=cmd_id, fState=state)
                win32gui.InsertMenuItem(menu, 0, 1, item)

    def update_status_menu(self):
        if self.status_cmd_id is None or not self.menu:
            return

        with server_status_lock:
            status = server_status

        if status is None:
            label = "Server: Checking..."
        elif status:
            label = "Server: Connected"
        else:
            label = "Server: Disconnected"

        item, _ = win32gui_struct.PackMENUITEMINFO(
            text=label,
            wID=self.status_cmd_id,
            fState=win32con.MFS_DISABLED,
        )
        win32gui.SetMenuItemInfo(self.menu, self.status_cmd_id, False, item)

    # ------------------------------------------------------------------
    # Window message handlers (run on the tray thread)
    # ------------------------------------------------------------------
    def _on_notify(self, hwnd, msg, wparam, lparam):
        if lparam == win32con.WM_RBUTTONUP:
            self.update_status_menu()
            self._show_menu()
        elif lparam == win32con.WM_LBUTTONDBLCLK:
            threading.Thread(target=show_interface, name="ShowInterface", daemon=True).start()
        return 0

    def _on_command(self, hwnd, msg, wparam, lparam):
        cmd_id = win32gui.LOWORD(wparam)
        action = self.menu_actions_by_id.get(cmd_id)
        if action:
            # Run in a worker thread so slow actions (server restart,
            # shutdown) never freeze the tray message loop.
            threading.Thread(target=action, name=f"MenuAction-{cmd_id}", daemon=True).start()
        return 0

    def _on_status_updated(self, hwnd, msg, wparam, lparam):
        self.update_status_menu()
        return 0

    def _on_settings_updated(self, hwnd, msg, wparam, lparam):
        logging.debug("Settings updated")
        return 0

    def _on_taskbar_created(self, hwnd, msg, wparam, lparam):
        # Explorer restarted: the icon is gone, add it again.
        try:
            self._add_icon()
        except Exception:
            logging.exception("Could not re-add tray icon")
        return 0

    def _on_close(self, hwnd, msg, wparam, lparam):
        win32gui.DestroyWindow(hwnd)
        return 0

    def _on_destroy(self, hwnd, msg, wparam, lparam):
        self.terminate = True
        win32gui.PostQuitMessage(0)
        return 0

    def _show_menu(self):
        try:
            pos = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(self.hwnd)
            win32gui.TrackPopupMenu(
                self.menu,
                win32con.TPM_LEFTALIGN,
                pos[0], pos[1],
                0, self.hwnd, None,
            )
            win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        except pywintypes.error:
            pass


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def main():
    global tray_app  # without this, tray_app stays None everywhere else

    if not SERVER or not SERVER_ENDPOINT:
        logging.error("SERVER and SERVER_ENDPOINT must be set in %s", os.path.join(RUNNING_PATH, ".env"))
        raise SystemExit(1)

    logging.info("System tray app starting. Right-click the icon in the tray.")

    try:
        executable = get_server_executable()
        if not executable.is_file():
            logging.warning(
                "SERVER points to a missing file: %s (auto-restart will not work until you fix the path)",
                executable,
            )
    except ValueError:
        logging.exception("Invalid SERVER")
        raise SystemExit(1)

    tray_app = TrayIcon()
    tray_app.ready.wait(timeout=10)

    if tray_app.terminate and not tray_app.is_alive():
        logging.error("Tray icon could not be created. See errors above.")
        raise SystemExit(1)

    threading.Thread(target=server_worker, name="ServerMonitor", daemon=True).start()
    threading.Thread(target=settings_worker, name="SettingsMonitor", daemon=True).start()

    try:
        while not stop_event.is_set() and tray_app.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        logging.info("Exiting...")
    finally:
        # Idempotent: if Shutdown was clicked, this just waits for it to finish.
        tray_app.stop()
        tray_app.join(timeout=3)


if __name__ == "__main__":
    main()