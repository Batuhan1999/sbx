"""A sandbox the runtime paused or deleted between the keeper's listing and
its next call (runtime.NotRunning) is not a failure: the round carries on
and the next listing shows the sandbox as it is."""
import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sbx_control import core, keeper, resources, runtime


def listed(name, state="running"):
    return runtime.Info(sandbox_id=f"{name}-vm", metadata={"sbx": name}, state=state)


class Keeper(unittest.TestCase):
    def round(self, infos, set_timeout):
        """look_once over infos, every running one used; what it delivered to and printed."""
        out = io.StringIO()
        used = SimpleNamespace(accessed_within=lambda seconds: True)
        with contextlib.ExitStack() as stack:
            for target, name, value in (
                    (keeper.sandboxes, "registered", lambda found: found),
                    (keeper.sandboxes, "record_transition", None),
                    (keeper.sandboxes, "prepare_waiting", None),
                    (keeper.tls_pin, "redeliver", None),
                    (keeper.routing, "forget_except", None),
                    (keeper.routing, "detect_ports", None),
                    (keeper.sandbox_layers, "tick", None),
                    (keeper.sandbox_layers, "round", None),
                    (keeper, "notice_stopped_app", None),
                    (keeper.extensions, "keeper_policy", lambda info, use: None),
                    (keeper, "use_of", lambda info, running: used)):
                stack.enter_context(patch.object(target, name, side_effect=value))
            stack.enter_context(patch.object(keeper.core, "sandboxes", return_value=infos))
            stack.enter_context(patch.object(keeper.lifecycle, "busy", return_value=set()))
            stack.enter_context(patch.object(keeper.settings, "current", return_value=SimpleNamespace(
                sandboxes=SimpleNamespace(paused_days=0, detected_previews="off"))))
            stack.enter_context(patch.object(keeper.runtime, "set_timeout", side_effect=set_timeout))
            deliver = stack.enter_context(patch.object(keeper.sandboxes, "deliver_waiting"))
            stack.enter_context(contextlib.redirect_stdout(out))
            keeper.look_once()
        return deliver.call_args.args[0], out.getvalue()

    def test_a_sandbox_paused_since_the_listing_is_skipped_quietly(self):
        kept = []

        def set_timeout(sandbox_id, seconds):
            if sandbox_id == "a-vm":
                raise runtime.NotRunning("The sandbox was not found: it reached its timeout")
            kept.append(sandbox_id)

        keeper.gate._resumed_for_preview.add("a-vm")
        delivered, printed = self.round([listed("a"), listed("b")], set_timeout)
        self.assertEqual(kept, ["b-vm"])         # the round went on to the next sandbox
        self.assertEqual(delivered, {"b"})        # nothing is delivered to one that no longer runs
        self.assertEqual(printed, "")
        self.assertFalse(keeper.gate.resumed_for_preview("a-vm"))

    def test_a_real_failure_still_ends_the_round_and_is_logged(self):
        def set_timeout(sandbox_id, seconds):
            raise runtime.Failed("500: the runtime broke")

        with self.assertRaises(runtime.Failed):
            self.round([listed("a")], set_timeout)
        out, stop = io.StringIO(), Mock()
        stop.wait.side_effect = [False, True]
        with patch.object(keeper, "look_once", side_effect=runtime.Failed("500: the runtime broke")), \
                patch.object(keeper.threading, "Thread"), contextlib.redirect_stdout(out):
            keeper.run(stop)
        self.assertIn("the runtime broke", out.getvalue())

    def test_checking_the_app_of_a_sandbox_that_paused_ends_its_round(self):
        info = listed("a")
        with patch.object(keeper.lifecycle, "get", return_value={"active": None, "application": "healthy"}), \
                patch.object(keeper.core, "check_app", side_effect=runtime.NotRunning("gone")), \
                self.assertRaises(runtime.NotRunning):
            keeper.notice_stopped_app(info)


class Attach(unittest.TestCase):
    def test_a_sandbox_paused_since_it_was_listed_is_resumed(self):
        paused = listed("a", "paused")
        with patch.object(core, "connect", side_effect=runtime.NotRunning("paused")), \
                patch.object(core, "find", return_value=paused), \
                patch.object(core, "_resume", return_value=("vm", paused)) as resume, \
                patch.object(core, "app_of"):
            attached = core.attach("a", "test", info=listed("a"))
        self.assertEqual(attached.sbx, "vm")
        self.assertEqual(resume.call_args.args[2], paused)

    def test_a_sandbox_deleted_since_it_was_listed_is_no_sandbox(self):
        with patch.object(core, "connect", side_effect=runtime.NotRunning("gone")), \
                patch.object(core, "find", return_value=None), \
                self.assertRaisesRegex(core.SandboxError, "no sbx-a"):
            core.attach("a", "test", info=listed("a"))


class Resources(unittest.TestCase):
    def snapshot(self, **runtime_calls):
        info = runtime.Info(sandbox_id="a-vm", metadata={"sbx": "a"}, state="running", cpu_count=2, memory_mb=4096)
        app = SimpleNamespace(size={"cpu": 2, "memory_mb": 4096, "disk_mb": 0}, swap_mb=0, spec={})
        with patch.object(core, "find", return_value=info), patch.object(core, "app_of", return_value=app), \
                patch.object(core, "capacity", return_value={}), \
                patch.object(runtime, "info", **runtime_calls.get("info", {"return_value": info})), \
                patch.object(runtime, "connect", **runtime_calls.get("connect", {})):
            return resources.snapshot("a", live=True)

    def test_live_usage_of_a_sandbox_that_paused_meanwhile_is_unavailable(self):
        result = self.snapshot(connect={"side_effect": runtime.NotRunning("paused")})
        self.assertIsNone(result["usage"])
        self.assertIn("usage_unavailable", [i["code"] for i in result["issues"]])

    def test_a_sandbox_deleted_meanwhile_is_no_sandbox(self):
        with self.assertRaisesRegex(core.SandboxError, "no sandbox a"):
            self.snapshot(info={"side_effect": runtime.NotRunning("gone")})


if __name__ == "__main__":
    unittest.main()
