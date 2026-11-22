import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "panel_claim.py"
CONFIG = ROOT / "examples" / "config.example.json"
RESERVATION = ROOT / "examples" / "reservation-request.json"
DECLINE = ROOT / "examples" / "decline.json"
SELF_LAUNCH_BLOCKED = ROOT / "examples" / "self-launch-blocked.json"


class PanelClaimCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state = self.dir / "state.json"
        self.run_cli("--state", str(self.state), "init", "--config", str(CONFIG), "--by", "test-registrar")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, expect=0):
        result = subprocess.run(
            [sys.executable, str(CLI), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        if expect == 0:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def submit(self, path):
        result = self.run_cli("--state", str(self.state), "submit", "--input", str(path))
        return json.loads(result.stdout)

    def load_state(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_reservation_can_be_approved_claimed_and_released(self):
        request = self.submit(RESERVATION)
        self.assertEqual(request["status"], "requested")

        approved = json.loads(
            self.run_cli(
                "--state", str(self.state),
                "approve", request["id"],
                "--by", "test-registrar",
                "--claim-if-free",
            ).stdout
        )
        self.assertEqual(approved["status"], "claimed")
        state = self.load_state()
        self.assertEqual(state["machines"]["shared-mac-1"]["active_claim"], request["id"])

        board = self.run_cli("--state", str(self.state), "render-board", "--machine", "shared-mac-1").stdout
        self.assertIn("Review a shared research result", board)
        self.assertNotIn("Registrar-only example details", board)

        released = json.loads(
            self.run_cli(
                "--state", str(self.state),
                "release", request["id"],
                "--by", "test-registrar",
                "--outcome", "Completed in the test harness.",
            ).stdout
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["park_at_release"], "allowed_sole_use")
        state = self.load_state()
        self.assertIsNone(state["machines"]["shared-mac-1"]["active_claim"])

    def test_release_shared_use_skips_park(self):
        request = self.submit(RESERVATION)
        approved = json.loads(
            self.run_cli(
                "--state", str(self.state),
                "approve", request["id"],
                "--by", "test-registrar",
                "--claim-if-free",
            ).stdout
        )
        self.assertEqual(approved["status"], "claimed")

        released = json.loads(
            self.run_cli(
                "--state", str(self.state),
                "release", request["id"],
                "--by", "test-registrar",
                "--shared-use",
                "--outcome", "Owner worked the main display throughout.",
            ).stdout
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["park_at_release"], "skipped_shared_use")
        state = self.load_state()
        self.assertIsNone(state["machines"]["shared-mac-1"]["active_claim"])

    def test_decline_is_a_terminal_answer_not_a_request(self):
        request = self.submit(DECLINE)
        self.assertEqual(request["status"], "declined")
        self.assertEqual(request["requested_access"], "none")

    def test_self_launch_without_capability_is_blocked(self):
        request = self.submit(SELF_LAUNCH_BLOCKED)
        self.assertEqual(request["status"], "capability_blocked")
        self.run_cli(
            "--state", str(self.state),
            "approve", request["id"],
            "--by", "test-registrar",
            expect=2,
        )

    def test_self_launch_approval_fails_while_policy_disables_it(self):
        data = json.loads(SELF_LAUNCH_BLOCKED.read_text(encoding="utf-8"))
        data["capabilities"]["can_self_launch"] = True
        path = self.dir / "self-launch-capable.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        request = self.submit(path)
        self.assertEqual(request["status"], "requested")
        self.run_cli(
            "--state", str(self.state),
            "approve", request["id"],
            "--by", "test-registrar",
            expect=2,
        )

    def test_registrar_sets_effective_priority_and_queue_order(self):
        first = self.submit(RESERVATION)
        second_data = json.loads(RESERVATION.read_text(encoding="utf-8"))
        second_data["requester"] = "example-seat-c"
        second_data["requested_access"] = "registrar_launched"
        second_data["public_title"] = "Second queued task"
        second_path = self.dir / "second.json"
        second_path.write_text(json.dumps(second_data), encoding="utf-8")
        second = self.submit(second_path)

        self.run_cli("--state", str(self.state), "approve", first["id"], "--by", "test-registrar", "--effective-priority", "low")
        self.run_cli("--state", str(self.state), "approve", second["id"], "--by", "test-registrar", "--effective-priority", "urgent")
        claimed = json.loads(
            self.run_cli("--state", str(self.state), "claim-next", "--machine", "shared-mac-1", "--by", "test-registrar").stdout
        )
        self.assertEqual(claimed["id"], second["id"])

    def test_expired_claim_releases_machine(self):
        request = self.submit(RESERVATION)
        self.run_cli(
            "--state", str(self.state), "--now", "2026-11-06T17:00:00Z",
            "approve", request["id"], "--by", "test-registrar", "--claim-if-free",
        )
        expired = json.loads(
            self.run_cli(
                "--state", str(self.state), "--now", "2026-11-06T18:00:01Z",
                "expire", "--by", "test-registrar",
            ).stdout
        )
        self.assertEqual(expired["expired"], [request["id"]])
        self.assertIsNone(self.load_state()["machines"]["shared-mac-1"]["active_claim"])

    def test_markdown_intake_parses_to_json(self):
        intake = self.dir / "intake.md"
        intake.write_text(
            """```panel-claim
Requester: example-seat-a
Machine: shared-mac-1
Preference: accept
Requested access: reservation_request
Public title: Parsed from Markdown
Duration minutes: 30
Proposed priority: high
Can request: yes
Can self-launch: no
Can monitor: no
Can release: no
```""",
            encoding="utf-8",
        )
        parsed = json.loads(self.run_cli("parse-markdown", "--input", str(intake)).stdout)
        self.assertEqual(parsed["requester"], "example-seat-a")
        self.assertEqual(parsed["duration_minutes"], 30)
        self.assertFalse(parsed["capabilities"]["can_self_launch"])

    def test_duplicate_request_id_is_rejected(self):
        path = self.write_request("dup.json", id="req_fixed_duplicate")
        first = self.submit(path)
        self.assertEqual(first["id"], "req_fixed_duplicate")
        result = self.run_cli("--state", str(self.state), "submit", "--input", str(path), expect=1)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate request id", result.stderr)
        matches = [r for r in self.load_state()["requests"] if r["id"] == "req_fixed_duplicate"]
        self.assertEqual(len(matches), 1)

    def test_markdown_invalid_duration_is_a_clean_error(self):
        intake = self.dir / "bad-intake.md"
        intake.write_text(
            """```panel-claim
Requester: example-seat-a
Machine: shared-mac-1
Duration minutes: abc
```""",
            encoding="utf-8",
        )
        result = self.run_cli("parse-markdown", "--input", str(intake), expect=1)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duration_minutes must be an integer", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_audit_log_records_key_events(self):
        request = self.submit(RESERVATION)
        self.run_cli("--state", str(self.state), "approve", request["id"], "--by", "test-registrar")
        audit_path = self.state.with_suffix(self.state.suffix + ".audit.jsonl")
        events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        actions = {event["action"] for event in events}
        self.assertIn("init", actions)
        self.assertIn("submit", actions)
        self.assertIn("approve", actions)

    # -- Fast lane (v0.2.0) -------------------------------------------------

    def write_request(self, name, **overrides):
        base = {
            "requester": "example-seat-b",
            "machine": "shared-mac-1",
            "preference": "accept",
            "requested_access": "reservation_request",
            "public_title": "Fast-lane candidate work",
            "duration_minutes": 5,
            "proposed_priority": "normal",
            "capabilities": {"can_request": True, "can_self_launch": False, "can_monitor": False, "can_release": False},
            "resources": {"app": "utility-app", "uses_clipboard": False, "modal": "no", "signin": False, "park": False},
        }
        base.update(overrides)
        path = self.dir / name
        path.write_text(json.dumps(base), encoding="utf-8")
        return path

    def claim_browser_session(self, **claim_overrides):
        claim = {
            "requester": "example-seat-a",
            "machine": "shared-mac-1",
            "preference": "accept",
            "requested_access": "reservation_request",
            "public_title": "Long browser session",
            "duration_minutes": 60,
            "proposed_priority": "normal",
            "capabilities": {"can_request": True, "can_self_launch": False, "can_monitor": False, "can_release": False},
            "resources": {"app": "browser", "uses_clipboard": True, "modal": "possible", "signin": False, "park": False},
        }
        claim.update(claim_overrides)
        path = self.dir / "claim.json"
        path.write_text(json.dumps(claim), encoding="utf-8")
        request = self.submit(path)
        self.run_cli("--state", str(self.state), "approve", request["id"], "--by", "test-registrar", "--claim-if-free")
        return request

    def fastlane_check(self, path, expect=0):
        return self.run_cli("--state", str(self.state), "fastlane-check", "--input", str(path), expect=expect)

    def test_fastlane_ok_then_claim_fastlane_and_release(self):
        self.claim_browser_session()
        candidate_path = self.write_request("cand.json")
        result = self.fastlane_check(candidate_path)
        self.assertEqual(json.loads(result.stdout)["verdict"], "FAST-LANE OK")

        candidate = self.submit(candidate_path)
        self.run_cli("--state", str(self.state), "approve", candidate["id"], "--by", "test-registrar")
        promoted = json.loads(self.run_cli("--state", str(self.state), "claim-fastlane", "--machine", "shared-mac-1", "--by", "test-registrar").stdout)
        self.assertEqual(promoted["status"], "fastlane_claimed")
        state = self.load_state()
        machine = state["machines"]["shared-mac-1"]
        self.assertEqual(machine["active_fastlane"], candidate["id"])
        self.assertIsNotNone(machine["active_claim"])

        board = self.run_cli("--state", str(self.state), "render-board", "--machine", "shared-mac-1").stdout
        self.assertIn("Fast lane:", board)

        released = json.loads(self.run_cli("--state", str(self.state), "release", candidate["id"], "--by", "test-registrar").stdout)
        self.assertEqual(released["status"], "released")
        state = self.load_state()
        self.assertIsNone(state["machines"]["shared-mac-1"]["active_fastlane"])
        self.assertIsNotNone(state["machines"]["shared-mac-1"]["active_claim"])

    def test_fastlane_same_app_clipboard_and_duration_all_queue(self):
        self.claim_browser_session()
        same_app = self.write_request("same.json", resources={"app": "browser", "uses_clipboard": False, "modal": "no", "signin": False, "park": False})
        result = self.fastlane_check(same_app, expect=1)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["verdict"], "QUEUE")

        clip = self.write_request("clip.json", resources={"app": "utility-app", "uses_clipboard": True, "modal": "no", "signin": False, "park": False})
        result = self.fastlane_check(clip, expect=1)
        self.assertEqual(json.loads(result.stdout)["verdict"], "QUEUE")

        long_job = self.write_request("long.json", duration_minutes=30)
        result = self.fastlane_check(long_job, expect=1)
        self.assertEqual(json.loads(result.stdout)["verdict"], "QUEUE")

    def test_fastlane_untagged_claim_is_unknown_fail_closed(self):
        legacy = self.submit(RESERVATION)  # the shipped example carries no resource tags
        self.run_cli("--state", str(self.state), "approve", legacy["id"], "--by", "test-registrar", "--claim-if-free")
        result = self.fastlane_check(self.write_request("cand2.json"), expect=1)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["verdict"], "UNKNOWN")

    def test_fastlane_free_machine_says_claim(self):
        result = self.fastlane_check(self.write_request("cand3.json"))
        self.assertEqual(json.loads(result.stdout)["verdict"], "CLAIM")

    def test_fastlane_exclusive_claim_queues_everything(self):
        self.claim_browser_session(exclusive=True)
        result = self.fastlane_check(self.write_request("cand4.json"), expect=1)
        self.assertEqual(json.loads(result.stdout)["verdict"], "QUEUE")

    def test_markdown_intake_parses_resource_tags(self):
        intake = self.dir / "intake.md"
        intake.write_text(
            "Requester: example-seat-b\nMachine: shared-mac-1\nPublic title: Tagged intake\n"
            "Duration minutes: 5\nApp: utility-app\nUses clipboard: no\nModal: no\n"
            "Signin: no\nPark: no\nExclusive: no\n",
            encoding="utf-8",
        )
        parsed = json.loads(self.run_cli("parse-markdown", "--input", str(intake)).stdout)
        self.assertEqual(parsed["resources"]["app"], "utility-app")
        self.assertFalse(parsed["resources"]["uses_clipboard"])
        self.assertFalse(parsed["exclusive"])

    # -- Regression: owner-approval gate (null/blank refs) ------------------

    def test_self_launch_owner_approval_ref_must_be_a_real_string(self):
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        config["policy"]["self_launch_enabled"] = True
        config_path = self.dir / "config-self-launch.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        sl_state = self.dir / "state-sl.json"
        self.run_cli("--state", str(sl_state), "init", "--config", str(config_path), "--by", "test-registrar")

        base = json.loads(SELF_LAUNCH_BLOCKED.read_text(encoding="utf-8"))
        base["capabilities"]["can_self_launch"] = True

        def submit_variant(name, **overrides):
            data = dict(base)
            data.update(overrides)
            path = self.dir / name
            path.write_text(json.dumps(data), encoding="utf-8")
            result = self.run_cli("--state", str(sl_state), "submit", "--input", str(path))
            return json.loads(result.stdout)

        # A JSON null ref normalizes to "" (never the string "None") and
        # approval without a real ref fails -- as does a whitespace-only
        # ref supplied on the command line.
        null_req = submit_variant("sl-null.json", owner_approval_ref=None)
        self.assertEqual(null_req["owner_approval_ref"], "")
        self.assertEqual(null_req["status"], "requested")
        self.run_cli("--state", str(sl_state), "approve", null_req["id"], "--by", "test-registrar", expect=2)
        self.run_cli(
            "--state", str(sl_state), "approve", null_req["id"],
            "--by", "test-registrar", "--owner-approval-ref", "   ", expect=2,
        )

        # A whitespace-only stored ref counts as missing too.
        blank_req = submit_variant("sl-blank.json", owner_approval_ref="   ")
        self.assertEqual(blank_req["owner_approval_ref"], "")
        self.run_cli("--state", str(sl_state), "approve", blank_req["id"], "--by", "test-registrar", expect=2)

        # A real stored ref succeeds.
        real_req = submit_variant("sl-real.json", owner_approval_ref="owner-msg-123")
        approved = json.loads(
            self.run_cli("--state", str(sl_state), "approve", real_req["id"], "--by", "test-registrar").stdout
        )
        self.assertEqual(approved["status"], "queued")

        # A real CLI-supplied ref succeeds for a request with no stored ref.
        cli_req = submit_variant("sl-cli.json", owner_approval_ref=None)
        approved = json.loads(
            self.run_cli(
                "--state", str(sl_state), "approve", cli_req["id"],
                "--by", "test-registrar", "--owner-approval-ref", "owner-msg-456",
            ).stdout
        )
        self.assertEqual(approved["status"], "queued")
        self.assertEqual(approved["owner_approval_ref"], "owner-msg-456")

    # -- Regression: blank fast-lane tags fail closed ------------------------

    def test_fastlane_blank_tags_are_unknown_fail_closed(self):
        self.claim_browser_session()
        blank_bool = self.write_request(
            "blank-bool.json",
            resources={"app": "utility-app", "uses_clipboard": "", "modal": "no", "signin": False, "park": False},
        )
        result = self.fastlane_check(blank_bool, expect=1)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["verdict"], "UNKNOWN")

        blank_modal = self.write_request(
            "blank-modal.json",
            resources={"app": "utility-app", "uses_clipboard": False, "modal": "", "signin": False, "park": False},
        )
        result = self.fastlane_check(blank_modal, expect=1)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["verdict"], "UNKNOWN")

    # -- Regression: fast-lane lifecycle guards ------------------------------

    def claim_fastlane_session(self):
        main = self.claim_browser_session()
        candidate = self.submit(self.write_request("cand-fl.json"))
        self.run_cli("--state", str(self.state), "approve", candidate["id"], "--by", "test-registrar")
        promoted = json.loads(
            self.run_cli("--state", str(self.state), "claim-fastlane", "--machine", "shared-mac-1", "--by", "test-registrar").stdout
        )
        self.assertEqual(promoted["status"], "fastlane_claimed")
        return main, candidate

    def test_deny_or_withdraw_fastlane_claim_fails_and_pointer_survives(self):
        main, candidate = self.claim_fastlane_session()
        self.run_cli("--state", str(self.state), "deny", candidate["id"], "--by", "test-registrar", expect=2)
        self.run_cli("--state", str(self.state), "withdraw", candidate["id"], "--by", "example-seat-b", expect=2)
        state = self.load_state()
        self.assertEqual(state["machines"]["shared-mac-1"]["active_fastlane"], candidate["id"])
        statuses = {r["id"]: r["status"] for r in state["requests"]}
        self.assertEqual(statuses[candidate["id"]], "fastlane_claimed")

    def test_deny_released_request_fails_terminal_status_sticks(self):
        request = self.submit(RESERVATION)
        self.run_cli("--state", str(self.state), "approve", request["id"], "--by", "test-registrar", "--claim-if-free")
        self.run_cli("--state", str(self.state), "release", request["id"], "--by", "test-registrar")
        self.run_cli("--state", str(self.state), "deny", request["id"], "--by", "test-registrar", expect=2)
        statuses = {r["id"]: r["status"] for r in self.load_state()["requests"]}
        self.assertEqual(statuses[request["id"]], "released")

    def test_board_still_shows_fastlane_after_main_claim_releases(self):
        main, candidate = self.claim_fastlane_session()
        self.run_cli("--state", str(self.state), "release", main["id"], "--by", "test-registrar")
        board = self.run_cli("--state", str(self.state), "render-board", "--machine", "shared-mac-1").stdout
        self.assertIn("Fast lane:", board)
        self.assertIn("Fast-lane candidate work", board)
        machine = self.load_state()["machines"]["shared-mac-1"]
        self.assertIsNone(machine["active_claim"])
        self.assertEqual(machine["active_fastlane"], candidate["id"])

    def test_second_fastlane_candidate_queues_while_one_active(self):
        main, candidate = self.claim_fastlane_session()
        result = self.fastlane_check(self.write_request("cand-second.json"), expect=1)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["verdict"], "QUEUE")
        # Still QUEUE after the main claim releases (fast lane outlives it).
        self.run_cli("--state", str(self.state), "release", main["id"], "--by", "test-registrar")
        result = self.fastlane_check(self.write_request("cand-third.json"), expect=1)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["verdict"], "QUEUE")

    def test_claim_next_blocked_when_incoming_claim_shares_fastlane_app(self):
        main, candidate = self.claim_fastlane_session()
        self.run_cli("--state", str(self.state), "release", main["id"], "--by", "test-registrar")
        rival = self.submit(self.write_request(
            "rival.json",
            requester="example-seat-c",
            public_title="Utility work too",
            resources={"app": "utility-app", "uses_clipboard": False, "modal": "no", "signin": False, "park": False},
        ))
        self.run_cli("--state", str(self.state), "approve", rival["id"], "--by", "test-registrar")
        self.run_cli("--state", str(self.state), "claim-next", "--machine", "shared-mac-1", "--by", "test-registrar", expect=2)
        machine = self.load_state()["machines"]["shared-mac-1"]
        self.assertIsNone(machine["active_claim"])
        self.assertEqual(machine["active_fastlane"], candidate["id"])

    def test_claim_next_compatible_with_running_fastlane(self):
        main, candidate = self.claim_fastlane_session()
        self.run_cli("--state", str(self.state), "release", main["id"], "--by", "test-registrar")
        nxt = self.submit(self.write_request(
            "next.json",
            requester="example-seat-c",
            public_title="Editor session",
            resources={"app": "editor", "uses_clipboard": True, "modal": "possible", "signin": False, "park": False},
        ))
        self.run_cli("--state", str(self.state), "approve", nxt["id"], "--by", "test-registrar")
        claimed = json.loads(
            self.run_cli("--state", str(self.state), "claim-next", "--machine", "shared-mac-1", "--by", "test-registrar").stdout
        )
        self.assertEqual(claimed["status"], "claimed")
        machine = self.load_state()["machines"]["shared-mac-1"]
        self.assertEqual(machine["active_claim"], nxt["id"])
        self.assertEqual(machine["active_fastlane"], candidate["id"])


if __name__ == "__main__":
    unittest.main()
