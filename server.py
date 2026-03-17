#!/usr/bin/env python3
"""
Onion Search MCP Server.

Provides three tools:
- get_sources: Search DuckDuckGo anonymously through Tor
- fetch_pages: Fetch full page content for search results by index
- fetch_specific_page: Fetch a specific URL directly

All operations are routed through the TUI daemon (tor_tui.py) which manages the
persistent Tor connection. If the TUI is not running, it will be auto-launched.

Supports all platforms: macOS, Linux, Windows.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP


# ---------------------------------------------------------------------------
# TUI Session Detection
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
SESSION_FILE = SCRIPT_DIR / ".onion-session.json"


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def check_tui_session() -> Optional[dict]:
    """
    Check for an active TUI session.

    Returns:
        Session dict with 'pid' and 'port' if valid session exists, None otherwise.
    """
    if not SESSION_FILE.exists():
        return None

    try:
        with open(SESSION_FILE, "r") as f:
            session = json.load(f)

        pid = session.get("pid")
        port = session.get("port")

        if pid and port and _is_process_alive(pid):
            return session

        # Session file exists but process is dead - clean it up
        SESSION_FILE.unlink(missing_ok=True)
        return None
    except (json.JSONDecodeError, OSError):
        SESSION_FILE.unlink(missing_ok=True)
        return None


def send_tui_command(port: int, command: dict, timeout: float = 120.0) -> Optional[dict]:
    """
    Send a command to the TUI and receive the response.

    Args:
        port: The TUI socket port
        command: The command dict to send
        timeout: Socket timeout in seconds

    Returns:
        Response dict from TUI, or None if communication failed.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", port))

        # Send command
        sock.sendall(json.dumps(command).encode())

        # Receive response
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            # Check for complete JSON
            try:
                json.loads(data.decode())
                break
            except json.JSONDecodeError:
                continue

        if data:
            return json.loads(data.decode())
        return None

    except Exception:
        return None
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# TUI Auto-Launch Functions
# ---------------------------------------------------------------------------


def _is_macos() -> bool:
    """Check if the current platform is macOS."""
    return sys.platform == "darwin"


def _is_windows() -> bool:
    """Check if the current platform is Windows."""
    return sys.platform == "win32"


def _is_linux() -> bool:
    """Check if the current platform is Linux."""
    return sys.platform.startswith("linux")

def has_desktop_environment() -> bool:
    """Check if a desktop environment is available for launching a visible TUI."""
    if _is_macos():
        # macOS always has a desktop when running
        return True
    elif _is_linux():
        # Check for DISPLAY or WAYLAND_DISPLAY environment variables
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    elif _is_windows():
        # Check if we're in an interactive session
        try:
            import ctypes
            return ctypes.windll.user32.GetDesktopWindow() != 0
        except Exception:
            return True  # Assume desktop is available
    return False


def launch_tui_on_desktop() -> bool:
    """
    Launch the TUI in a visible terminal window on the user's desktop.

    Returns:
        True if launch was successful, False otherwise.
    """
    tui_script = SCRIPT_DIR / "tor_tui.py"

    if _is_windows():
        venv_python = SCRIPT_DIR / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = SCRIPT_DIR / ".venv" / "bin" / "python"

    if not venv_python.exists():
        return False

    try:
        if _is_macos():
            # macOS: Use `open` to launch the .command file
            # This opens Terminal.app without requiring automation permissions
            launcher_script = SCRIPT_DIR / "Start Onion Session.command"
            if launcher_script.exists():
                subprocess.Popen(["open", str(launcher_script)])
                return True
            # Fallback: create a temporary .command file and open it
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".command", delete=False
            ) as f:
                f.write(f'#!/bin/bash\ncd "{SCRIPT_DIR}" && "{venv_python}" "{tui_script}"\n')
                temp_script = f.name
            os.chmod(temp_script, 0o755)
            subprocess.Popen(["open", temp_script])
            return True

        elif _is_linux():
            # Linux: Try common terminal emulators in order of preference
            terminals = [
                ["gnome-terminal", "--", str(venv_python), str(tui_script)],
                ["konsole", "-e", str(venv_python), str(tui_script)],
                ["xfce4-terminal", "-e", f"{venv_python} {tui_script}"],
                ["xterm", "-e", str(venv_python), str(tui_script)],
            ]
            for terminal_cmd in terminals:
                try:
                    subprocess.Popen(terminal_cmd, cwd=str(SCRIPT_DIR))
                    return True
                except FileNotFoundError:
                    continue
            return False

        elif _is_windows():
            # Windows: Use cmd.exe start command
            subprocess.Popen(
                f'start cmd /k "cd /d "{SCRIPT_DIR}" && "{venv_python}" "{tui_script}""',
                shell=True
            )
            return True

    except Exception:
        return False

    return False


