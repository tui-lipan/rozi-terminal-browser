# Terminal browser for rozi

Open clicked links and uppercase hint selections in [terminal-browser](https://github.com/zenbu-labs/terminal-browser).
The extension creates a browser pane when none exists, reuses a browser in the same rozi session,
and asks which browser to use when several are open. Installing the extension supplies the link
handler; no config-file edit is needed.

## Requirements

- Linux or macOS, Python 3.11 or newer available as `python3`.
- terminal-browser installed and available on `PATH`.
- A rozi build whose `rozi api describe` includes `extension-link-handlers`.
  This requires the extension link-handler feature, not just extension API 1.
- An attached local session and a terminal capable of displaying rozi's terminal images.

Remote sessions and native Windows are not supported by this extension. It never installs or
upgrades terminal-browser, opens remote files as local files, or starts a browser outside rozi.

## Install

Review the source and manifest, then install:

```sh
rozi extensions install https://github.com/tui-lipan/rozi-terminal-browser.git
rozi run-action reload-extensions
```

Hold `Ctrl` and click an HTTP or HTTPS link. In hint mode (`Ctrl+A`, then `u` by default), finish the
label with an uppercase character to open it; lowercase labels still copy.

User-configured link handlers take precedence over this extension. Disabling the extension in
**Extensions…** removes its handlers and commands and restores the remaining opening policy.

## Commands

| Command | Behavior |
| --- | --- |
| `terminal-browser.open` | Open the selected link, reusing a browser when possible. Without a selected link, opens a URL prompt. |
| `terminal-browser.new-pane` | Open a URL in a new browser pane. |
| `terminal-browser.browsers` | Choose and focus a browser in this session. |

Find these commands in the command palette or run `rozi run-action terminal-browser.open`.
The `open` command suggests `Ctrl+A`, then `x`, then `b`; existing bindings win.
In the URL picker, press `Ctrl+O` to enter a URL. `Esc` cancels without creating a pane.

The browser has its own tabs and shortcuts. Closing a browser closes its wrapper pane. A failed
reuse reports an error rather than creating a duplicate browser. Browser discovery excludes other
rozi sessions and panes that have already closed. If the client switches sessions while a command
is waiting for a choice, the command refuses to act on the newly displayed session.

## Optional configuration

Defaults work without configuration. To change them:

```toml
[extensions.terminal-browser]
browser = "terminal-browser" # PATH name or an absolute executable path
reuse = true
focus = true
```

To explicitly use the new-pane command for links:

```toml
[[links.handlers]]
schemes = ["http", "https"]
command = "terminal-browser.new-pane"
```

Local HTML file URLs can be opted in separately; the extension does not claim all file links:

```toml
[[links.handlers]]
schemes = ["file"]
command = "terminal-browser.open"
```

## How it works

Commands use rozi's public CLI, structured argv, and its generation-fenced extension environment.
Each browser pane runs a small Python wrapper with a private, temporary Unix socket implementing
[terminal-browser's terminal socket protocol v1](https://github.com/zenbu-labs/terminal-browser/blob/main/shared/src/terminal-socket.ts).
The wrapper reports the browser's pane and session identity, so terminal-browser can discover and
reuse it without a built-in rozi adapter. The socket closes when the browser exits; there is no
extension daemon or registry file. URLs never become shell command text.

The extension launches terminal-browser with `TERMINAL_BROWSER_PRESENT=full` as a temporary
workaround for upstream partial-update transport issues in Rozi. This overrides any inherited
presentation setting for new browser panes. Close and reopen existing browser panes to apply it;
opening another tab in an existing browser does not change that browser's environment.

The bridge supports pane identity, listing, and focus. It does not advertise terminal-browser's
automatic splitting or text-sending operations. Use this extension's **Open in new browser pane…**
command to create browser panes. It reads no rozi state files and opens no rozi IPC sockets directly.

## Development and verification

```sh
python3 -m unittest discover -s tests -v
rozi extensions check .
```

For an isolated live review, follow rozi's
[extension testing guide](https://github.com/tui-lipan/rozi/blob/master/docs/extension-testing.md),
then link this checkout with `rozi extensions install --link /path/to/rozi-terminal-browser` inside
the lab. Keep terminal-browser's own state isolated too: give the lab a temporary `HOME`,
`TERMINAL_BROWSER_CONFIG_DIR`, and `TERMINAL_BROWSER_INTEROP_DIR`.

Verify:

1. Ctrl-click and an uppercase hint each open the URL in a browser pane.
2. A second link creates a browser tab while the rozi pane count stays unchanged.
3. After opening a second browser pane, a link offers a browser chooser; cancelling changes nothing.
4. Another rozi session's browsers are not offered.
5. Missing terminal-browser produces one actionable error.
6. Disable/re-enable removes/restores handlers; a broken manifest contributes nothing.
7. The extension works from a checkout and source directory containing spaces and Unicode.

Contributions use MPL-2.0 and require a DCO sign-off (`git commit -s`).
