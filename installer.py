#!/usr/bin/env python3
"""
Cross-platform installer for Onion Search MCP Server.
Uses only Python standard library - no external dependencies.

Requirements: Python 3.11+
"""

import concurrent.futures
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

# ===========================================================================
# Constants
# ===========================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
COMPONENTS_DIR = SCRIPT_DIR / "components"
CONFIG_FILE = SCRIPT_DIR / "config.toml"
VENV_DIR = SCRIPT_DIR / ".venv"


# ===========================================================================
# Progress Display
# ===========================================================================


class ProgressDisplay:
    """Thread-safe in-place task list display."""

    PENDING = "pending"
    ACTIVE = "active"
    COMPLETE = "complete"
    ERROR = "error"

    BRAILLE_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    SPINNER_INTERVAL = 0.1  # 100ms per frame

    _ANSI_RE = __import__("re").compile(r"\033\[[0-9;]*m")

    def __init__(self):
        self._tasks: list[dict] = []
        self._task_index: dict[str, int] = {}
        self._lock = threading.Lock()
        self._lines_printed = 0
        self._is_tty = sys.stdout.isatty()
        self._finished = False
        self._spinner_frame = 0
        self._spinner_active = False
        self._spinner_thread: threading.Thread | None = None

    def add_task(self, task_id: str, label: str) -> None:
        """Register a task (called before any parallel work starts)."""
        with self._lock:
            self._task_index[task_id] = len(self._tasks)
            self._tasks.append({
                "id": task_id,
                "label": label,
                "state": self.PENDING,
                "progress": 0.0,
                "detail": "",
                "duration": None,
            })

    def _start_spinner(self) -> None:
        """Start the background spinner thread if not already running. Must be called with _lock held."""
        if self._spinner_active or not self._is_tty:
            return
        self._spinner_active = True
        self._spinner_thread = threading.Thread(target=self._spin, daemon=True)
        self._spinner_thread.start()

    def _stop_spinner(self) -> None:
        """Stop the background spinner thread. Must be called with _lock held."""
        self._spinner_active = False

    def _spin(self) -> None:
        """Background thread: advance spinner frame and redraw at regular intervals."""
        while self._spinner_active:
            time.sleep(self.SPINNER_INTERVAL)
            with self._lock:
                if not self._spinner_active or self._finished:
                    break
                # Only advance if there are active tasks
                has_active = any(t["state"] == self.ACTIVE for t in self._tasks)
                if has_active:
                    self._spinner_frame = (self._spinner_frame + 1) % len(self.BRAILLE_SPINNER)
                    self._redraw()
                else:
                    break

    def update(
        self,
        task_id: str,
        state: str,
        progress: float = 0.0,
        detail: str = "",
        duration: float | None = None,
    ) -> None:
        """Thread-safe update of a single task's display state."""
        with self._lock:
            idx = self._task_index[task_id]
            task = self._tasks[idx]
            task["state"] = state
            task["progress"] = progress
            task["detail"] = detail
            if duration is not None:
                task["duration"] = duration
            if self._finished:
                # After finish(), in-place redraw is disabled. Emit one-line updates
                # so late sequential tasks (e.g. configuration/launchers) remain visible.
                print(self._format_line(task), flush=True)
            else:
                self._redraw()
                # Start spinner thread if a task became active
                if state == self.ACTIVE:
                    self._start_spinner()

    def _format_line(self, task: dict) -> str:
        """Format a single task line."""
        state = task["state"]
        label = task["label"]
        detail = task["detail"]
        duration = task["duration"]
        progress = task["progress"]

        if state == self.PENDING:
            indicator = "  \033[2m○\033[0m" if self._is_tty else "  -"
            return f"{indicator} {label}"
        elif state == self.ACTIVE:
            spinner_char = self.BRAILLE_SPINNER[self._spinner_frame % len(self.BRAILLE_SPINNER)]
            indicator = f"  \033[1m{spinner_char}\033[0m" if self._is_tty else "  >"
            if progress > 0 and self._is_tty:
                # Inline progress bar
                bar_width = 20
                filled = int(bar_width * progress / 100)
                bar = "█" * filled + "░" * (bar_width - filled)
                return f"{indicator} {label}  \033[2m{bar}\033[0m {progress:.0f}%"
            else:
                parts = [f"{indicator} {label}"]
                if progress > 0:
                    parts.append(f"{progress:.0f}%")
                if detail:
                    parts.append(detail)
                return "  ".join(parts)
        elif state == self.COMPLETE:
            indicator = "  \033[32m✓\033[0m" if self._is_tty else "  +"
            suffix = ""
            if duration is not None:
                if self._is_tty:
                    suffix = f"  \033[2m{duration:.1f}s\033[0m"
                else:
                    suffix = f"  {duration:.1f}s"
            return f"{indicator} {label}{suffix}"
        else:  # ERROR
            indicator = "  \033[31m✗\033[0m" if self._is_tty else "  !"
            suffix = ""
            if detail:
                suffix = f" — {detail}"
            return f"{indicator} {label}{suffix}"

    def _visible_len(self, text: str) -> int:
        """Return the visible length of a string, ignoring ANSI escape codes."""
        return len(self._ANSI_RE.sub("", text))

    def _truncate_visible(self, text: str, max_width: int) -> str:
        """Truncate text to max_width visible characters, preserving ANSI codes."""
        visible = 0
        result: list[str] = []
        i = 0
        while i < len(text):
            m = self._ANSI_RE.match(text, i)
            if m:
                result.append(m.group())
                i = m.end()
            else:
                if visible >= max_width:
                    break
                result.append(text[i])
                visible += 1
                i += 1
        result.append("\033[0m")
        return "".join(result)

    def _redraw(self) -> None:
        """Reprint all task lines in-place. Must be called with _lock held."""
        if not self._is_tty or self._finished:
            return

        # Move cursor up to overwrite previous output
        if self._lines_printed > 0:
            sys.stdout.write(f"\033[{self._lines_printed}A")

        try:
            cols = os.get_terminal_size().columns
        except OSError:
            cols = 80

        lines = []
        for task in self._tasks:
            line = self._format_line(task)
            if self._visible_len(line) > cols:
                line = self._truncate_visible(line, cols)
            lines.append(line)

        output = "\n".join(line + "\033[K" for line in lines) + "\n"
        sys.stdout.write(output)
        sys.stdout.flush()
        self._lines_printed = len(lines)

    def log_non_tty(self, task_id: str) -> None:
        """For non-TTY output: print a single status line for a completed/errored task."""
        if self._is_tty:
            return
        with self._lock:
            idx = self._task_index[task_id]
            task = self._tasks[idx]
            print(self._format_line(task), flush=True)

    def finish(self) -> None:
        """Finalize display so subsequent output doesn't overwrite. Idempotent."""
        with self._lock:
            if self._finished:
                return
            self._stop_spinner()
            if self._is_tty:
                self._redraw()
            self._finished = True
        # Wait for spinner thread to exit (outside lock to avoid deadlock)
        if self._spinner_thread is not None:
            self._spinner_thread.join(timeout=1.0)
        print()


