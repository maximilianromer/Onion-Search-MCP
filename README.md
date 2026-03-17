# Onion Search MCP

**Absolutely anonymous knowledge retrieval** for your LLM: the world's first [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that exposes tools to search the web and fetch pages anonymously through [Tor](https://www.torproject.org/). Search results come from DuckDuckGo, and page content is retrieved through an actual Tor Browser instance, preserving the universal fingerprint that makes Tor users indistinguishable from one another.

<img width="944" height="641" alt="Onion Search MCP" src="https://github.com/user-attachments/assets/13ccada4-310d-4027-9399-701249f32c28" />

## Why this exists

Millions of people use local LLMs through apps like [LM Studio](https://lmstudio.ai/) or [Ollama](https://ollama.com/). Running models locally offers an extremely private and low-cost way to access information, explore ideas, and automate computation. 

https://github.com/user-attachments/assets/1e07d780-8f9e-474e-a7f0-7581c4d64fcb

Search tools are becoming ubiquitous in LLM chat interfaces—ChatGPT, Gemini, and others use them automatically in the background for most queries. Meanwhile, every web search integration tool recommended for local LLMs route requests through off-the-shelf installations of Google Chrome, Firefox, or Brave, which (despite their marketing) leave specific [browser fingerprints](https://coveryourtracks.eff.org/learn) that can be used to track, surveil, rate-limit, or geo-restrict you.

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

### Download the project

**For those comfortable with Git**, clone the repository:

```bash
git clone https://github.com/maximilianromer/Onion-Search-MCP.git
```

**For the less technical**: download the project by clicking the green "Code" button above and selecting "Download ZIP"; then extract the ZIP file to a location to keep it in.

> **⚠️ Warning:** To speed up initialization, Tor network information is cached in this folder. I **highly discourage** saving this MCP in a directory that backs up to cloud storage.

### Run the installer

Next, open a terminal within the `onion-search-mcp` directory and run the installer:

- `python3 installer.py` on macOS or Linux
- `python installer.py` on Windows

### Choose your DuckDuckGo region

The installer will prompt you to choose your DuckDuckGo region, which affects the language and locality of your search results. Use the arrow keys to browse available regions, or press ENTER to accept the default (`us-en`, English/United States). A full list of region codes is available [here](https://serpapi.com/duckduckgo-regions).

### Add the MCP to your client

Congratulations! You have installed the MCP server. Now you need to add it to your LLM client. After a successful installation, the `installer.py` will output a configuration snippet like this:

```json
{
  "mcpServers": {
    "onion-search-mcp": {
      "command": "/Users/maxro/Documents/Onion-Search-MCP/.venv/bin/python",
      "args": [
        "/Users/maxro/Documents/Onion-Search-MCP/server.py"
      ]
    }
  }
}
```

Copy this into your MCP client's configuration file. For example, in [LM Studio](https://lmstudio.ai/), you can add it to your `mcp.json` file by clicking on the plug icon in the chat box, opening the `Install` dropdown, and selecting `Edit mcp.json`.

## How it works

All searches are handled by a **persistent session TUI** that manages the Tor connection and browser. The first time your LLM client calls a tool, the TUI auto-launches in a terminal window, connects to Tor, and stays running for subsequent searches — no cold starts after the first call.

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

**Headless mode** is available for servers and automation — set `headless = true` in `config.toml` or pass `--headless` when launching the TUI directly.

**To end the session**: Close the terminal window or press `Ctrl+C`. Tor and browser processes are automatically cleaned up.

***Note:** The MCP uses its own dedicated Tor connection on separate ports from Tor Browser, so you can have Tor Browser open for personal browsing while the MCP runs independently.*

## Architecture

The MCP server (`server.py`) is a thin client that delegates all operations to the persistent TUI (`tor_tui.py`) over a local socket. A typical usage flow looks like this:

1. The AI model calls `get_sources` with 1-3 search queries
2. The MCP server forwards the request to the TUI, which searches DuckDuckGo through Tor (running queries in parallel) and returns titles, URLs, and page snippets
3. The model calls `fetch_pages` with the indexes of the pages it wants to read
4. The TUI opens each page in the Tor Browser, extracts and cleans the text content with [Trafilatura](https://github.com/adbar/trafilatura), and returns it to the model

All platforms run Tor Browser natively using Selenium WebDriver, a tool that controls the browser over Python. The platform-specific tbselenium libraries handle the differences in Tor Browser directory structure:

- **Linux** uses [tbselenium](https://github.com/webfp/tor-browser-selenium)
- **macOS** uses [tbselenium-macos](https://github.com/maximilianromer/tbselenium-macos) (made by me)
- **Windows** uses [tbselenium-windows](https://github.com/maximilianromer/tbselenium-windows) (made by me)

The Tor process is managed via [Stem](https://github.com/torproject/stem), Tor's Python controller library.

The `installer.py` handles per-platform adjustment automatically.

## Functionality

### Tool Details

**`get_sources(queries: list[str])`**

Accepts 1-3 search queries. Multiple queries are useful when a topic could be phrased different ways or spans multiple concepts. Results are deduplicated by URL and indexed linearly (1-15). This tool description includes the current date, helping your LLM formulate time-aware queries and evaluate result recency.

Returns Markdown-formatted results grouped by query, with titles, URLs, and truncated snippets.

**`fetch_pages(indexes: list[int])`**

Fetches up to 5 pages by their index numbers from the most recent search. Can only be called once per search—this prevents unbounded fetching and encourages the LLM to choose wisely based on snippets and source credibility.

Returns extracted and cleaned text content (via [Trafilatura](https://github.com/adbar/trafilatura)) for each page.

**`fetch_specific_page(url: str)`**

Fetches a single URL directly, without a preceding search. Primarily intended for when users share a specific link they want analyzed.

### Tor Lifecycle

The TUI manages a **persistent** Tor connection for the duration of the session:

- Tor connects when the TUI starts (either manually or auto-launched by the first tool call)
- The connection stays alive for as long as the TUI is running, so subsequent searches don't incur cold-start latency
- Tor is terminated when the TUI exits (closing the window or pressing `Ctrl+C`)
- All Tor and browser processes are cleaned up automatically on exit

## Custom configuration

`config.toml` is generated by the installer but can be edited manually:

```toml
[server]
platform = "darwin"  # 'darwin' (macOS), 'linux', or 'win32'
mode = "native"      # all platforms use native mode

[search]
region = "us-en"           # DuckDuckGo region code, a full list is available at https://serpapi.com/duckduckgo-regions
safesearch = "off"         # `off`, `moderate`, or `strict`. Disabled by default.
max_results_per_query = 5  # Maximum number of results fetched per query. Defaults to 5.
max_concurrent_queries = 3 # Maximum number of query requests sent in parallel for get_sources.
query_timeout_seconds = 60 # Hard timeout per query to prevent a stuck search from hanging forever.

[browser]
page_timeout = 10          # Maximum amount of time allotted to load a page after the URL has been entered.
overall_timeout = 60       # Total fetch operation timeout
max_concurrent_tabs = 5    # Parallel tab limit for batch fetches

[tor]
data_dir = "tor_data"      # Persistent cache of Tor network information. Speeds up initialization dramatically, and recommended for anonymity.

[tui]
headless = false           # Set to true to run the TUI in headless mode (no visible window). Useful for servers or automation.
# log_file = "tor_tui.log" # Log file for headless mode (optional, defaults to stderr)
```

## Dependencies

- [Python](https://www.python.org/) — Programming language
- [FastMCP](https://github.com/jlowin/fastmcp) — Easy framework for building MCP servers
- [DDGS](https://github.com/deedy5/duckduckgo_search) — Credentialless Python API for fetching DuckDuckGo search results
- [Tor Browser](https://www.torproject.org/download/) — The anonymity web browser this project is built around
- [Stem](https://stem.torproject.org/) — Programmatic Python controller for Tor, enabling fast connection to the Tor network before opening the browser
- [Trafilatura](https://github.com/adbar/trafilatura) — A library used to extract and clean text content from the HTML source of fetched pages
- [Geckodriver](https://github.com/mozilla/geckodriver) — Used to control Tor Browser over Selenium
- [tbselenium](https://github.com/webfp/tor-browser-selenium) — Python library for using Tor Browser over Selenium (Linux)
- [tbselenium-macos](https://github.com/maximilianromer/tbselenium-macos) — macOS port of tbselenium (made by me)
- [tbselenium-windows](https://github.com/maximilianromer/tbselenium-windows) — Windows port of tbselenium (made by me)
- [PyVirtualDisplay](https://github.com/ponty/pyvirtualdisplay) and [Xvfb](https://en.wikipedia.org/wiki/Xvfb) — Headless display library for Linux
- [Textual](https://github.com/Textualize/textual) — Modern TUI framework for the persistent session interface
- [tomllib](https://docs.python.org/3/library/tomllib.html) — TOML configuration parser (Python standard library)

## Limitations

- **Tor network suspicion**: Many popular websites block or throttle requests from Tor exit nodes due to anonymity.
- **JavaScript-heavy sites**: The browser waits for `document.readyState` but doesn't execute arbitrary wait conditions. SPAs may return incomplete content.
- **Tor latency**: Tor bootstrap takes 3-15 seconds when the session starts, but this only happens once. Subsequent searches and page fetches go through the already-established connection. Individual page fetches typically take 2-5 seconds due to onion routing.

## Acknowledgements

- [The Tor Project](https://www.torproject.org/): A nonprofit leading the path for digital anonymity
- [Claude Code](https://claude.com/product/claude-code) and [Claude Opus 4.5](https://www.anthropic.com/claude/opus): Truly magical AI coding agents
- [Google Gemini 3.0 Pro](https://gemini.google.com/): An excellent model to upload your codebase to and have it use web search to find information on how to move forward
- [LM Studio](https://lmstudio.ai/): A great LLM client for macOS
- [Alibaba Cloud's Qwen](https://huggingface.co/Qwen): My favorite AI models to use this MCP with
- [Apple's MLX](https://github.com/ml-explore/mlx): A framework that makes Machine Learning work great on Apple Silicon

 
