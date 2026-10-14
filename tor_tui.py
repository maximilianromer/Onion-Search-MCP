#!/usr/bin/env python3
"""
Persistent Tor Session TUI for Tor Browser MCP.

Redesigned with operation cards, spinners, and a single continuous flow.
"""

import atexit
import base64
import json
import logging
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.widgets import Static, Footer
from textual.binding import Binding
from textual.reactive import reactive
from textual.message import Message
from rich.text import Text as RichText
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
SESSION_FILE = SCRIPT_DIR / ".onion-session.json"
DEFAULT_SOCKET_PORT = 9160

# Default Tor ports — deliberately different from Tor Browser (9150/9151) so
# the user can have Tor Browser open at the same time without conflict.
DEFAULT_TOR_SOCKS_PORT = 9250
DEFAULT_TOR_CONTROL_PORT = 9251
# If defaults are busy (e.g. two MCP sessions), scan upward from here
ALT_TOR_PORT_START = 9252

# Spinner characters
BRAILLE_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # Light/fast operations
SEMICIRCLE_SPINNER = "◐◓◑◒"  # Page fetches (heavier operations)
SPINNER_INTERVAL = 0.1  # 100ms per frame

# Status indicators
STATUS_PENDING = "○"
STATUS_COMPLETE = "●"
STATUS_ERROR = "✗"

# Link highlight colors
HL_BG = "#1c3d6e"
HL_FG = "#e8eeff"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Header Widget - Block Letters ASCII Art Masthead (Style L)
# ---------------------------------------------------------------------------

class HeaderWidget(Static):
    """ASCII block letters masthead with TOR stacked over BROWSER MCP."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._build_display()

    def _build_display(self):
        lines = []
        # TOR in purple gradient (top), BROWSER MCP in pale yellow (bottom)
        # Pale yellow colors: #FFFCDA (light) -> #F0E5A8 (medium) -> #E5D890 (soft gold)
        lines.append("[#875FAF]████████╗ ██████╗ ██████╗  [/#875FAF]")
        lines.append("[#875FAF]╚══██╔══╝██╔═══██╗██╔══██╗ [/#875FAF]")
        lines.append("[#d7afff]   ██║   ██║   ██║██████╔╝ [/#d7afff]")
        lines.append("[#d7afff]   ██║   ██║   ██║██╔══██╗ [/#d7afff]")
        lines.append("[#d7afff]   ██║   ╚██████╔╝██║  ██║ [/#d7afff]")
        lines.append("[#d7afff]   ╚═╝    ╚═════╝ ╚═╝  ╚═╝ [/#d7afff]")
        lines.append("[#FFFCDA]██████╗ ██████╗   ██████╗ ██╗    ██╗███████╗███████╗██████╗    ███╗   ███╗ ██████╗██████╗ [/#FFFCDA]")
        lines.append("[#FFFCDA]██╔══██╗██╔══██╗ ██╔═══██╗██║    ██║██╔════╝██╔════╝██╔══██╗   ████╗ ████║██╔════╝██╔══██╗[/#FFFCDA]")
        lines.append("[#E5D890]██████╔╝██████╔╝ ██║   ██║██║ █╗ ██║███████╗█████╗  ██████╔╝   ██╔████╔██║██║     ██████╔╝[/#E5D890]")
        lines.append("[#E5D890]██╔══██╗██╔══██╗ ██║   ██║██║███╗██║╚════██║██╔══╝  ██╔══██╗   ██║╚██╔╝██║██║     ██║     [/#E5D890]")
        lines.append("[#E5D890]██████╔╝██║  ██║ ╚██████╔╝╚███╔███╔╝███████║███████╗██║  ██║   ██║ ╚═╝ ██║╚██████╗██║     [/#E5D890]")
        lines.append("[#E5D890]╚═════╝ ╚═╝  ╚═╝  ╚═════╝  ╚══╝╚══╝ ╚══════╝╚══════╝╚═╝  ╚═╝   ╚═╝     ╚═╝ ╚═════╝╚═╝     [/#E5D890]")
        self.update("\n".join(lines))


# ---------------------------------------------------------------------------
# Configuration Loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load configuration from config.toml."""
    import tomllib
    config_path = SCRIPT_DIR / "config.toml"
    if not config_path.exists():
        raise RuntimeError(
            "config.toml not found. Run 'python installer.py' first."
        )
    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise RuntimeError(
            f"config.toml is malformed: {e}. Please fix or re-run 'python installer.py'."
        )


# Set by main() from the --mode CLI flag; None when not given.
_CLI_MODE: Optional[str] = None


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


def resolve_mode() -> str:
    """Resolve the session mode: CLI --mode > config [server] mode > default.

    Resolution order: --mode CLI flag > config.toml [server] mode > default
    "browser". Only "tui" and "browser" are valid modes ("native" is accepted
    as a legacy alias for "tui"). Any other value raises a clear ValueError
    listing the valid options.
    """
    if _CLI_MODE is not None:
        return _validate_mode(_CLI_MODE, "the --mode CLI flag")
    try:
        cfg_mode = load_config().get("server", {}).get("mode")
    except Exception:
        # No/unreadable config.toml: startup fails later with a clearer
        # message anyway; fall back to the default mode here.
        return "browser"
    if cfg_mode is None:
        # Key absent -> default (config.toml often has no [server] mode set).
        return "browser"
    return _validate_mode(cfg_mode, "[server] mode in config.toml")


def _param_bool(value, default: bool = False) -> bool:
    """Coerce a socket/JSON parameter to bool."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _param_int(value, default: int = 0) -> int:
    """Coerce a socket/JSON parameter to int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _param_float(value, default: float = 0.0) -> float:
    """Coerce a socket/JSON parameter to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _param_str(value) -> str:
    """Coerce a socket/JSON parameter to str ('' for None)."""
    return "" if value is None else str(value)


# Deterministic element-ref stamping. Every located element gets a
# data-onion-ref attribute ("e1", "e2", ...) the first time it is seen; the
# counter lives on the page (window.__onionSeq) and dies with the document,
# so refs are deterministic for a given page and invalidated by navigation.
_STAMP_JS = """
  const sel = arguments[0], by = arguments[1], limit = arguments[2], hidden = arguments[3];
  window.__onionSeq = window.__onionSeq || 0;
  let loc = [];
  if (by === 'css') {
    loc = Array.from(document.querySelectorAll(sel));
  } else if (by === 'xpath') {
    const r = document.evaluate(sel, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    for (let i = 0; i < r.snapshotLength; i++) loc.push(r.snapshotItem(i));
  } else if (by === 'id') {
    const e = document.getElementById(sel);
    if (e) loc.push(e);
  } else if (by === 'link_text' || by === 'partial_link_text') {
    const want = sel.trim();
    loc = Array.from(document.querySelectorAll('a')).filter(function (a) {
      const t = (a.textContent || '').trim();
      return by === 'link_text' ? t === want : t.indexOf(want) !== -1;
    });
  }
  const out = [];
  for (const el of loc) {
    if (!el || !el.tagName) continue;
    if (!hidden && (el.offsetParent === null)) continue;
    if (!el.hasAttribute('data-onion-ref')) {
      window.__onionSeq += 1;
      el.setAttribute('data-onion-ref', 'e' + window.__onionSeq);
    }
    out.push({
      ref: el.getAttribute('data-onion-ref'),
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || '').trim().slice(0, 80),
      id: el.id || '',
      name: el.name || '',
      href: el.href || '',
      visible: el.offsetParent !== null
    });
    if (out.length >= limit) break;
  }
  return out;
"""


# ---------------------------------------------------------------------------
# Platform Detection
# ---------------------------------------------------------------------------

def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# Session File Management
# ---------------------------------------------------------------------------

def check_existing_session() -> Optional[dict]:
    """Check for an existing session file and validate the PID is alive."""
    if not SESSION_FILE.exists():
        return None

    try:
        with open(SESSION_FILE, "r") as f:
            session = json.load(f)

        pid = session.get("pid")
        if pid and _is_process_alive(pid):
            return session

        SESSION_FILE.unlink(missing_ok=True)
        return None
    except (json.JSONDecodeError, OSError):
        SESSION_FILE.unlink(missing_ok=True)
        return None


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    if _is_windows():
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


def write_session_file(port: int) -> None:
    """Write session file with PID and socket port."""
    session = {
        "pid": os.getpid(),
        "port": port,
        "started": datetime.now().isoformat(),
    }
    with open(SESSION_FILE, "w") as f:
        json.dump(session, f)


def delete_session_file() -> None:
    """Delete the session file."""
    SESSION_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Computer Name Utility
# ---------------------------------------------------------------------------

def _get_computer_name() -> str:
    """Get the computer name, falling back to home folder name if needed.

    Works cross-platform (macOS, Windows, Linux).
    """
    try:
        # Try to get hostname (works on all platforms)
        hostname = socket.gethostname()
        # On macOS, this often returns "Name.local", remove the ".local" suffix
        if hostname.endswith('.local'):
            hostname = hostname[:-6]
        # Replace hyphens with spaces for readability (e.g., "Maxs-MacBook-Pro" -> "Maxs MacBook Pro")
        # Actually, keep as-is since it's more recognizable
        if hostname:
            return hostname
    except Exception:
        pass

    try:
        # Fallback: use home folder name
        return Path.home().name
    except Exception:
        return "Your computer"


# ---------------------------------------------------------------------------
# Country Utilities
# ---------------------------------------------------------------------------

def _country_to_flag(country_code: Optional[str]) -> str:
    """Convert a 2-letter country code to a flag emoji.

    Windows does not support flag emojis (Regional Indicator sequences) in any
    terminal, so we fall back to the plain 2-letter country code there.
    """
    if not country_code or len(country_code) != 2:
        return "??" if _is_windows() else "🌐"
    if _is_windows():
        return country_code.upper()
    try:
        return "".join(chr(0x1F1E6 + ord(c) - ord('A')) for c in country_code.upper())
    except Exception:
        return country_code.upper()


def _country_name(country_code: Optional[str]) -> str:
    """Get a human-readable country name from code."""
    countries = {
        'US': 'United States', 'DE': 'Germany', 'FR': 'France', 'NL': 'Netherlands',
        'GB': 'United Kingdom', 'CH': 'Switzerland', 'SE': 'Sweden', 'FI': 'Finland',
        'CA': 'Canada', 'AT': 'Austria', 'RO': 'Romania', 'LU': 'Luxembourg',
        'NO': 'Norway', 'DK': 'Denmark', 'BE': 'Belgium', 'CZ': 'Czech Republic',
        'PL': 'Poland', 'ES': 'Spain', 'IT': 'Italy', 'RU': 'Russia',
        'UA': 'Ukraine', 'JP': 'Japan', 'AU': 'Australia', 'SG': 'Singapore',
        'HK': 'Hong Kong', 'BR': 'Brazil', 'IN': 'India', 'IS': 'Iceland',
        'IE': 'Ireland', 'PT': 'Portugal', 'BG': 'Bulgaria', 'MD': 'Moldova',
        'LV': 'Latvia', 'LT': 'Lithuania', 'EE': 'Estonia', 'SK': 'Slovakia',
        'HU': 'Hungary', 'SI': 'Slovenia', 'HR': 'Croatia', 'RS': 'Serbia',
    }
    if country_code:
        return countries.get(country_code.upper(), country_code.upper())
    return "Unknown"


# ---------------------------------------------------------------------------
# Port Utilities
# ---------------------------------------------------------------------------

def _is_port_available(port: int) -> bool:
    """Check if a TCP port is available for binding."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def _find_available_tor_ports() -> tuple[int, int]:
    """
    Find available ports for Tor SOCKS and Control.

    Tries the default ports (9250/9251) first. If those are busy
    (e.g., two MCP sessions running), scans upward for alternatives.

    Returns:
        tuple: (socks_port, control_port)

    Raises:
        RuntimeError: If no available ports found after 100 attempts.
    """
    # Try default ports first
    if _is_port_available(DEFAULT_TOR_SOCKS_PORT) and _is_port_available(DEFAULT_TOR_CONTROL_PORT):
        return DEFAULT_TOR_SOCKS_PORT, DEFAULT_TOR_CONTROL_PORT

    # Search for alternatives (need two consecutive available ports)
    for base_port in range(ALT_TOR_PORT_START, ALT_TOR_PORT_START + 200, 2):
        socks_port = base_port
        control_port = base_port + 1
        if _is_port_available(socks_port) and _is_port_available(control_port):
            return socks_port, control_port

    raise RuntimeError("Could not find available ports for Tor")


# ---------------------------------------------------------------------------
# Link Element (for copy-link selector)
# ---------------------------------------------------------------------------

class LinkElement:
    """Tracks a copyable URL in the activity feed."""
    __slots__ = ("url", "display_label", "card_ref", "card_type", "is_loading", "card_index")

    def __init__(self, url, display_label, card_ref, card_type, is_loading=False, card_index=0):
        self.url = url
        self.display_label = display_label
        self.card_ref = card_ref
        self.card_type = card_type
        self.is_loading = is_loading
        self.card_index = card_index


# ---------------------------------------------------------------------------
# Operation Card Widget
# ---------------------------------------------------------------------------