# ===========================================================================
# Version Fetching
# ===========================================================================


def fetch_latest_geckodriver_version() -> str:
    """Fetch the latest geckodriver version from GitHub releases API."""
    url = "https://api.github.com/repos/mozilla/geckodriver/releases/latest"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "onion-search-mcp-installer",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    ctx = ssl.create_default_context()

    with urllib.request.urlopen(request, context=ctx, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
        return data["tag_name"]  # e.g., "v0.36.0"


def fetch_latest_tor_browser_version() -> tuple[str, dict]:
    """Fetch the latest Tor Browser version and download URLs from Tor Project API."""
    url = "https://aus1.torproject.org/torbrowser/update_3/release/downloads.json"
    request = urllib.request.Request(url, headers={"User-Agent": "onion-search-mcp-installer"})
    ctx = ssl.create_default_context()

    with urllib.request.urlopen(request, context=ctx, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))

        version = data["version"]
        downloads = data["downloads"]
        base_url = f"https://dist.torproject.org/torbrowser/{version}"

        # Extract URLs from API, with fallback construction for missing platforms
        urls = {
            "darwin": (
                downloads.get("macos", {}).get("ALL", {}).get("binary")
                or f"{base_url}/tor-browser-macos-{version}.dmg"
            ),
            "linux_x86_64": (
                downloads.get("linux-x86_64", {}).get("ALL", {}).get("binary")
                or f"{base_url}/tor-browser-linux-x86_64-{version}.tar.xz"
            ),
            "linux_aarch64": (
                downloads.get("linux-aarch64", {}).get("ALL", {}).get("binary")
                or f"{base_url}/tor-browser-linux-aarch64-{version}.tar.xz"
            ),
            "win32_x86_64": (
                downloads.get("win64", {}).get("ALL", {}).get("binary")
                or f"{base_url}/tor-browser-windows-x86_64-portable-{version}.exe"
            ),
            "win32_aarch64": (
                downloads.get("win64", {}).get("ALL", {}).get("binary")
                or f"{base_url}/tor-browser-windows-x86_64-portable-{version}.exe"
            ),
        }

        return version, urls


def get_geckodriver_url(os_name: str, arch: str, version: str) -> str:
    """
    Get the download URL for geckodriver for the specified platform.

    Args:
        version: Pre-fetched version string (e.g., "v0.36.0")

    Returns:
        The download URL string.
    """
    # Build platform-specific URL
    if os_name == "darwin":
        if arch == "arm64":
            filename = f"geckodriver-{version}-macos-aarch64.tar.gz"
        else:
            filename = f"geckodriver-{version}-macos.tar.gz"
    elif os_name == "linux":
        if arch == "aarch64":
            filename = f"geckodriver-{version}-linux-aarch64.tar.gz"
        else:
            filename = f"geckodriver-{version}-linux64.tar.gz"
    elif os_name == "win32":
        filename = f"geckodriver-{version}-win64.zip"
    else:
        raise RuntimeError(f"Unsupported platform: {os_name}")

    return f"https://github.com/mozilla/geckodriver/releases/download/{version}/{filename}"


def get_tor_browser_url(os_name: str, arch: str, version: str, urls: dict) -> str:
    """
    Get the download URL for Tor Browser for the specified platform.

    Args:
        version: Pre-fetched version string.
        urls: Pre-fetched URL dict from fetch_latest_tor_browser_version().

    Returns:
        The platform-specific download URL string.
    """
    # Get platform-specific URL
    if os_name == "darwin":
        url = urls.get("darwin")
    elif os_name == "linux":
        url_key = f"linux_{arch}"
        url = urls.get(url_key)
    elif os_name == "win32":
        url_key = f"win32_{arch}"
        url = urls.get(url_key)
    else:
        raise RuntimeError(f"Unsupported platform: {os_name}")

    if not url:
        raise RuntimeError(f"No Tor Browser download available for {os_name}/{arch}")

    return url


