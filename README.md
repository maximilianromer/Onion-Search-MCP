# Tor Browser MCP

**Absolutely anonymous knowledge retrieval** for your LLM: the world's first [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that exposes tools to search the web, fetch pages, and even **drive a live Tor Browser** — all anonymously through [Tor](https://www.torproject.org/).

It ships with **two modes**:

- **Browser mode** (default) — a visible Tor Browser you can automate like a full Selenium bot: navigate, click, type, run JavaScript, take screenshots, manage tabs.
- **TUI mode** — search DuckDuckGo and fetch clean page text through Tor, with a persistent terminal interface.

Search results come from DuckDuckGo, and page content is retrieved through an actual Tor Browser instance, preserving the universal fingerprint that makes Tor users indistinguishable from one another.

## Two modes at a glance

| | **TUI mode** | **Browser mode** (default) |
|---|---|---|
| MCP tools | 3: `get_sources`, `fetch_pages`, `fetch_specific_page` | the same 3 **plus** 22 `browser_*` automation tools (25 total) |
| Browser | headless — used only to extract clean text | **headful / visible** Tor Browser you can drive like Selenium |
| Typical use | quick research: search → read results | interact with live pages: click, fill forms, run JS, scrape dynamic sites |
| How to enable | `[server] mode = "tui"` or `--mode tui`, then restart the MCP | nothing to do — this is the default |


## Why this exists