def launch_tui_headless() -> bool:
    """
    Launch the TUI in headless mode (background process).

    Returns:
        True if launch was successful, False otherwise.
    """
    tui_script = SCRIPT_DIR / "tor_tui.py"

    if _is_windows():
        venv_python = SCRIPT_DIR / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = SCRIPT_DIR / ".venv" / "bin" / "python"

    if not venv_python.exists():
        return False

    try:
        if _is_windows():
            # Windows: Use subprocess with CREATE_NO_WINDOW flag
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                [str(venv_python), str(tui_script), "--headless"],
                cwd=str(SCRIPT_DIR),
                creationflags=CREATE_NO_WINDOW
            )
        else:
            # Unix: Use subprocess with proper detachment
            subprocess.Popen(
                [str(venv_python), str(tui_script), "--headless"],
                cwd=str(SCRIPT_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        return True
    except Exception:
        return False


def wait_for_tui_ready(timeout: float = 60.0, poll_interval: float = 0.5) -> Optional[dict]:
    """
    Wait for the TUI to start and become ready.

    Polls the session file until it exists and the TUI socket is responsive.

    Args:
        timeout: Maximum time to wait in seconds.
        poll_interval: Time between polls in seconds.

    Returns:
        Session dict if TUI is ready, None if timeout reached.
    """
    start_time = time.time()
    while (time.time() - start_time) < timeout:
        session = check_tui_session()
        if session:
            # Verify socket is actually accepting connections
            try:
                response = send_tui_command(session["port"], {"command": "ping"}, timeout=5.0)
                if response and response.get("success"):
                    return session
            except Exception:
                pass
        time.sleep(poll_interval)
    return None


def ensure_tui_running() -> dict:
    """
    Ensure a TUI session is running, launching one if necessary.

    Returns:
        Session dict with 'pid' and 'port'.

    Raises:
        RuntimeError: If TUI cannot be started or reached.
    """
    # Check for existing TUI session
    session = check_tui_session()
    if session:
        return session

    # No TUI running - need to launch one
    config = load_config()
    headless_mode = config.get("tui", {}).get("headless", False)
    raw_startup_timeout = config.get("tor", {}).get("startup_timeout_seconds", 240)
    try:
        startup_timeout = float(raw_startup_timeout)
    except (TypeError, ValueError):
        startup_timeout = 240.0
    startup_timeout = max(startup_timeout, 30.0)
    wait_timeout = startup_timeout + 20.0

    if headless_mode or not has_desktop_environment():
        # Launch in headless mode
        if not launch_tui_headless():
            raise RuntimeError(
                "Failed to launch TUI in headless mode. "
                "Please start it manually with: python tor_tui.py --headless"
            )
    else:
        # Launch in visible terminal window
        if not launch_tui_on_desktop():
            # Try headless as fallback
            if not launch_tui_headless():
                raise RuntimeError(
                    "Failed to launch TUI. Please start it manually:\n"
                    "  - Double-click 'Start Onion Session.command' (macOS)\n"
                    "  - Run './Start Onion Session.sh' (Linux)\n"
                    "  - Double-click 'Start Onion Session.bat' (Windows)"
                )

    # Wait for TUI to be ready
    session = wait_for_tui_ready(timeout=wait_timeout)
    if not session:
        raise RuntimeError(
            f"TUI failed to start within {int(wait_timeout)} seconds. "
            "Check for errors in the TUI window or try starting it manually."
        )

    return session


# ---------------------------------------------------------------------------
# Configuration Loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load configuration from config.toml."""
    config_path = Path(__file__).parent / "config.toml"
    if not config_path.exists():
        raise RuntimeError(
            "config.toml not found. Run 'python installer.py' first."
        )
    with open(config_path, "rb") as f:
        return tomllib.load(f)


# Load config at module level
try:
    CONFIG = load_config()
except RuntimeError:
    # Allow import without config for testing purposes
    CONFIG = None

# Initialize MCP server
mcp = FastMCP(name="onion-search-mcp")


# ---------------------------------------------------------------------------
# Global State (minimal - TUI handles most state)
# ---------------------------------------------------------------------------

_fetch_pages_called: bool = False
_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


_get_sources_description = f"""\
Search DuckDuckGo anonymously through Tor. Accepts 1-3 search queries, returning up to 5 results \
per query (max 15 total). Results are indexed linearly across all queries and deduplicated by URL.

WHEN TO USE MULTIPLE QUERIES:
- Use multiple queries when the topic is ambiguous or could be phrased different ways
- Use multiple queries when different phrasings might yield complementary results
- Use multiple queries when the question spans multiple distinct concepts
- Stick with a single well-crafted query when the information need is straightforward and specific

The goal is appropriate query count, not always maximizing to three. A focused single query often \
outperforms multiple vague ones.

CURRENT DATE AND TIME: {datetime.now().strftime("%Y-%m-%d %H:%M")}
Use this to inform your search queries. Include the current year for time-sensitive topics, \
and consider recency when evaluating which results to fetch.

IMPORTANT: After calling this tool, you MUST call fetch_pages to retrieve the full content \
of the relevant results. This is required to provide comprehensive answers to user questions. \
The search results only contain snippets - the full page content is needed for accurate responses."""


@mcp.tool(description=_get_sources_description)
def get_sources(queries: list[str]) -> str:
    global _fetch_pages_called

    # Validate queries
    if not queries or len(queries) == 0:
        raise ValueError("At least one query is required.")
    if len(queries) > 3:
        raise ValueError("Maximum of 3 queries allowed.")

    # Ensure TUI is running (auto-launches if needed)
    session = ensure_tui_running()

    # Route search through TUI
    response = send_tui_command(session["port"], {
        "command": "search",
        "queries": queries,
    })

    if not response:
        raise RuntimeError("Failed to communicate with TUI. Please check if it's running.")

    if response.get("success"):
        with _state_lock:
            _fetch_pages_called = False
        return response.get("results", "")
    else:
        raise RuntimeError(response.get("error", "Unknown error from TUI"))


@mcp.tool()
def fetch_pages(indexes: list[int]) -> str:
    """
    Fetch the full page content for search results by their index numbers.

    This tool MUST be called after get_sources. You can only call this once per search.
    Pass indexes like [1, 3, 11] for the results you want to fetch.

    IMPORTANT: With multi-query searches, up to 15 results may be available (5 per query, minus
    duplicates), but you can only fetch a maximum of 5 pages per fetch_pages call. Choose the most 
    relevant indexes based on the abbreviated snippets and source credibility shown in get_sources
    results. Review all query sections before selecting - relevant results may appear under any query.
    NEVER make a fetch_pages request with more than five total indexes.

    ERROR conditions:
    - If called without a preceding get_sources call
    - If more than 5 indexes are requested
    """
    # Ensure TUI is running (auto-launches if needed)
    session = ensure_tui_running()

    # Route fetch through TUI
    response = send_tui_command(session["port"], {
        "command": "fetch_pages",
        "indexes": indexes,
    })

    if not response:
        raise RuntimeError("Failed to communicate with TUI. Please check if it's running.")

    if response.get("success"):
        return response.get("results", "")
    else:
        raise RuntimeError(response.get("error", "Unknown error from TUI"))


@mcp.tool()
def fetch_specific_page(url: str) -> str:
    """
    Fetch a specific page by URL. Use this pretty much only when the user provides a direct URL in their prompt.

    For general web research, ALWAYS use the get_sources -> fetch_pages workflow instead. NEVER use this tool to fetch links from get_sources. This tool is meant for cases where a URL is provided to you by the user or another tool.
    """
    # Ensure TUI is running (auto-launches if needed)
    session = ensure_tui_running()

    # Route fetch through TUI
    response = send_tui_command(session["port"], {
        "command": "fetch_specific_page",
        "url": url,
    })

    if not response:
        raise RuntimeError("Failed to communicate with TUI. Please check if it's running.")

    if response.get("success"):
        return response.get("results", "")
    else:
        raise RuntimeError(response.get("error", "Unknown error from TUI"))


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
