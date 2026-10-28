# Scene for the extension gallery screenshot; see the rozi-extension skill's screenshots guide.
# A reading list beside the site it links to, opened through `terminal-browser.open`.
cat > "$DEMO/links.md" <<'MD'
# Reading list

- rozi docs           https://rozi.tui-lipan.dev
- terminal-browser    https://github.com/zenbu-labs/terminal-browser
- tui-lipan           https://tui-lipan.dev
MD
teardown() { terminal-browser shutdown >/dev/null 2>&1; }

keys "clear; cat links.md" Enter
settle 500
# With no link selected, the command lists browsers to pick from; `Ctrl+O` asks for a URL instead.
rozi run-action terminal-browser.open >/dev/null 2>&1 &
settle 1500
keys C-o
settle 800
keys "https://rozi.tui-lipan.dev" Enter
# The page loads over the network.
settle 15000
