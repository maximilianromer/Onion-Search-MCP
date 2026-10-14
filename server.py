#!/usr/bin/env python3
"""
Tor Browser MCP Server.

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


def launch_tui_headless(extra_args: list[str] | None = None) -> bool:
    """
    Launch the TUI in headless mode (background process).

    Args:
        extra_args: Extra CLI arguments appended to the daemon argv
            (e.g. ["--mode", "browser"]).

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

    daemon_args = [str(venv_python), str(tui_script), "--headless"]
    if extra_args:
        daemon_args.extend(extra_args)

    try:
        if _is_windows():
            # Windows: Use subprocess with CREATE_NO_WINDOW flag
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                daemon_args,
                cwd=str(SCRIPT_DIR),
                creationflags=CREATE_NO_WINDOW
            )
        else:
            # Unix: Use subprocess with proper detachment
            subprocess.Popen(
                daemon_args,
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

    if MODE == "browser" or headless_mode or not has_desktop_environment():
        # Browser mode always launches the no-UI daemon (socket-driven only).
        extra_args = ["--mode", "browser"] if MODE == "browser" else None
        # Launch in headless mode
        if not launch_tui_headless(extra_args=extra_args):
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


def _validate_mode(value: str, source: str) -> str:
    """Return the canonical mode for `value`, otherwise raise a clear error.

    Valid modes: "browser", and "tui" (with "native" accepted as a legacy
    alias that maps to "tui"). Anything else is rejected with an explicit
    message listing the valid options.
    """
    if value == "browser":
        return "browser"
    if value in ("tui", "native"):
        return "tui"
    raise ValueError(
        f"Invalid Tor Browser MCP mode: {value!r} (from {source}). "
        "The only available modes are 'tui' and 'browser' "
        "('native' is accepted as an alias for 'tui'). "
        "Fix [server] mode in config.toml, or relaunch with --mode tui|browser."
    )


def _resolve_mode() -> str:
    """Resolve the session mode: CLI --mode > config [server] mode > default "browser".

    Only "tui" and "browser" are valid modes ("native" is accepted as a legacy
    alias for "tui"). Any other value — from the --mode flag or from
    [server] mode in config.toml — raises a clear ValueError listing the valid
    options. The --mode flag (and its value) is also stripped from sys.argv so
    that mcp.run() never sees it.
    """
    import argparse

    mode: Optional[str] = None
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", dest="mode", default=None)
    try:
        known, _ = parser.parse_known_args()
        mode = known.mode
    except SystemExit:
        # Malformed --mode on argv; fall back to config below.
        mode = None

    # Strip "--mode <value>" / "--mode=<value>" from sys.argv.
    argv = sys.argv
    cleaned: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--mode":
            i += 2
            continue
        if arg.startswith("--mode="):
            i += 1
            continue
        cleaned.append(arg)
        i += 1
    sys.argv[:] = cleaned

    if mode is not None:
        return _validate_mode(mode, "the --mode CLI flag")

    cfg_mode = (CONFIG or {}).get("server", {}).get("mode")
    if cfg_mode is None:
        # Key absent -> default (config.toml often has no [server] mode set).
        return "browser"
    return _validate_mode(cfg_mode, "[server] mode in config.toml")


# Computed at import; server.py only runs --mode handling as a script or an
# MCP entry point, both of which have a reliable sys.argv.
MODE: str = _resolve_mode()

# Initialize MCP server
mcp = FastMCP(name="tor-browser-mcp")


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
# Browser automation tools (registered only when MODE == "browser")
# ---------------------------------------------------------------------------
# Thin proxies: each sends {"command": "browser_<action>", ...} to the daemon
# and formats the response. Structured payloads are JSON (indent=2); the text
# tools (get_url/get_title/get_text/get_html/get_attribute) return raw strings.
# Element refs are strings like "e7" produced by browser_find; they die on
# navigation, so call browser_status / browser_wait_ready after navigating and
# re-find elements on a new page. Screenshots are base64 PNG. Everything runs
# through the daemon's Tor Browser, i.e. over Tor.


def _browser_call(action: str, payload: dict, timeout: float = 120.0) -> dict:
    """Send one browser_* command to the daemon and validate its response."""
    session = ensure_tui_running()
    if MODE == "browser":
        # Guard against a mixed daemon: an already-running tui-mode daemon
        # would answer "Unknown command" for every browser_* request.
        ping = send_tui_command(session["port"], {"command": "ping"}, timeout=5.0)
        if ping and ping.get("success") and ping.get("mode") == "tui":
            raise RuntimeError(
                "The running Onion daemon is in tui mode; stop it and restart "
                "with --mode browser (mixed daemons are not supported)."
            )
    request = {"command": f"browser_{action}"}
    request.update(payload)
    response = send_tui_command(session["port"], request, timeout=timeout)
    if not response:
        raise RuntimeError("Failed to communicate with the browser daemon.")
    if not response.get("success"):
        raise RuntimeError(response.get("error", "Unknown browser error"))
    return response


def _as_json(resp: dict) -> str:
    """Format a daemon response as indented JSON, dropping the success flag."""
    return json.dumps(
        {k: v for k, v in resp.items() if k != "success"},
        indent=2,
        ensure_ascii=False,
    )


def browser_status() -> str:
    """
    Report the state of the Tor Browser session: mode, Tor liveness, browser readiness, headful/headless, current URL and title, every window/tab (handle, url, title), virtual display info and uptime. Never launches a browser by itself. After navigating, use this or browser_wait_ready to confirm the page loaded. All traffic goes through Tor.
    """
    return _as_json(_browser_call("status", {}, timeout=30.0))


def browser_navigate(url: str, wait: bool = True, wait_timeout: float = 30.0) -> str:
    """
    Navigate the Tor Browser to a URL (adds https:// when no scheme is given). With wait=True (default) polls until document.readyState is "complete" or wait_timeout seconds elapse. Navigation invalidates all element refs - re-find them with browser_find; check browser_status/browser_wait_ready afterwards. All traffic goes through Tor.
    """
    return _as_json(_browser_call(
        "navigate",
        {"url": url, "wait": wait, "wait_timeout": wait_timeout},
        timeout=max(120.0, float(wait_timeout) + 60.0),
    ))


def browser_click(ref: str, wait: bool = False) -> str:
    """
    Click the element identified by ref (refs come from browser_find, look like "e7", and die on navigation). Returns the ref and the resulting URL. Use wait=True to briefly wait for page readiness after the click. The browser runs through Tor.
    """
    return _as_json(_browser_call("click", {"ref": ref, "wait": wait}))


def browser_fill(ref: str, text: str, clear: bool = True, submit: bool = False) -> str:
    """
    Type into the element identified by ref (from browser_find; refs die on navigation). Clears the field first unless clear=false, then inserts text; set submit=true to press ENTER afterwards. Returns the ref, whether it submitted, and the current URL. The browser runs through Tor.
    """
    return _as_json(_browser_call(
        "fill",
        {"ref": ref, "text": text, "clear": clear, "submit": submit},
    ))


def browser_keys(keys: str, ref: str = "") -> str:
    """
    Send a key combination to the page, e.g. "ENTER", "CTRL+A", "PAGEDOWN" (tokens joined with "+"; single characters are literal, aliases like CTRL map to selenium keys). Optionally targets an element ref from browser_find (refs die on navigation), otherwise the active element. Returns the keys sent and the current URL. The browser runs through Tor.
    """
    return _as_json(_browser_call("keys", {"keys": keys, "ref": ref}))


def browser_execute_js(script: str, args: list = []) -> str:
    """
    Run JavaScript in the current page and return {result, truncated}; JSON-serializable args are passed as script arguments, returned WebElements become element refs (from browser_find's scheme; refs die on navigation). String results are capped by the daemon's max_output_chars with a truncation marker. All traffic goes through Tor.
    """
    return _as_json(_browser_call("execute_js", {"script": script, "args": args}))


def browser_get_text(ref: str = "", max_chars: int = 0) -> str:
    """
    Return the visible text of the element identified by ref (from browser_find; refs die on navigation), or of the whole page (document.body.innerText) when ref is empty. Long output is truncated to max_chars (0 = the daemon's max_output_chars) with a truncation marker. Content arrives over Tor.
    """
    resp = _browser_call("get_text", {"ref": ref, "max_chars": max_chars})
    return str(resp.get("text", ""))


def browser_get_html(ref: str = "", max_chars: int = 0) -> str:
    """
    Return the HTML of the element identified by ref (outerHTML; refs come from browser_find and die on navigation), or the full page source when ref is empty. Output is truncated to max_chars (0 = the daemon's max_output_chars) with a truncation marker. Content arrives over Tor.
    """
    resp = _browser_call("get_html", {"ref": ref, "max_chars": max_chars})
    return str(resp.get("html", ""))


def browser_get_url() -> str:
    """
    Return the current page URL as plain text (no JSON wrapper). After browser_navigate, wait with browser_status or browser_wait_ready before trusting it. The page is loaded over Tor.
    """
    resp = _browser_call("get_url", {})
    return str(resp.get("url", ""))


def browser_get_title() -> str:
    """
    Return the current page title as plain text (no JSON wrapper). After navigation, confirm readiness with browser_status or browser_wait_ready first. The page is loaded over Tor.
    """
    resp = _browser_call("get_title", {})
    return str(resp.get("title", ""))


def browser_find(selector: str, by: str = "css", limit: int = 10, include_hidden: bool = False) -> str:
    """
    Find elements in the current page and return a JSON list of {ref, tag, text, id, name, href, visible} (text truncated to 80 chars, at most limit entries). by is one of css, xpath, id, link_text, partial_link_text. The returned refs are what all other browser_* tools take, and they are invalidated by navigation - re-find after any page change. The browser runs through Tor.
    """
    resp = _browser_call(
        "find",
        {"selector": selector, "by": by, "limit": limit, "include_hidden": include_hidden},
    )
    return json.dumps(resp.get("results", []), indent=2, ensure_ascii=False)


def browser_get_attribute(ref: str, attribute: str) -> str:
    """
    Return one attribute of the element identified by ref (from browser_find; refs die on navigation) as plain text. The pseudo-attributes "text" and "html" return the element's visible text or outerHTML instead. Content arrives over Tor.
    """
    resp = _browser_call("get_attribute", {"ref": ref, "attribute": attribute})
    return str(resp.get("value", ""))


def browser_screenshot() -> str:
    """
    Capture the current viewport as a PNG and return JSON {mime, base64, bytes} (render as data:image/png;base64,...; viewport-only, a geckodriver limitation). Use it to observe the page after navigating or clicking - element refs come from browser_find and die on navigation. The browser runs through Tor.
    """
    return _as_json(_browser_call("screenshot", {}, timeout=60.0))


def browser_back(wait: bool = True) -> str:
    """
    Go back in history and return the resulting {url, title}, optionally waiting for readiness (wait=True). Going back reloads a document, which invalidates every element ref - re-find them with browser_find and check browser_status/browser_wait_ready. All traffic goes through Tor.
    """
    return _as_json(_browser_call("back", {"wait": wait}))


def browser_forward(wait: bool = True) -> str:
    """
    Go forward in history and return the resulting {url, title}, optionally waiting for readiness (wait=True). The new document invalidates all element refs - re-find them with browser_find. All traffic goes through Tor.
    """
    return _as_json(_browser_call("forward", {"wait": wait}))


def browser_refresh(wait: bool = True) -> str:
    """
    Reload the current page and return the resulting {url, title}, optionally waiting for readiness (wait=True). Reloading invalidates every element ref - re-find them with browser_find. All traffic goes through Tor.
    """
    return _as_json(_browser_call("refresh", {"wait": wait}))


def browser_scroll(direction: str = "down", amount_px: int = 800, ref: str = "") -> str:
    """
    Scroll the page (direction: down, up, top or bottom; amount_px applies to down/up) and return the resulting {scroll_x, scroll_y}. With a ref (from browser_find, dies on navigation) the element is scrolled into view instead. The browser runs through Tor.
    """
    return _as_json(_browser_call(
        "scroll",
        {"direction": direction, "amount_px": amount_px, "ref": ref},
    ))


def browser_new_tab(url: str = "") -> str:
    """
    Open a new tab (optionally loading url, with https:// added when no scheme is given) and return {handle, url, window_count}. Switch to it with browser_switch_tab; refs from browser_find are per-page and must be re-found after any navigation. All traffic goes through Tor.
    """
    return _as_json(_browser_call("new_tab", {"url": url}))


def browser_switch_tab(handle: str = "", index: int = -1) -> str:
    """
    Switch to another tab by handle (from browser_status or browser_new_tab), or by zero-based index into the window list when handle is empty. Returns {handle, url, title, window_count}. Element refs are page-specific: after switching, re-find them with browser_find on the newly active tab. The browser runs through Tor.
    """
    return _as_json(_browser_call(
        "switch_tab",
        {"handle": handle, "index": index},
    ))


def browser_close_tab(handle: str = "") -> str:
    """
    Close a tab by handle (default: the active tab) and return {closed, remaining}. Refuses to close the last remaining tab - use browser_close for that. Switching or closing tabs does not preserve element refs across pages: re-find them with browser_find. The browser runs through Tor.
    """
    return _as_json(_browser_call("close_tab", {"handle": handle}))


def browser_wait_ready(timeout: float = 30.0, selector: str = "") -> str:
    """
    Wait until document.readyState is "complete" (up to timeout seconds) and, when a CSS selector is given, until an element matching it appears; returns {ready_state, url, waited_s}. Call it after browser_navigate (or any navigation) before using element refs from browser_find, which die on navigation. All traffic goes through Tor.
    """
    return _as_json(_browser_call(
        "wait_ready",
        {"timeout": timeout, "selector": selector},
        timeout=max(120.0, float(timeout) + 60.0),
    ))


def browser_close() -> str:
    """
    Shut the Tor Browser down (driver.quit plus the virtual display if the daemon started one) and return {closed: true}. Tor stays up; the next browser_* call re-warms a fresh browser. All element refs are discarded by this. All traffic goes through Tor.
    """
    return _as_json(_browser_call("close", {}, timeout=60.0))


_BROWSER_TOOLS = (
    browser_status,
    browser_navigate,
    browser_click,
    browser_fill,
    browser_keys,
    browser_execute_js,
    browser_get_text,
    browser_get_html,
    browser_get_url,
    browser_get_title,
    browser_find,
    browser_get_attribute,
    browser_screenshot,
    browser_back,
    browser_forward,
    browser_refresh,
    browser_scroll,
    browser_new_tab,
    browser_switch_tab,
    browser_close_tab,
    browser_wait_ready,
    browser_close,
)


def _register_browser_tools() -> None:
    """Register the 22 browser_* tools (browser mode only; never in tui mode)."""
    if MODE != "browser":
        return
    built = None
    try:
        from fastmcp.tools.function_tool import FunctionTool

        # Build every tool first so a partial failure cannot double-register.
        built = [FunctionTool.from_function(fn) for fn in _BROWSER_TOOLS]
    except Exception:
        built = None
    if built is not None:
        for tool in built:
            mcp.add_tool(tool)
    else:
        # Fallback: decorator-style registration at runtime.
        for fn in _BROWSER_TOOLS:
            mcp.tool(name=fn.__name__, description=fn.__doc__)(fn)


_register_browser_tools()


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
