#!/usr/bin/env python3
"""Rozi extension commands and terminal-browser's public terminal socket v1 bridge."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlparse


class ExtensionError(Exception):
    pass


def run_json(argv, *, env=None, input_text=None, timeout=15):
    result = subprocess.run(
        argv, input=input_text, text=True, capture_output=True, env=env, timeout=timeout,
    )
    if result.returncode:
        raise ExtensionError(result.stderr.strip() or f"{Path(argv[0]).name} failed")
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise ExtensionError(f"{Path(argv[0]).name} returned invalid JSON") from error
    if isinstance(value, dict) and value.get("ok") is False:
        raise ExtensionError(str(value.get("error", "Rozi request failed")))
    return value


class Rozi:
    def __init__(self, env=None):
        self.env = dict(os.environ if env is None else env)
        self.binary = self.env.get("ROZI_BIN") or shutil.which("rozi")
        if not self.binary or not self.env.get("ROZI_SOCKET"):
            raise ExtensionError("Run this command in an attached Rozi client")
        if self.env.get("ROZI_REMOTE_HOST"):
            raise ExtensionError("Terminal browser currently supports local Rozi sessions")
        api = self.call("api", "describe")
        if "extension-link-handlers" not in api.get("capabilities", []):
            raise ExtensionError("Update Rozi to a version supporting extension link handlers")
        layout = self.call("layout", "get", "--format", "json")
        self.tab = self.session_tab(layout)
        expected = self.env.get("ROZI_SOURCE_SESSION") or self.env.get("ROZI_SESSION_INSTANCE")
        if expected and self.tab != expected:
            raise ExtensionError("The source session is no longer on screen")
        raw_pane = self.env.get("ROZI_SOURCE_PANE") or self.env.get("ROZI_PANE")
        self.pane = str(raw_pane or layout.get("client", {}).get("focused_pane") or "")
        self.validate_pane(self.pane, layout)

    def call(self, *args):
        reply = run_json([self.binary, "--socket", self.env["ROZI_SOCKET"], *args], env=self.env)
        return reply.get("data", reply)

    @staticmethod
    def session_tab(layout):
        for workspace in layout.get("workspaces", []):
            for pane in workspace.get("panes", []):
                instance = pane.get("reference", {}).get("session_instance")
                if instance:
                    return instance
        raise ExtensionError("Open a session pane before launching a browser")

    def validate_pane(self, pane_id, layout=None):
        layout = self.call("layout", "get", "--format", "json") if layout is None else layout
        if self.session_tab(layout) != self.tab:
            raise ExtensionError("The browser's session is no longer on screen")
        if not any(str(pane["id"]) == str(pane_id)
                   for workspace in layout["workspaces"] for pane in workspace["panes"]):
            raise ExtensionError("The source browser pane has closed")

    def pick(self, rows, *, title, prompt=False):
        request = {"title": title, "rows": rows, "empty": "No browsers in this session"}
        if prompt:
            request["empty"] = "Press Ctrl+O to enter a URL"
            request["actions"] = [{
                "id": "open", "key": "ctrl-o", "label": "open URL", "close": True,
                "prompt": {"title": "Open URL", "placeholder": "https://example.com"},
            }]
        # Pick is a stream, even for an action that closes its UI. Read the first decision and
        # close our producer; waiting for the CLI to exit would hang on a terminal action.
        process = subprocess.Popen(
            [self.binary, "--socket", self.env["ROZI_SOCKET"], "pick", "--json"],
            env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
            if not line:
                message = process.stderr.read().strip()
                if message:
                    raise ExtensionError(message)
                return {"cancelled": True}
            return json.loads(line)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def focus(self, pane_id):
        self.validate_pane(pane_id)
        self.call("focus", str(pane_id))

    def spawn(self, program, url, *, focus):
        self.validate_pane(self.pane)
        argv = ["split", "--title", "Browser", "--cwd", os.getcwd()]
        if focus:
            argv.append("--focus")
        # The wrapper inherits the new pane's own Rozi environment, not the command's source id.
        argv.extend(["--argv", sys.executable, str(Path(__file__).resolve()), "pane", program, url, self.tab])
        return self.call(*argv)


class TerminalBridge:
    """One private socket per invocation or browser pane; no separate daemon or state file."""
    METHODS = ["getCurrentPane", "listPanes", "focusPane"]

    def __init__(self, rozi):
        self.rozi = rozi

    def request(self, method, params):
        if method == "hello":
            if params.get("protocol") != 1:
                raise ExtensionError("Unsupported terminal socket protocol")
            return {"name": "rozi", "methods": self.METHODS}
        if method == "getCurrentPane":
            self.rozi.validate_pane(self.rozi.pane)
            return {"pane": {"id": self.rozi.pane, "tab": self.rozi.tab}}
        if method == "listPanes":
            self.rozi.validate_pane(self.rozi.pane)
            panes = self.rozi.call("list-panes", "--format", "json")
            return {"panes": [{"id": str(pane["id"]), "tab": self.rozi.tab,
                               "tty": None, "command": pane.get("foreground_program")}
                              for pane in panes]}
        if method == "focusPane":
            pane = params.get("pane", {})
            if pane.get("tab") != self.rozi.tab:
                raise ExtensionError("Browser belongs to another session")
            self.rozi.focus(pane.get("id"))
            return {}
        raise ExtensionError(f"Unsupported terminal operation: {method}")

    @contextlib.contextmanager
    def running(self):
        bridge = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(3)
                request_id = ""
                try:
                    line = self.rfile.readline(65537)
                    if len(line) > 65536 or not line.endswith(b"\n"):
                        raise ExtensionError("Invalid terminal request")
                    request = json.loads(line)
                    request_id = request["id"]
                    result = bridge.request(request["method"], request.get("params") or {})
                    reply = {"id": request_id, "result": result}
                except (ValueError, KeyError, TypeError, OSError, ExtensionError, subprocess.TimeoutExpired) as error:
                    reply = {"id": request_id, "error": {"message": str(error)}}
                self.wfile.write(json.dumps(reply).encode() + b"\n")

        with tempfile.TemporaryDirectory(prefix="rozi-browser-") as directory:
            path = str(Path(directory) / "terminal.sock")
            with socketserver.UnixStreamServer(path, Handler) as server:
                os.chmod(path, 0o600)
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
                thread.start()
                try:
                    env = self.rozi.env.copy()
                    env["TERMINAL_BROWSER_TERMINAL_SOCKET"] = path
                    yield env
                finally:
                    server.shutdown()
                    thread.join(timeout=5)


def browser_rows(browsers):
    return [{"id": browser["key"],
             "label": next((tab.get("title") or tab.get("url") for tab in browser.get("tabs", [])
                            if tab.get("active")), "Browser"),
             "description": f"Pane {browser['pane']['pane']} · {len(browser.get('tabs', []))} tabs"}
            for browser in browsers]


def session_browsers(program, env, rozi):
    listing = run_json([program, "ls", "--all", "--json"], env=env)
    live = rozi.call("list-panes", "--format", "json")
    panes = {str(pane["id"]) for pane in live}
    return [browser for browser in listing.get("browsers", [])
            if browser.get("pane", {}).get("tab") == rozi.tab
            and str(browser.get("pane", {}).get("pane")) in panes]


def validate_url(value):
    if not value or "\0" in value:
        raise ExtensionError("Enter a URL")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https", "file"):
        raise ExtensionError("Use an http, https, or local file URL")
    if parsed.scheme in ("http", "https") and not parsed.netloc:
        raise ExtensionError("URL needs a host")
    if parsed.scheme == "file" and parsed.netloc not in ("", "localhost"):
        raise ExtensionError("File URL must refer to a local file")
    return value


def command(mode, rozi, settings):
    program = shutil.which(settings.get("browser", "terminal-browser"))
    if not program:
        raise ExtensionError("Install terminal-browser and make it available on PATH")
    url = rozi.env.get("ROZI_URL") or ""
    with TerminalBridge(rozi).running() as env:
        browsers = session_browsers(program, env, rozi)
        if mode == "browsers":
            result = rozi.pick(browser_rows(browsers), title="Terminal browsers")
            chosen = next((browser for browser in browsers if browser["key"] == result.get("selected")), None)
            if chosen:
                rozi.focus(chosen["pane"]["pane"])
            return
        if not url:
            result = rozi.pick([], title="Open in terminal browser", prompt=True)
            if result.get("cancelled") or result.get("action") != "open":
                return
            url = result.get("input", "")
        validate_url(url)
        rozi.validate_pane(rozi.pane)
        reuse = settings.get("reuse", True) and mode != "new-pane"
        chosen = None
        if reuse and len(browsers) == 1:
            chosen = browsers[0]
        elif reuse and len(browsers) > 1:
            result = rozi.pick(browser_rows(browsers), title="Open link in browser")
            chosen = next((browser for browser in browsers if browser["key"] == result.get("selected")), None)
            if not chosen:
                return
        if chosen:
            rozi.validate_pane(chosen["pane"]["pane"])
            # A failed reuse is reported; it must not silently create an extra browser.
            subprocess.run([program, "new-tab", "--browser", chosen["key"], url],
                           env=env, check=True, capture_output=True, text=True, timeout=20)
            if settings.get("focus", True):
                rozi.focus(chosen["pane"]["pane"])
        else:
            rozi.spawn(program, url, focus=settings.get("focus", True))


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if args and args[0] == "pane":
            if len(args) != 4:
                raise ExtensionError("Invalid browser pane launch")
            _, program, url, expected = args
            env = os.environ.copy()
            env["ROZI_SOURCE_SESSION"] = expected
            # Each browser must identify its own pane, not the original clicked pane.
            env.pop("ROZI_SOURCE_PANE", None)
            rozi = Rozi(env)
            with TerminalBridge(rozi).running() as browser_env:
                # Upstream partial updates need reusable-file transport fixes for Rozi.
                browser_env["TERMINAL_BROWSER_PRESENT"] = "full"
                status = subprocess.call([program, "open", "--no-merge", validate_url(url)], env=browser_env)
                if status:
                    raise ExtensionError(f"terminal-browser exited with status {status}; check its installation and display access")
                return 0
        if len(args) != 1 or args[0] not in ("open", "new-pane", "browsers"):
            raise ExtensionError("Expected open, new-pane, or browsers")
        rozi = Rozi()
        settings = json.loads(rozi.env.get("ROZI_EXTENSION_CONFIG", "{}"))
        command(args[0], rozi, settings)
        return 0
    except (ExtensionError, OSError, ValueError, subprocess.SubprocessError) as error:
        message = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
        binary = os.environ.get("ROZI_BIN")
        if binary and os.environ.get("ROZI_SOCKET"):
            try:
                result = subprocess.run([binary, "notify", message, "--title", "Terminal browser", "--level", "error"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                if result.returncode == 0:
                    return 0
            except (OSError, subprocess.SubprocessError):
                pass
        print(f"Terminal browser: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
