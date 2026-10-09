import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "terminal_browser", Path(__file__).parents[1] / "bin" / "terminal_browser.py"
)
browser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser)


class FakeRozi:
    tab = "session-instance"
    pane = "3"
    env = {"ROZI_URL": "https://example.test/$(touch nope);quoted'"}

    def __init__(self):
        self.spawns = []
        self.focuses = []
        self.pick_reply = {"cancelled": True}

    def validate_pane(self, pane, layout=None):
        if str(pane) not in ("3", "4", "5"):
            raise browser.ExtensionError("closed")

    def call(self, *args):
        if args == ("list-panes", "--format", "json"):
            return [{"id": number, "foreground_program": "python3"} for number in (3, 4, 5)]
        raise AssertionError(args)

    def pick(self, rows, **kwargs):
        return self.pick_reply

    def spawn(self, program, url, *, focus):
        self.spawns.append((program, url, focus))

    def focus(self, pane):
        self.focuses.append(pane)


def instance(key="one", pane="4", tab="session-instance"):
    return {"key": key, "pane": {"pane": pane, "tab": tab},
            "tabs": [{"title": "Example", "url": "https://example.test", "active": True}]}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.rozi = FakeRozi()
        self.which = patch.object(browser.shutil, "which", return_value="/opt/browser app/terminal-browser")
        self.which.start()
        self.addCleanup(self.which.stop)

    def run_command(self, instances, mode="open"):
        with patch.object(browser, "run_json", return_value={"browsers": instances}):
            browser.command(mode, self.rozi, {})

    def test_missing_browser_is_actionable(self):
        with patch.object(browser.shutil, "which", return_value=None):
            with self.assertRaisesRegex(browser.ExtensionError, "Install terminal-browser"):
                browser.command("open", self.rozi, {})

    def test_no_browser_creates_pane_with_literal_url(self):
        self.run_command([])
        self.assertEqual(self.rozi.spawns, [("/opt/browser app/terminal-browser", self.rozi.env["ROZI_URL"], True)])

    def test_reuse_passes_browser_and_url_as_separate_arguments(self):
        with patch.object(browser.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.run_command([instance()])
        self.assertEqual(run.call_args.args[0], ["/opt/browser app/terminal-browser", "new-tab", "--browser", "one", self.rozi.env["ROZI_URL"]])
        self.assertEqual(self.rozi.focuses, ["4"])
        self.assertEqual(self.rozi.spawns, [])

    def test_multiple_browsers_require_choice_and_cancel_is_inert(self):
        self.run_command([instance(), instance("two", "5")])
        self.assertEqual(self.rozi.spawns, [])
        self.assertEqual(self.rozi.focuses, [])
        self.rozi.pick_reply = {"selected": "two"}
        with patch.object(browser.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.run_command([instance(), instance("two", "5")])
        self.assertEqual(run.call_args.args[0][-2:], ["two", self.rozi.env["ROZI_URL"]])

    def test_explicit_new_pane_does_not_reuse(self):
        self.run_command([instance()], "new-pane")
        self.assertEqual(len(self.rozi.spawns), 1)

    def test_reuse_failure_does_not_create_a_duplicate(self):
        with patch.object(browser.subprocess, "run", side_effect=subprocess.CalledProcessError(1, [], stderr="browser closed")):
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_command([instance()])
        self.assertEqual(self.rozi.spawns, [])

    def test_other_sessions_and_closed_panes_are_excluded(self):
        instances = [instance(), instance("other", "4", "another-session"), instance("closed", "99")]
        with patch.object(browser, "run_json", return_value={"browsers": instances}):
            found = browser.session_browsers("terminal-browser", {}, self.rozi)
        self.assertEqual(found, [instances[0]])

    def test_url_prompt_cancel_creates_nothing(self):
        self.rozi.env = {}
        self.run_command([])
        self.assertEqual(self.rozi.spawns, [])

    def test_url_validation_rejects_options_and_nonlocal_files(self):
        for url in ("--help", "javascript:alert(1)", "https:///path", "file://other-host/tmp/a"):
            with self.assertRaises(browser.ExtensionError):
                browser.validate_url(url)


class PickerTests(unittest.TestCase):
    def pick(self, reply):
        rozi = browser.Rozi.__new__(browser.Rozi)
        rozi.binary = "rozi"
        rozi.env = {"ROZI_SOCKET": "unused-test-endpoint"}

        class Process:
            stdin = io.StringIO()
            stdout = io.StringIO(reply)
            stderr = io.StringIO()
            terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True
                self.request = json.loads(self.stdin.getvalue())

            def wait(self, timeout=None):
                return -15

        process = Process()
        with patch.object(browser.subprocess, "Popen", return_value=process):
            result = rozi.pick([], title="Open URL", prompt=True)
        self.assertTrue(process.terminated)
        self.assertTrue(process.stdin.closed)
        return result

    def test_closing_action_is_read_without_waiting_for_cli_exit(self):
        self.assertEqual(self.pick('{"action":"open","input":"https://example.test"}\n'),
                         {"action": "open", "input": "https://example.test"})

    def test_cancel_and_retired_stream_are_inert(self):
        self.assertEqual(self.pick('{"cancelled":true}\n'), {"cancelled": True})
        self.assertEqual(self.pick(''), {"cancelled": True})


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "terminal bridge requires Unix sockets")
class BridgeTests(unittest.TestCase):
    def test_public_socket_protocol_and_cleanup(self):
        rozi = FakeRozi()
        bridge = browser.TerminalBridge(rozi)
        with bridge.running() as env:
            path = env["TERMINAL_BROWSER_TERMINAL_SOCKET"]
            def call(method, params):
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(path)
                    connection.sendall(json.dumps({"id": "request-id", "method": method, "params": params}).encode() + b"\n")
                    reply = json.loads(connection.makefile("rb").readline())
                    self.assertEqual(reply["id"], "request-id")
                    return reply
            hello = call("hello", {"protocol": 1})["result"]
            self.assertEqual(hello["name"], "rozi")
            self.assertNotIn("split", hello["methods"])
            pane = call("getCurrentPane", {"tty": None, "cwd": "/irrelevant"})["result"]["pane"]
            self.assertEqual(pane, {"id": "3", "tab": "session-instance"})
            self.assertIn("error", call("focusPane", {"pane": {"id": "4", "tab": "another-session"}}))
            self.assertEqual(call("focusPane", {"pane": {"id": "4", "tab": "session-instance"}})["result"], {})
            self.assertEqual(rozi.focuses, ["4"])
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
        self.assertFalse(Path(path).exists())


if __name__ == "__main__":
    unittest.main()
