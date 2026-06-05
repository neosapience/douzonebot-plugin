"""
Local launcher for Douzone Bot.

Launches a separate Chrome instance for automation (normal Chrome stays open)
and opens the web dashboard at localhost:5000.

CLI usage (from BOT_DIR):
    uv run python ui/launcher.py --chrome           # Launch Chrome with auto port
    uv run python ui/launcher.py --dashboard        # Launch dashboard with auto port
    uv run python ui/launcher.py --find-port 9444   # Find free port starting from 9444
"""
import argparse
import json
import os
import sys
import subprocess
import time
import urllib.request
import webbrowser
import platform
import shutil
import socket
import yaml

# Configuration
FLASK_PORT = 5000
FLASK_URL = f"http://localhost:{FLASK_PORT}"
DEFAULT_DOUZONE_URL = "https://erp.neosapience.com"
DEFAULT_CHROME_DEBUG_PORT = 9444

# Config search paths (same order as src/config.py)
_CONFIG_PATHS = [
    os.path.join(os.path.expanduser("~"), "douzone-bot", "config.yaml"),
    os.path.join(os.path.expanduser("~"), ".config", "douzone-bot", "config.yaml"),
]


def get_chrome_path():
    """Find Chrome/Chromium executable path for the current OS."""
    system = platform.system()
    if system == "Windows":
        paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        for p in paths:
            if os.path.exists(p):
                return p
    elif system == "Darwin":
        path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(path):
            return path
    elif system == "Linux":
        for name in ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]:
            path = shutil.which(name)
            if path:
                return path
    return None


def is_port_in_use(port):
    """Check if a TCP port is already listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


def wait_for_port(port, timeout=10):
    """Wait until a port is listening, or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        if is_port_in_use(port):
            return True
        time.sleep(0.5)
    return False


def _cdp_responds(port: int, timeout: float = 1.0) -> bool:
    try:
        resp = urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=timeout)
        return resp.status == 200
    except Exception:
        return False


def wait_for_cdp_ready(port, timeout=20):
    """Wait until Chrome's CDP endpoint at `port` answers /json/version.

    TCP listen alone is not enough — Chrome opens the port before CDP is wired up.
    Polls every 0.25s. After the loop exits, makes one final probe to cover the
    timeout-boundary case where Chrome became ready in the last interval.
    """
    start = time.time()
    while time.time() - start < timeout:
        if is_port_in_use(port) and _cdp_responds(port):
            return True
        time.sleep(0.25)
    return _cdp_responds(port, timeout=2.0)


def find_free_port(start=9444, scan_range=20):
    """Find a free TCP port starting from `start`.

    Scans up to `scan_range` ports. Returns the first free port,
    or raises RuntimeError if none found.
    """
    for port in range(start, start + scan_range):
        if not is_port_in_use(port):
            return port
    raise RuntimeError(f"No free port found in range {start}-{start + scan_range - 1}")


def _find_config_path():
    """Return the first existing config.yaml path, or the default write path."""
    for config_path in _CONFIG_PATHS:
        if os.path.exists(config_path):
            return config_path
    # Default: ~/douzone-bot/config.yaml (DATA_DIR)
    return _CONFIG_PATHS[0]


def _load_config_yaml():
    """Load config.yaml from known search paths, return dict or {}."""
    for config_path in _CONFIG_PATHS:
        try:
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        except (FileNotFoundError, yaml.YAMLError):
            continue
    return {}


def update_config_port(key, port):
    """Update a port value in config.yaml (e.g., chrome_debug_port).

    Creates the file/directory if needed.
    """
    config_path = _find_config_path()
    os.makedirs(os.path.dirname(config_path), exist_ok=True)

    config = {}
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        pass

    if config.get(key) == port:
        return  # Already set

    config[key] = port
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
    print(f"[+] Updated {key}: {port} in {config_path}")


def get_douzone_url():
    """Read douzone_url from config.yaml, fall back to default."""
    return _load_config_yaml().get("douzone_url", DEFAULT_DOUZONE_URL)


