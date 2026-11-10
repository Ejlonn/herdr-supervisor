"""Exact role profiles of the optional helper definitions: only the listed role with its exact model and effort is approved."""
import datetime as dt
import json

import herdr_codex_children as cc
from test_codex_child_guard import Base, Flow, P, write_lines
from v2_fixtures import FakeHerdr


class ProfileBuilder:
    def make(self, role="repository_explorer", model="gpt-6-luna", effort="max", *, after=True, child_role=None, **world_args):
        made = self.world.build(**world_args)
        for label, path in made.items():
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            for row in rows:
                payload = row.get("payload", {})
                if after and "timestamp" in row:
                    stamp = dt.datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) + dt.timedelta(days=5)
                    row["timestamp"] = stamp.isoformat()
                if payload.get("name") == "spawn_agent":
                    args = json.loads(payload["arguments"])
                    args["agent_type"] = role
                    payload["arguments"] = json.dumps(args)
                if label == "child" and row["type"] == "session_meta":
                    payload["source"]["subagent"]["thread_spawn"]["agent_role"] = child_role or role
                if label == "child" and payload.get("type") == "thread_settings_applied":
                    settings = payload["thread_settings"]
                    settings["model"] = model
                    settings["collaboration_mode"]["settings"]["reasoning_effort"] = effort
            write_lines(path, rows)
        return made


class ProfilePolicyTests(ProfileBuilder, Base):
    def test_new_explorer_requires_luna_max(self):
        self.make()
        v = cc.verify(P, self.root)
        self.assertEqual(v.kind, cc.COMPLETE)
        self.assertEqual((v.evidence["expected_model"], v.evidence["expected_effort"]), ("gpt-6-luna", "max"))

    def test_new_explorer_lower_efforts_or_wrong_model_are_refused(self):
        for model, effort in (("gpt-6-luna", "high"), ("gpt-6-luna", "xhigh"), ("gpt-6.1-sol", "max")):
            with self.subTest(model=model, effort=effort):
                self.make(model=model, effort=effort)
                self.assertEqual(cc.verify(P, self.root).kind, cc.VIOLATION)

    def test_new_reviewer_requires_exact_sol_high(self):
        self.make("repository_reviewer", "gpt-6.1-sol", "high")
        v = cc.verify(P, self.root)
        self.assertEqual(v.kind, cc.COMPLETE)
        self.assertEqual((v.evidence["expected_model"], v.evidence["expected_effort"]), ("gpt-6.1-sol", "high"))

    def test_reviewer_does_not_inherit_explorer_or_arbitrary_effort(self):
        for model, effort in (("gpt-6-luna", "max"), ("gpt-6.1-sol", "max"), ("gpt-6.1-sol", "medium")):
            with self.subTest(model=model, effort=effort):
                self.make("repository_reviewer", model, effort)
                self.assertEqual(cc.verify(P, self.root).kind, cc.VIOLATION)

    def test_even_two_approved_roles_must_match_the_exact_requested_role(self):
        for role, child in (("repository_explorer", "repository_reviewer"), ("repository_reviewer", "repository_explorer")):
            with self.subTest(role=role):
                self.make(role, child_role=child)
                self.assertEqual(cc.verify(P, self.root).kind, cc.VIOLATION)

    def test_one_helper_total_not_one_per_role(self):
        made = self.make()
        path = made["parent"]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        original = next(row for row in rows if row.get("payload", {}).get("name") == "spawn_agent")
        second = json.loads(json.dumps(original))
        second["payload"]["call_id"] += "-reviewer"
        args = json.loads(second["payload"]["arguments"])
        args["agent_type"] = "repository_reviewer"
        second["payload"]["arguments"] = json.dumps(args)
        rows.append(second)
        write_lines(path, rows)
        v = cc.verify(P, self.root)
        self.assertEqual(v.kind, cc.VIOLATION)
        self.assertEqual(v.evidence["spawn_calls"], 2)


class CurrentProfileConsumerTests(ProfileBuilder, Flow):
    def run_profile(self, profile):
        self.write_plan()

        def record(fake, name, text):
            run, turn = FakeHerdr.ids(text)
            self.make(*profile, prompt=text, final=self.native_frame(run, turn))

        # A competing pane gate must still lose to the exact primary final.
        self.herdr.responses = [{"v2": ("plan", "human", "generic_question", str(self.question_payload())), "before": record}]
        self.sup.run_new("Profile route fixture", "codex", workflow_policy="gated_v2")
        self.assertEqual(self.state()["pending_gate"]["gate_type"], "plan_approval")
        self.assertEqual(len(self.codex_prompts()), 1)
        self.assertEqual(self.herdr.sent_keys, [])

    def test_new_max_explorer_routes_only_its_native_final(self):
        self.run_profile(("repository_explorer", "gpt-6-luna", "max"))

    def test_new_reviewer_routes_only_its_native_final(self):
        self.run_profile(("repository_reviewer", "gpt-6.1-sol", "high"))