# Common DuckDuckGo regions (code, description)
COMMON_REGIONS = [
    ("us-en", "United States"),
    ("uk-en", "United Kingdom"),
    ("au-en", "Australia"),
    ("ca-en", "Canada"),
    ("de-de", "Germany"),
    ("fr-fr", "France"),
    ("es-es", "Spain"),
    ("it-it", "Italy"),
    ("jp-jp", "Japan"),
    ("br-pt", "Brazil"),
]


# ===========================================================================
# Platform Detection
# ===========================================================================


def detect_platform() -> tuple[str, str]:
    """
    Detect OS and architecture.

    Returns:
        Tuple of (os_name, arch) where:
        - os_name: 'darwin', 'linux', 'win32'
        - arch: 'x86_64', 'arm64', 'aarch64'
    """
    os_name = sys.platform  # 'darwin', 'linux', 'win32'

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64" if os_name == "darwin" else "aarch64"
    else:
        raise RuntimeError(f"Unsupported architecture: {machine}")

    return os_name, arch


# ===========================================================================
# SSL Certificate Helper
# ===========================================================================


def get_venv_cert_path() -> Optional[str]:
    """
    Get the certificate bundle path from the virtual environment's certifi.

    On macOS and Windows, system certificates often cause SSL verification errors.
    This uses the venv's Python to get certifi's certificate bundle path.

    Returns:
        Path to certificate bundle, or None if not available.
    """
    if sys.platform == "win32":
        venv_python = VENV_DIR / "Scripts" / "python.exe"
    else:
        venv_python = VENV_DIR / "bin" / "python"

    if not venv_python.exists():
        return None

    try:
        result = subprocess.run(
            [str(venv_python), "-c", "import certifi; print(certifi.where())"],
            capture_output=True,
            text=True,
            check=True,
        )
        cert_path = result.stdout.strip()
        if cert_path and Path(cert_path).exists():
            return cert_path
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    return None


# ===========================================================================
# Idempotency Checks
# ===========================================================================


def check_venv_exists() -> bool:
    """Check if virtual environment exists and is valid."""
    if sys.platform == "win32":
        venv_python = VENV_DIR / "Scripts" / "python.exe"
    else:
        venv_python = VENV_DIR / "bin" / "python"
    return venv_python.exists()


def check_requirements_installed(platform_name: str) -> bool:
    """Check if platform requirements are already installed."""
    req_file = SCRIPT_DIR / f"requirements-{platform_name}.txt"
    if not req_file.exists():
        return False

    if sys.platform == "win32":
        venv_pip = VENV_DIR / "Scripts" / "pip.exe"
    else:
        venv_pip = VENV_DIR / "bin" / "pip"

    if not venv_pip.exists():
        return False

    try:
        result = subprocess.run(
            [str(venv_pip), "freeze"],
            capture_output=True,
            text=True,
            check=True,
        )
        installed = set(
            pkg.split("==")[0].lower()
            for pkg in result.stdout.strip().split("\n")
            if pkg
        )

        with open(req_file) as f:
            required = set(
                line.strip().split(">=")[0].split("==")[0].lower()
                for line in f
                if line.strip() and not line.startswith("#")
            )

        return required.issubset(installed)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def check_geckodriver_exists() -> bool:
    """Check if geckodriver is installed in components folder."""
    if sys.platform == "win32":
        geckodriver_path = COMPONENTS_DIR / "geckodriver.exe"
        return geckodriver_path.exists()
    geckodriver_path = COMPONENTS_DIR / "geckodriver"
    return geckodriver_path.exists() and os.access(geckodriver_path, os.X_OK)


def check_tor_browser_exists() -> bool:
    """Check if Tor Browser is installed."""
    if sys.platform == "darwin":
        return (COMPONENTS_DIR / "Tor Browser.app").exists()
    elif sys.platform == "linux":
        return (COMPONENTS_DIR / "tor-browser").exists()
    else:  # Windows
        return (COMPONENTS_DIR / "TorBrowser" / "Browser" / "firefox.exe").exists()


def check_tor_browser_profile_exists() -> bool:
    """Check if Tor Browser profile directory exists (macOS and Windows)."""
    if sys.platform == "darwin":
        profile_path = COMPONENTS_DIR / "Tor Browser.app" / "Contents" / "Resources" / "TorBrowser" / "Data" / "Browser" / "profile.default"
        return profile_path.is_dir()
    elif sys.platform == "win32":
        profile_path = COMPONENTS_DIR / "TorBrowser" / "Browser" / "TorBrowser" / "Data" / "Browser" / "profile.default"
        return profile_path.is_dir()
    else:  # Linux
        return True  # Linux profile is created automatically


def check_config_exists() -> bool:
    """Check if config.toml exists."""
    return CONFIG_FILE.exists()


# ===========================================================================
# Download Helpers
# ===========================================================================


def download_file(
    url: str,
    dest: Path,
    desc: str,
    progress_callback: Callable[[float, int, int], None] | None = None,
) -> None:
    """Download file with optional progress callback.

    Args:
        progress_callback: Called with (fraction_complete, bytes_downloaded, total_bytes).
    """
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    # Create SSL context for secure downloads
    ctx = ssl.create_default_context()

    # On macOS and Windows, use certifi certificates from venv if available
    cert_path = get_venv_cert_path()
    if cert_path:
        ctx.load_verify_locations(cafile=cert_path)

    try:
        with urllib.request.urlopen(request, timeout=300, context=ctx) as response:
            total_size = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 65536

            with open(dest, "wb") as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size and progress_callback:
                        progress_callback(
                            downloaded / total_size,
                            downloaded,
                            total_size,
                        )
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to download {url}: {e}")


