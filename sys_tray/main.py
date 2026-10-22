"""
Minimal Windows system-tray icon inspired by SABnzbd's sabtraywin.py
+ server status + background workers + process management
"""
import os 
import sys 
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import win32con
import win32gui
import win32gui_struct
import pywintypes
import psutil
from dotenv import load_dotenv

load_dotenv()


def get_running_path():    
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
WINDOW_CLASS_NAME = "MyTrayIconDemo"
print("type", type(get_running_path()), get_running_path())
ICON_PATH = os.path.join(get_running_path(), "icon.ico")   # or None for default system icon
APP_NAME = "SABnzbd Demo"

SERVER_COMMAND = [
    os.getenv("SERVER"),
]

SERVER_ENDPOINT = os.getenv("SERVER_ENDPOINT")
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# Shared state
stop_event = threading.Event()
server_status = None          # None = Checking, True/False = Connected/Disconnected
server_status_lock = threading.Lock()
latest_settings = None
settings_lock = threading.Lock()
server_check_lock = threading.Lock()
settings_request_lock = threading.Lock()

tray_app = None


# ----------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------
def get_json(url: str):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "TrayDemo/1.0"},
    )
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
        raise ValueError("SERVER_COMMAND cannot be empty.")
    return Path(SERVER_COMMAND[0]).resolve()


def find_server_processes():
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

    processes = find_server_processes()
    if processes:
        logging.warning("Server endpoint failed. Restarting %s matching process(es).", len(processes))
        for process in processes:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                logging.exception("Cannot terminate server PID %s.", process.pid)
                return False

        _, alive = psutil.wait_procs(processes, timeout=PROCESS_STOP_TIMEOUT)
        for process in alive:
            try:
                logging.warning("Force-stopping unresponsive server PID %s.", process.pid)
                process.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        _, alive = psutil.wait_procs(alive, timeout=3)
        if alive:
            logging.error("Server process could not be stopped.")
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


def server_worker():
    """Periodic health check + automatic recovery."""
    while not stop_event.is_set():
        if server_check_lock.acquire(blocking=False):
            try:
                status = check_server()
                publish_server_status(status)

                if status:
                    logging.info("Server status: Connected")
                else:
                    logging.warning("Server status: Disconnected – attempting recovery")
                    if not stop_event.is_set():
                        recovered = restart_server()
                        status = check_server() if recovered and not stop_event.is_set() else False
                        publish_server_status(status)
                        logging.info(
                            "Server status after recovery: %s",
                            "Connected" if status else "Disconnected",
                        )
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
        status = check_server()
        publish_server_status(status)
        logging.info("Manual server status: %s", "Connected" if status else "Disconnected")

        if not status and not stop_event.is_set():
            recovered = restart_server()
            status = check_server() if recovered and not stop_event.is_set() else False
            publish_server_status(status)
    except Exception:
        logging.exception("Manual server check failed")
        publish_server_status(False)
    finally:
        server_check_lock.release()


def settings_worker():
    """Periodic settings refresh."""
    global latest_settings
    while not stop_event.is_set():
        if settings_request_lock.acquire(blocking=False):
            try:
                settings = fetch_settings()
                print(settings)
                if settings is not None:
                    with settings_lock:
                        latest_settings = settings
                    if tray_app is not None:
                        tray_app.post_message(WM_SETTINGS_UPDATED)
            except Exception:
                logging.exception("settings_worker error")
            finally:
                settings_request_lock.release()

        if stop_event.wait(SETTINGS_CHECK_INTERVAL):
            break


def refresh_settings_async():
    def worker():
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
        finally:
            settings_request_lock.release()

    threading.Thread(target=worker, name="SettingsRefresh", daemon=True).start()


# ----------------------------------------------------------------------
# Menu actions
# ----------------------------------------------------------------------
def show_interface():
    print("→ Show interface")


def open_complete_folder():
    print("→ Open complete folder")


def pause_resume():
    print("→ Pause / Resume toggled")


def pause_for(minutes: int):
    print(f"→ Pause for {minutes} minutes")


def read_rss():
    print("→ Read all RSS feeds")


def restart():
    print("→ Restart")