Millions of people use local LLMs through apps like [LM Studio](https://lmstudio.ai/) or [Ollama](https://ollama.com/). Running models locally offers an extremely private and low-cost way to access information, explore ideas, and automate computation.

**Browser mode:**

https://github.com/user-attachments/assets/dfd9dc9b-9c4d-4b37-a7e4-34f21319d4b8

**TUI mode** (video from the original version):

https://github.com/user-attachments/assets/1e07d780-8f9e-474e-a7f0-7581c4d64fcb

Search tools are becoming ubiquitous in LLM chat interfaces—ChatGPT, Gemini, and others use them automatically in the background for most queries. Meanwhile, every web search integration tool recommended for local LLMs routes requests through off-the-shelf installations of Google Chrome, Firefox, or Brave, which (despite their marketing) leave specific [browser fingerprints](https://coveryourtracks.eff.org/learn) that can be used to track, surveil, rate-limit, or geo-restrict you.

This tool takes a different approach: every request flows through the [Tor](https://www.torproject.org/) network and browser, which routes your requests through an anonymity network and makes your traffic indistinguishable from millions of other users.

## Installation

> ⚡ Install quickly by giving [this prompt](https://gist.github.com/maximilianromer/420fb8a651b39e68364b3abb1ba876a4) to Claude Code, Codex, or any other agent of choice.

### Prerequisites

**Download** Python 3.11+. Check if you have it installed by running in your terminal:

- `python3 --version` on macOS or Linux
- `python --version` on Windows

If you don't, download and install the latest version from [python.org](https://www.python.org/downloads/).

On Linux, link-copy mode in the TUI (`C` key) requires `xclip`:

- Debian/Ubuntu: `sudo apt install xclip`
- Fedora: `sudo dnf install xclip`
- Arch: `sudo pacman -S xclip`

On Linux, **Browser mode** runs the Tor Browser on a virtual X display when no display is available; that needs `xvfb` (or `xephyr`):

- Debian/Ubuntu: `sudo apt install xvfb`

### Download the project

**For those comfortable with Git**, clone the repository:

```bash
git clone https://github.com/muhammedikbalyildiz/Tor-Browser-MCP.git
```

**For the less technical**: download the project by clicking the green "Code" button above and selecting "Download ZIP"; then extract the ZIP file to a location to keep it in.

> **⚠️ Warning:** To speed up initialization, Tor network information is cached in this folder. I **highly discourage** saving this MCP in a directory that backs up to cloud storage.

### Run the installer

Next, open a terminal within the `tor-browser-mcp` directory and run the installer:

- `python3 installer.py` on macOS or Linux
- `python installer.py` on Windows

### Choose your DuckDuckGo region

The installer will prompt you to choose your DuckDuckGo region, which affects the language and locality of your search results. Use the arrow keys to browse available regions, or press ENTER to accept the default (`us-en`, English/United States). A full list of region codes is available [here](https://serpapi.com/duckduckgo-regions).

### Add the MCP to your client

Congratulations! You have installed the MCP server. Now you need to add it to your LLM client. After a successful installation, the `installer.py` will output a configuration snippet like this:

```json
{
  "mcpServers": {
    "tor-browser-mcp": {
      "command": "/Users/maxro/Documents/tor-browser-mcp/.venv/bin/python",
      "args": [
        "/Users/maxro/Documents/tor-browser-mcp/server.py"
      ]
    }
  }
}
```

Copy this into your MCP client's configuration file. For example, in [LM Studio](https://lmstudio.ai/), you can add it to your `mcp.json` file by clicking on the plug icon in the chat box, opening the `Install` dropdown, and selecting `Edit mcp.json`.

## How it works

A **persistent session daemon** manages the Tor connection and the browser. The first time your LLM client calls a tool, the daemon auto-launches, connects to Tor, and stays running for subsequent calls — no cold starts after the first one.

- In **TUI mode** the daemon also shows a terminal interface (see below).
- In **Browser mode** the daemon runs without a UI and drives a **visible Tor Browser** (headful), answering the Selenium-style `browser_*` tools over the same session socket.

### The TUI window (TUI mode)

The TUI window shows:

- **Your Tor circuit** — the three relays your traffic passes through, with country flags and IP addresses
- **Your exit IP** — so you can see what region of the internet you appear to be browsing from
- **Live search activity** — animated operation cards for every search and page fetch, with spinners and elapsed timers
- **Search results** — titles, domains, and snippets rendered in the terminal as they come in
- **Session uptime** — always visible in the status bar

Press **C** to enter link selection mode, where you can navigate results with arrow keys and copy any URL to your clipboard. Press **Enter** to copy a result or **Esc** to leave link selection mode.

You can also **start the TUI ahead of time** so it's ready before your first search:

1. **macOS**: Double-click `Start Onion Session.command` in the project folder
2. **Linux**: Run `./Start Onion Session.sh` in a terminal
3. **Windows**: Double-click `Start Onion Session.bat`

**Headless mode** is available for servers and automation — set `headless = true` in `config.toml` or pass `--headless` when launching the daemon directly. Browser mode always runs this way (no UI), because everything is driven over the socket.

**To end the session**: Close the terminal window or press `Ctrl+C`. Tor and browser processes are automatically cleaned up.

***Note:** The MCP uses its own dedicated Tor connection on separate ports from Tor Browser, so you can have Tor Browser open for personal browsing while the MCP runs independently.*

## Browser mode

Browser mode turns the MCP into a **full Selenium bot on Tor**. Opt in with `[server] mode = "browser"` in `config.toml`, or launch with `--mode browser`. The mode is read at server startup, so **restart the MCP after changing it** — the tool list then grows from 3 to **25 tools**.

Valid modes are `"tui"` and `"browser"` (the legacy `"native"` is accepted as an alias for `"tui"`); anything else is rejected at startup with a clear error.

## Architecture

The MCP server (`server.py`) is a thin client that delegates **all** operations to the persistent daemon (`tor_tui.py`) over a local socket. `server.py` has no Tor or browser logic of its own — the daemon owns Tor, the DuckDuckGo client, and the Tor Browser. A typical usage flow looks like this:

1. The AI model calls `get_sources` with 1-3 search queries
2. The MCP server forwards the request to the daemon, which searches DuckDuckGo through Tor (running queries in parallel) and returns titles, URLs, and page snippets
3. The model calls `fetch_pages` with the indexes of the pages it wants to read
4. The daemon opens each page in the Tor Browser, extracts and cleans the text content with [Trafilatura](https://github.com/adbar/trafilatura), and returns it to the model
5. In **browser mode**, the daemon also answers the `browser_*` commands (navigate/click/JS/screenshot/…) by driving the same Tor Browser as a Selenium session

All platforms run Tor Browser natively using Selenium WebDriver, a tool that controls the browser over Python. The platform-specific tbselenium libraries handle the differences in Tor Browser directory structure:

- **Linux** uses [tbselenium](https://github.com/webfp/tor-browser-selenium)
- **macOS** uses [tbselenium-macos](https://github.com/maximilianromer/tbselenium-macos) (made by the original author)
- **Windows** uses [tbselenium-windows](https://github.com/maximilianromer/tbselenium-windows) (made by the original author)

The Tor process is managed via [Stem](https://github.com/torproject/stem), Tor's Python controller library.

The `installer.py` handles per-platform adjustment automatically.

## Functionality

### Tools

<details>
<summary><b>Core tools (both modes)</b></summary>

- **get_sources**
  - Title: Search the web
  - Description: Accepts 1-3 DuckDuckGo search queries and returns Markdown results grouped by query, with titles, URLs, and truncated snippets. Results are deduplicated by URL and indexed linearly (1-15). The tool description includes the current date, helping your LLM formulate time-aware queries and evaluate result recency. Always start research with this tool.
  - Parameters:
    - `queries` (array): 1-3 search queries. Multiple queries help when a topic can be phrased different ways.
  - Read-only: **true**

- **fetch_pages**
  - Title: Fetch search results
  - Description: Fetch the full page content for search results by their index numbers. This tool MUST be called after get_sources. You can only call this once per search. Pass indexes like [1, 3, 11] for the results you want to fetch. IMPORTANT: With multi-query searches, up to 15 results may be available (5 per query, minus duplicates), but you can only fetch a maximum of 5 pages per fetch_pages call. Choose the most relevant indexes based on the abbreviated snippets and source credibility shown in get_sources results. Review all query sections before selecting - relevant results may appear under any query. NEVER make a fetch_pages request with more than five total indexes. Returns extracted and cleaned text content (via [Trafilatura](https://github.com/adbar/trafilatura)) for each page. ERROR conditions: - If called without a preceding get_sources call - If more than 5 indexes are requested
  - Parameters:
    - `indexes` (array): Result indexes from the last get_sources call. At most 5 per call; callable once per search.
  - Read-only: **true**

- **fetch_specific_page**
  - Title: Fetch a specific page
  - Description: Fetch a specific page by URL. Use this pretty much only when the user provides a direct URL in their prompt. For general web research, ALWAYS use the get_sources -> fetch_pages workflow instead. NEVER use this tool to fetch links from get_sources. This tool is meant for cases where a URL is provided to you by the user or another tool.
  - Parameters:
    - `url` (string): The URL to fetch directly, without a preceding search.
  - Read-only: **true**

</details>

<details>
<summary><b>Core automation (browser mode)</b></summary>

- **browser_navigate**
  - Title: Navigate to a URL
  - Description: Navigate the Tor Browser to a URL (adds https:// when no scheme is given). With wait=True (default) polls until document.readyState is "complete" or wait_timeout seconds elapse. Navigation invalidates all element refs - re-find them with browser_find; check browser_status/browser_wait_ready afterwards. All traffic goes through Tor.
  - Parameters:
    - `url` (string): The URL to navigate to. `https://` is prepended when no scheme is given.
    - `wait` (boolean, optional): Wait for `document.readyState` to be "complete" before responding. Defaults to true.
    - `wait_timeout` (number, optional): Maximum seconds to wait for readiness. Defaults to 30.
  - Read-only: **false**

- **browser_back**
  - Title: Go back
  - Description: Go back in history and return the resulting {url, title}, optionally waiting for readiness (wait=True). Going back reloads a document, which invalidates every element ref - re-find them with browser_find and check browser_status/browser_wait_ready. All traffic goes through Tor.
  - Parameters:
    - `wait` (boolean, optional): Wait for readiness after navigating. Defaults to true.
  - Read-only: **false**

- **browser_forward**
  - Title: Go forward
  - Description: Go forward in history and return the resulting {url, title}, optionally waiting for readiness (wait=True). The new document invalidates all element refs - re-find them with browser_find. All traffic goes through Tor.
  - Parameters:
    - `wait` (boolean, optional): Wait for readiness after navigating. Defaults to true.
  - Read-only: **false**

- **browser_refresh**
  - Title: Reload page
  - Description: Reload the current page and return the resulting {url, title}, optionally waiting for readiness (wait=True). Reloading invalidates every element ref - re-find them with browser_find. All traffic goes through Tor.
  - Parameters:
    - `wait` (boolean, optional): Wait for readiness after reloading. Defaults to true.
  - Read-only: **false**

- **browser_click**
  - Title: Click
  - Description: Click the element identified by ref (refs come from browser_find, look like "e7", and die on navigation). Returns the ref and the resulting URL. Use wait=True to briefly wait for page readiness after the click. The browser runs through Tor.
  - Parameters:
    - `ref` (string): Exact element reference from `browser_find` (e.g. "e7"). Refs are invalidated by navigation.
    - `wait` (boolean, optional): Briefly wait for page readiness after the click. Defaults to false.
  - Read-only: **false**

- **browser_fill**
  - Title: Fill text input
  - Description: Type into the element identified by ref (from browser_find; refs die on navigation). Clears the field first unless clear=false, then inserts text; set submit=true to press ENTER afterwards. Returns the ref, whether it submitted, and the current URL. The browser runs through Tor.
  - Parameters:
    - `ref` (string): Exact element reference from `browser_find`.
    - `text` (string): Text to type into the element.
    - `clear` (boolean, optional): Clear the field before typing. Defaults to true.
    - `submit` (boolean, optional): Press ENTER after typing. Defaults to false.
  - Read-only: **false**

- **browser_keys**
  - Title: Press a key
  - Description: Send a key combination to the page, e.g. "ENTER", "CTRL+A", "PAGEDOWN" (tokens joined with "+"; single characters are literal, aliases like CTRL map to selenium keys). Optionally targets an element ref from browser_find (refs die on navigation), otherwise the active element. Returns the keys sent and the current URL. The browser runs through Tor.
  - Parameters:
    - `keys` (string): Key combination such as "ENTER", "CTRL+A" or "PAGEDOWN"; tokens joined with "+".
    - `ref` (string, optional): Element reference to send the keys to, defaults to the active element.
  - Read-only: **false**

- **browser_find**
  - Title: Find elements
  - Description: Find elements in the current page and return a JSON list of {ref, tag, text, id, name, href, visible} (text truncated to 80 chars, at most limit entries). by is one of css, xpath, id, link_text, partial_link_text. The returned refs are what all other browser_* tools take, and they are invalidated by navigation - re-find after any page change. The browser runs through Tor.
  - Parameters:
    - `selector` (string): Selector or expression to search the current page for.
    - `by` (string, optional): One of "css" (default), "xpath", "id", "link_text", "partial_link_text".
    - `limit` (integer, optional): Maximum number of matches, clamped to 1-500. Defaults to 10.
    - `include_hidden` (boolean, optional): Include invisible elements. Defaults to false.
  - Read-only: **true**

- **browser_execute_js**
  - Title: Evaluate JavaScript
  - Description: Run JavaScript in the current page and return {result, truncated}; JSON-serializable args are passed as script arguments, returned WebElements become element refs (from browser_find's scheme; refs die on navigation). String results are capped by the daemon's max_output_chars with a truncation marker. All traffic goes through Tor.
  - Parameters:
    - `script` (string): JavaScript to evaluate in the current page.
    - `args` (array, optional): JSON-serializable arguments passed to the script. Defaults to [].
  - Read-only: **false**

- **browser_screenshot**
  - Title: Take a screenshot
  - Description: Capture the current viewport as a PNG and return JSON {mime, base64, bytes} (render as data:image/png;base64,...; viewport-only, a geckodriver limitation). Use it to observe the page after navigating or clicking - element refs come from browser_find and die on navigation. The browser runs through Tor.
  - Parameters: None
  - Read-only: **true**

- **browser_scroll**
  - Title: Scroll page
  - Description: Scroll the page (direction: down, up, top or bottom; amount_px applies to down/up) and return the resulting {scroll_x, scroll_y}. With a ref (from browser_find, dies on navigation) the element is scrolled into view instead. The browser runs through Tor.
  - Parameters:
    - `direction` (string, optional): One of "down", "up", "top", "bottom". Defaults to "down".
    - `amount_px` (integer, optional): Pixels to scroll for down/up. Defaults to 800.
    - `ref` (string, optional): Element reference to scroll into view instead of scrolling the page.
  - Read-only: **false**

- **browser_wait_ready**
  - Title: Wait for
  - Description: Wait until document.readyState is "complete" (up to timeout seconds) and, when a CSS selector is given, until an element matching it appears; returns {ready_state, url, waited_s}. Call it after browser_navigate (or any navigation) before using element refs from browser_find, which die on navigation. All traffic goes through Tor.
  - Parameters:
    - `timeout` (number, optional): Maximum seconds to wait. Defaults to 30.
    - `selector` (string, optional): CSS selector to also wait for an element matching it to appear.
  - Read-only: **true**

</details>

<details>
<summary><b>Reading content (browser mode)</b></summary>

- **browser_get_text**
  - Title: Get element text
  - Description: Return the visible text of the element identified by ref (from browser_find; refs die on navigation), or of the whole page (document.body.innerText) when ref is empty. Long output is truncated to max_chars (0 = the daemon's max_output_chars) with a truncation marker. Content arrives over Tor.
  - Parameters:
    - `ref` (string, optional): Element reference; when empty returns the whole page's visible text.
    - `max_chars` (integer, optional): Truncation threshold; 0 uses the daemon's `max_output_chars`. Defaults to 0.
  - Read-only: **true**

- **browser_get_html**
  - Title: Get element HTML
  - Description: Return the HTML of the element identified by ref (outerHTML; refs come from browser_find and die on navigation), or the full page source when ref is empty. Output is truncated to max_chars (0 = the daemon's max_output_chars) with a truncation marker. Content arrives over Tor.
  - Parameters:
    - `ref` (string, optional): Element reference (returns `outerHTML`); when empty returns the full page source.
    - `max_chars` (integer, optional): Truncation threshold; 0 uses the daemon's `max_output_chars`. Defaults to 0.
  - Read-only: **true**

- **browser_get_url**
  - Title: Get current URL
  - Description: Return the current page URL as plain text (no JSON wrapper). After browser_navigate, wait with browser_status or browser_wait_ready before trusting it. The page is loaded over Tor.
  - Parameters: None
  - Read-only: **true**

- **browser_get_title**
  - Title: Get page title
  - Description: Return the current page title as plain text (no JSON wrapper). After navigation, confirm readiness with browser_status or browser_wait_ready first. The page is loaded over Tor.
  - Parameters: None
  - Read-only: **true**

- **browser_get_attribute**
  - Title: Get element attribute
  - Description: Return one attribute of the element identified by ref (from browser_find; refs die on navigation) as plain text. The pseudo-attributes "text" and "html" return the element's visible text or outerHTML instead. Content arrives over Tor.
  - Parameters:
    - `ref` (string): Exact element reference from `browser_find`.
    - `attribute` (string): Attribute name; the pseudo-attributes "text" and "html" return the element's visible text or `outerHTML`.
  - Read-only: **true**

</details>

<details>
<summary><b>Tab management (browser mode)</b></summary>

- **browser_new_tab**
  - Title: New tab
  - Description: Open a new tab (optionally loading url, with https:// added when no scheme is given) and return {handle, url, window_count}. Switch to it with browser_switch_tab; refs from browser_find are per-page and must be re-found after any navigation. All traffic goes through Tor.
  - Parameters:
    - `url` (string, optional): URL to load in the new tab; `about:blank` when omitted.
  - Read-only: **false**

- **browser_switch_tab**
  - Title: Select tab
  - Description: Switch to another tab by handle (from browser_status or browser_new_tab), or by zero-based index into the window list when handle is empty. Returns {handle, url, title, window_count}. Element refs are page-specific: after switching, re-find them with browser_find on the newly active tab. The browser runs through Tor.
  - Parameters:
    - `handle` (string, optional): Tab handle from `browser_status` or `browser_new_tab`.
    - `index` (integer, optional): Zero-based index into the window list, used when `handle` is empty. Defaults to -1.
  - Read-only: **false**

- **browser_close_tab**
  - Title: Close tab
  - Description: Close a tab by handle (default: the active tab) and return {closed, remaining}. Refuses to close the last remaining tab - use browser_close for that. Switching or closing tabs does not preserve element refs across pages: re-find them with browser_find. The browser runs through Tor.
  - Parameters:
    - `handle` (string, optional): Handle of the tab to close, defaults to the active tab.
  - Read-only: **false**

</details>

<details>
<summary><b>Session management (browser mode)</b></summary>

- **browser_status**
  - Title: Session status
  - Description: Report the state of the Tor Browser session: mode, Tor liveness, browser readiness, headful/headless, current URL and title, every window/tab (handle, url, title), virtual display info and uptime. Never launches a browser by itself. After navigating, use this or browser_wait_ready to confirm the page loaded. All traffic goes through Tor.
  - Parameters: None
  - Read-only: **true**

- **browser_close**
  - Title: Close browser
  - Description: Shut the Tor Browser down (driver.quit plus the virtual display if the daemon started one) and return {closed: true}. Tor stays up; the next browser_* call re-warms a fresh browser. All element refs are discarded by this. All traffic goes through Tor.
  - Parameters: None
  - Read-only: **false**

</details>

**Notes:** every tool call is serialized one at a time on the daemon lock, and all of them go through Tor. Long text/JS results are capped by `max_output_chars` (default 100000) with a `…[truncated]` marker. The first `browser_*` call (other than `browser_status`) lazily boots the Tor Browser; element refs are minted by `browser_find` and invalidated by any navigation.

### Tor Lifecycle

The daemon manages a **persistent** Tor connection for the duration of the session:

- Tor connects when the daemon starts (either manually or auto-launched by the first tool call)
- The connection stays alive for as long as the daemon is running, so subsequent searches don't incur cold-start latency
- Tor is terminated when the daemon exits (closing the window or pressing `Ctrl+C`)
- All Tor and browser processes are cleaned up automatically on exit

## Custom configuration

`config.toml` is generated by the installer but can be edited manually:

```toml
[server]
platform = "darwin"  # 'darwin' (macOS), 'linux', or 'win32'
mode = "browser"     # 'browser' (default) or 'tui'; 'native' is accepted as a legacy alias for 'tui'.

[search]
region = "us-en"           # DuckDuckGo region code, a full list is available at https://serpapi.com/duckduckgo-regions
safesearch = "off"         # `off`, `moderate`, or `strict`. Disabled by default.
max_results_per_query = 5  # Maximum number of results fetched per query. Defaults to 5.
max_concurrent_queries = 3 # Maximum number of query requests sent in parallel for get_sources.
query_timeout_seconds = 60 # Hard timeout per query to prevent a stuck search from hanging forever.

[browser]
page_timeout = 10          # Maximum amount of time allotted to load a page after the URL has been entered.
overall_timeout = 60       # Total fetch operation timeout (fetch_pages / fetch_specific_page).
max_concurrent_tabs = 5    # Parallel tab limit for batch fetches.
wait_timeout_seconds = 30  # Browser mode: default wait budget for browser_navigate(wait=True) / browser_wait_ready.
max_output_chars = 100000  # Browser mode: cap for get_text/get_html/execute_js results (truncation marker appended).

[tor]
data_dir = "tor_data"      # Persistent cache of Tor network information. Speeds up initialization dramatically, and recommended for anonymity.

[tui]
headless = false           # Set to true to run the daemon in headless mode (no visible window). Useful for servers or automation.
# log_file = "tor_tui.log" # Log file for headless mode (optional, defaults to stderr)
```

To switch to **Browser mode**, set `mode = "browser"` and restart the MCP — the tool list changes from 3 to 25. You can also override per-launch with `--mode browser` on `server.py` or `tor_tui.py`.

## Dependencies

- [Python](https://www.python.org/) — Programming language
- [FastMCP](https://github.com/jlowin/fastmcp) — Easy framework for building MCP servers
- [DDGS](https://github.com/deedy5/duckduckgo_search) — Credentialless Python API for fetching DuckDuckGo search results
- [Tor Browser](https://www.torproject.org/download/) — The anonymity web browser this project is built around
- [Stem](https://stem.torproject.org/) — Programmatic Python controller for Tor, enabling fast connection to the Tor network before opening the browser
- [Trafilatura](https://github.com/adbar/trafilatura) — A library used to extract and clean text content from the HTML source of fetched pages
- [Geckodriver](https://github.com/mozilla/geckodriver) — Used to control Tor Browser over Selenium
- [tbselenium](https://github.com/webfp/tor-browser-selenium) — Python library for using Tor Browser over Selenium (Linux)
- [tbselenium-macos](https://github.com/maximilianromer/tbselenium-macos) — macOS port of tbselenium (made by the original author)
- [tbselenium-windows](https://github.com/maximilianromer/tbselenium-windows) — Windows port of tbselenium (made by the original author)
- [PyVirtualDisplay](https://github.com/ponty/pyvirtualdisplay) and [Xvfb](https://en.wikipedia.org/wiki/Xvfb) — Headless/virtual display library for Linux (needed to show a headful browser when there's no real display)
- [Textual](https://github.com/Textualize/textual) — Modern TUI framework for the persistent session interface
- [tomllib](https://docs.python.org/3/library/tomllib.html) — TOML configuration parser (Python standard library)

## Limitations

- **Tor network suspicion**: Many popular websites block or throttle requests from Tor exit nodes due to anonymity.
- **JavaScript-heavy sites**: The core `fetch_pages` flow waits for `document.readyState` but doesn't execute arbitrary wait conditions; SPAs may return incomplete content. In **browser mode** you can work around this with `browser_wait_ready`, `browser_execute_js`, and the interactive tools.
- **Browser-mode screenshots are viewport-only** (a geckodriver limitation) — scroll first to bring content into view.
- **Element refs die on navigation** — re-run `browser_find` after any page change.
- **Empty feeds on a fresh identity**: sites like YouTube start with an empty homepage for a brand‑new Tor identity — search first, or let a watch populate the feed.
- **Tor latency**: Tor bootstrap takes 3-15 seconds when the session starts, but this only happens once. Subsequent searches and page fetches go through the already-established connection. Individual page fetches typically take 2-5 seconds due to onion routing; live browser actions each carry a little Tor-level latency too.
- **First browser call is slow**: it warms the headful browser (1–3 minutes). Pre-start the daemon to avoid the wait (see [Browser mode](#browser-mode)).

## Known Issues
- **In Browser mode, if a browser closed, opening another browser requires re-establish the browser session, which an LLM can understand and do it so the MCP is still useful, but costs some token (too less though) and may annoy user. This issue is planning to be fixed as soon as possible.** 