class OperationCard(Static):
    """
    A visual card representing an operation with state.

    States: pending, running, complete, error
    """

    class Completed(Message):
        """Message sent when operation completes."""
        def __init__(self, card: "OperationCard"):
            self.card = card
            super().__init__()

    state = reactive("pending")

    def __init__(
        self,
        title: str,
        spinner_type: str = "braille",  # "braille" or "semicircle"
        search_queries: Optional[list[str]] = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.title = title
        self.spinner_type = spinner_type
        self.search_queries = search_queries or []
        self._spinner_frame = 0
        self._spinner_timer = None
        self._start_time = time.time()
        self.sub_items: list = []
        self.duration: Optional[float] = None
        self.error_message: Optional[str] = None
        self.search_results: list[dict] = []  # For displaying search results
        self._display_width: int = 100  # Configurable display width for result lines
        self._hl: int = -1  # Highlighted link index within this card (-1 = none)

    def on_mount(self) -> None:
        """Start spinner animation when mounted."""
        if self.state in ("pending", "running"):
            self.state = "running"
            self._spinner_timer = self.set_interval(SPINNER_INTERVAL, self._advance_spinner)
        self._update_display()

    def _advance_spinner(self) -> None:
        """Advance the spinner animation frame."""
        if self.state == "running":
            spinner_chars = SEMICIRCLE_SPINNER if self.spinner_type == "semicircle" else BRAILLE_SPINNER
            self._spinner_frame = (self._spinner_frame + 1) % len(spinner_chars)
            self._update_display()

    def _get_spinner_char(self) -> str:
        """Get the current spinner character."""
        spinner_chars = SEMICIRCLE_SPINNER if self.spinner_type == "semicircle" else BRAILLE_SPINNER
        return spinner_chars[self._spinner_frame % len(spinner_chars)]

    def set_hl(self, idx: int) -> None:
        """Set the highlighted link index within this card. -1 clears highlight."""
        self._hl = idx
        # Skip explicit redraw if spinner timer is running — it redraws at 100ms anyway
        if not self._spinner_timer:
            self._update_display()

    def _update_display(self) -> None:
        """Render the card content."""
        lines = []

        # Calculate elapsed time for running state
        elapsed = time.time() - self._start_time

        # Determine indicator and duration based on state
        if self.state == "running":
            indicator = f"[white]{self._get_spinner_char()}[/white]"
            duration_str = f"[dim]{elapsed:.1f}s[/dim]"
        elif self.state == "complete":
            indicator = f"[green]{STATUS_COMPLETE}[/green]"
            duration_str = f"[dim]{self.duration:.1f}s[/dim]" if self.duration else ""
        elif self.state == "error":
            indicator = f"[red]{STATUS_ERROR}[/red]"
            duration_str = ""
        else:  # pending
            indicator = f"[dim]{STATUS_PENDING}[/dim]"
            duration_str = ""

        # Build main header line
        # For search operations with results: Search("query") → N results
        if self.search_queries and len(self.search_queries) == 1:
            query = self.search_queries[0]
            query_display = self._truncate_text(query, 40)
            if self.state == "complete" and self.search_results:
                count = len(self.search_results)
                header_text = f'Search("{query_display}") [dim]→[/dim] {count} result{"s" if count != 1 else ""}'
            else:
                header_text = f'Search("{query_display}")'
        else:
            header_text = self.title

        # Add duration right-aligned
        if duration_str:
            header_len = len(header_text.replace("[dim]", "").replace("[/dim]", ""))
            padding = max(1, 70 - header_len)
            main_line = f"{indicator} {header_text}{' ' * padding}{duration_str}"
        else:
            main_line = f"{indicator} {header_text}"

        lines.append(main_line)

        # Show loading message for search operations while running
        if self.state == "running" and self.search_queries and not self.sub_items:
            lines.append(f"  [dim]└─[/dim] [dim italic]Requesting from DuckDuckGo...[/dim italic]")

        # Error message
        if self.state == "error" and self.error_message:
            lines.append(f"  [dim]└─[/dim] [red]{self.error_message}[/red]")

        # Search results display (when complete and has results)
        if self.state == "complete" and self.search_results:
            # Calculate space for domain (right-aligned)
            domain_max_len = 25
            prefix_len = 5  # "  ├─ "
            min_gap = 4  # Minimum space between title and domain
            available_for_title = self._display_width - prefix_len - domain_max_len - min_gap

            for i, result in enumerate(self.search_results):
                is_last = (i == len(self.search_results) - 1)
                prefix = "└─" if is_last else "├─"

                # Get title and URL
                title = result.get("title", "")
                url = result.get("url", "")
                snippet = result.get("snippet", "")

                # Extract domain (max 25 chars)
                display_domain = self._extract_domain(url, domain_max_len)

                # Truncate title only if needed, allowing more space
                display_title = self._truncate_text(title, available_for_title)

                # Calculate padding to right-align domain
                content_width = self._display_width - prefix_len
                used_width = len(display_title) + len(display_domain)
                padding = max(min_gap, content_width - used_width)

                # Title line with bold domain (highlighted if selected)
                if self._hl == i:
                    inner = f"{display_title}{' ' * padding}{display_domain}"
                    lines.append(f"  [dim]{prefix}[/dim] [on {HL_BG}][bold {HL_FG}] ▸ {inner} [/bold {HL_FG}][/on {HL_BG}]")
                else:
                    lines.append(f"  [dim]{prefix}[/dim] {display_title}{' ' * padding}[bold]{display_domain}[/bold]")

                # Snippet lines (italicized, max 2 lines) with proper tree continuation
                if snippet:
                    # Use │ for non-last items, space for last item
                    cont_char = " " if is_last else "[dim]│[/dim]"
                    snippet_width = self._display_width - 5  # Account for "  │  " prefix
                    snippet_lines = self._wrap_text(snippet, snippet_width, 2)
                    for snippet_line in snippet_lines:
                        lines.append(f"  {cont_char}    [dim italic]{snippet_line}[/dim italic]")

        # Sub-items (for nested operations like page fetches) - Stacked Style B
        elif self.sub_items:
            for i, item in enumerate(self.sub_items):
                is_last = (i == len(self.sub_items) - 1)
                prefix = "└─" if is_last else "├─"
                cont_prefix = "  " if is_last else "│ "

                item_state = item.get("state", "pending")
                item_title = item.get("title", "")
                item_subtitle = item.get("subtitle")
                item_duration = item.get("duration")
                item_error = item.get("error")

                if item_state == "running":
                    item_indicator = f"[white]{self._get_spinner_char()}[/white]"
                    item_duration_str = ""
                elif item_state == "complete":
                    item_indicator = f"[green]{STATUS_COMPLETE}[/green]"
                    item_duration_str = f"[dim]{item_duration:.1f}s[/dim]" if item_duration else ""
                elif item_state == "error":
                    item_indicator = f"[red]{STATUS_ERROR}[/red]"
                    item_duration_str = "[red]Failed[/red]"
                else:
                    item_indicator = f"[dim]{STATUS_PENDING}[/dim]"
                    item_duration_str = ""

                # Main line (domain) — highlighted if selected
                if self._hl == i:
                    pad = max(1, 50 - len(item_title))
                    if item_state == "running":
                        item_line = (
                            f"  {prefix} {item_indicator} "
                            f"[on {HL_BG}][bold {HL_FG}] ▸ {item_title}{' ' * pad}"
                            f"[/bold {HL_FG}][dim #88aadd](loading…)[/dim #88aadd][/on {HL_BG}]"
                        )
                    elif item_duration_str:
                        item_line = (
                            f"  {prefix} {item_indicator} "
                            f"[on {HL_BG}][bold {HL_FG}] ▸ {item_title}{' ' * pad}"
                            f"[/bold {HL_FG}]{item_duration_str} [/on {HL_BG}]"
                        )
                    else:
                        item_line = (
                            f"  {prefix} {item_indicator} "
                            f"[on {HL_BG}][bold {HL_FG}] ▸ {item_title} [/bold {HL_FG}][/on {HL_BG}]"
                        )
                else:
                    item_line = f"  {prefix} {item_indicator} {item_title}"
                    if item_duration_str:
                        padding = max(1, 50 - len(item_title))
                        item_line = f"  {prefix} {item_indicator} {item_title}{' ' * padding}{item_duration_str}"

                lines.append(item_line)

                # Subtitle line (page title) - Stacked Style B
                if item_subtitle and item_state != "error":
                    max_subtitle = 60
                    disp_subtitle = item_subtitle[:max_subtitle-1] + "…" if len(item_subtitle) > max_subtitle else item_subtitle
                    lines.append(f"  {cont_prefix}   [dim italic]{disp_subtitle}[/dim italic]")

        self.update("\n".join(lines))

    def complete(self, sub_result: Optional[str] = None, new_title: Optional[str] = None) -> None:
        """Mark the operation as complete.

        Args:
            sub_result: Optional text to add as a sub-item
            new_title: Optional new title to replace the original (for "sent to model" indicator)
        """
        if self._spinner_timer:
            self._spinner_timer.stop()
            self._spinner_timer = None

        self.duration = time.time() - self._start_time
        self.state = "complete"

        if new_title:
            self.title = new_title

        # Mark any incomplete sub-items as failed
        for item in self.sub_items:
            if item.get("state") in ("running", "pending"):
                item["state"] = "error"
                item["error"] = "Failed to load"

        if sub_result:
            self.sub_items.append({
                "state": "complete",
                "title": sub_result,
                "subtitle": None,
            })

        self._update_display()
        self.post_message(self.Completed(self))

    def fail(self, error: str) -> None:
        """Mark the operation as failed."""
        if self._spinner_timer:
            self._spinner_timer.stop()
            self._spinner_timer = None

        self.duration = time.time() - self._start_time
        self.state = "error"
        self.error_message = error
        self._update_display()

    def add_sub_item(self, title: str, state: str = "pending", subtitle: str = None) -> int:
        """Add a sub-item and return its index. Subtitle is shown on second line (stacked style)."""
        self.sub_items.append({
            "state": state,
            "title": title,
            "subtitle": subtitle,
            "duration": None,
            "error": None,
        })
        self._update_display()
        return len(self.sub_items) - 1

    def update_sub_item(
        self,
        index: int,
        state: Optional[str] = None,
        duration: Optional[float] = None,
        error: Optional[str] = None
    ) -> None:
        """Update a sub-item's state."""
        if 0 <= index < len(self.sub_items):
            if state:
                self.sub_items[index]["state"] = state
            if duration is not None:
                self.sub_items[index]["duration"] = duration
            if error:
                self.sub_items[index]["error"] = error
            self._update_display()

    def set_search_results(self, results: list[dict]) -> None:
        """Set search results to display in the card."""
        self.search_results = results
        self._update_display()

    def _truncate_text(self, text: str, max_len: int) -> str:
        """Truncate text with ellipsis if needed."""
        if len(text) <= max_len:
            return text
        return text[:max_len - 1] + "…"

    def _extract_domain(self, url: str, max_len: int = 30) -> str:
        """Extract domain from URL, truncating at end only if needed."""
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            if domain.startswith("www."):
                domain = domain[4:]
            # Only truncate at the end, never in the middle
            if len(domain) > max_len:
                domain = domain[:max_len - 1] + "…"
            return domain
        except Exception:
            if len(url) > max_len:
                return url[:max_len - 1] + "…"
            return url

    def _wrap_text(self, text: str, width: int, max_lines: int) -> list[str]:
        """Wrap text to specified width, limiting to max_lines."""
        if not text:
            return []

        words = text.split()
        lines = []
        current_line = []
        current_len = 0

        for word in words:
            word_len = len(word)
            # Check if adding this word would exceed width
            if current_len + word_len + (1 if current_line else 0) <= width:
                current_line.append(word)
                current_len += word_len + (1 if len(current_line) > 1 else 0)
            else:
                if current_line:
                    lines.append(" ".join(current_line))
                    if len(lines) >= max_lines:
                        # Add ellipsis to last line if we're truncating
                        if lines[-1] and not lines[-1].endswith("..."):
                            lines[-1] = lines[-1].rstrip() + "..."
                        return lines
                current_line = [word]
                current_len = word_len

        if current_line and len(lines) < max_lines:
            lines.append(" ".join(current_line))

        return lines[:max_lines]


# ---------------------------------------------------------------------------
# Circuit Display Widget
# ---------------------------------------------------------------------------

class CircuitDisplay(Static):
    """Clean circuit visualization without borders."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._circuit_info: Optional[list[dict]] = None

    def set_circuit(self, circuit_info: Optional[list[dict]]) -> None:
        """Update the circuit display."""
        self._circuit_info = circuit_info
        self._update_display()

    def _update_display(self) -> None:
        """Render the circuit visualization."""
        if not self._circuit_info:
            self.update("")
            return

        lines = []

        # Computer name (hostname or fallback to home folder name)
        computer_name = _get_computer_name()
        lines.append(f"[dim]{computer_name}[/dim]")
        lines.append("  │")

        for i, relay in enumerate(self._circuit_info):
            country = relay.get('country')
            # Use 2-letter country codes instead of emoji flags (emoji cause width issues)
            country_code = country.upper() if country else "??"
            country_name = _country_name(country)
            address = relay.get('address', '')
            role = relay.get('role', '')

            # Role label
            role_color = {
                'guard': 'cyan',
                'middle': 'yellow',
                'exit': 'green'
            }.get(role, 'white')

            role_label = f"[{role_color}]{role.upper()}[/{role_color}]" if role else ""

            # IP address (dimmed)
            addr_str = f"[dim]{address}[/dim]" if address else ""

            # Build line with flag emoji (scrollbar hidden to avoid width glitches)
            flag = _country_to_flag(country)
            lines.append(f"  ○ {flag} {country_name}  {role_label}  {addr_str}")

            # Connection line (except after last)
            if i < len(self._circuit_info) - 1:
                lines.append("  │")

        # Final destination indicator
        lines.append("  │")
        lines.append("  ◉ [green]Internet[/green]")

        self.update("\n".join(lines))


# ---------------------------------------------------------------------------
# Status Bar Widget
# ---------------------------------------------------------------------------

class StatusBar(Static):
    """Minimal status bar pinned to bottom."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._start_time: Optional[float] = None
        self._circuit_countries: Optional[list[Optional[str]]] = None
        self._link_mode: bool = False
        self._link_index: int = 0
        self._link_total: int = 0

    def set_link_mode(self, active: bool, index: int = 0, total: int = 0) -> None:
        """Set link selection mode state and refresh."""
        self._link_mode = active
        self._link_index = index
        self._link_total = total
        self._update_display()

    def set_start_time(self, start_time: float) -> None:
        """Set the session start time for uptime calculation."""
        self._start_time = start_time
        self._update_display()

    def set_circuit_countries(self, countries: list[Optional[str]]) -> None:
        """Set circuit country codes for flag display in status bar."""
        self._circuit_countries = countries
        self._update_display()

    def _update_display(self) -> None:
        """Render the status bar."""
        if self._start_time:
            uptime_secs = int(time.time() - self._start_time)
            mins, secs = divmod(uptime_secs, 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                uptime_str = f"{hours}h {mins:02d}m {secs:02d}s"
            else:
                uptime_str = f"{mins}m {secs:02d}s"

            left = f"[dim]Uptime:[/dim] {uptime_str}"
        else:
            left = "[dim]Starting...[/dim]"

        if self._circuit_countries:
            flags = "  →  ".join(_country_to_flag(c) for c in self._circuit_countries)
            left += f"    {flags}"

        if self._link_mode:
            right = (
                f"[on {HL_BG}]"
                f" [{HL_FG}]↑↓[/{HL_FG}] [dim #aabbdd]navigate[/dim #aabbdd]  "
                f"[{HL_FG}]↵[/{HL_FG}] [dim #aabbdd]copy[/dim #aabbdd]  "
                f"[{HL_FG}]esc[/{HL_FG}] [dim #aabbdd]cancel[/dim #aabbdd]  "
                f"[dim #7799bb]({self._link_index + 1}/{self._link_total})[/dim #7799bb] "
                f"[/on {HL_BG}]"
            )
        else:
            right = f"[dim]press[/dim] [bold]C[/bold] [dim]to copy links[/dim]"

        # Right-align using the actual widget width
        plain_left = RichText.from_markup(left).plain
        plain_right = RichText.from_markup(right).plain
        width = self.size.width
        padding = max(1, width - len(plain_left) - len(plain_right))
        self.update(f"{left}{' ' * padding}{right}")


# ---------------------------------------------------------------------------
# Workflow Complete Widget
# ---------------------------------------------------------------------------

class WorkflowComplete(Static):
    """Visual indicator that a workflow has completed and results are ready."""

    def __init__(self, message: str = "Results returned to model", **kwargs):
        super().__init__(**kwargs)
        self.message = message
        self._update_display()

    def _update_display(self) -> None:
        """Render the completion message."""
        self.update(f"[green]✓[/green] [dim]{self.message}[/dim]")


# ---------------------------------------------------------------------------
# Session Divider Widget (Style B: Centered Timestamp)
# ---------------------------------------------------------------------------

class SessionDivider(Static):
    """Visual divider between search sessions with centered timestamp."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._build_display()

    def _build_display(self) -> None:
        """Render the divider with centered timestamp."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        # Total width ~66 chars, timestamp is 8 chars, so ~29 dashes on each side
        side_len = 27
        self.update(f"[dim]{'─' * side_len} {timestamp} {'─' * side_len}[/dim]")


# ---------------------------------------------------------------------------
# Tor Backend Interface
# ---------------------------------------------------------------------------

class TorSession:
    """Manages persistent Tor connection and browser."""

    def __init__(self, operation_callback: Callable = None, mode: Optional[str] = None):
        self._tor_process = None
        self._pending_tor_process = None
        self._tor_socks_port: Optional[int] = None
        self._tor_control_port: Optional[int] = None
        self._pending_tor_control_port: Optional[int] = None
        self._tbb_path: Optional[str] = None
        self._tbb_profile_path: Optional[str] = None
        self._exit_ip: Optional[str] = None
        self._start_time: Optional[float] = None
        self._last_search_results: list = []
        self._last_results_by_query: list = []  # Results grouped by query for UI
        self._fetch_pages_called: bool = False
        self._lock = threading.Lock()
        self._operation_callback = operation_callback
        self._virtual_display = None
        self._browser_driver = None
        self._browser_ready = False
        self._circuit_info: Optional[list[dict]] = None
        self._ddgs = None  # Persistent DDGS instance for connection reuse
        self._circuit_monitor_controller = None
        self._circuit_change_callback: Optional[Callable] = None
        self._current_circuit_countries: Optional[tuple] = None
        self._shutdown_event = threading.Event()
        self._stop_in_progress_event = threading.Event()
        self._startup_in_progress_event = threading.Event()
        # Session mode: "browser" (default) or "tui" (text-only searches).
        self._mode: str = mode or resolve_mode()
        # True iff *we* started the virtual display (never stop a user's X).
        self._browser_display_owned: bool = False
        # Python-side element-ref fallback counter (JS-side is primary).
        self._browser_element_seq: int = 0

    def request_shutdown(self) -> None:
        """Mark session as shutting down so new work can be rejected quickly."""
        self._shutdown_event.set()

    def is_shutting_down(self) -> bool:
        """Return True when permanent shutdown or an active stop is in progress."""
        return self._shutdown_event.is_set() or self._stop_in_progress_event.is_set()

    def _process_is_running(self, process) -> bool:
        """Best-effort check for a child process still being alive."""
        if not process:
            return False

        poll = getattr(process, "poll", None)
        if callable(poll):
            try:
                return poll() is None
            except Exception:
                pass

        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            return _is_process_alive(pid)

        return False

    def _wait_for_process_exit(self, process, timeout: float) -> bool:
        """Wait for process exit using wait()/poll() and return True if exited."""
        if not process:
            return True

        timeout = max(timeout, 0.0)
        deadline = time.time() + timeout

        wait = getattr(process, "wait", None)
        if callable(wait):
            try:
                remaining = max(0.0, deadline - time.time())
                wait(timeout=remaining)
                return True
            except TypeError:
                # Some wait() implementations do not accept timeout;
                # fall back to bounded polling below.
                pass
            except Exception:
                pass

        while time.time() < deadline:
            if not self._process_is_running(process):
                return True
            time.sleep(0.1)

        return not self._process_is_running(process)

    def _terminate_tor_process(self, tor_process, control_port: Optional[int]) -> None:
        """Shut down Tor gracefully first, then force-kill if needed."""
        if not tor_process:
            return

        if not self._process_is_running(tor_process):
            return

        # Try graceful shutdown via control port first.
        if control_port:
            try:
                from stem.control import Controller
                from stem import Signal

                with Controller.from_port(port=control_port) as controller:
                    controller.authenticate()
                    controller.signal(Signal.SHUTDOWN)
            except Exception:
                pass

            if self._wait_for_process_exit(tor_process, timeout=3.0):
                return

        for method_name in ("terminate", "kill"):
            method = getattr(tor_process, method_name, None)
            if not callable(method):
                continue
            try:
                method()
            except Exception:
                continue

            if self._wait_for_process_exit(tor_process, timeout=3.0):
                return

        pid = getattr(tor_process, "pid", None)
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
                if self._wait_for_process_exit(tor_process, timeout=2.0):
                    return
            except Exception:
                pass

            if hasattr(signal, "SIGKILL"):
                try:
                    os.kill(pid, signal.SIGKILL)
                    self._wait_for_process_exit(tor_process, timeout=2.0)
                except Exception:
                    pass

    def _ensure_tbselenium(self):
        """Import the appropriate tbselenium package for the current platform."""
        if _is_macos():
            import tbselenium_macos  # noqa: F401
        elif _is_windows():
            import tbselenium_windows  # noqa: F401
        else:
            import tbselenium  # noqa: F401

    def _get_tor_startup_timeout_seconds(self, config: dict) -> int:
        """Read and sanitize the Tor startup timeout from config."""
        raw_timeout = config.get("tor", {}).get("startup_timeout_seconds", 240)
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError):
            timeout = 240
        return max(timeout, 30)

    def _resolve_tor_binary_path(self, tbb_path: str, cm) -> str:
        """Resolve the Tor binary path inside the bundled Tor Browser."""
        tor_binary = os.path.join(tbb_path, cm.DEFAULT_TOR_BINARY_PATH)
        if _is_macos() and not os.path.isfile(tor_binary):
            alt_tor_binary = os.path.join(tbb_path, "Contents", "MacOS", "Tor", "tor")
            if os.path.isfile(alt_tor_binary):
                tor_binary = alt_tor_binary

        if not os.path.isfile(tor_binary):
            raise RuntimeError(f"Invalid Tor binary: {tor_binary}")

        return tor_binary

    def _prepend_env_value(self, env: dict[str, str], key: str, value: str) -> None:
        """Prepend a path entry to an environment variable without duplicates."""
        path_sep = os.pathsep
        current = env.get(key, "")
        existing = [p for p in current.split(path_sep) if p]
        if value in existing:
            return
        env[key] = f"{value}{path_sep}{current}" if current else value

    def _launch_tor_with_timeout(
        self,
        tbb_path: str,
        torrc_config: dict,
        startup_timeout_seconds: int,
        cm,
    ):
        """Launch Tor and wait for 100% bootstrap with an explicit timeout."""
        tor_binary = self._resolve_tor_binary_path(tbb_path, cm)

        torrc = dict(torrc_config)
        log_entries = torrc.get("Log")
        if log_entries is None:
            log_values = []
        elif isinstance(log_entries, str):
            log_values = [log_entries]
        else:
            log_values = [str(entry) for entry in log_entries]
        if not any("stdout" in entry.lower() for entry in log_values):
            log_values.append("NOTICE stdout")
        torrc["Log"] = log_values

        torrc_path: Optional[str] = None
        tor_process = None
        startup_done = threading.Event()
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".torrc", delete=False) as f:
                torrc_path = f.name
                for key, value in torrc.items():
                    values = value if isinstance(value, (list, tuple)) else [value]
                    for item in values:
                        f.write(f"{key} {str(item)}\n")

            env = os.environ.copy()
            tor_dir = str(Path(tor_binary).parent)
            if _is_windows():
                self._prepend_env_value(env, "PATH", tor_dir)
            elif _is_macos():
                self._prepend_env_value(env, "DYLD_LIBRARY_PATH", tor_dir)
            else:
                self._prepend_env_value(env, "LD_LIBRARY_PATH", tor_dir)

            tor_process = subprocess.Popen(
                [tor_binary, "-f", torrc_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            self._pending_tor_process = tor_process

            if not tor_process.stdout:
                raise RuntimeError("Tor process did not provide startup logs")

            line_queue: queue.Queue[Optional[str]] = queue.Queue()

            def _reader():
                try:
                    assert tor_process and tor_process.stdout
                    for line in iter(tor_process.stdout.readline, ""):
                        if startup_done.is_set():
                            continue  # Keep draining tor logs without growing the queue.
                        line_queue.put(line)
                except Exception:
                    pass
                finally:
                    line_queue.put(None)

            threading.Thread(target=_reader, daemon=True).start()

            bootstrap_line = re.compile(r"Bootstrapped\s+([0-9]+)%")
            problem_line = re.compile(r"\[(warn|err)\]\s+(.*)$", re.IGNORECASE)
            last_problem = "Timed out while waiting for Tor bootstrap"
            latest_bootstrap = 0
            deadline = time.time() + startup_timeout_seconds

            while True:
                if time.time() >= deadline:
                    raise TimeoutError(
                        f"Tor bootstrap timed out after {startup_timeout_seconds}s at {latest_bootstrap}%"
                    )

                try:
                    line = line_queue.get(timeout=0.2)
                except queue.Empty:
                    line = None

                if line:
                    init_line = line.strip()
                    if init_line:
                        logger.debug("Tor startup: %s", init_line)

                    bootstrap_match = bootstrap_line.search(init_line)
                    if bootstrap_match:
                        latest_bootstrap = int(bootstrap_match.group(1))
                        if latest_bootstrap >= 100:
                            startup_done.set()
                            return tor_process

                    problem_match = problem_line.search(init_line)
                    if problem_match:
                        _runlevel, msg = problem_match.groups()
                        msg = msg.strip()
                        if "see warnings above" not in msg.lower():
                            if ": " in msg:
                                msg = msg.split(": ", 1)[-1].strip()
                            last_problem = msg

                if tor_process.poll() is not None:
                    exit_code = tor_process.returncode
                    raise RuntimeError(
                        f"Tor process exited during bootstrap (exit code {exit_code}): {last_problem}"
                    )

        except TimeoutError as e:
            if tor_process:
                self._terminate_tor_process(tor_process, control_port=None)
            raise RuntimeError(str(e)) from e
        except Exception:
            if tor_process:
                self._terminate_tor_process(tor_process, control_port=None)
            raise
        finally:
            startup_done.set()
            if torrc_path:
                try:
                    os.remove(torrc_path)
                except OSError:
                    pass

    def _get_tbselenium_common(self):
        """Get the tbselenium common module for the current platform."""
        self._ensure_tbselenium()
        if _is_macos():
            import tbselenium_macos.common as cm
        elif _is_windows():
            import tbselenium_windows.common as cm
        else:
            import tbselenium.common as cm
        return cm

    def _resolve_tbb_path(self) -> Optional[str]:
        """Resolve Tor Browser path from the project's own components/ directory.

        Only looks in the known install location populated by the installer.
        Never searches the system or the user's personal Tor Browser — this
        prevents conflicts when the user has Tor Browser open.
        """
        components_dir = SCRIPT_DIR / "components"

        if _is_macos():
            candidate = components_dir / "Tor Browser.app"
        elif _is_windows():
            candidate = components_dir / "TorBrowser"
        else:
            candidate = components_dir / "tor-browser"

        if candidate.is_dir():
            return str(candidate.resolve())

        return None

    def _find_geoip_files(self, tbb_path: str) -> dict:
        """Find GeoIP files in the Tor Browser bundle."""
        result = {'geoip': None, 'geoip6': None}

        if _is_macos():
            base = os.path.join(tbb_path, "Contents", "Resources", "TorBrowser", "Tor")
        elif _is_windows():
            base = os.path.join(tbb_path, "Browser", "TorBrowser", "Data", "Tor")
        else:
            base = os.path.join(tbb_path, "Browser", "TorBrowser", "Data", "Tor")

        geoip_path = os.path.join(base, "geoip")
        geoip6_path = os.path.join(base, "geoip6")

        if os.path.isfile(geoip_path):
            result['geoip'] = geoip_path
        if os.path.isfile(geoip6_path):
            result['geoip6'] = geoip6_path

        return result

    def _resolve_profile_path(self, tbb_path: str) -> Optional[str]:
        """Resolve the Tor Browser profile directory within the project's own bundle.

        Only looks inside the bundle at tbb_path (which is always in
        components/). Never touches the user's personal Tor Browser profile.
        """
        cm = self._get_tbselenium_common()

        bundle_profile = os.path.join(tbb_path, cm.DEFAULT_TBB_PROFILE_PATH)
        if os.path.isdir(bundle_profile):
            return bundle_profile

        return None

    def start_tor(self) -> tuple[bool, str, Optional[float]]:
        """
        Start Tor connection.
        Returns: (success, message, duration_seconds)
        """
        self._startup_in_progress_event.set()
        try:
            with self._lock:
                if self.is_shutting_down():
                    return False, "Session is shutting down", None

                if self._tor_process:
                    return True, "Tor already running", None

                try:
                    start_time = time.time()

                    self._ensure_tbselenium()
                    cm = self._get_tbselenium_common()

                    tbb_path = self._resolve_tbb_path()
                    if not tbb_path:
                        return False, "Tor Browser not found in components/. Please run installer.py.", None

                    profile_path = self._resolve_profile_path(tbb_path)
                    if not profile_path:
                        return False, "Tor Browser profile not found in components/. Please run installer.py.", None

                    config = load_config()
                    tor_data_dir = SCRIPT_DIR / config.get("tor", {}).get("data_dir", "tor_data")
                    tor_data_dir.mkdir(exist_ok=True)
                    startup_timeout_seconds = self._get_tor_startup_timeout_seconds(config)

                    # Find available ports
                    socks_port, control_port = _find_available_tor_ports()
                    self._pending_tor_control_port = control_port

                    torrc_config = {
                        "DataDirectory": str(tor_data_dir.resolve()),
                        "SOCKSPort": str(socks_port),
                        "ControlPort": str(control_port),
                    }

                    geoip_paths = self._find_geoip_files(tbb_path)
                    if geoip_paths.get('geoip'):
                        torrc_config["GeoIPFile"] = geoip_paths['geoip']
                    if geoip_paths.get('geoip6'):
                        torrc_config["GeoIPv6File"] = geoip_paths['geoip6']

                    tor_process = self._launch_tor_with_timeout(
                        tbb_path=tbb_path,
                        torrc_config=torrc_config,
                        startup_timeout_seconds=startup_timeout_seconds,
                        cm=cm,
                    )
                    if self.is_shutting_down():
                        # Shutdown happened during startup; don't leave a freshly spawned Tor process running.
                        self._terminate_tor_process(tor_process, control_port)
                        return False, "Session is shutting down", None

                    self._tor_process = tor_process

                    self._tor_socks_port = socks_port
                    self._tor_control_port = control_port
                    self._tbb_path = tbb_path
                    self._tbb_profile_path = profile_path
                    self._start_time = time.time()

                    elapsed = time.time() - start_time

                    try:
                        self._exit_ip = self._get_exit_ip()
                    except Exception as e:
                        self._exit_ip = f"Error: {type(e).__name__}"

                    try:
                        self._circuit_info = self.get_circuit_info()
                    except Exception as e:
                        self._circuit_info = {"error": str(e)}

                    # Warm up DDGS connection for faster first search
                    try:
                        from ddgs import DDGS
                        self._ddgs = DDGS(proxy=f"socks5h://127.0.0.1:{self._tor_socks_port}", timeout=60)
                    except Exception:
                        self._ddgs = None  # Will be created lazily on first search

                    return True, "Tor connected", elapsed

                except Exception as e:
                    self._tor_process = None
                    return False, f"Failed to start Tor: {str(e)}", None
                finally:
                    # A shutdown can race with startup before _tor_process is assigned.
                    # Keep no stale bootstrap handles once startup finishes/fails.
                    self._pending_tor_process = None
                    self._pending_tor_control_port = None
        finally:
            self._startup_in_progress_event.clear()

    def _get_exit_ip(self) -> str:
        """Get the current Tor exit node IP."""
        import urllib.request

        proxy_handler = urllib.request.ProxyHandler({
            'http': f'socks5h://127.0.0.1:{self._tor_socks_port}',
            'https': f'socks5h://127.0.0.1:{self._tor_socks_port}'
        })
        opener = urllib.request.build_opener(proxy_handler)

        try:
            response = opener.open("https://check.torproject.org/api/ip", timeout=10)
            data = json.loads(response.read().decode())
            return data.get("IP", "Unknown")
        except Exception:
            return "Unknown"

    def get_circuit_info(self, max_retries: int = 10) -> Optional[list[dict]]:
        """Get the current Tor circuit information."""
        if not self._tor_process or not self._tor_control_port:
            return None

        try:
            from stem.control import Controller

            with Controller.from_port(port=self._tor_control_port) as controller:
                controller.authenticate()

                built_circuit = None
                for attempt in range(max_retries):
                    circuits = controller.get_circuits()
                    for circuit in circuits:
                        if circuit.status == 'BUILT' and len(circuit.path) >= 3:
                            built_circuit = circuit
                            break
                    if built_circuit:
                        break
                    time.sleep(0.5)

                if not built_circuit:
                    return None

                relays = []
                roles = ['guard', 'middle', 'exit']

                for i, (fingerprint, nickname) in enumerate(built_circuit.path[:3]):
                    relay_info = {
                        'nickname': nickname,
                        'fingerprint': fingerprint,
                        'address': None,
                        'address_v6': None,
                        'country': None,
                        'role': roles[i] if i < len(roles) else 'relay',
                    }

                    try:
                        ns = controller.get_network_status(fingerprint)
                        if ns:
                            relay_info['address'] = ns.address

                            try:
                                country = controller.get_info(f"ip-to-country/{ns.address}")
                                if country and country != "??":
                                    relay_info['country'] = country.upper()
                            except Exception:
                                pass

                    except Exception:
                        pass

                    try:
                        desc = controller.get_server_descriptor(fingerprint)
                        if desc and hasattr(desc, 'or_addresses'):
                            for addr, port, is_ipv6 in desc.or_addresses:
                                if is_ipv6:
                                    relay_info['address_v6'] = addr
                                    break
                    except Exception:
                        pass

                    relays.append(relay_info)

                return relays if len(relays) == 3 else None

        except Exception:
            return None

    def stop_tor(self) -> None:
        """Stop Tor process and browser."""
        self._stop_in_progress_event.set()
        try:
            controller = None
            browser_driver = None
            virtual_display = None
            tor_targets: list[tuple[object, Optional[int]]] = []

            def add_tor_target(process, control_port: Optional[int]) -> None:
                if not process:
                    return

                pid = getattr(process, "pid", None)
                for i, (existing_process, existing_control_port) in enumerate(tor_targets):
                    same_process = process is existing_process
                    existing_pid = getattr(existing_process, "pid", None)
                    same_pid = isinstance(pid, int) and isinstance(existing_pid, int) and pid == existing_pid
                    if not (same_process or same_pid):
                        continue
                    if existing_control_port is None and control_port is not None:
                        tor_targets[i] = (existing_process, control_port)
                    return

                tor_targets.append((process, control_port))

            lock_acquired = self._lock.acquire(timeout=2.0)
            if not lock_acquired and self._startup_in_progress_event.is_set():
                # Startup keeps the lock for a while; wait briefly so we can shut down safely.
                logger.info("Shutdown requested during Tor startup; waiting for startup to finish.")
                deadline = time.time() + 30.0
                while self._startup_in_progress_event.is_set() and time.time() < deadline:
                    time.sleep(0.1)
                lock_acquired = self._lock.acquire(timeout=5.0)

            if lock_acquired:
                try:
                    controller = self._circuit_monitor_controller
                    browser_driver = self._browser_driver
                    virtual_display = self._virtual_display
                    add_tor_target(self._tor_process, self._tor_control_port)
                    add_tor_target(self._pending_tor_process, self._pending_tor_control_port)
                    self._circuit_monitor_controller = None
                    self._circuit_change_callback = None
                    self._current_circuit_countries = None
                    self._browser_driver = None
                    self._browser_ready = False
                    self._browser_display_owned = False
                    self._browser_element_seq = 0
                    self._tor_process = None
                    self._pending_tor_process = None
                    self._tor_socks_port = None
                    self._tor_control_port = None
                    self._pending_tor_control_port = None
                    self._start_time = None
                    self._ddgs = None
                    self._virtual_display = None
                finally:
                    self._lock.release()
            else:
                # During shutdown we must still attempt teardown even if another
                # thread holds the session lock; otherwise Tor can be orphaned.
                logger.warning(
                    "Timed out waiting for session lock during shutdown; "
                    "forcing best-effort cleanup without lock."
                )
                controller = self._circuit_monitor_controller
                browser_driver = self._browser_driver
                virtual_display = self._virtual_display
                add_tor_target(self._tor_process, self._tor_control_port)
                add_tor_target(self._pending_tor_process, self._pending_tor_control_port)
                self._browser_display_owned = False
                self._browser_element_seq = 0

            if controller:
                try:
                    controller.close()
                except Exception:
                    pass

            if browser_driver:
                try:
                    browser_driver.quit()
                except Exception:
                    pass

            for tor_process, control_port in tor_targets:
                self._terminate_tor_process(tor_process, control_port)
                if tor_process and self._process_is_running(tor_process):
                    pid = getattr(tor_process, "pid", "unknown")
                    logger.warning("Tor process still appears alive after shutdown attempts (pid=%s).", pid)

            if virtual_display:
                try:
                    virtual_display.stop()
                except Exception:
                    pass
        finally:
            self._stop_in_progress_event.clear()

    def _ensure_virtual_display(self) -> None:
        """Headful browser needs an X display. Try Xvfb, fall back to Xephyr.

        The display is assigned to self._virtual_display only after .start()
        succeeds, so a failed attempt cannot poison later teardown.
        """
        if not self._needs_virtual_display() or self._virtual_display:
            return
        from pyvirtualdisplay import Display
        last_err = None
        for backend in ("xvfb", "xephyr"):  # pyvirtualdisplay auto-detects backends
            try:
                display = Display(visible=False, size=(1920, 1080), backend=backend)
                display.start()  # sets os.environ["DISPLAY"], restored on stop()
            except Exception as e:
                last_err = e
                continue
            self._virtual_display = display
            self._browser_display_owned = True
            return
        raise RuntimeError(
            "No X display available for headful Tor Browser "
            f"(install xvfb or xephyr): {last_err}"
        )

    def warm_up_browser(self) -> tuple[bool, str, Optional[float]]:
        """
        Initialize the persistent browser instance (acquires the session lock).
        Returns: (success, message, duration_seconds)
        """
        if not self._lock.acquire(timeout=30.0):
            if self._browser_driver:
                return True, "Browser already warm", None
            return False, "Browser busy (session lock not acquired within 30s)", None
        try:
            return self._warm_up_browser_locked()
        finally:
            self._lock.release()

    def _warm_up_browser_locked(self) -> tuple[bool, str, Optional[float]]:
        """Initialize the persistent browser instance.

        Caller must already hold self._lock (the lock is non-reentrant, so
        warm_up_browser() and browser_command() both funnel into this body).
        Returns: (success, message, duration_seconds)
        """
        if self.is_shutting_down():
            return False, "Session is shutting down", None

        if self._browser_driver:
            return True, "Browser already warm", None

        if not self._tor_process:
            return False, "Tor not running", None

        try:
            start_time = time.time()

            from selenium.webdriver.firefox.options import Options

            self._ensure_tbselenium()
            if _is_macos():
                from tbselenium_macos import TorBrowserDriver, USE_STEM
            elif _is_windows():
                from tbselenium_windows import TorBrowserDriver, USE_STEM
            else:
                from tbselenium.tbdriver import TorBrowserDriver
                from tbselenium.common import USE_STEM

            config = load_config()
            page_timeout = config.get("browser", {}).get("page_timeout", 10)
            tor_data_dir = SCRIPT_DIR / config.get("tor", {}).get("data_dir", "tor_data")

            tbb_logfile_path = "NUL" if _is_windows() else "/dev/null"

            self._ensure_virtual_display()

            if self._mode == "browser":
                # tbselenium resolves geckodriver via shutil.which(); put the
                # configured directory on PATH so geckodriver_path is honored.
                gecko = str(config.get("browser", {}).get("geckodriver_path") or "")
                if gecko:
                    gecko_dir = gecko if os.path.isdir(gecko) else os.path.dirname(gecko)
                    current_path = os.environ.get("PATH", "")
                    if gecko_dir and gecko_dir not in current_path.split(os.pathsep):
                        os.environ["PATH"] = (
                            f"{gecko_dir}{os.pathsep}{current_path}" if current_path else gecko_dir
                        )

            options = Options()
            options.page_load_strategy = "none"

            self._browser_driver = TorBrowserDriver(
                tbb_path=self._tbb_path,
                tor_cfg=USE_STEM,
                tbb_logfile_path=tbb_logfile_path,
                tor_data_dir=str(tor_data_dir.resolve()),
                headless=(self._mode != "browser"),
                options=options,
                tbb_profile_path=self._tbb_profile_path,
                socks_port=self._tor_socks_port,  # Use the actual port Tor is running on
                control_port=self._tor_control_port,
            )

            self._browser_driver.set_page_load_timeout(page_timeout)
            self._browser_ready = True

            elapsed = time.time() - start_time
            return True, "Browser ready", elapsed

        except Exception as e:
            self._browser_driver = None
            self._browser_ready = False
            return False, f"Failed to start browser: {str(e)}", None

    def is_browser_ready(self) -> bool:
        """Check if browser is warmed up and ready."""
        return self._browser_ready and self._browser_driver is not None

    # ------------------------------------------------------------------
    # Browser automation (browser mode) — see .opencode/BROWSER_MODE.md
    #
    # Contract: browser_command() acquires self._lock EXACTLY ONCE and calls
    # the _browser_* internals below, which never acquire it (the lock is
    # non-reentrant). Every action returns {"success": True, ...payload} or
    # {"success": False, "error": "..."}.
    # ------------------------------------------------------------------

    _BROWSER_ACTIONS = frozenset({
        "status", "navigate", "click", "fill", "keys", "execute_js", "get_text",
        "get_html", "get_url", "get_title", "find", "get_attribute", "screenshot",
        "back", "forward", "refresh", "scroll", "new_tab", "switch_tab",
        "close_tab", "wait_ready", "close",
    })

    # Actions that read or manipulate the page: they wait for a navigation we
    # started (e.g. browser_navigate(wait=False)) to commit before running, so
    # they cannot race the still-loading document.
    _SETTLE_ACTIONS = frozenset({
        "click", "fill", "keys", "execute_js", "get_text", "get_html",
        "get_url", "get_title", "find", "get_attribute", "screenshot", "scroll",
    })

    _KEY_ALIASES = {
        "CTRL": "CONTROL",
        "CMD": "COMMAND",
        "COMMANDKEY": "COMMAND",
        "PAGEUP": "PAGE_UP",
        "PAGEDOWN": "PAGE_DOWN",
        "ESC": "ESCAPE",
        "DEL": "DELETE",
        "INS": "INSERT",
        "SPACEBAR": "SPACE",
        "RETURN": "ENTER",
        "ARROWUP": "UP",
        "ARROWDOWN": "DOWN",
        "ARROWLEFT": "LEFT",
        "ARROWRIGHT": "RIGHT",
    }

    # Keys that must remain held while the rest of the combo is typed.
    _MODIFIER_KEYS = frozenset({
        Keys.CONTROL, Keys.SHIFT, Keys.ALT, Keys.COMMAND, Keys.META,
    })

    def browser_command(self, action: str, params: dict) -> dict:
        """Run one browser automation action while holding the session lock.

        Acquires self._lock exactly once (timeout 30s) so browser commands
        serialize with search/fetch_pages/stop_tor; the internal _browser_*
        helpers are only ever called from here (or from code already holding
        the lock) and never re-acquire it.
        """
        params = params if isinstance(params, dict) else {}
        action = str(action or "").strip()
        if action not in self._BROWSER_ACTIONS:
            return {"success": False, "error": f"Unknown browser action: {action}"}

        # status must stay answerable even if Tor died (reports tor_running honestly)
        if action != "status" and not self.is_running():
            return {"success": False, "error": "Tor not running"}

        if not self._lock.acquire(timeout=30.0):
            return {
                "success": False,
                "error": "Browser busy (another command holds the session lock)",
            }
        try:
            if self.is_shutting_down():
                return {"success": False, "error": "Session is shutting down"}

            # These two never need a live driver.
            if action == "status":
                return self._browser_status()
            if action == "close":
                return self._browser_close()

            # Everything else drives the Tor Browser: warm it up on demand.
            ok, message, _ = self._warm_up_browser_locked()
            if not ok:
                return {"success": False, "error": message}

            # Do not race an in-flight navigation (navigate(wait=False)).
            if action in self._SETTLE_ACTIONS:
                self._settle_pending_navigation()

            if action == "navigate":
                return self._browser_navigate(
                    _param_str(params.get("url")),
                    _param_bool(params.get("wait"), True),
                    _param_float(params.get("wait_timeout"), 0.0),
                )
            if action == "click":
                return self._browser_click(
                    _param_str(params.get("ref")),
                    _param_bool(params.get("wait"), False),
                )
            if action == "fill":
                return self._browser_fill(
                    _param_str(params.get("ref")),
                    _param_str(params.get("text")),
                    _param_bool(params.get("clear"), True),
                    _param_bool(params.get("submit"), False),
                )
            if action == "keys":
                return self._browser_keys(
                    _param_str(params.get("keys")),
                    _param_str(params.get("ref")),
                )
            if action == "execute_js":
                return self._browser_execute_js(
                    _param_str(params.get("script")),
                    params.get("args"),
                )
            if action == "get_text":
                return self._browser_get_text(
                    _param_str(params.get("ref")),
                    _param_int(params.get("max_chars"), 0),
                )
            if action == "get_html":
                return self._browser_get_html(
                    _param_str(params.get("ref")),
                    _param_int(params.get("max_chars"), 0),
                )
            if action == "get_url":
                return self._browser_get_url()
            if action == "get_title":
                return self._browser_get_title()
            if action == "find":
                return self._browser_find(
                    _param_str(params.get("selector")),
                    _param_str(params.get("by")) or "css",
                    _param_int(params.get("limit"), 10),
                    _param_bool(params.get("include_hidden"), False),
                )
            if action == "get_attribute":
                return self._browser_get_attribute(
                    _param_str(params.get("ref")),
                    _param_str(params.get("attribute")),
                )
            if action == "screenshot":
                return self._browser_screenshot()
            if action == "back":
                return self._browser_back(_param_bool(params.get("wait"), True))
            if action == "forward":
                return self._browser_forward(_param_bool(params.get("wait"), True))
            if action == "refresh":
                return self._browser_refresh(_param_bool(params.get("wait"), True))
            if action == "scroll":
                return self._browser_scroll(
                    _param_str(params.get("direction")) or "down",
                    _param_int(params.get("amount_px"), 800),
                    _param_str(params.get("ref")),
                )
            if action == "new_tab":
                return self._browser_new_tab(_param_str(params.get("url")))
            if action == "switch_tab":
                return self._browser_switch_tab(
                    _param_str(params.get("handle")),
                    _param_int(params.get("index"), -1),
                )
            if action == "close_tab":
                return self._browser_close_tab(_param_str(params.get("handle")))
            if action == "wait_ready":
                return self._browser_wait_ready(
                    _param_float(params.get("timeout"), 0.0),
                    _param_str(params.get("selector")),
                )
            return {"success": False, "error": f"Unknown browser action: {action}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            self._lock.release()

    # -- shared helpers (all assume self._lock is held) ------------------

    def _get_browser_cfg(self) -> dict:
        try:
            cfg = load_config().get("browser", {})
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _get_wait_timeout_default(self) -> float:
        """Default wait budget from config [browser] wait_timeout_seconds."""
        try:
            value = float(self._get_browser_cfg().get("wait_timeout_seconds", 30))
        except (TypeError, ValueError):
            value = 30.0
        return value if value > 0 else 30.0

    def _get_max_output_chars(self) -> int:
        """Output cap from config [browser] max_output_chars."""
        try:
            value = int(self._get_browser_cfg().get("max_output_chars", 100000))
        except (TypeError, ValueError):
            value = 100000
        return value if value > 0 else 100000

    def _truncate(self, text: str, max_chars: int = 0) -> tuple[str, bool]:
        """Cap text at max_chars (0 = config max_output_chars); flag truncation."""
        limit = int(max_chars) if max_chars and max_chars > 0 else self._get_max_output_chars()
        if limit <= 0 or len(text) <= limit:
            return text, False
        return text[:limit] + "\n…[truncated]", True

    def _mark_document(self) -> None:
        """Tag the outgoing document so a later poll can tell when it is gone.

        With page_load_strategy="none" driver.get()/back()/click() return before
        the new document commits, so readyState (and current_url) still describe
        the *previous* page for a while. The mark lives on the outgoing window,
        so it disappears exactly when the new document has committed.
        """
        try:
            self._browser_driver.execute_script("window.__onionMark = true;")
        except Exception:
            pass

    def _document_is_marked(self) -> bool:
        """True while the marked (outgoing) document is still the current one."""
        try:
            return bool(self._browser_driver.execute_script(
                "return !!window.__onionMark;"))
        except Exception:
            return False

    def _clear_document_mark(self) -> None:
        """Drop a stale mark (navigation never produced a new document)."""
        try:
            self._browser_driver.execute_script(
                "try { delete window.__onionMark; } catch (e) "
                "{ window.__onionMark = false; }")
        except Exception:
            pass

    def _settle_pending_navigation(self) -> None:
        """Block until an in-flight navigation we started has committed.

        Bounded by [browser] wait_timeout_seconds; a mark that never clears
        (a navigation that produced no new document) is dropped at the deadline
        so later commands are not slowed down repeatedly.
        """
        if self._browser_driver is None or not self._document_is_marked():
            return
        budget = self._get_wait_timeout_default()
        self._wait_ready_locked(
            budget, require_new_doc=True, new_doc_grace=budget)

    def _wait_ready_locked(
        self,
        timeout: float,
        require_new_doc: bool = True,
        new_doc_grace: float = 0.0,
    ) -> str:
        """Poll until the (new) document reports readyState "complete".

        Callers must call _mark_document() before starting a navigation; this
        then waits for the mark to disappear (i.e. for the new document to
        commit) before trusting readyState - otherwise the previous document
        reports "complete" immediately and callers race the loading page.

        new_doc_grace > 0 gives up on seeing a new document after that many
        seconds (used by browser_click, where most clicks do not navigate) and
        clears the mark; 0 waits the whole budget.
        """
        budget = _param_float(timeout, 0.0)
        if budget <= 0:
            budget = self._get_wait_timeout_default()
        start = time.time()
        deadline = start + budget
        driver = self._browser_driver
        state = ""
        while True:
            now = time.time()
            if now >= deadline:
                break
            if require_new_doc and self._document_is_marked():
                if new_doc_grace > 0 and (now - start) >= new_doc_grace:
                    # No new document within the grace window: the action
                    # probably did not navigate. Stop waiting on the mark.
                    self._clear_document_mark()
                else:
                    time.sleep(0.1)
                    continue
            try:
                state = str(driver.execute_script("return document.readyState;") or "")
            except Exception as e:
                state = f"error: {e}"
                break
            if state == "complete":
                break
            time.sleep(0.1)
        return state

    @staticmethod
    def _normalize_browser_url(url: str) -> str:
        """Ensure an absolute navigable URL (adds https:// when no scheme)."""
        url = (url or "").strip()
        if not url:
            return url
        parsed = urllib.parse.urlparse(url)
        if not parsed.scheme or (
            "://" not in url
            and parsed.scheme not in ("about", "data", "file", "javascript")
        ):
            # urlparse("example.com") -> scheme ""; urlparse("localhost:8080")
            # -> scheme "localhost", which is not a URL scheme carrying "://".
            url = "https://" + url
        return url

    def _python_stamp_ref(self, element) -> str:
        """Fallback ref allocation when JS stamping is unavailable."""
        ref = element.get_attribute("data-onion-ref")
        if ref:
            return ref
        try:
            seq = int(self._browser_driver.execute_script(
                "return window.__onionSeq || 0;") or 0)
            if seq > self._browser_element_seq:
                self._browser_element_seq = seq
        except Exception:
            pass
        self._browser_element_seq += 1
        ref = f"e{self._browser_element_seq}"
        try:
            # Keep the JS counter in sync so later JS stamps cannot collide.
            # The attribute MUST carry the exact string we return: _by_ref()
            # looks elements up by [data-onion-ref="<ref>"], so stamping the
            # bare counter (an int) here made every execute_js ref unusable.
            self._browser_driver.execute_script(
                "window.__onionSeq = Math.max(window.__onionSeq || 0, arguments[1]);"
                "arguments[0].setAttribute('data-onion-ref', arguments[2]);",
                element,
                self._browser_element_seq,
                ref,
            )
        except Exception:
            pass
        return ref

    def _by_ref(self, ref: str):
        """Locate a stamped element by its ref. Caller must hold self._lock."""
        if not ref:
            raise RuntimeError("Element ref is required")
        element = None
        try:
            element = self._browser_driver.find_element(
                By.CSS_SELECTOR, f'[data-onion-ref="{ref}"]'
            )
        except Exception:
            element = None
        if element is None:
            raise RuntimeError(
                f"Unknown element ref '{ref}'; the page changed - call browser_find again"
            )
        return element

    def _normalize_js_value(self, value):
        """JSON-normalize a script result (WebElements become element refs)."""
        if isinstance(value, WebElement):
            try:
                return {"ref": self._python_stamp_ref(value)}
            except Exception:
                try:
                    return str(value)
                except Exception:
                    return "<element>"
        if isinstance(value, dict):
            return {str(k): self._normalize_js_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._normalize_js_value(v) for v in value]
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        return str(value)

    # -- browser actions (all assume self._lock is held) -----------------

    def _browser_status(self) -> dict:
        """Report session/browser state without launching anything."""
        driver = self._browser_driver
        handles: list[dict] = []
        active_handle = ""
        window_count = 0
        current_url = ""
        title = ""
        if driver is not None:
            try:
                window_count = len(driver.window_handles)
            except Exception:
                window_count = 0
            try:
                active_handle = driver.current_window_handle
            except Exception:
                active_handle = ""
            try:
                current_url = driver.current_url or ""
                title = driver.title or ""
            except Exception:
                pass
            for handle in list(driver.window_handles):
                info = {"handle": handle, "url": "", "title": ""}
                if handle == active_handle:
                    info["url"] = current_url
                    info["title"] = title
                else:
                    try:
                        driver.switch_to.window(handle)
                        info["url"] = driver.current_url or ""
                        info["title"] = driver.title or ""
                    except Exception:
                        pass
                handles.append(info)
            if active_handle:
                try:
                    driver.switch_to.window(active_handle)
                except Exception:
                    pass
        display_number = None
        match = re.search(r":(\d+)", os.environ.get("DISPLAY", "") or "")
        if match:
            display_number = int(match.group(1))
        return {
            "success": True,
            "mode": self._mode,
            "tor_running": self.is_running(),
            "browser_ready": self.is_browser_ready(),
            "headless": self._mode != "browser",
            "current_url": current_url,
            "title": title,
            "window_count": window_count,
            "active_handle": active_handle,
            "handles": handles,
            "display": {"virtual": self._virtual_display is not None, "number": display_number},
            "uptime_s": self.get_uptime(),
        }

    def _browser_navigate(self, url: str, wait: bool = True, wait_timeout: float = 0.0) -> dict:
        if not url.strip():
            return {"success": False, "error": "url is required"}
        target = self._normalize_browser_url(url)
        driver = self._browser_driver
        start = time.time()
        self._mark_document()
        try:
            driver.get(target)
        except Exception as e:
            return {"success": False, "error": f"Navigation failed: {e}"}
        state = "skipped"
        if wait:
            state = self._wait_ready_locked(
                wait_timeout or self._get_wait_timeout_default(),
                require_new_doc=True,
                new_doc_grace=0.0,
            )
        # New document: element refs from the previous page are dead.
        self._browser_element_seq = 0
        try:
            final_url = driver.current_url or target
            page_title = driver.title or ""
        except Exception:
            final_url, page_title = target, ""
        return {
            "success": True,
            "url": final_url,
            "title": page_title,
            "ready_state": state,
            "elapsed_s": round(time.time() - start, 3),
        }

    def _browser_click(self, ref: str, wait: bool = False) -> dict:
        try:
            element = self._by_ref(ref)
            if wait:
                self._mark_document()
            element.click()
        except RuntimeError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"Click failed: {e}"}
        if wait:
            # Most clicks do not navigate, so only wait a few seconds for a new
            # document to commit before falling back to the current page state.
            self._wait_ready_locked(
                self._get_wait_timeout_default(),
                require_new_doc=True,
                new_doc_grace=3.0,
            )
        return {"success": True, "clicked": ref, "url": self._browser_driver.current_url or ""}

    def _browser_fill(self, ref: str, text: str, clear: bool = True, submit: bool = False) -> dict:
        try:
            element = self._by_ref(ref)
            if clear:
                try:
                    element.clear()
                except Exception:
                    pass
            element.send_keys(text)
            submitted = False
            if submit:
                element.send_keys(Keys.ENTER)
                submitted = True
        except RuntimeError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"Fill failed: {e}"}
        return {
            "success": True,
            "filled": ref,
            "submitted": submitted,
            "url": self._browser_driver.current_url or "",
        }

    def _browser_keys(self, keys: str, ref: str = "") -> dict:
        if not keys.strip():
            return {"success": False, "error": "keys is required"}
        try:
            combo = []
            for token in keys.split("+"):
                token = token.strip()
                if not token:
                    continue
                name = token.upper()
                name = self._KEY_ALIASES.get(name, name)
                value = getattr(Keys, name, None)
                if value is None:
                    if len(token) == 1:
                        value = token  # literal character
                    else:
                        return {"success": False, "error": f"Unknown key: {token}"}
                combo.append(value)
            if not combo:
                return {"success": False, "error": "keys is required"}
            # Selenium's ActionChains.send_keys() presses AND releases every
            # key, so a combo like CTRL+A would type a plain "A". Hold the
            # modifiers down explicitly, type the rest, then release them.
            modifiers = [k for k in combo if k in self._MODIFIER_KEYS]
            others = [k for k in combo if k not in self._MODIFIER_KEYS]
            chain = ActionChains(self._browser_driver)
            if ref:
                chain = chain.move_to_element(self._by_ref(ref))
            for mod in modifiers:
                chain = chain.key_down(mod)
            if others:
                chain = chain.send_keys(*others)
            for mod in reversed(modifiers):
                chain = chain.key_up(mod)
            chain.perform()
        except RuntimeError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"Key send failed: {e}"}
        return {"success": True, "sent": keys, "url": self._browser_driver.current_url or ""}

    def _browser_execute_js(self, script: str, args=None) -> dict:
        if not script.strip():
            return {"success": False, "error": "script is required"}
        if args is None:
            args = []
        if not isinstance(args, list):
            return {"success": False, "error": "args must be a JSON array"}
        try:
            result = self._browser_driver.execute_script(script, *args)
        except Exception as e:
            return {"success": False, "error": f"Script failed: {e}"}
        normalized = self._normalize_js_value(result)
        raw = json.dumps(normalized, ensure_ascii=False, default=str)
        capped, truncated = self._truncate(raw)
        return {"success": True, "result": capped, "truncated": truncated}

    def _browser_get_text(self, ref: str = "", max_chars: int = 0) -> dict:
        try:
            if ref:
                text = self._by_ref(ref).text or ""
            else:
                text = self._browser_driver.execute_script(
                    "return document.body ? (document.body.innerText || '') : '';"
                ) or ""
        except RuntimeError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"get_text failed: {e}"}
        capped, _ = self._truncate(str(text), max_chars)
        return {"success": True, "text": capped}

    def _browser_get_html(self, ref: str = "", max_chars: int = 0) -> dict:
        try:
            if ref:
                html = self._by_ref(ref).get_attribute("outerHTML") or ""
            else:
                html = self._browser_driver.page_source or ""
        except RuntimeError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"get_html failed: {e}"}
        capped, _ = self._truncate(str(html), max_chars)
        return {"success": True, "html": capped}

    def _browser_get_url(self) -> dict:
        try:
            url = self._browser_driver.current_url or ""
        except Exception as e:
            return {"success": False, "error": f"get_url failed: {e}"}
        return {"success": True, "url": url}

    def _browser_get_title(self) -> dict:
        try:
            title = self._browser_driver.title or ""
        except Exception as e:
            return {"success": False, "error": f"get_title failed: {e}"}
        return {"success": True, "title": title}

    def _browser_find(
        self,
        selector: str,
        by: str = "css",
        limit: int = 10,
        include_hidden: bool = False,
    ) -> dict:
        if not selector.strip():
            return {"success": False, "error": "selector is required"}
        by = (by or "css").strip().lower()
        if by not in ("css", "xpath", "id", "link_text", "partial_link_text"):
            return {"success": False, "error": f"Unsupported by: {by}"}
        limit = _param_int(limit, 10)
        limit = max(1, min(limit, 500))
        driver = self._browser_driver
        items = None
        try:
            items = driver.execute_script(
                _STAMP_JS, selector, by, limit, bool(include_hidden)
            )
        except Exception:
            items = None
        if items is None:
            # Python-side fallback: locate via selenium and stamp refs ourselves.
            try:
                by_map = {
                    "css": By.CSS_SELECTOR,
                    "xpath": By.XPATH,
                    "id": By.ID,
                    "link_text": By.LINK_TEXT,
                    "partial_link_text": By.PARTIAL_LINK_TEXT,
                }
                elements = driver.find_elements(by_map[by], selector)
            except Exception as e:
                return {"success": False, "error": f"find failed: {e}"}
            items = []
            for element in elements:
                if len(items) >= limit:
                    break
                try:
                    visible = bool(element.is_displayed())
                except Exception:
                    visible = False
                if not include_hidden and not visible:
                    continue
                try:
                    items.append({
                        "ref": self._python_stamp_ref(element),
                        "tag": (element.tag_name or "").lower(),
                        "text": (element.text or "").strip()[:80],
                        "id": element.get_attribute("id") or "",
                        "name": element.get_attribute("name") or "",
                        "href": element.get_attribute("href") or "",
                        "visible": visible,
                    })
                except Exception:
                    continue
        else:
            # Keep the Python-side fallback counter in sync with the JS one.
            try:
                seq = int(driver.execute_script("return window.__onionSeq || 0;") or 0)
                if seq > self._browser_element_seq:
                    self._browser_element_seq = seq
            except Exception:
                pass
        return {"success": True, "results": items, "count": len(items)}

    def _browser_get_attribute(self, ref: str, attribute: str) -> dict:
        if not attribute.strip():
            return {"success": False, "error": "attribute is required"}
        try:
            element = self._by_ref(ref)
            if attribute == "text":
                value = element.text
            elif attribute == "html":
                value = element.get_attribute("outerHTML")
            else:
                value = element.get_attribute(attribute)
        except RuntimeError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"get_attribute failed: {e}"}
        return {"success": True, "value": value if value is not None else ""}

    def _browser_screenshot(self) -> dict:
        try:
            png = self._browser_driver.get_screenshot_as_png()
        except Exception as e:
            return {"success": False, "error": f"Screenshot failed: {e}"}
        encoded = base64.b64encode(png).decode("ascii")
        return {"success": True, "mime": "image/png", "base64": encoded, "bytes": len(png)}

    def _run_history_op(self, op: str, wait: bool) -> dict:
        driver = self._browser_driver
        try:
            if wait:
                self._mark_document()
            getattr(driver, op)()
            state = (
                self._wait_ready_locked(
                    self._get_wait_timeout_default(),
                    require_new_doc=True,
                    # back()/forward() are no-ops when there is no such history
                    # entry; bail after a few seconds instead of burning the
                    # whole budget when nothing was loaded.
                    new_doc_grace=5.0,
                )
                if wait
                else "skipped"
            )
        except Exception as e:
            return {"success": False, "error": f"{op} failed: {e}"}
        # New/reloaded document: element refs from the old page are dead.
        self._browser_element_seq = 0
        try:
            url = driver.current_url or ""
            title = driver.title or ""
        except Exception:
            url, title = "", ""
        return {"success": True, "url": url, "title": title, "ready_state": state}

    def _browser_back(self, wait: bool = True) -> dict:
        return self._run_history_op("back", wait)

    def _browser_forward(self, wait: bool = True) -> dict:
        return self._run_history_op("forward", wait)

    def _browser_refresh(self, wait: bool = True) -> dict:
        return self._run_history_op("refresh", wait)

    def _browser_scroll(self, direction: str = "down", amount_px: int = 800, ref: str = "") -> dict:
        direction = direction.strip().lower()
        if direction not in ("down", "up", "top", "bottom"):
            return {"success": False, "error": f"Unsupported direction: {direction}"}
        amount_px = _param_int(amount_px, 800)
        driver = self._browser_driver
        try:
            if ref:
                element = self._by_ref(ref)
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
            elif direction == "down":
                driver.execute_script("window.scrollBy(0, arguments[0]);", amount_px)
            elif direction == "up":
                driver.execute_script("window.scrollBy(0, -arguments[0]);", amount_px)
            elif direction == "top":
                driver.execute_script("window.scrollTo(0, 0);")
            else:  # bottom
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            coords = driver.execute_script(
                "return [window.scrollX || window.pageXOffset || 0,"
                " window.scrollY || window.pageYOffset || 0];"
            ) or [0, 0]
        except RuntimeError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"Scroll failed: {e}"}
        return {"success": True, "scroll_x": int(coords[0]), "scroll_y": int(coords[1])}

    def _browser_new_tab(self, url: str = "") -> dict:
        driver = self._browser_driver
        try:
            driver.switch_to.new_window("tab")
            handle = driver.current_window_handle
        except Exception as e:
            return {"success": False, "error": f"new_tab failed: {e}"}
        if url.strip():
            target = self._normalize_browser_url(url)
            try:
                self._mark_document()
                driver.get(target)
                self._wait_ready_locked(
                    self._get_wait_timeout_default(),
                    require_new_doc=True,
                    new_doc_grace=0.0,
                )
                self._browser_element_seq = 0
            except Exception as e:
                return {"success": False, "error": f"new_tab navigation failed: {e}"}
        try:
            current_url = driver.current_url or ""
            window_count = len(driver.window_handles)
        except Exception:
            current_url, window_count = "", 0
        return {
            "success": True,
            "handle": handle,
            "url": current_url,
            "window_count": window_count,
        }

    def _browser_switch_tab(self, handle: str = "", index: int = -1) -> dict:
        driver = self._browser_driver
        try:
            handles = list(driver.window_handles)
        except Exception as e:
            return {"success": False, "error": f"switch_tab failed: {e}"}
        if handle:
            if handle not in handles:
                return {"success": False, "error": f"Unknown window handle: {handle}"}
            target = handle
        else:
            idx = _param_int(index, -1)
            if idx < 0 or idx >= len(handles):
                return {
                    "success": False,
                    "error": f"index out of range: {index} (window_count={len(handles)})",
                }
            target = handles[idx]
        try:
            driver.switch_to.window(target)
            url = driver.current_url or ""
            title = driver.title or ""
        except Exception as e:
            return {"success": False, "error": f"switch_tab failed: {e}"}
        return {
            "success": True,
            "handle": target,
            "url": url,
            "title": title,
            "window_count": len(handles),
        }

    def _browser_close_tab(self, handle: str = "") -> dict:
        driver = self._browser_driver
        try:
            handles = list(driver.window_handles)
            original = driver.current_window_handle
        except Exception as e:
            return {"success": False, "error": f"close_tab failed: {e}"}
        target = handle or original
        if target not in handles:
            return {"success": False, "error": f"Unknown window handle: {target}"}
        if len(handles) <= 1:
            return {
                "success": False,
                "error": "Refusing to close the last tab; use browser_close instead.",
            }
        try:
            driver.switch_to.window(target)
            driver.close()
        except Exception as e:
            return {"success": False, "error": f"close_tab failed: {e}"}
        remaining: list = []
        try:
            remaining = list(driver.window_handles)
        except Exception:
            pass
        fallback = original if original in remaining else (remaining[0] if remaining else None)
        if fallback:
            try:
                driver.switch_to.window(fallback)
            except Exception:
                pass
        return {"success": True, "closed": target, "remaining": len(remaining)}

    def _browser_wait_ready(self, timeout: float = 0.0, selector: str = "") -> dict:
        budget = _param_float(timeout, 0.0)
        if budget <= 0:
            budget = self._get_wait_timeout_default()
        start = time.time()
        # A mark on the current document means a navigation we started is still
        # in flight (e.g. browser_navigate(wait=False)) - wait for it to commit
        # before trusting readyState; a stale mark self-heals after the budget.
        state = self._wait_ready_locked(
            budget, require_new_doc=True, new_doc_grace=budget)
        try:
            url = self._browser_driver.current_url or ""
        except Exception:
            url = ""
        waited = round(time.time() - start, 3)
        if selector.strip():
            remaining = max(0.5, budget - waited)
            try:
                WebDriverWait(self._browser_driver, remaining).until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, selector)) > 0
                )
            except Exception:
                return {
                    "success": False,
                    "error": f"Selector not found within {remaining:.1f}s: {selector}",
                    "ready_state": state,
                    "url": url,
                    "waited_s": waited,
                }
        return {"success": True, "ready_state": state, "url": url, "waited_s": waited}

    def _browser_close(self) -> dict:
        """Quit the browser (and the display we started); Tor stays up."""
        driver = self._browser_driver
        display = self._virtual_display if self._browser_display_owned else None
        self._browser_driver = None
        self._browser_ready = False
        self._virtual_display = None if self._browser_display_owned else self._virtual_display
        self._browser_display_owned = False
        self._browser_element_seq = 0
        warnings = []
        if driver:
            try:
                driver.quit()
            except Exception as e:
                warnings.append(f"driver.quit: {e}")
        if display:
            try:
                display.stop()
            except Exception as e:
                warnings.append(f"display.stop: {e}")
        payload = {"success": True, "closed": True}
        if warnings:
            payload["warnings"] = warnings
        return payload

    def is_running(self) -> bool:
        """Check if Tor is running."""
        return self._tor_process is not None

    def get_exit_ip(self) -> Optional[str]:
        """Get cached exit IP."""
        return self._exit_ip

    def get_cached_circuit_info(self) -> Optional[list[dict]]:
        """Get cached circuit info."""
        return self._circuit_info

    def start_circuit_monitor(self, on_circuit_change: Callable[[list], None]) -> None:
        """Start monitoring active circuit changes via Stem stream events.

        Listens for STREAM SUCCEEDED events to detect when traffic actually
        flows through a circuit, rather than tracking all prebuilt circuits.
        Only fires the callback when the active circuit's countries differ
        from the previously displayed ones.
        """
        if not self._tor_control_port:
            return

        try:
            from stem.control import Controller, EventType

            self._circuit_monitor_controller = Controller.from_port(port=self._tor_control_port)
            self._circuit_monitor_controller.authenticate()
            self._circuit_change_callback = on_circuit_change

            # Seed with initial circuit countries
            if self._circuit_info and isinstance(self._circuit_info, list):
                self._current_circuit_countries = tuple(
                    r.get('country') for r in self._circuit_info
                )

            def _on_stream_event(event):
                if event.status != 'SUCCEEDED' or not event.circ_id:
                    return
                try:
                    ctrl = self._circuit_monitor_controller
                    if not ctrl:
                        return
                    # Find the circuit this stream is attached to
                    target_circuit = None
                    for circ in ctrl.get_circuits():
                        if circ.id == event.circ_id:
                            target_circuit = circ
                            break
                    if not target_circuit or len(target_circuit.path) < 3:
                        return
                    countries = []
                    for fingerprint, _nickname in target_circuit.path[:3]:
                        try:
                            ns = ctrl.get_network_status(fingerprint)
                            country = ctrl.get_info(f"ip-to-country/{ns.address}")
                            countries.append(country.upper() if country and country != "??" else None)
                        except Exception:
                            countries.append(None)
                    country_tuple = tuple(countries)
                    if country_tuple != self._current_circuit_countries:
                        self._current_circuit_countries = country_tuple
                        if self._circuit_change_callback:
                            self._circuit_change_callback(list(countries))
                except Exception:
                    pass

            self._circuit_monitor_controller.add_event_listener(_on_stream_event, EventType.STREAM)
        except Exception:
            self._circuit_monitor_controller = None

    def get_uptime(self) -> Optional[float]:
        """Get uptime in seconds."""
        if self._start_time:
            return time.time() - self._start_time
        return None

    def get_start_time(self) -> Optional[float]:
        """Get session start time."""
        return self._start_time

    def search(self, queries: list[str], progress_callback: Callable = None) -> dict:
        """Execute search queries via DuckDuckGo through Tor."""
        with self._lock:
            if self.is_shutting_down():
                return {"success": False, "error": "Session is shutting down"}

            if not self._tor_process:
                return {"success": False, "error": "Tor not running"}

            self._last_search_results = []
            self._last_results_by_query = []

            try:
                from ddgs import DDGS

                config = load_config()
                search_config = config.get("search", {})
                region = search_config.get("region", "us-en")
                safesearch = search_config.get("safesearch", "off")

                def positive_int(value, default: int) -> int:
                    try:
                        return max(1, int(value))
                    except (TypeError, ValueError):
                        return default

                max_results_per_query = positive_int(
                    search_config.get("max_results_per_query", 5),
                    5,
                )
                max_concurrent_queries = min(
                    positive_int(search_config.get("max_concurrent_queries", 3), 3),
                    3,
                )
                query_timeout_seconds = positive_int(
                    search_config.get("query_timeout_seconds", 60),
                    60,
                )

                proxy_url = f"socks5h://127.0.0.1:{self._tor_socks_port}"

                def run_query(query_index: int, query: str, use_shared_client: bool = False) -> dict:
                    query_start = time.time()

                    if self.is_shutting_down():
                        return {
                            "query_index": query_index,
                            "query": query,
                            "raw_results": [],
                            "duration": 0.0,
                            "error": "Session is shutting down",
                        }

                    try:
                        if use_shared_client:
                            if self._ddgs is None:
                                self._ddgs = DDGS(proxy=proxy_url, timeout=60)
                            ddgs_client = self._ddgs
                        else:
                            ddgs_client = DDGS(proxy=proxy_url, timeout=60)

                        raw_results = list(
                            ddgs_client.text(
                                query,
                                region=region,
                                safesearch=safesearch,
                                backend="duckduckgo",
                                max_results=max_results_per_query,
                            )
                        )
                        duration = time.time() - query_start

                        return {
                            "query_index": query_index,
                            "query": query,
                            "raw_results": raw_results,
                            "duration": duration,
                            "error": None,
                        }
                    except Exception as e:
                        duration = time.time() - query_start
                        error_msg = str(e)
                        logger.debug(
                            "Search query %d failed in %.2fs: %s",
                            query_index,
                            duration,
                            error_msg,
                        )
                        return {
                            "query_index": query_index,
                            "query": query,
                            "raw_results": [],
                            "duration": duration,
                            "error": error_msg,
                        }

                def emit_query_progress(result: dict) -> None:
                    if not progress_callback:
                        return
                    event = "search_complete"
                    payload = {"duration": result.get("duration")}
                    if result.get("error"):
                        event = "search_error"
                        payload["error"] = result.get("error")
                    progress_callback(
                        event,
                        result["query_index"],
                        result["query"],
                        payload,
                    )

                query_metadata: list[dict] = [None] * len(queries)

                use_parallel = len(queries) > 1 and max_concurrent_queries > 1
                if use_parallel:
                    worker_count = min(len(queries), max_concurrent_queries)
                    try:
                        results_queue: queue.Queue[dict] = queue.Queue()
                        concurrency_gate = threading.Semaphore(worker_count)
                        pending_indices = set()
                        start_times: dict[int, float] = {}
                        worker_threads: list[threading.Thread] = []

                        def worker(query_index: int, query: str) -> None:
                            with concurrency_gate:
                                result = run_query(query_index, query, False)
                            results_queue.put(result)

                        for i, query in enumerate(queries):
                            if progress_callback:
                                progress_callback("search_start", i, query)
                            logger.debug("Search query %d started: %s", i, query)
                            start_times[i] = time.time()
                            pending_indices.add(i)
                            thread = threading.Thread(
                                target=worker,
                                args=(i, query),
                                daemon=True,
                            )
                            worker_threads.append(thread)
                            thread.start()

                        while pending_indices:
                            if self.is_shutting_down():
                                for query_index in list(pending_indices):
                                    elapsed = time.time() - start_times[query_index]
                                    result = {
                                        "query_index": query_index,
                                        "query": queries[query_index],
                                        "raw_results": [],
                                        "duration": elapsed,
                                        "error": "Session is shutting down",
                                    }
                                    query_metadata[query_index] = result
                                    emit_query_progress(result)
                                    pending_indices.remove(query_index)
                                break

                            try:
                                result = results_queue.get(timeout=0.1)
                            except queue.Empty:
                                result = None

                            if result is not None:
                                query_index = result["query_index"]
                                if query_index in pending_indices:
                                    query_metadata[query_index] = result
                                    emit_query_progress(result)
                                    pending_indices.remove(query_index)

                            now = time.time()
                            for query_index in list(pending_indices):
                                elapsed = now - start_times[query_index]
                                if elapsed < query_timeout_seconds:
                                    continue

                                timeout_error = f"Search timed out after {query_timeout_seconds}s"
                                result = {
                                    "query_index": query_index,
                                    "query": queries[query_index],
                                    "raw_results": [],
                                    "duration": elapsed,
                                    "error": timeout_error,
                                }
                                query_metadata[query_index] = result
                                emit_query_progress(result)
                                pending_indices.remove(query_index)
                                logger.debug("Search query %d timed out in %.2fs", query_index, elapsed)
                    except Exception as e:
                        logger.warning("Parallel query execution failed, falling back to sequential: %s", str(e))
                        query_metadata = []
                        for i, query in enumerate(queries):
                            if self.is_shutting_down():
                                query_metadata.append({
                                    "query_index": i,
                                    "query": query,
                                    "raw_results": [],
                                    "duration": 0.0,
                                    "error": "Session is shutting down",
                                })
                                continue

                            if progress_callback:
                                progress_callback("search_start", i, query)
                            logger.debug("Search query %d started: %s", i, query)
                            result = run_query(i, query, False)
                            query_metadata.append(result)
                            emit_query_progress(result)
                else:
                    use_shared_client = len(queries) == 1
                    query_metadata = []
                    for i, query in enumerate(queries):
                        if self.is_shutting_down():
                            query_metadata.append({
                                "query_index": i,
                                "query": query,
                                "raw_results": [],
                                "duration": 0.0,
                                "error": "Session is shutting down",
                            })
                            continue

                        if progress_callback:
                            progress_callback("search_start", i, query)
                        logger.debug("Search query %d started: %s", i, query)
                        result = run_query(i, query, use_shared_client)
                        query_metadata.append(result)
                        emit_query_progress(result)

                query_durations = [None] * len(queries)
                query_status = ["error"] * len(queries)
                query_errors = [None] * len(queries)

                for i in range(len(queries)):
                    metadata = query_metadata[i]
                    if metadata is None:
                        query_errors[i] = "Search did not return metadata."
                        continue
                    query_durations[i] = metadata.get("duration")
                    query_errors[i] = metadata.get("error")
                    query_status[i] = "complete" if not metadata.get("error") else "error"

                if any(status == "error" for status in query_status):
                    first_error = next((err for err in query_errors if err), "Search query failed.")
                    return {
                        "success": False,
                        "error": first_error,
                        "query_durations": query_durations,
                        "query_status": query_status,
                        "query_errors": query_errors,
                    }

                seen_urls = set()
                current_index = 1
                results_by_query = []

                for metadata in query_metadata:
                    query = metadata["query"]
                    raw_results = metadata["raw_results"]

                    query_results = []
                    for r in raw_results:
                        url = r.get("href", "")
                        if url in seen_urls:
                            continue

                        seen_urls.add(url)
                        snippet = r.get("body", "")
                        if len(snippet) > 125:
                            snippet = snippet[:125] + "..."

                        item = {
                            "index": current_index,
                            "title": r.get("title", ""),
                            "url": url,
                            "snippet": snippet,
                        }
                        self._last_search_results.append(item)
                        query_results.append(item)
                        current_index += 1

                    results_by_query.append({
                        "query": query,
                        "results": query_results,
                    })

                self._last_results_by_query = results_by_query  # Store for UI access
                self._fetch_pages_called = False
                total_results = len(self._last_search_results)

                # Format as markdown
                output_parts = []
                for group in results_by_query:
                    query = group["query"]
                    query_results = group["results"]

                    output_parts.append(f"## Query: \"{query}\"")

                    if not query_results:
                        output_parts.append("_No unique results for this query._\n")
                    else:
                        entries = []
                        for item in query_results:
                            idx = item["index"]
                            title = item["title"]
                            url = item["url"]
                            snippet = item["snippet"]
                            blockquote = "\n".join(f"> {line}" for line in snippet.split("\n"))
                            entries.append(f"### {idx}. [{title}]({url})\n{blockquote}")
                        output_parts.append("\n\n".join(entries))

                    output_parts.append("")

                return {
                    "success": True,
                    "results": "\n".join(output_parts),
                    "count": total_results,
                    "query_durations": query_durations,
                    "query_status": query_status,
                    "query_errors": query_errors,
                }

            except Exception as e:
                return {"success": False, "error": str(e)}

    def fetch_pages(self, indexes: list[int], progress_callback: Callable = None) -> dict:
        """Fetch pages by index from the last search."""
        with self._lock:
            if self.is_shutting_down():
                return {"success": False, "error": "Session is shutting down"}

            if not self._tor_process:
                return {"success": False, "error": "Tor not running"}

            if not self._last_search_results:
                return {"success": False, "error": "No search results available. Call search first."}

            if self._fetch_pages_called:
                return {"success": False, "error": "fetch_pages already called for this search."}

            if len(indexes) > 5:
                return {"success": False, "error": f"Maximum of 5 pages allowed. You requested {len(indexes)}."}

            valid_indexes = {item["index"] for item in self._last_search_results}
            for idx in indexes:
                if idx not in valid_indexes:
                    return {"success": False, "error": f"Invalid index {idx}. Valid: {sorted(valid_indexes)}"}

            urls_to_fetch = []
            for item in self._last_search_results:
                if item["index"] in indexes:
                    urls_to_fetch.append(item["url"])

            if not urls_to_fetch:
                return {"success": False, "error": "No valid URLs to fetch."}

            self._fetch_pages_called = True

            try:
                results = self._fetch_urls_with_browser(urls_to_fetch, progress_callback)

                # Format results
                output_parts = []
                for item in self._last_search_results:
                    if item["index"] in indexes:
                        url = item["url"]
                        title = item["title"]
                        content = results.get(url)

                        output_parts.append(f"## {item['index']}. {title}")
                        output_parts.append(f"URL: {url}\n")

                        if isinstance(content, str):
                            try:
                                data = json.loads(content)
                                text = data.get("text", content)
                                output_parts.append(text)
                            except json.JSONDecodeError:
                                output_parts.append(content)
                        elif isinstance(content, dict):
                            error_type = content.get("error", "unknown")
                            error_msg = content.get("message", "Unknown error")
                            output_parts.append(f"**Error fetching page:** [{error_type}] {error_msg}")
                        else:
                            output_parts.append("**Error:** No content retrieved")

                        output_parts.append("\n---\n")

                return {
                    "success": True,
                    "results": "\n".join(output_parts),
                }

            except Exception as e:
                return {"success": False, "error": str(e)}

    def fetch_specific_page(self, url: str, progress_callback: Callable = None) -> dict:
        """Fetch a specific URL."""
        with self._lock:
            if self.is_shutting_down():
                return {"success": False, "error": "Session is shutting down"}

            if not self._tor_process:
                return {"success": False, "error": "Tor not running"}

            try:
                results = self._fetch_urls_with_browser([url], progress_callback)

                content = results.get(url)

                if isinstance(content, str):
                    try:
                        data = json.loads(content)
                        text = data.get("text", content)
                        return {
                            "success": True,
                            "results": f"## Content from {url}\n\n{text}",
                        }
                    except json.JSONDecodeError:
                        return {
                            "success": True,
                            "results": f"## Content from {url}\n\n{content}",
                        }
                elif isinstance(content, dict):
                    error_type = content.get("error", "unknown")
                    error_msg = content.get("message", "Unknown error")
                    return {"success": False, "error": f"[{error_type}] {error_msg}"}
                else:
                    return {"success": False, "error": "No content retrieved"}

            except Exception as e:
                return {"success": False, "error": str(e)}

    def _needs_virtual_display(self) -> bool:
        """Check if a virtual display is needed (Linux without DISPLAY)."""
        if not _is_linux():
            return False
        return not os.environ.get("DISPLAY")

    def _fetch_urls_with_browser(self, urls: list, progress_callback: Callable = None) -> dict:
        """Fetch URLs using the persistent Tor Browser."""
        import trafilatura

        config = load_config()
        page_timeout = config.get("browser", {}).get("page_timeout", 10)
        overall_timeout = config.get("browser", {}).get("overall_timeout", 60)
        max_concurrent_tabs = config.get("browser", {}).get("max_concurrent_tabs", 5)

        results = {}

        if self.is_shutting_down():
            for url in urls:
                results[url] = {"error": "shutdown", "message": "Session is shutting down"}
            return results

        if not self._browser_driver:
            # _fetch_urls_with_browser runs while the caller (fetch_pages /
            # fetch_specific_page) already holds self._lock, so use the
            # non-locking warm-up entry point.
            success, msg, _ = self._warm_up_browser_locked()
            if not success:
                for url in urls:
                    results[url] = {"error": "browser_error", "message": msg}
                return results

        driver = self._browser_driver

        try:
            deadline = time.time() + overall_timeout if overall_timeout > 0 else None

            all_results = {}
            for i in range(0, len(urls), max_concurrent_tabs):
                if self.is_shutting_down():
                    for url in urls[i:]:
                        all_results[url] = {"error": "shutdown", "message": "Session is shutting down"}
                    break

                if deadline and time.time() > deadline:
                    for url in urls[i:]:
                        all_results[url] = {"error": "timeout", "message": "Overall timeout reached"}
                    break

                batch = urls[i:i + max_concurrent_tabs]
                batch_results = self._process_batch(driver, batch, page_timeout, deadline, progress_callback)
                all_results.update(batch_results)

            # Extract content with Trafilatura
            for url, content in all_results.items():
                if isinstance(content, str):
                    extracted = trafilatura.extract(
                        content,
                        url=url,
                        include_formatting=True,
                        include_tables=True,
                        include_links=True,
                        include_comments=False,
                        favor_precision=True,
                        deduplicate=True,
                        output_format="json",
                    )
                    if extracted:
                        results[url] = extracted
                    else:
                        results[url] = {"error": "extraction_error", "message": "Trafilatura failed"}
                else:
                    results[url] = content

            # Reset browser to clean state. In browser mode the agent's tabs
            # and session must survive, so skip the close-all + about:blank
            # reset; _process_batch already closed the tabs it opened and
            # restored the agent's original window handle.
            if self._mode != "browser":
                try:
                    while len(driver.window_handles) > 1:
                        driver.switch_to.window(driver.window_handles[-1])
                        driver.close()
                    driver.switch_to.window(driver.window_handles[0])
                    driver.get("about:blank")
                except Exception:
                    pass

        except Exception as e:
            for url in urls:
                if url not in results:
                    results[url] = {"error": "browser_error", "message": str(e)}
            self._browser_ready = False

        return results

    def _process_batch(self, driver, urls: list, timeout: int, deadline: Optional[float], progress_callback: Callable = None) -> dict:
        """Process a batch of URLs."""
        tab_url_map = {}
        tab_dispatch_times = {}

        browser_mode = self._mode == "browser"
        original_handle = None
        batch_opened: list = []  # browser mode: handles this batch opened
        if browser_mode:
            # Remember the agent's pre-existing tab: in browser mode it must
            # never be navigated, closed, or left unfocused.
            try:
                original_handle = driver.current_window_handle
            except Exception:
                original_handle = None

        # Dispatch
        for i, url in enumerate(urls):
            if self.is_shutting_down():
                break

            if i == 0 and not browser_mode:
                handle = driver.current_window_handle
            elif browser_mode:
                # Browser mode: every URL (including the first) gets its own
                # tab so the agent's original handle stays untouched.
                handle = None
                if original_handle is not None:
                    try:
                        driver.execute_script("window.open('');")
                        candidate = driver.window_handles[-1]
                        if candidate != original_handle:
                            driver.switch_to.window(candidate)
                            handle = candidate
                            batch_opened.append(handle)
                    except Exception:
                        handle = None
                if handle is None:
                    # Fallback (popup blocked / no original handle): reuse the
                    # current tab. That tab must then never be closed below.
                    handle = (
                        original_handle
                        if original_handle is not None
                        else driver.current_window_handle
                    )
                    try:
                        driver.switch_to.window(handle)
                    except Exception:
                        pass
            else:
                driver.execute_script("window.open('');")
                handle = driver.window_handles[-1]
                driver.switch_to.window(handle)

            tab_url_map[handle] = url
            try:
                tab_dispatch_times[url] = time.time()
                if progress_callback:
                    progress_callback("fetch_start", url)
                driver.get(url)
            except Exception:
                pass

        # Collect
        results = {}
        pending = set(tab_url_map.keys())
        start = time.time()

        while pending and (time.time() - start) < timeout:
            if self.is_shutting_down():
                for handle in list(pending):
                    url = tab_url_map.get(handle, "unknown")
                    results[url] = {"error": "shutdown", "message": "Session is shutting down"}
                pending.clear()
                break

            if deadline and time.time() > deadline:
                break

            for handle in list(pending):
                try:
                    driver.switch_to.window(handle)
                except Exception as e:
                    url = tab_url_map[handle]
                    results[url] = {"error": "tab_error", "message": str(e)}
                    if progress_callback:
                        progress_callback("fetch_error", url, str(e))
                    pending.remove(handle)
                    continue

                url = tab_url_map[handle]

                # Verify navigation has actually started (not still on about: pages)
                # This fixes a race condition with page_load_strategy="none" where
                # the browser may still be on the previous page when we check readyState
                try:
                    current_url = driver.current_url
                    if current_url.startswith('about:'):
                        # Navigation hasn't started yet, continue polling
                        continue
                except Exception:
                    pass

                try:
                    ready_state = driver.execute_script("return document.readyState;")
                except Exception as e:
                    results[url] = {"error": "script_error", "message": str(e)}
                    if progress_callback:
                        progress_callback("fetch_error", url, str(e))
                    pending.remove(handle)
                    continue

                if ready_state in ("complete", "interactive"):
                    try:
                        if hasattr(driver, "is_connection_error_page") and driver.is_connection_error_page:
                            results[url] = {"error": "connection_error", "message": f"Site unreachable: {url}"}
                            if progress_callback:
                                progress_callback("fetch_error", url, "Site unreachable")
                        else:
                            html = driver.page_source
                            if html:
                                results[url] = html
                                elapsed = time.time() - tab_dispatch_times.get(url, start)
                                if progress_callback:
                                    progress_callback("fetch_complete", url, elapsed)
                            else:
                                results[url] = {"error": "empty_response", "message": "No HTML content"}
                                if progress_callback:
                                    progress_callback("fetch_error", url, "Empty response")
                    except Exception as e:
                        results[url] = {"error": "capture_error", "message": str(e)}
                        if progress_callback:
                            progress_callback("fetch_error", url, str(e))

                    try:
                        # Browser mode: the agent's original tab is never
                        # closed, even if it had to be reused for dispatch.
                        if len(driver.window_handles) > 1 and not (
                            browser_mode and handle == original_handle
                        ):
                            driver.close()
                    except Exception:
                        pass

                    pending.remove(handle)

            if pending:
                time.sleep(0.5)

        # Handle timeouts
        for handle in list(pending):
            try:
                driver.switch_to.window(handle)
                url = tab_url_map[handle]
                try:
                    html = driver.page_source
                    if html and len(html.strip()) > 0:
                        results[url] = html
                        elapsed = time.time() - tab_dispatch_times.get(url, start)
                        if progress_callback:
                            progress_callback("fetch_complete", url, elapsed)
                    else:
                        results[url] = {"error": "timeout", "message": "Page load timed out"}
                        if progress_callback:
                            progress_callback("fetch_error", url, "Timeout")
                except Exception:
                    results[url] = {"error": "timeout", "message": "Page load timed out"}
                    if progress_callback:
                        progress_callback("fetch_error", url, "Timeout")

                try:
                    if len(driver.window_handles) > 1 and not (
                        browser_mode and handle == original_handle
                    ):
                        driver.close()
                except Exception:
                    pass
            except Exception as e:
                url = tab_url_map.get(handle, "unknown")
                results[url] = {"error": "timeout", "message": f"Tab error: {e}"}
                if progress_callback:
                    progress_callback("fetch_error", url, str(e))

        if browser_mode:
            # Close only the handles this batch opened, then hand focus back
            # to the agent's original tab (which was never navigated).
            try:
                for open_handle in batch_opened:
                    if open_handle in driver.window_handles:
                        try:
                            driver.switch_to.window(open_handle)
                            driver.close()
                        except Exception:
                            pass
            except Exception:
                pass
            try:
                handles = list(driver.window_handles)
                target = (
                    original_handle
                    if original_handle in handles
                    else (handles[0] if handles else None)
                )
                if target:
                    driver.switch_to.window(target)
            except Exception:
                pass

        return results