# ===========================================================================
# Installation Steps
# ===========================================================================


def create_venv() -> None:
    """Create virtual environment in project folder."""
    subprocess.run(
        [sys.executable, "-m", "venv", str(VENV_DIR)],
        check=True,
        capture_output=True,
    )


def install_requirements(platform_name: str) -> None:
    """Install platform-specific requirements into venv."""
    req_file = SCRIPT_DIR / f"requirements-{platform_name}.txt"
    if sys.platform == "win32":
        venv_pip = VENV_DIR / "Scripts" / "pip.exe"
    else:
        venv_pip = VENV_DIR / "bin" / "pip"

    subprocess.run(
        [str(venv_pip), "install", "--prefer-binary", "-r", str(req_file)],
        check=True,
        capture_output=True,
    )


def setup_geckodriver(
    os_name: str,
    arch: str,
    version: str,
    display: ProgressDisplay | None = None,
    task_id: str = "",
) -> None:
    """Download and setup geckodriver (macOS/Linux)."""
    COMPONENTS_DIR.mkdir(exist_ok=True)
    url = get_geckodriver_url(os_name, arch, version)

    def on_progress(frac: float, downloaded: int, total: int) -> None:
        if display:
            mb_dl = downloaded / (1024 * 1024)
            mb_tot = total / (1024 * 1024)
            display.update(
                task_id, ProgressDisplay.ACTIVE,
                progress=frac * 100,
                detail=f"({mb_dl:.1f}/{mb_tot:.1f} MB)",
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        archive_path = Path(tmpdir) / "geckodriver.tar.gz"
        download_file(url, archive_path, f"geckodriver {version}", on_progress)

        if display:
            display.update(task_id, ProgressDisplay.ACTIVE, progress=100, detail="extracting")

        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(COMPONENTS_DIR)

        geckodriver_path = COMPONENTS_DIR / "geckodriver"
        os.chmod(geckodriver_path, 0o755)


def setup_geckodriver_windows(
    arch: str,
    version: str,
    display: ProgressDisplay | None = None,
    task_id: str = "",
) -> None:
    """Download and setup geckodriver on Windows (.zip)."""
    COMPONENTS_DIR.mkdir(exist_ok=True)
    url = get_geckodriver_url("win32", arch, version)

    def on_progress(frac: float, downloaded: int, total: int) -> None:
        if display:
            mb_dl = downloaded / (1024 * 1024)
            mb_tot = total / (1024 * 1024)
            display.update(
                task_id, ProgressDisplay.ACTIVE,
                progress=frac * 100,
                detail=f"({mb_dl:.1f}/{mb_tot:.1f} MB)",
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        archive_path = Path(tmpdir) / "geckodriver.zip"
        download_file(url, archive_path, f"geckodriver {version}", on_progress)

        if display:
            display.update(task_id, ProgressDisplay.ACTIVE, progress=100, detail="extracting")

        with zipfile.ZipFile(archive_path, "r") as zip_ref:
            zip_ref.extractall(COMPONENTS_DIR)


def setup_tor_browser_macos(
    tb_version: str,
    tb_url: str,
    display: ProgressDisplay | None = None,
    task_id: str = "",
) -> None:
    """Download and setup Tor Browser on macOS (.dmg)."""
    COMPONENTS_DIR.mkdir(exist_ok=True)

    def on_progress(frac: float, downloaded: int, total: int) -> None:
        if display:
            mb_dl = downloaded / (1024 * 1024)
            mb_tot = total / (1024 * 1024)
            display.update(
                task_id, ProgressDisplay.ACTIVE,
                progress=frac * 100,
                detail=f"({mb_dl:.1f}/{mb_tot:.1f} MB)",
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        dmg_path = Path(tmpdir) / "TorBrowser.dmg"
        download_file(tb_url, dmg_path, f"Tor Browser v{tb_version}", on_progress)

        if display:
            display.update(task_id, ProgressDisplay.ACTIVE, progress=100, detail="mounting DMG")

        mount_point = Path(tmpdir) / "mount"
        mount_point.mkdir()

        subprocess.run(
            [
                "hdiutil", "attach", str(dmg_path),
                "-mountpoint", str(mount_point),
                "-nobrowse", "-quiet",
            ],
            check=True,
        )

        try:
            if display:
                display.update(task_id, ProgressDisplay.ACTIVE, progress=100, detail="copying app")

            src_app = mount_point / "Tor Browser.app"
            dest_app = COMPONENTS_DIR / "Tor Browser.app"

            if src_app.exists():
                shutil.copytree(src_app, dest_app, symlinks=True)
            else:
                raise RuntimeError("Tor Browser.app not found in DMG")
        finally:
            subprocess.run(
                ["hdiutil", "detach", str(mount_point), "-quiet"],
                capture_output=True,
            )


def ensure_tor_browser_profile_macos() -> None:
    """
    Ensure in-bundle Tor Browser profile directory exists for tbselenium.

    Fresh Tor Browser 15.x installations don't include the profile.default
    directory until first manual launch. tbselenium requires this directory
    to exist, even if empty.
    """
    profile_path = COMPONENTS_DIR / "Tor Browser.app" / "Contents" / "Resources" / "TorBrowser" / "Data" / "Browser" / "profile.default"

    if profile_path.exists():
        return

    profile_path.mkdir(parents=True, exist_ok=True)
    (profile_path / "extensions").mkdir(exist_ok=True)


def setup_tor_browser_linux(
    arch: str,
    tb_version: str,
    tb_url: str,
    display: ProgressDisplay | None = None,
    task_id: str = "",
) -> None:
    """Download and setup Tor Browser on Linux (tarball)."""
    COMPONENTS_DIR.mkdir(exist_ok=True)

    def on_progress(frac: float, downloaded: int, total: int) -> None:
        if display:
            mb_dl = downloaded / (1024 * 1024)
            mb_tot = total / (1024 * 1024)
            display.update(
                task_id, ProgressDisplay.ACTIVE,
                progress=frac * 100,
                detail=f"({mb_dl:.1f}/{mb_tot:.1f} MB)",
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        archive_path = Path(tmpdir) / "tor-browser.tar.xz"
        download_file(tb_url, archive_path, f"Tor Browser v{tb_version}", on_progress)

        if display:
            display.update(task_id, ProgressDisplay.ACTIVE, progress=100, detail="extracting")

        subprocess.run(
            ["tar", "-xJf", str(archive_path), "-C", str(COMPONENTS_DIR)],
            check=True,
            capture_output=True,
        )


def setup_tor_browser_windows(
    arch: str,
    tb_version: str,
    tb_url: str,
    display: ProgressDisplay | None = None,
    task_id: str = "",
) -> None:
    """Download and setup Tor Browser on Windows (portable version)."""
    COMPONENTS_DIR.mkdir(exist_ok=True)

    def on_progress(frac: float, downloaded: int, total: int) -> None:
        if display:
            mb_dl = downloaded / (1024 * 1024)
            mb_tot = total / (1024 * 1024)
            display.update(
                task_id, ProgressDisplay.ACTIVE,
                progress=frac * 100,
                detail=f"({mb_dl:.1f}/{mb_tot:.1f} MB)",
            )

    # Download to components directory (not temp) to avoid file locking issues
    installer_path = COMPONENTS_DIR / "torbrowser-installer.exe"
    try:
        download_file(tb_url, installer_path, f"Tor Browser v{tb_version}", on_progress)

        if display:
            display.update(task_id, ProgressDisplay.ACTIVE, progress=100, detail="installing")

        # The portable version is an NSIS installer.
        # Use silent install flags: /S = silent (uppercase!), /D= must be last.
        # The path must be absolute and should NOT have quotes.
        # Use "TorBrowser" (no space) to avoid NSIS path parsing issues.
        dest_dir = (COMPONENTS_DIR / "TorBrowser").resolve()
        subprocess.run(
            [str(installer_path), "/S", f"/D={dest_dir}"],
            check=True,
        )
    finally:
        if installer_path.exists():
            try:
                installer_path.unlink()
            except OSError:
                pass


def ensure_tor_browser_profile_windows() -> None:
    """
    Ensure Tor Browser profile directory exists on Windows.

    Fresh installations may not include profile.default until first launch.
    tbselenium-windows requires this directory to exist.
    """
    profile_path = (
        COMPONENTS_DIR / "TorBrowser" / "Browser" / "TorBrowser" /
        "Data" / "Browser" / "profile.default"
    )

    if profile_path.exists():
        return

    profile_path.mkdir(parents=True, exist_ok=True)
    (profile_path / "extensions").mkdir(exist_ok=True)


def _read_key() -> str:
    """Read a single keypress, returning a string identifier.

    Returns one of: 'up', 'down', 'enter', 'backspace', or the character typed.
    Works on macOS/Linux (termios) and Windows (msvcrt).
    """
    if sys.platform == "win32":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            if ch2 == "H":
                return "up"
            elif ch2 == "P":
                return "down"
            return ""
        elif ch == "\r":
            return "enter"
        elif ch == "\x08":
            return "backspace"
        return ch
    else:
        import tty
        import termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A":
                        return "up"
                    elif ch3 == "B":
                        return "down"
                return ""
            elif ch in ("\r", "\n"):
                return "enter"
            elif ch == "\x7f":
                return "backspace"
            elif ch == "\x03":
                raise KeyboardInterrupt
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def prompt_region() -> str:
    """Prompt user for DuckDuckGo region setting with interactive selector."""
    is_tty = sys.stdout.isatty()

    if not is_tty:
        # Fallback for non-interactive terminals
        print("  Region (default: us-en): ", end="", flush=True)
        user_input = input().strip()
        return user_input if user_input else "us-en"

    # Build option list: common regions + custom entry
    options = [(code, description) for code, description in COMMON_REGIONS]
    custom_idx = len(options)  # index of the "Custom" entry
    selected = 0  # default to us-en (first item)
    custom_text = ""
    in_custom_input = False
    total = len(options) + 1  # +1 for custom entry

    # Lines to move cursor up: header(1) + blank(1) + options(total) + blank(1) = total + 3
    # (hint line uses end="" so cursor stays on that row — not counted)
    line_count = total + 3

    def draw() -> None:
        """Draw the selector list, overwriting previous output."""
        # Move cursor up to overwrite previous draw (except first draw)
        if hasattr(draw, "_drawn"):
            sys.stdout.write(f"\033[{line_count}A\r")
        draw._drawn = True

        print(f"  \033[1mSearch Region\033[0m\033[K")
        print(f"\033[K")

        for i, (code, desc) in enumerate(options):
            if i == selected and not in_custom_input:
                print(f"  \033[36m❯ {code:<8} {desc}\033[0m\033[K")
            else:
                print(f"    \033[2m{code:<8} {desc}\033[0m\033[K")

        # Custom entry
        if in_custom_input:
            print(f"  \033[36m❯ Custom: {custom_text}\033[0m\033[K")
        elif selected == custom_idx:
            print(f"  \033[36m❯ Custom region code...\033[0m\033[K")
        else:
            print(f"    \033[2mCustom region code...\033[0m\033[K")

        print(f"\033[K")
        print(f"  \033[2m↑/↓ navigate  ⏎ select\033[0m\033[K", end="")
        sys.stdout.flush()

    # Hide cursor
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    try:
        draw()

        while True:
            key = _read_key()

            if in_custom_input:
                if key == "enter":
                    result = custom_text.strip() if custom_text.strip() else "us-en"
                    print()  # move past the last drawn line
                    return result
                elif key == "backspace":
                    if custom_text:
                        custom_text = custom_text[:-1]
                    else:
                        in_custom_input = False
                    draw()
                elif len(key) == 1 and key.isprintable():
                    custom_text += key
                    draw()
                continue

            if key == "up":
                selected = (selected - 1) % total
                draw()
            elif key == "down":
                selected = (selected + 1) % total
                draw()
            elif key == "enter":
                if selected == custom_idx:
                    in_custom_input = True
                    draw()
                else:
                    code, _ = options[selected]
                    print()  # move past the last drawn line
                    return code
    finally:
        # Always restore cursor visibility
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def read_existing_region() -> Optional[str]:
    """Read region from existing config.toml."""
    if not CONFIG_FILE.exists():
        return None

    try:
        with open(CONFIG_FILE) as f:
            for line in f:
                if line.strip().startswith("region"):
                    # Parse: region = "us-en"
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        value = parts[1].strip().strip('"').strip("'")
                        return value
    except Exception:
        pass
    return None


def write_config(os_name: str, region: str) -> None:
    """Write config.toml with settings."""

    # Determine paths based on platform - all platforms now use native mode
    if os_name == "darwin":
        tbb_path = str(COMPONENTS_DIR / "Tor Browser.app")
        geckodriver_path = str(COMPONENTS_DIR / "geckodriver")
    elif os_name == "linux":
        tbb_path = str(COMPONENTS_DIR / "tor-browser")
        geckodriver_path = str(COMPONENTS_DIR / "geckodriver")
    else:  # Windows
        # Use forward slashes to avoid TOML escape sequence issues with backslashes
        tbb_path = str(COMPONENTS_DIR / "TorBrowser").replace("\\", "/")
        geckodriver_path = str(COMPONENTS_DIR / "geckodriver.exe").replace("\\", "/")

    config_content = f'''# Onion Search MCP Server Configuration
# Generated by installer.py

[server]
platform = "{os_name}"
mode = "native"

[search]
region = "{region}"
safesearch = "off"
max_results_per_query = 5
max_concurrent_queries = 3
query_timeout_seconds = 60

[browser]
tbb_path = "{tbb_path}"
geckodriver_path = "{geckodriver_path}"
page_timeout = 10
overall_timeout = 60
max_concurrent_tabs = 5

[tor]
keepalive_seconds = 120
data_dir = "tor_data"
# Maximum time to wait for Tor bootstrap on session startup
startup_timeout_seconds = 240

[tui]
# Set to true to run TUI in headless mode (no visible window)
# Useful for servers or automation environments
headless = false
# Log file for headless mode (optional, defaults to stderr)
# log_file = "tor_tui.log"
'''

    with open(CONFIG_FILE, "w") as f:
        f.write(config_content)


def create_launcher_scripts(os_name: str) -> None:
    """Create platform-specific launcher scripts for the TUI."""

    if os_name == "darwin":
        # macOS: .command file (double-clickable shell script)
        launcher_path = SCRIPT_DIR / "Start Onion Session.command"
        venv_python = VENV_DIR / "bin" / "python"
        tui_script = SCRIPT_DIR / "tor_tui.py"

        script_content = f'''#!/bin/bash
# Start Onion Session - Persistent Tor Session TUI
# Double-click this file to launch the TUI

cd "{SCRIPT_DIR}"
"{venv_python}" "{tui_script}"
'''
        with open(launcher_path, "w") as f:
            f.write(script_content)
        os.chmod(launcher_path, 0o755)

        # Headless launcher for macOS
        headless_launcher_path = SCRIPT_DIR / "Start Onion Session (Headless).command"
        headless_script_content = f'''#!/bin/bash
# Start Onion Session (Headless) - Background Tor daemon
# Double-click this file to launch in headless mode

cd "{SCRIPT_DIR}"
"{venv_python}" "{tui_script}" --headless
'''
        with open(headless_launcher_path, "w") as f:
            f.write(headless_script_content)
        os.chmod(headless_launcher_path, 0o755)

    elif os_name == "linux":
        # Linux: .sh file
        launcher_path = SCRIPT_DIR / "Start Onion Session.sh"
        venv_python = VENV_DIR / "bin" / "python"
        tui_script = SCRIPT_DIR / "tor_tui.py"

        script_content = f'''#!/bin/bash
# Start Onion Session - Persistent Tor Session TUI
# Run this script to launch the TUI

cd "{SCRIPT_DIR}"
"{venv_python}" "{tui_script}"
'''
        with open(launcher_path, "w") as f:
            f.write(script_content)
        os.chmod(launcher_path, 0o755)

        # Headless launcher for Linux (runs in background with nohup)
        headless_launcher_path = SCRIPT_DIR / "Start Onion Session (Headless).sh"
        headless_script_content = f'''#!/bin/bash
# Start Onion Session (Headless) - Background Tor daemon
# Run this script to launch in headless mode (background process)

cd "{SCRIPT_DIR}"
nohup "{venv_python}" "{tui_script}" --headless > /dev/null 2>&1 &
echo "Started headless Onion session (PID: $!)"
'''
        with open(headless_launcher_path, "w") as f:
            f.write(headless_script_content)
        os.chmod(headless_launcher_path, 0o755)

    else:  # Windows
        # Windows: .bat file
        launcher_path = SCRIPT_DIR / "Start Onion Session.bat"
        venv_python = VENV_DIR / "Scripts" / "python.exe"
        tui_script = SCRIPT_DIR / "tor_tui.py"

        script_content = f'''@echo off
REM Start Onion Session - Persistent Tor Session TUI
REM Double-click this file to launch the TUI

cd /d "{SCRIPT_DIR}"
"{venv_python}" "{tui_script}"
if %ERRORLEVEL% NEQ 0 pause
'''
        with open(launcher_path, "w") as f:
            f.write(script_content)

        # Headless launcher for Windows (runs in background)
        headless_launcher_path = SCRIPT_DIR / "Start Onion Session (Headless).bat"
        headless_script_content = f'''@echo off
REM Start Onion Session (Headless) - Background Tor daemon
REM Double-click this file to launch in headless mode

cd /d "{SCRIPT_DIR}"
start /B "" "{venv_python}" "{tui_script}" --headless
echo Started headless Onion session
timeout /t 2 >nul
'''
        with open(headless_launcher_path, "w") as f:
            f.write(headless_script_content)


def print_mcp_json(os_name: str) -> None:
    """Print mcp.json snippet with absolute paths in a box-drawn frame."""
    is_tty = sys.stdout.isatty()

    if os_name == "win32":
        venv_python = str(VENV_DIR / "Scripts" / "python.exe").replace("\\", "/")
        server_path = str(SCRIPT_DIR / "server.py").replace("\\", "/")
    else:
        venv_python = str(VENV_DIR / "bin" / "python")
        server_path = str(SCRIPT_DIR / "server.py")

    mcp_config: dict = {
        "mcpServers": {
            "onion-search-mcp": {
                "command": venv_python,
                "args": [server_path],
            }
        }
    }

    json_str = json.dumps(mcp_config, indent=2)
    json_lines = json_str.split("\n")

    if is_tty:
        max_len = max(len(line) for line in json_lines)

        print(f"  \033[1mMCP Configuration\033[0m")
        print(f"  \033[2mAdd this to your mcp.json:\033[0m")
        print()
        print(f"  \033[2m{'─' * (max_len + 2)}\033[0m")
        for line in json_lines:
            print(f"  {line}")
        print(f"  \033[2m{'─' * (max_len + 2)}\033[0m")
    else:
        print("MCP Configuration")
        print("Add this to your mcp.json in LM Studio, Ollama, or other LLM client:")
        print()
        print(json_str)


# ===========================================================================
# Main Entry Point
# ===========================================================================


def main() -> int:
    """Main installer entry point."""
    is_tty = sys.stdout.isatty()

    # Header
    if is_tty:
        print()
        print("  \U0001f9c5  \033[1mOnion Search MCP Installer\033[0m")
        print(f"  \033[2m{'─' * 36}\033[0m")
    else:
        print("Onion Search MCP Installer")
        print("-" * 36)

    # Check Python version
    if sys.version_info < (3, 11):
        print(f"\n  Error: Python 3.11+ is required. You have Python {sys.version_info.major}.{sys.version_info.minor}")
        return 1

    display = ProgressDisplay()

    try:
        # Detect platform
        os_name, arch = detect_platform()
        if is_tty:
            print(f"  \033[2mPlatform: {os_name}/{arch}\033[0m")
        else:
            print(f"  Platform: {os_name}/{arch}")
        print()

        # Map to requirements file name
        platform_names = {
            "darwin": "macos",
            "linux": "linux",
            "win32": "windows",
        }
        platform_name = platform_names[os_name]

        # Register all tasks in display order
        display.add_task("venv", "Virtual environment")
        display.add_task("deps", "Installing dependencies")
        display.add_task("geckodriver", "Downloading geckodriver")
        display.add_task("tor_browser", "Downloading Tor Browser")
        display.add_task("config", "Configuration")
        display.add_task("launchers", "Launcher scripts")

        # Initial draw of all pending tasks
        with display._lock:
            display._redraw()

        # ── Phase 1: venv + version fetches in parallel ─────────────
        skip_venv = check_venv_exists()
        skip_deps = skip_venv and check_requirements_installed(platform_name)
        skip_geckodriver = check_geckodriver_exists()
        skip_tor = check_tor_browser_exists()

        if skip_venv:
            display.update("venv", ProgressDisplay.COMPLETE)
            display.log_non_tty("venv")
        if skip_deps:
            display.update("deps", ProgressDisplay.COMPLETE)
            display.log_non_tty("deps")
        if skip_geckodriver:
            display.update("geckodriver", ProgressDisplay.COMPLETE)
            display.log_non_tty("geckodriver")
        if skip_tor:
            display.update("tor_browser", ProgressDisplay.COMPLETE)
            display.log_non_tty("tor_browser")

        gd_version: str | None = None
        tb_version: str | None = None
        tb_urls: dict | None = None
        errors: list[str] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures: dict[concurrent.futures.Future, str] = {}

            # Create venv (unless already exists)
            if not skip_venv:
                def _phase1_venv() -> None:
                    t0 = time.monotonic()
                    display.update("venv", ProgressDisplay.ACTIVE)
                    create_venv()
                    display.update("venv", ProgressDisplay.COMPLETE, duration=time.monotonic() - t0)
                    display.log_non_tty("venv")

                futures[pool.submit(_phase1_venv)] = "venv"

            # Fetch geckodriver version (unless geckodriver already cached)
            if not skip_geckodriver:
                def _phase1_gd_version() -> str:
                    return fetch_latest_geckodriver_version()

                futures[pool.submit(_phase1_gd_version)] = "gd_version"

            # Fetch Tor Browser version (unless tor browser already cached)
            if not skip_tor:
                def _phase1_tb_version() -> tuple[str, dict]:
                    return fetch_latest_tor_browser_version()

                futures[pool.submit(_phase1_tb_version)] = "tb_version"

            # Wait for phase 1
            for future in concurrent.futures.as_completed(futures):
                tag = futures[future]
                try:
                    result = future.result()
                    if tag == "gd_version":
                        gd_version = result
                    elif tag == "tb_version":
                        tb_version, tb_urls = result
                except Exception as e:
                    errors.append(f"Phase 1 ({tag}): {e}")

        if errors:
            display.finish()
            for err in errors:
                print(f"Error: {err}")
            return 1

        # Update geckodriver label with version once known
        if gd_version:
            with display._lock:
                display._tasks[display._task_index["geckodriver"]]["label"] = f"geckodriver {gd_version}"
                display._redraw()

        # Update tor browser label with version once known
        if tb_version:
            with display._lock:
                display._tasks[display._task_index["tor_browser"]]["label"] = f"Tor Browser v{tb_version}"
                display._redraw()

        # ── Phase 2: downloads + pip install in parallel ────────────
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures: dict[concurrent.futures.Future, str] = {}

            # pip install (needs venv from phase 1)
            if not skip_deps:
                def _phase2_deps() -> None:
                    t0 = time.monotonic()
                    display.update("deps", ProgressDisplay.ACTIVE)
                    install_requirements(platform_name)
                    display.update("deps", ProgressDisplay.COMPLETE, duration=time.monotonic() - t0)
                    display.log_non_tty("deps")

                futures[pool.submit(_phase2_deps)] = "deps"

            # geckodriver download + extract
            if not skip_geckodriver:
                def _phase2_geckodriver() -> None:
                    t0 = time.monotonic()
                    display.update("geckodriver", ProgressDisplay.ACTIVE)
                    if os_name == "win32":
                        setup_geckodriver_windows(arch, gd_version, display, "geckodriver")
                    else:
                        setup_geckodriver(os_name, arch, gd_version, display, "geckodriver")
                    display.update("geckodriver", ProgressDisplay.COMPLETE, duration=time.monotonic() - t0)
                    display.log_non_tty("geckodriver")

                futures[pool.submit(_phase2_geckodriver)] = "geckodriver"

            # Tor Browser download + extract/install
            if not skip_tor:
                def _phase2_tor() -> None:
                    t0 = time.monotonic()
                    display.update("tor_browser", ProgressDisplay.ACTIVE)
                    tb_url = get_tor_browser_url(os_name, arch, tb_version, tb_urls)
                    if os_name == "darwin":
                        setup_tor_browser_macos(tb_version, tb_url, display, "tor_browser")
                        ensure_tor_browser_profile_macos()
                    elif os_name == "linux":
                        setup_tor_browser_linux(arch, tb_version, tb_url, display, "tor_browser")
                    else:
                        setup_tor_browser_windows(arch, tb_version, tb_url, display, "tor_browser")
                        ensure_tor_browser_profile_windows()
                    display.update("tor_browser", ProgressDisplay.COMPLETE, duration=time.monotonic() - t0)
                    display.log_non_tty("tor_browser")

                futures[pool.submit(_phase2_tor)] = "tor_browser"

            # Wait for phase 2
            for future in concurrent.futures.as_completed(futures):
                tag = futures[future]
                try:
                    future.result()
                except Exception as e:
                    display.update(tag, ProgressDisplay.ERROR, detail=str(e))
                    display.log_non_tty(tag)
                    errors.append(f"{tag}: {e}")

        if errors:
            display.finish()
            print("Installation failed:")
            for err in errors:
                print(f"  - {err}")
            return 1

        # ── Phase 3: sequential configuration ───────────────────────

        # Region prompt (blocks on stdin — must be on main thread)
        existing_region = read_existing_region()
        if existing_region:
            region = existing_region
        else:
            # Finish display before prompting so the prompt prints below
            display.finish()
            region = prompt_region()

        # Write config
        write_config(os_name, region)
        display.update("config", ProgressDisplay.COMPLETE)

        # Create launcher scripts
        create_launcher_scripts(os_name)
        display.update("launchers", ProgressDisplay.COMPLETE)

        # Finalize display (idempotent — no-op if already finished by region prompt)
        display.finish()

        # Print MCP JSON
        print()
        print_mcp_json(os_name)

        # Success
        print()
        if is_tty:
            print("  \033[32m✓\033[0m \033[1mInstallation complete\033[0m")
        else:
            print("  + Installation complete")
        print()
        return 0

    except KeyboardInterrupt:
        display.finish()
        print("\nInstallation cancelled.")
        return 1
    except subprocess.CalledProcessError as e:
        display.finish()
        print(f"\nError: Command failed: {' '.join(str(x) for x in e.cmd)}")
        if e.stderr:
            stderr_text = e.stderr if isinstance(e.stderr, str) else e.stderr.decode("utf-8", errors="replace")
            print(f"Stderr: {stderr_text}")
        return 1
    except Exception as e:
        display.finish()
        print(f"\nError: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