def get_chrome_debug_port():
    """Read chrome_debug_port from config.yaml, fall back to default."""
    return _load_config_yaml().get("chrome_debug_port", DEFAULT_CHROME_DEBUG_PORT)


def launch_chrome(auto_port=True):
    """Launch a separate Chrome instance for automation.

    Uses a persistent profile at ~/.douzone-chrome so Douzone login
    persists across sessions. The user's normal Chrome stays open.
    Automatically opens the Douzone groupware page.

    If auto_port is True and the configured port is busy (by a non-Chrome
    process), automatically finds a free port and updates config.yaml.
    """
    port = get_chrome_debug_port()

    if is_port_in_use(port):
        # Check if it's already our Chrome (CDP responds)
        try:
            import urllib.request
            resp = urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=2)
            if resp.status == 200:
                print(f"[+] Chrome already running on port {port}")
                return True
        except Exception:
            pass

        # Port is occupied by something else
        if auto_port:
            old_port = port
            port = find_free_port(port + 1)
            print(f"[+] Port {old_port} occupied, using {port} instead")
            update_config_port("chrome_debug_port", port)
        else:
            print(f"[+] Chrome debug port {port} already in use, skipping launch.")
            return True

    chrome_path = get_chrome_path()
    if not chrome_path:
        print("[-] Error: Google Chrome not found.")
        print("    Install Chrome or set the path manually.")
        return False

    douzone_url = get_douzone_url()
    print(f"[+] Launching automation Chrome → {douzone_url}")

    # Persistent profile directory (login persists across sessions)
    profile_dir = os.path.join(os.path.expanduser("~"), ".douzone-chrome")
    os.makedirs(profile_dir, exist_ok=True)

    flags = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--remote-allow-origins=*",
        "--start-maximized",
        douzone_url,
    ]

    try:
        if platform.system() == "Windows":
            subprocess.Popen(
                [chrome_path] + flags,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        else:
            subprocess.Popen(
                [chrome_path] + flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        if wait_for_cdp_ready(port):
            print(f"[+] Chrome CDP ready on port {port}")
            return True
        else:
            print(f"[-] Chrome did not become CDP-ready on port {port} within timeout.")
            return False
    except Exception as e:
        print(f"[-] Error launching Chrome: {e}")
        return False


def _is_our_dashboard(port):
    """Check if the process on `port` is our Flask dashboard (via /health).

    Checks for the 'app' field to distinguish from other services
    (FastAPI, VS Code, etc.) that may also return {"status": "ok"}.
    """
    try:
        resp = urllib.request.urlopen(
            f"http://localhost:{port}/health", timeout=2
        )
        data = json.loads(resp.read())
        return data.get("app") == "douzone-bot"
    except Exception:
        return False


def launch_flask_server(port=None):
    """Start the Flask dashboard server if not already running.

    If port is None, uses FLASK_PORT (5000) as start and auto-increments
    if occupied.  Verifies ownership via /health before declaring
    "already running" (prevents false positives from VS Code, etc.).

    Returns (proc, actual_port) tuple. proc is None if already running.
    """
    if port is None:
        port = FLASK_PORT

    if is_port_in_use(port):
        if _is_our_dashboard(port):
            print(f"[+] Dashboard server already running on port {port}")
            return None, port
        # Port occupied by another app — find a free one
        old_port = port
        port = find_free_port(port + 1, scan_range=10)
        print(f"[+] Port {old_port} occupied by another app, using {port} instead")

    print(f"[+] Starting dashboard server on port {port}...")
    server_script = os.path.join(os.path.dirname(__file__), "server.py")

    proc = subprocess.Popen(
        [sys.executable, server_script, "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    if wait_for_port(port, timeout=15):
        print(f"[+] Dashboard server listening on port {port}")
        print(f"DASHBOARD_URL=http://localhost:{port}")
        return proc, port
    else:
        print(f"[-] Dashboard server did not start within timeout.")
        stderr_output = proc.stderr.read().decode(errors="replace").strip()
        if stderr_output:
            print(f"[-] Server error output:\n{stderr_output}")
        proc.terminate()
        return None, port


def main():
    """Interactive launcher (launches both Chrome + dashboard)."""
    print("=" * 50)
    print("   Douzone Bot - Local Launcher")
    print("=" * 50)

    flask_proc = None

    # 1. Start the Flask dashboard server
    flask_proc, flask_port = launch_flask_server()
    if not is_port_in_use(flask_port):
        print("\n[-] Failed to start dashboard server.")
        print("    Try manually: python ui/server.py")
        input("Press Enter to exit...")
        return

    flask_url = f"http://localhost:{flask_port}"

    # 2. Launch automation Chrome (if not already running)
    if not launch_chrome():
        print("\n[-] Failed to launch Chrome.")
        print("    Close any existing automation Chrome and retry.")
        input("Press Enter to exit...")
        return

    print("\n[i] Your normal Chrome stays open. The new window is for automation only.")
    print("[i] Log into Douzone in the automation Chrome (first time only).")

    # 3. Open the web dashboard in user's default browser
    print(f"\n[+] Opening dashboard: {flask_url}")
    time.sleep(1)
    webbrowser.open(flask_url)

    print("\n[!] Keep this window open while using automation.")
    print("[!] Press Ctrl+C to exit.\n")

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[+] Shutting down...")
        if flask_proc:
            flask_proc.terminate()
            flask_proc.wait(timeout=5)
            print("[+] Dashboard server stopped.")
        print("[+] Done.")


def cli():
    """CLI entry point for skill-driven usage."""
    parser = argparse.ArgumentParser(description="Douzone Bot Launcher")
    parser.add_argument("--chrome", action="store_true",
                        help="Launch automation Chrome (auto port detection)")
    parser.add_argument("--force-restart", action="store_true",
                        help="Kill existing Chrome/dashboard before relaunching")
    parser.add_argument("--dashboard", action="store_true",
                        help="Launch dashboard server (auto port detection)")
    parser.add_argument("--find-port", type=int, metavar="START",
                        help="Find and print a free port starting from START")
    parser.add_argument("--port", type=int,
                        help="Override port (for --chrome or --dashboard)")
    args = parser.parse_args()

    # Ensure UTF-8 output on Windows
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.find_port is not None:
        port = find_free_port(args.find_port)
        print(f"FREE_PORT={port}")
        return

    if args.chrome:
        if args.force_restart:
            port = args.port or get_chrome_debug_port()
            print(f"[+] Force-restarting: killing Chrome on port {port}...")
            import signal
            # Find and kill Chrome using the debug port
            try:
                if platform.system() == "Darwin":
                    subprocess.run(["pkill", "-f", f"--remote-debugging-port={port}"],
                                   capture_output=True, timeout=5)
                elif platform.system() == "Windows":
                    subprocess.run(["taskkill", "/F", "/FI",
                                    f"COMMANDLINE eq *--remote-debugging-port={port}*"],
                                   capture_output=True, timeout=5)
                else:
                    subprocess.run(["pkill", "-f", f"--remote-debugging-port={port}"],
                                   capture_output=True, timeout=5)
                time.sleep(1)
            except Exception as e:
                print(f"[!] Kill attempt: {e}")
        if args.port:
            update_config_port("chrome_debug_port", args.port)
        ok = launch_chrome(auto_port=True)
        port = get_chrome_debug_port()
        print(f"CHROME_PORT={port}")
        print(f"CHROME_OK={'true' if ok else 'false'}")
        chrome_path = get_chrome_path()
        if chrome_path:
            print(f"CHROME_PATH={chrome_path}")
        return

    if args.dashboard:
        port = args.port or FLASK_PORT
        proc, actual_port = launch_flask_server(port=port)
        ok = is_port_in_use(actual_port)
        print(f"DASHBOARD_PORT={actual_port}")
        print(f"DASHBOARD_OK={'true' if ok else 'false'}")
        return

    # No subcommand — run interactive launcher
    main()


if __name__ == "__main__":
    cli()