def restart_no_login():
    print("→ Restart without login")


def restart_localhost():
    print("→ Restart - 127.0.0.1:8080")


def shutdown():
    print("→ Shutdown requested")
    stop_event.set()
    if tray_app is not None:
        tray_app.stop()


# ----------------------------------------------------------------------
# Tray implementation
# ----------------------------------------------------------------------
class TrayIcon(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.terminate = False
        self.hwnd = None
        self.menu = None
        self.notify_id = None
        self.hicon = None

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
        for text, action, cmd_id in self.menu_options:
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

    def run(self):
        self._create_window()
        self._create_menu()
        self._add_icon()

        while not self.terminate:
            win32gui.PumpWaitingMessages()
            time.sleep(0.05)

        if self.notify_id:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, self.notify_id)
            except Exception:
                pass
        if self.menu:
            win32gui.DestroyMenu(self.menu)

    def stop(self):
        self.terminate = True
        stop_event.set()
        exe_name = Path(SERVER_COMMAND[0]).name
        
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", exe_name],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                shell=False,
            )

            if result.returncode == 0:
                logging.info("Terminated %s.", exe_name)
            else:
                logging.warning(
                    "taskkill failed: %s",
                    result.stderr.strip() or result.stdout.strip(),
                )

        except (OSError, subprocess.TimeoutExpired):
            logging.exception("Failed to terminate %s.", exe_name)

        finally:
            if self.hwnd:
                win32gui.DestroyWindow(self.hwnd)
                
        time.sleep(0.2)

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

        class_atom = win32gui.RegisterClass(wc)

        style = win32con.WS_OVERLAPPED | win32con.WS_SYSMENU
        self.hwnd = win32gui.CreateWindow(
            class_atom,
            WINDOW_CLASS_NAME,
            style,
            0, 0, win32con.CW_USEDEFAULT, win32con.CW_USEDEFAULT,
            0, 0, hinst, None,
        )
        win32gui.UpdateWindow(self.hwnd)

        if ICON_PATH and os.path.isfile(ICON_PATH):
            self.hicon = win32gui.LoadImage(
                0, ICON_PATH, win32con.IMAGE_ICON, 0, 0,
                win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
            )
        else:
            self.hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

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
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, self.notify_id)
        except Exception as e:
            print("Failed to add tray icon:", e)

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
                item, _ = win32gui_struct.PackMENUITEMINFO(
                    text=text,
                    wID=cmd_id,
                    fState=state,
                )
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
    def _on_notify(self, hwnd, msg, wparam, lparam):
        if lparam == win32con.WM_RBUTTONUP:
            self.update_status_menu()
            self._show_menu()
        elif lparam == win32con.WM_LBUTTONDBLCLK:
            show_interface()
        return True

    def _on_command(self, hwnd, msg, wparam, lparam):
        cmd_id = win32gui.LOWORD(wparam)
        if cmd_id in self.menu_actions_by_id:
            self.menu_actions_by_id[cmd_id]()
        return True

    def _on_status_updated(self, hwnd, msg, wparam, lparam):
        self.update_status_menu()
        return True

    def _on_settings_updated(self, hwnd, msg, wparam, lparam):
        logging.debug("Settings updated")
        return True

    def _on_taskbar_created(self, hwnd, msg, wparam, lparam):
        self.notify_id = None
        self._add_icon()
        return True

    def _on_destroy(self, hwnd, msg, wparam, lparam):
        self.terminate = True
        stop_event.set()
        return True

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
    print("System tray icon started. Right-click the icon in the tray.")

    try:
        executable = get_server_executable()
        if not executable.is_file():
            logging.warning(
                "SERVER_COMMAND points to a missing file: %s  "
                "(auto-restart will not work until you fix the path)",
                executable,
            )
    except ValueError:
        logging.exception("Invalid SERVER_COMMAND")
        raise SystemExit(1)

    tray_app = TrayIcon()

    # Start the two background workers
    threading.Thread(target=server_worker, name="ServerMonitor", daemon=True).start()
    threading.Thread(target=settings_worker, name="SettingsMonitor", daemon=True).start()

    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nExiting…")
        stop_event.set()
        tray_app.stop()