# ---------------------------------------------------------------------------
# Socket Server for IPC with server.py
# ---------------------------------------------------------------------------

class TuiSocketServer:
    """TCP socket server for communication with server.py."""

    def __init__(self, tor_session: TorSession, app: "OnionSearchTUI", port: int = DEFAULT_SOCKET_PORT):
        self.tor_session = tor_session
        self.app = app
        self.port = port
        self._server_socket: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._client_threads: set[threading.Thread] = set()
        self._active_client_sockets: set[socket.socket] = set()
        self._clients_lock = threading.Lock()

    def start(self) -> int:
        """Start the socket server. Returns the actual port."""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        for port in range(self.port, self.port + 100):
            try:
                self._server_socket.bind(("127.0.0.1", port))
                self.port = port
                break
            except OSError:
                continue
        else:
            raise RuntimeError("Could not find available port for socket server")

        self._server_socket.listen(5)
        self._server_socket.settimeout(1.0)
        self._running = True

        self._thread = threading.Thread(target=self._server_loop, daemon=True)
        self._thread.start()

        return self.port

    def stop(self) -> None:
        """Stop the socket server."""
        self._running = False

        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass

        # Force-close active client sockets so worker threads can exit promptly.
        with self._clients_lock:
            active_sockets = list(self._active_client_sockets)
        for client_socket in active_sockets:
            try:
                client_socket.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                client_socket.close()
            except Exception:
                pass

        if self._thread:
            self._thread.join(timeout=2.0)

        with self._clients_lock:
            client_threads = list(self._client_threads)
        for thread in client_threads:
            if thread.is_alive():
                thread.join(timeout=2.0)

    def _server_loop(self) -> None:
        """Main server loop accepting connections."""
        while self._running:
            try:
                client_socket, addr = self._server_socket.accept()
                client_thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_socket,),
                    daemon=True
                )
                with self._clients_lock:
                    self._client_threads.add(client_thread)
                client_thread.start()
            except socket.timeout:
                continue
            except Exception:
                if self._running:
                    continue
                break

    def _handle_client(self, client_socket: socket.socket) -> None:
        """Handle a client connection."""
        current_thread = threading.current_thread()
        with self._clients_lock:
            self._active_client_sockets.add(client_socket)

        try:
            client_socket.settimeout(60.0)

            data = b""
            while True:
                chunk = client_socket.recv(4096)
                if not chunk:
                    break
                data += chunk
                try:
                    json.loads(data.decode())
                    break
                except json.JSONDecodeError:
                    continue

            if not data:
                return

            request = json.loads(data.decode())
            response = self._handle_request(request)

            response_data = json.dumps(response).encode()
            client_socket.sendall(response_data)

        except Exception as e:
            try:
                error_response = json.dumps({"success": False, "error": str(e)}).encode()
                client_socket.sendall(error_response)
            except Exception:
                pass
        finally:
            with self._clients_lock:
                self._active_client_sockets.discard(client_socket)

            try:
                client_socket.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                client_socket.close()
            except Exception:
                pass
            with self._clients_lock:
                self._client_threads.discard(current_thread)

    def _handle_request(self, request: dict) -> dict:
        """Process a request and return a response."""
        command = request.get("command")

        if command == "ping":
            return {
                "success": True,
                "tor_running": self.tor_session.is_running(),
                "exit_ip": self.tor_session.get_exit_ip(),
                "uptime": self.tor_session.get_uptime(),
                "shutting_down": self.tor_session.is_shutting_down(),
                "mode": getattr(self.tor_session, "_mode", "tui"),
            }

        if self.tor_session.is_shutting_down():
            return {"success": False, "error": "Session is shutting down"}

        if command == "search":
            queries = request.get("queries", [])
            if not queries:
                return {"success": False, "error": "No queries provided"}

            # Check if we need a session divider (not first tool call)
            current_count = self.app.call_from_thread(self.app.get_tool_call_count)
            if current_count > 0:
                self.app.call_from_thread(self.app.add_session_divider)

            # Increment tool call count
            self.app.call_from_thread(self.app.increment_tool_call_count)

            # Create running operation card
            self.app.call_from_thread(
                self.app.add_search_operation,
                queries
            )

            def progress_callback(event, query_index, query, data=None):
                duration = None
                error = None
                if isinstance(data, dict):
                    duration = data.get("duration")
                    error = data.get("error")

                if event == "search_complete":
                    self.app.call_from_thread(
                        self.app.update_search_query_status,
                        query_index,
                        "complete",
                        duration,
                        None,
                    )
                elif event == "search_error":
                    self.app.call_from_thread(
                        self.app.update_search_query_status,
                        query_index,
                        "error",
                        duration,
                        error or "Search failed",
                    )

            result = self.tor_session.search(queries, progress_callback)

            # Complete the operation card(s)
            if result.get("success"):
                results_by_query = self.tor_session._last_results_by_query or []
                self.app.call_from_thread(
                    self.app.complete_search_with_results,
                    results_by_query,
                    result.get("query_durations"),
                    result.get("query_status"),
                )
            else:
                self.app.call_from_thread(
                    self.app.finalize_failed_search,
                    result.get("error", "Unknown error"),
                    result.get("query_durations"),
                    result.get("query_status"),
                    result.get("query_errors"),
                )

            return result

        elif command == "fetch_pages":
            indexes = request.get("indexes", [])
            if not indexes:
                # Create error card for visibility
                self.app.call_from_thread(self.app.increment_tool_call_count)
                self.app.call_from_thread(self.app.add_fetch_operation, [])
                error_msg = "No indexes provided"
                self.app.call_from_thread(self.app.fail_current_operation, error_msg)
                return {"success": False, "error": error_msg}

            # Wait for browser to be ready before fetching pages
            # Do not use call_from_thread for this wait; it blocks the UI thread.
            browser_ready = self.app.wait_for_browser_ready(timeout=60.0)
            if not browser_ready:
                return {"success": False, "error": "Browser failed to start. Please restart the TUI."}

            # Increment tool call count (no divider for fetch_pages - it follows search)
            self.app.call_from_thread(self.app.increment_tool_call_count)

            # Get URLs and titles for display
            fetch_items = []
            for item in self.tor_session._last_search_results:
                if item["index"] in indexes:
                    fetch_items.append({
                        "url": item["url"],
                        "title": item.get("title")
                    })

            # Create operation card via app
            self.app.call_from_thread(
                self.app.add_fetch_operation,
                fetch_items
            )

            def progress_callback(event, url, data=None):
                try:
                    from urllib.parse import urlparse
                    domain = urlparse(url).netloc
                except Exception:
                    domain = url[:50] + "..." if len(url) > 50 else url

                if event == "fetch_complete":
                    self.app.call_from_thread(
                        self.app.update_fetch_sub_item,
                        domain, "complete", data
                    )
                elif event == "fetch_error":
                    self.app.call_from_thread(
                        self.app.update_fetch_sub_item,
                        domain, "error", None, data
                    )

            result = self.tor_session.fetch_pages(indexes, progress_callback)

            # Complete the operation card with updated title
            if result.get("success"):
                success_count = self.app.call_from_thread(
                    self.app.get_current_operation_success_count
                )
                self.app.call_from_thread(
                    self.app.complete_current_operation,
                    None,
                    f"✓ {success_count} results returned to model"
                )
            else:
                self.app.call_from_thread(
                    self.app.fail_current_operation,
                    result.get("error", "Unknown error")
                )

            return result

        elif command == "fetch_specific_page":
            url = request.get("url")
            if not url or not url.strip():
                # Create error card for visibility
                current_count = self.app.call_from_thread(self.app.get_tool_call_count)
                if current_count > 0:
                    self.app.call_from_thread(self.app.add_session_divider)
                self.app.call_from_thread(self.app.increment_tool_call_count)
                self.app.call_from_thread(self.app.add_fetch_operation, [{"url": "(empty)", "title": None}])
                error_msg = "No URL provided"
                self.app.call_from_thread(self.app.fail_current_operation, error_msg)
                return {"success": False, "error": error_msg}

            # Wait for browser to be ready before fetching page
            # Do not use call_from_thread for this wait; it blocks the UI thread.
            browser_ready = self.app.wait_for_browser_ready(timeout=60.0)
            if not browser_ready:
                return {"success": False, "error": "Browser failed to start. Please restart the TUI."}

            # Check if we need a session divider (not first tool call)
            current_count = self.app.call_from_thread(self.app.get_tool_call_count)
            if current_count > 0:
                self.app.call_from_thread(self.app.add_session_divider)

            # Increment tool call count
            self.app.call_from_thread(self.app.increment_tool_call_count)

            try:
                from urllib.parse import urlparse
                domain = urlparse(url).netloc
            except Exception:
                domain = url[:50] + "..." if len(url) > 50 else url

            # Create operation card via app (no title for direct URL fetch)
            self.app.call_from_thread(
                self.app.add_fetch_operation,
                [{"url": url, "title": None}]
            )

            def progress_callback(event, url, data=None):
                try:
                    from urllib.parse import urlparse
                    domain = urlparse(url).netloc
                except Exception:
                    domain = url[:50] + "..." if len(url) > 50 else url

                if event == "fetch_complete":
                    self.app.call_from_thread(
                        self.app.update_fetch_sub_item,
                        domain, "complete", data
                    )
                elif event == "fetch_error":
                    self.app.call_from_thread(
                        self.app.update_fetch_sub_item,
                        domain, "error", None, data
                    )

            result = self.tor_session.fetch_specific_page(url, progress_callback)

            # Complete the operation card with updated title
            if result.get("success"):
                success_count = self.app.call_from_thread(
                    self.app.get_current_operation_success_count
                )
                self.app.call_from_thread(
                    self.app.complete_current_operation,
                    None,
                    f"✓ {success_count} results returned to model"
                )
            else:
                self.app.call_from_thread(
                    self.app.fail_current_operation,
                    result.get("error", "Unknown error")
                )

            return result

        elif command and command.startswith("browser_"):
            action = command[len("browser_"):]
            try:
                if getattr(self.app, "log_browser_action", None):
                    detail = (
                        request.get("url")
                        or request.get("ref")
                        or request.get("selector")
                        or request.get("keys")
                        or ""
                    )
                    self.app.call_from_thread(self.app.log_browser_action, action, str(detail))
            except Exception:
                pass
            return self.tor_session.browser_command(action, request)

        else:
            return {"success": False, "error": f"Unknown command: {command}"}


# ---------------------------------------------------------------------------
# TUI Application
# ---------------------------------------------------------------------------

class OnionSearchTUI(App):
    """Persistent Tor Session TUI Application - Redesigned."""

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }

    #main-scroll {
        height: 1fr;
        scrollbar-size: 0 0;
    }

    #activity-feed {
        padding: 0 2 1 2;
        height: auto;
    }

    /* Header masthead - no border, clean display */
    .header {
        padding: 1 0 0 0;
        margin-bottom: 1;
        height: auto;
    }

    /* Circuit display - no border, slight spacing */
    .circuit-display {
        margin: 1 0 0 0;
        padding: 0;
        height: auto;
    }

    /* Operation cards - no border, slight spacing */
    .operation-card {
        margin: 1 0 0 0;
        padding: 0;
        height: auto;
    }

    /* Workflow complete message */
    .workflow-complete {
        margin: 1 0 0 0;
        padding: 0;
        height: auto;
    }

    /* Session divider */
    .session-divider {
        margin: 2 0 1 0;
        padding: 0;
        height: auto;
    }

    /* Copy confirmation */
    .copy-confirmation {
        margin: 1 0 0 0;
        padding: 0;
        height: auto;
    }

    #status-bar {
        height: 1;
        dock: bottom;
        background: $surface-darken-1;
        padding: 0 2;
    }

    Footer {
        display: none;
    }
    """

    BINDINGS = [
        Binding("c", "enter_link_mode", "Copy Links", show=False),
        Binding("up", "link_up", "Up", show=False, priority=True),
        Binding("down", "link_down", "Down", show=False, priority=True),
        Binding("enter", "link_copy", "Copy", show=False, priority=True),
        Binding("escape", "link_exit", "Exit", show=False, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    link_mode = reactive(False)

    def __init__(self):
        super().__init__()
        self._tor_session: Optional[TorSession] = None
        self._socket_server: Optional[TuiSocketServer] = None
        self._status_timer = None
        self._cleanup_registered = False
        self._cleanup_started = False
        self._cleanup_lock = threading.Lock()
        self._current_operation: Optional[OperationCard] = None
        self._browser_warmup_card: Optional[OperationCard] = None
        self._browser_ready_event = threading.Event()
        self._fetch_sub_item_map: dict[str, int] = {}  # domain -> sub_item index
        self._tool_call_count: int = 0  # Track tool calls for session dividers
        self._search_cards: list[OperationCard] = []  # Track search cards for per-query display
        # Link selector state
        self._links: list[LinkElement] = []
        self._sel: int = -1
        self._prev_card: Optional[OperationCard] = None
        self._card_link_counts: dict[str, int] = {}
        self._fetch_link_map: dict[str, LinkElement] = {}  # domain -> LinkElement

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="main-scroll"):
            with Container(id="activity-feed"):
                yield HeaderWidget(classes="header")
        yield StatusBar(id="status-bar")

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Gate link-mode keys so they only fire when link_mode is active."""
        if action in ("link_up", "link_down", "link_copy", "link_exit"):
            return self.link_mode
        return True

    def on_mount(self) -> None:
        """Called when app is mounted."""
        self._register_cleanup()
        self._start_session()

    def _register_cleanup(self) -> None:
        """Register cleanup handlers for various exit scenarios."""
        if self._cleanup_registered:
            return

        atexit.register(self._cleanup)

        if not _is_windows():
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                try:
                    signal.signal(sig, self._signal_handler)
                except (ValueError, OSError):
                    pass

        self._cleanup_registered = True

    def _signal_handler(self, signum, frame) -> None:
        """Handle termination signals."""
        self._cleanup()
        sys.exit(0)

    def _cleanup(self) -> None:
        """Clean up all resources."""
        with self._cleanup_lock:
            if self._cleanup_started:
                return
            self._cleanup_started = True

        if self._tor_session:
            self._tor_session.request_shutdown()

        # Unblock callers waiting for browser warm-up while shutdown is in progress.
        self._browser_ready_event.set()

        if self._status_timer:
            try:
                self._status_timer.stop()
            except Exception:
                pass

        if self._socket_server:
            try:
                self._socket_server.stop()
            except Exception:
                pass

        if self._tor_session:
            try:
                self._tor_session.stop_tor()
            except Exception:
                pass

        delete_session_file()

    def _add_operation(self, title: str, spinner_type: str = "braille", search_queries: list[str] = None) -> OperationCard:
        """Add a new operation card to the activity feed."""
        feed = self.query_one("#activity-feed", Container)
        card = OperationCard(title, spinner_type=spinner_type, search_queries=search_queries, classes="operation-card")
        feed.mount(card)
        self._current_operation = card
        # Auto-scroll to make the new card visible (suppressed in link mode)
        if not self.link_mode:
            card.scroll_visible()
        return card

    def _add_operation_without_current(self, title: str, spinner_type: str = "braille") -> OperationCard:
        """Add an operation card WITHOUT setting _current_operation.

        Used for background operations (like browser warmup) that run
        concurrently with user-initiated operations.
        """
        feed = self.query_one("#activity-feed", Container)
        card = OperationCard(title, spinner_type=spinner_type, classes="operation-card")
        feed.mount(card)
        if not self.link_mode:
            card.scroll_visible()
        return card

    def add_search_operation(self, queries: list[str]) -> None:
        """Add search operation cards (one per query, running state)."""
        feed = self.query_one("#activity-feed", Container)
        self._search_cards = []  # Track cards for later completion

        for query in queries:
            card = OperationCard(
                title="Search",
                spinner_type="braille",
                search_queries=[query],
                classes="operation-card"
            )
            feed.mount(card)
            self._search_cards.append(card)
            if not self.link_mode:
                card.scroll_visible()

        # Set last card as current operation for error handling
        if self._search_cards:
            self._current_operation = self._search_cards[-1]

    def update_search_query_status(
        self,
        query_index: int,
        state: str,
        duration: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        """Update a single search query card while search is in progress."""
        if query_index < 0 or query_index >= len(self._search_cards):
            return

        card = self._search_cards[query_index]
        if duration is not None:
            card.duration = duration

        if state in ("complete", "error") and card._spinner_timer:
            card._spinner_timer.stop()
            card._spinner_timer = None

        if state == "complete":
            card.state = "complete"
            card.error_message = None
        elif state == "error":
            card.state = "error"
            card.error_message = error or "Search failed"
        else:
            card.state = "running"

        card._update_display()

    def complete_search_with_results(
        self,
        results_by_query: list[dict],
        query_durations: Optional[list[Optional[float]]] = None,
        query_status: Optional[list[str]] = None,
    ) -> None:
        """Complete search by updating each card with its results.

        Args:
            results_by_query: List of {"query": str, "results": list[dict]}
            query_durations: Per-query durations in seconds
            query_status: Per-query status values ("complete" or "error")
        """
        # Match results to cards by query
        for i, group in enumerate(results_by_query):
            results = group["results"]

            # Get the corresponding card
            if i < len(getattr(self, '_search_cards', [])):
                card = self._search_cards[i]
                card.search_results = results

                if query_durations and i < len(query_durations) and query_durations[i] is not None:
                    card.duration = query_durations[i]

                state = "complete"
                if query_status and i < len(query_status):
                    state = query_status[i]

                if state == "error":
                    card.state = "error"
                else:
                    card.state = "complete"
                    card.error_message = None

                if card._spinner_timer:
                    card._spinner_timer.stop()
                    card._spinner_timer = None
                card._update_display()

                # Register links for the copy-link selector
                for result in results:
                    ckey = id(card)
                    ci = self._card_link_counts.get(ckey, 0)
                    self._links.append(LinkElement(
                        url=result["url"],
                        display_label=result.get("title", result["url"]),
                        card_ref=card,
                        card_type="search",
                        is_loading=False,
                        card_index=ci,
                    ))
                    self._card_link_counts[ckey] = ci + 1

        # Clear search cards and current operation
        self._search_cards = []
        self._current_operation = None

        # Update link bar if in link mode (total may have changed)
        if self.link_mode:
            self._update_link_bar()

        # Scroll to show latest (suppressed in link mode)
        if results_by_query and not self.link_mode:
            feed = self.query_one("#activity-feed", Container)
            feed.scroll_end(animate=False)

    def finalize_failed_search(
        self,
        error: str,
        query_durations: Optional[list[Optional[float]]] = None,
        query_status: Optional[list[str]] = None,
        query_errors: Optional[list[Optional[str]]] = None,
    ) -> None:
        """Finalize a failed search run while preserving per-query status."""
        for i, card in enumerate(getattr(self, "_search_cards", [])):
            if query_durations and i < len(query_durations) and query_durations[i] is not None:
                card.duration = query_durations[i]

            status = "error"
            if query_status and i < len(query_status):
                status = query_status[i]

            if status == "complete":
                card.state = "complete"
                card.error_message = None
            else:
                card.state = "error"
                specific_error = None
                if query_errors and i < len(query_errors):
                    specific_error = query_errors[i]
                card.error_message = specific_error or error

            if card._spinner_timer:
                card._spinner_timer.stop()
                card._spinner_timer = None
            card._update_display()

        self._search_cards = []
        self._current_operation = None

    def add_fetch_operation(self, items: list[dict]) -> None:
        """Add a fetch operation card with sub-items for each URL.

        Args:
            items: List of dicts with 'url' and optionally 'title' keys
        """
        title = f"Fetching {len(items)} page{'s' if len(items) != 1 else ''}..."
        card = self._add_operation(title, spinner_type="semicircle")

        # Add sub-items for each URL with page title as subtitle
        self._fetch_sub_item_map = {}
        for item in items:
            url = item.get("url", "")
            page_title = item.get("title")

            try:
                from urllib.parse import urlparse
                domain = urlparse(url).netloc
            except Exception:
                domain = url[:50] + "..." if len(url) > 50 else url

            idx = card.add_sub_item(domain, state="running", subtitle=page_title)
            self._fetch_sub_item_map[domain] = idx

            # Register link for copy-link selector (starts as loading)
            ckey = id(card)
            ci = self._card_link_counts.get(ckey, 0)
            display_label = f"{domain} — {page_title}" if page_title else domain
            link_el = LinkElement(
                url=url,
                display_label=display_label,
                card_ref=card,
                card_type="fetch",
                is_loading=True,
                card_index=ci,
            )
            self._links.append(link_el)
            self._fetch_link_map[domain] = link_el
            self._card_link_counts[ckey] = ci + 1

        # Update link bar if in link mode (total may have changed)
        if self.link_mode:
            self._update_link_bar()

    def update_fetch_sub_item(self, domain: str, state: str, duration: float = None, error: str = None) -> None:
        """Update a fetch sub-item's state."""
        if self._current_operation and domain in self._fetch_sub_item_map:
            idx = self._fetch_sub_item_map[domain]
            self._current_operation.update_sub_item(idx, state=state, duration=duration, error=error)

            # Update link loading state
            if state in ("complete", "error") and domain in self._fetch_link_map:
                link_el = self._fetch_link_map[domain]
                link_el.is_loading = False
                # If user is currently highlighting this link, refresh the highlight
                if self.link_mode and 0 <= self._sel < len(self._links) and self._links[self._sel] is link_el:
                    self._apply_hl()

    def get_current_operation_success_count(self) -> int:
        """Get the count of successfully completed sub-items in the current operation."""
        if self._current_operation and self._current_operation.sub_items:
            return sum(1 for item in self._current_operation.sub_items if item.get("state") == "complete")
        return 0

    def set_current_operation_search_results(self, results: list[dict]) -> None:
        """Set search results on the current operation card for display."""
        if self._current_operation:
            self._current_operation.set_search_results(results)

    def complete_current_operation(self, sub_result: Optional[str] = None, new_title: Optional[str] = None) -> None:
        """Complete the current operation card.

        Args:
            sub_result: Optional text to add as a sub-item
            new_title: Optional new title to replace the original (for "sent to model" indicator)
        """
        if self._current_operation:
            self._current_operation.complete(sub_result, new_title)
            self._current_operation = None
            self._fetch_sub_item_map = {}
            self._fetch_link_map = {}

    def add_workflow_complete(self, message: str = "Results returned to model") -> None:
        """Add a workflow completion indicator."""
        feed = self.query_one("#activity-feed", Container)
        widget = WorkflowComplete(message, classes="workflow-complete")
        feed.mount(widget)
        if not self.link_mode:
            widget.scroll_visible()

    def add_session_divider(self) -> None:
        """Add a session divider with timestamp."""
        feed = self.query_one("#activity-feed", Container)
        widget = SessionDivider(classes="session-divider")
        feed.mount(widget)
        if not self.link_mode:
            widget.scroll_visible()

    def increment_tool_call_count(self) -> int:
        """Increment and return the tool call count."""
        self._tool_call_count += 1
        return self._tool_call_count

    def get_tool_call_count(self) -> int:
        """Get current tool call count."""
        return self._tool_call_count

    def fail_current_operation(self, error: str) -> None:
        """Fail the current operation card."""
        if self._current_operation:
            self._current_operation.fail(error)
            self._current_operation = None
            self._fetch_sub_item_map = {}
            self._fetch_link_map = {}

    def log_browser_action(self, action: str, detail: str = "") -> None:
        """Best-effort status note for a browser automation action."""
        message = f"Browser: {action} {detail}".rstrip()
        logger.info(message)
        try:
            self.sub_title = message
        except Exception:
            pass

    def _start_session(self) -> None:
        """Initialize the Tor session and socket server."""
        # Check for existing session
        existing = check_existing_session()
        if existing:
            self._add_error_card(
                "Session already running",
                f"Another session is running (PID: {existing['pid']}). Close it first."
            )
            return

        # Create Tor session
        self._tor_session = TorSession(mode=resolve_mode())

        # Add Tor connection operation
        card = self._add_operation("Connecting to Tor network", spinner_type="braille")

        # Run Tor startup in a thread
        def start_tor_thread():
            success, message, duration = self._tor_session.start_tor()
            self.call_from_thread(self._on_tor_started, success, message, duration)

        threading.Thread(target=start_tor_thread, daemon=True).start()

    def _on_tor_started(self, success: bool, message: str, duration: Optional[float]) -> None:
        """Called when Tor startup completes."""
        if success:
            # Complete the connection card
            if self._current_operation:
                self._current_operation.duration = duration
                self._current_operation.complete()
                self._current_operation = None

            # Add circuit display
            feed = self.query_one("#activity-feed", Container)
            circuit = CircuitDisplay(classes="circuit-display")
            circuit_info = self._tor_session.get_cached_circuit_info()
            circuit.set_circuit(circuit_info)
            feed.mount(circuit)
            if not self.link_mode:
                circuit.scroll_visible()

            # Set initial circuit flags on status bar
            if circuit_info and isinstance(circuit_info, list):
                countries = [r.get('country') for r in circuit_info]
                status_bar = self.query_one("#status-bar", StatusBar)
                status_bar.set_circuit_countries(countries)

            # Start circuit change monitor
            def _on_circuit_change(countries):
                self.call_from_thread(self._on_circuit_changed, countries)

            self._tor_session.start_circuit_monitor(_on_circuit_change)

            # Start socket server
            self._socket_server = TuiSocketServer(self._tor_session, self)
            port = self._socket_server.start()

            # Write session file
            write_session_file(port)

            # Start browser warm-up (use dedicated reference, not _current_operation)
            self._browser_warmup_card = self._add_operation_without_current(
                "Warming up browser", spinner_type="braille"
            )

            def warm_up_browser_thread():
                browser_success, browser_message, browser_duration = self._tor_session.warm_up_browser()
                self.call_from_thread(self._on_browser_ready, browser_success, browser_message, browser_duration)

            threading.Thread(target=warm_up_browser_thread, daemon=True).start()

            # Start status bar timer
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar.set_start_time(self._tor_session.get_start_time())
            self._status_timer = self.set_interval(1.0, self._update_status_bar)

        else:
            # Fail the connection card
            if self._current_operation:
                self._current_operation.fail(message)
                self._current_operation = None

    def _on_browser_ready(self, success: bool, message: str, duration: Optional[float]) -> None:
        """Called when browser warm-up completes."""
        if success:
            if self._browser_warmup_card:
                self._browser_warmup_card.duration = duration
                self._browser_warmup_card.complete(new_title="Browser ready")
                self._browser_warmup_card = None
            self._browser_ready_event.set()
        else:
            if self._browser_warmup_card:
                self._browser_warmup_card.fail(message)
                self._browser_warmup_card = None
            # Still set event so waiting threads don't hang
            self._browser_ready_event.set()

    def _on_circuit_changed(self, countries: list) -> None:
        """Handle circuit change from Stem event listener."""
        try:
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar.set_circuit_countries(countries)
        except Exception:
            pass

    def wait_for_browser_ready(self, timeout: float = 60.0) -> bool:
        """Wait for browser to be ready.

        Args:
            timeout: Maximum seconds to wait

        Returns:
            True if browser is ready, False if timeout or browser failed
        """
        if self._browser_ready_event.is_set():
            return self._tor_session and self._tor_session.is_browser_ready()

        ready = self._browser_ready_event.wait(timeout=timeout)
        if ready:
            return self._tor_session and self._tor_session.is_browser_ready()
        return False

    def _update_status_bar(self) -> None:
        """Update the status bar."""
        if self._tor_session and self._tor_session.is_running():
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar._update_display()

    def _add_error_card(self, title: str, message: str) -> None:
        """Add an error card to the activity feed."""
        card = self._add_operation(title, spinner_type="braille")
        card.fail(message)

    # -------------------------------------------------------------------
    # Link selector actions
    # -------------------------------------------------------------------

    def action_enter_link_mode(self) -> None:
        """Enter link selection mode (C key)."""
        if self.link_mode or not self._links:
            return
        self.link_mode = True
        self._sel = len(self._links) - 1
        self._scroll_bottom()
        self._apply_hl()
        self._update_link_bar()

    def action_link_up(self) -> None:
        """Move selection up."""
        if self._sel > 0:
            self._sel -= 1
            self._apply_hl()
            self._update_link_bar()

    def action_link_down(self) -> None:
        """Move selection down."""
        if self._sel < len(self._links) - 1:
            self._sel += 1
            self._apply_hl()
            self._update_link_bar()

    def action_link_copy(self) -> None:
        """Copy the selected link to clipboard and exit link mode."""
        if self._sel < 0 or self._sel >= len(self._links):
            return
        url = self._links[self._sel].url
        self._copy_to_clipboard(url)
        self._clear_hl()
        self.link_mode = False
        self._sel = -1
        self._update_link_bar()
        self._show_copy_confirm(url)

    def action_link_exit(self) -> None:
        """Exit link mode without copying."""
        self._clear_hl()
        self.link_mode = False
        self._sel = -1
        self._update_link_bar()
        self._scroll_bottom()

    # -------------------------------------------------------------------
    # Link selector helpers
    # -------------------------------------------------------------------

    def _apply_hl(self) -> None:
        """Apply highlight to the currently selected link."""
        if self._sel < 0 or self._sel >= len(self._links):
            return
        lk = self._links[self._sel]
        card = lk.card_ref
        # Clear previous card highlight if switching to a different card
        if self._prev_card and self._prev_card is not card:
            try:
                self._prev_card.set_hl(-1)
            except Exception:
                pass
        try:
            card.set_hl(lk.card_index)
            scroll = self.query_one("#main-scroll", VerticalScroll)
            scroll.scroll_to_widget(card, animate=False)
            self._prev_card = card
        except Exception:
            pass

    def _clear_hl(self) -> None:
        """Clear highlight from the previously highlighted card."""
        if self._prev_card:
            try:
                self._prev_card.set_hl(-1)
            except Exception:
                pass
        self._prev_card = None

    def _copy_to_clipboard(self, text: str) -> None:
        """Copy text to clipboard using platform-native commands."""
        cmd: Optional[list[str]]
        if _is_macos():
            cmd = ["pbcopy"]
        elif _is_windows():
            cmd = ["clip"]
        elif _is_linux():
            cmd = ["xclip", "-selection", "clipboard"]
        else:
            return

        try:
            p = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            p.communicate(text.encode("utf-8"))
        except Exception:
            pass

    def _show_copy_confirm(self, url: str) -> None:
        """Show a confirmation widget after copying a link."""
        feed = self.query_one("#activity-feed", Container)
        max_len = 75
        display_url = url if len(url) <= max_len else url[:max_len - 1] + "…"
        widget = Static(classes="copy-confirmation")
        widget.update(
            f"[green]●[/green] [white]✓[/white] [white]Link copied to clipboard:[/white]  [bold]{display_url}[/bold]\n"
            f"    [#ff8c00]⚠[/#ff8c00]  [dim italic]Warning: opening this link in a browser other than Tor could deanonymize you[/dim italic]"
        )
        feed.mount(widget)
        widget.scroll_visible()

    def _update_link_bar(self) -> None:
        """Update the status bar with link mode state."""
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.set_link_mode(self.link_mode, self._sel, len(self._links))

    def _scroll_bottom(self) -> None:
        """Scroll the main scroll area to the bottom."""
        try:
            self.query_one("#main-scroll", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    def action_quit(self) -> None:
        """Quit the application."""
        self._cleanup()
        self.exit()


# ---------------------------------------------------------------------------
# Headless Mode (No TUI)
# ---------------------------------------------------------------------------

class HeadlessOnionSession:
    """
    Headless version of OnionSearchTUI that runs without a visible UI.

    Provides the same socket server interface but logs to stderr/file instead
    of displaying a Textual UI. Used for servers or automation environments.
    """

    def __init__(self, log_file: Optional[Path] = None):
        self._tor_session: Optional[TorSession] = None
        self._socket_server: Optional[TuiSocketServer] = None
        self._running = False
        self._log_file = log_file
        self._tool_call_count: int = 0
        self._current_operation_success_count: int = 0
        self._cleanup_registered = False
        self._cleanup_started = False
        self._cleanup_lock = threading.Lock()

    def log(self, message: str) -> None:
        """Log a message to stderr or log file."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        if self._log_file:
            with open(self._log_file, "a") as f:
                f.write(line + "\n")
        print(line, file=sys.stderr)

    def log_browser_action(self, action: str, detail: str = "") -> None:
        """Log a browser automation action."""
        self.log(f"Browser: {action} {detail}".rstrip())

    def start(self) -> int:
        """
        Start the headless Tor session.

        Returns:
            Socket port number for IPC.
        """
        # Check for existing session
        existing = check_existing_session()
        if existing:
            raise RuntimeError(f"Session already running (PID: {existing['pid']})")

        self.log("Starting Tor connection...")

        # Create Tor session
        self._tor_session = TorSession(mode=resolve_mode())
        success, message, duration = self._tor_session.start_tor()

        if not success:
            raise RuntimeError(f"Failed to start Tor: {message}")

        self.log(f"Tor connected in {duration:.1f}s")

        # Warm up browser
        self.log("Warming up browser...")
        success, message, duration = self._tor_session.warm_up_browser()
        if not success:
            self.log(f"Warning: Browser warm-up failed: {message}")
        else:
            self.log(f"Browser ready in {duration:.1f}s")

        # Start socket server
        self._socket_server = TuiSocketServer(self._tor_session, self)
        port = self._socket_server.start()

        # Write session file
        write_session_file(port)

        self.log(f"Listening on port {port}")
        self._running = True

        return port

    def run(self) -> None:
        """Run the headless session until terminated."""
        self._register_cleanup()

        self.log("Ready for connections. Press Ctrl+C to stop.")

        # Block until terminated
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

        self._cleanup()

    def _register_cleanup(self) -> None:
        """Register cleanup handlers."""
        if self._cleanup_registered:
            return

        atexit.register(self._cleanup)

        if not _is_windows():
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                try:
                    signal.signal(sig, self._signal_handler)
                except (ValueError, OSError):
                    pass

        self._cleanup_registered = True

    def _signal_handler(self, signum, frame) -> None:
        """Handle termination signals."""
        self._running = False

    def _cleanup(self) -> None:
        """Clean up all resources."""
        with self._cleanup_lock:
            if self._cleanup_started:
                return
            self._cleanup_started = True

        self.log("Shutting down...")
        self._running = False
        if self._tor_session:
            self._tor_session.request_shutdown()
        if self._socket_server:
            try:
                self._socket_server.stop()
            except Exception:
                pass
        if self._tor_session:
            try:
                self._tor_session.stop_tor()
            except Exception:
                pass
        delete_session_file()

    # ---------------------------------------------------------------------------
    # Mock TUI methods for socket server compatibility
    # These are called by TuiSocketServer via call_from_thread()
    # ---------------------------------------------------------------------------

    def call_from_thread(self, func: Callable, *args, **kwargs):
        """Execute function directly (no thread dispatch needed in headless mode)."""
        return func(*args, **kwargs)

    def add_search_operation(self, queries: list[str]) -> None:
        """Log search operation start."""
        if len(queries) == 1:
            self.log(f"Search: {queries[0]}")
        else:
            self.log(f"Search: {len(queries)} queries")

    def update_search_query_status(
        self,
        query_index: int,
        state: str,
        duration: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        """Log per-query search status updates."""
        label = f"  Query {query_index + 1}"
        if state == "complete":
            if duration is not None:
                self.log(f"{label}: complete ({duration:.1f}s)")
            else:
                self.log(f"{label}: complete")
        elif state == "error":
            if duration is not None:
                self.log(f"{label}: error ({duration:.1f}s) - {error or 'unknown'}")
            else:
                self.log(f"{label}: error - {error or 'unknown'}")

    def complete_search_with_results(
        self,
        results_by_query: list[dict],
        query_durations: Optional[list[Optional[float]]] = None,
        query_status: Optional[list[str]] = None,
    ) -> None:
        """Log final per-query results for search."""
        for i, group in enumerate(results_by_query):
            count = len(group.get("results", []))
            duration = None
            if query_durations and i < len(query_durations):
                duration = query_durations[i]
            status = None
            if query_status and i < len(query_status):
                status = query_status[i]

            if status == "error":
                self.log(f"  Query {i + 1}: failed")
            elif duration is not None:
                self.log(f"  Query {i + 1}: {count} result(s) ({duration:.1f}s)")
            else:
                self.log(f"  Query {i + 1}: {count} result(s)")

    def finalize_failed_search(
        self,
        error: str,
        query_durations: Optional[list[Optional[float]]] = None,
        query_status: Optional[list[str]] = None,
        query_errors: Optional[list[Optional[str]]] = None,
    ) -> None:
        """Log search failure with per-query details."""
        self.log(f"Search failed: {error}")
        if not query_status:
            return
        for i, status in enumerate(query_status):
            duration = None
            if query_durations and i < len(query_durations):
                duration = query_durations[i]
            query_error = None
            if query_errors and i < len(query_errors):
                query_error = query_errors[i]

            if status == "complete":
                if duration is not None:
                    self.log(f"  Query {i + 1}: complete ({duration:.1f}s)")
                else:
                    self.log(f"  Query {i + 1}: complete")
            else:
                if duration is not None:
                    self.log(f"  Query {i + 1}: failed ({duration:.1f}s) - {query_error or error}")
                else:
                    self.log(f"  Query {i + 1}: failed - {query_error or error}")

    def add_fetch_operation(self, items: list[dict]) -> None:
        """Log fetch operation start."""
        self._current_operation_success_count = 0
        self.log(f"Fetching {len(items)} page{'s' if len(items) != 1 else ''}...")

    def update_fetch_sub_item(self, domain: str, state: str, duration: float = None, error: str = None) -> None:
        """Log fetch sub-item update."""
        if state == "complete":
            self._current_operation_success_count += 1
            if duration:
                self.log(f"  {domain}: complete ({duration:.1f}s)")
            else:
                self.log(f"  {domain}: complete")
        elif state == "error":
            self.log(f"  {domain}: error - {error or 'unknown'}")

    def complete_current_operation(self, sub_result: Optional[str] = None, new_title: Optional[str] = None) -> None:
        """Log operation completion."""
        if new_title:
            self.log(new_title)
        elif sub_result:
            self.log(f"Complete: {sub_result}")
        else:
            self.log("Operation complete")

    def fail_current_operation(self, error: str) -> None:
        """Log operation failure."""
        self.log(f"Operation failed: {error}")

    def add_session_divider(self) -> None:
        """Log session divider (no-op in headless mode)."""
        pass

    def increment_tool_call_count(self) -> int:
        """Increment and return the tool call count."""
        self._tool_call_count += 1
        return self._tool_call_count

    def get_tool_call_count(self) -> int:
        """Get current tool call count."""
        return self._tool_call_count

    def get_current_operation_success_count(self) -> int:
        """Get the count of successfully completed sub-items."""
        return self._current_operation_success_count

    def wait_for_browser_ready(self, timeout: float = 60.0) -> bool:
        """Return current browser readiness (headless compatibility shim)."""
        if not self._tor_session:
            return False
        return self._tor_session.is_browser_ready()


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main():
    """Main entry point."""
    import argparse

    global _CLI_MODE

    parser = argparse.ArgumentParser(description="Tor Browser MCP TUI")
    parser.add_argument("--headless", action="store_true",
                        help="Run in headless mode (no UI)")
    parser.add_argument("--log-file", type=Path,
                        help="Log file path for headless mode")
    parser.add_argument("--mode", choices=["tui", "browser"],
                        help="Session mode (overrides config.toml [server] mode; default: browser)")
    args = parser.parse_args()
    _CLI_MODE = args.mode
    mode = resolve_mode()

    # Check for existing session first
    existing = check_existing_session()
    if existing:
        print(f"Error: Another Onion Search session is already running (PID: {existing['pid']})")
        print(f"Close that session first or delete {SESSION_FILE}")
        sys.exit(1)

    # Check config exists
    config_path = SCRIPT_DIR / "config.toml"
    if not config_path.exists():
        print("Error: config.toml not found. Run 'python installer.py' first.")
        sys.exit(1)

    # Check config for headless mode if not specified on command line
    config = load_config()
    headless = args.headless or config.get("tui", {}).get("headless", False)
    log_file = args.log_file
    if not log_file:
        log_file_config = config.get("tui", {}).get("log_file")
        if log_file_config:
            log_file = Path(log_file_config)

    if mode == "browser" or headless:
        # Browser mode is a socket-driven daemon: never the Textual UI.
        session = HeadlessOnionSession(log_file=log_file)
        try:
            session.start()
            session.run()
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        app = OnionSearchTUI()
        app.run()


if __name__ == "__main__":
    main()
