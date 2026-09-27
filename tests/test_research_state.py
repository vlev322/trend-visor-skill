import hashlib
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.report_helpers import saved_analysis, saved_study
from tests.test_report_criteria import QUESTION, rule, saved_rules
from tools.pageviews.artifacts import JsonArtifact, save_json_artifact
from tools.pageviews.errors import PageviewsError
from tools.pageviews.research_state import (
    approve_criteria, create_research, propose_criteria, read_research, research_report, research_summary,
    revise_research,
)


class ResearchStateTests(unittest.TestCase):
    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network"))
    def test_saved_proposal_requires_separate_confirmation_before_report(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            rules, _ = saved_rules(root, study)
            initial = create_research(study, question=QUESTION, output=root / "initial.json")
            pending = propose_criteria(initial, rules, output=root / "pending.json")
            self.assertEqual(read_research(pending)["status"], "awaiting_confirmation")
            summary = research_summary(pending)
            self.assertEqual(summary["criteria_proposal"]["rules"][0]["threshold"], 150)
            self.assertNotIn("counts", summary)
            with self.assertRaises(PageviewsError) as caught:
                research_report(pending)
            self.assertEqual(caught.exception.code, "confirmation_required")
            approved = approve_criteria(pending, confirmation=pending.sha256,
                                        user_reply="Так, застосуй це правило.", output=root / "approved.json")
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            report = research_report(approved)
            self.assertEqual(report["prioritization"]["counts"], {"matches": 1, "does_not_match": 1, "undetermined": 1})
            self.assertEqual(report["research_state"]["sha256"], approved.sha256)
            self.assertEqual(read_research(approved)["approval"]["user_reply"], "Так, застосуй це правило.")
            self.assertEqual(read_research(initial)["status"], "descriptive")
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
        network.assert_not_called()

    def approved(self, root, *, analysis_artifacts=()):
        study, _ = saved_study(root)
        rules, _ = saved_rules(root, study)
        initial = create_research(study, question=QUESTION, output=root / "initial.json", analysis_artifacts=analysis_artifacts)
        pending = propose_criteria(initial, rules, output=root / "pending.json")
        approved = approve_criteria(pending, confirmation=pending.sha256, user_reply="Так.", output=root / "approved.json")
        return study, rules, initial, pending, approved

    def test_revised_question_clears_approval_and_old_rules_are_not_rebound(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, rules, _, pending, approved = self.approved(root)
            changed = revise_research(approved, question="Нове питання", output=root / "revised.json", criteria=["Уточнити критерій"])
            state = read_research(changed)
            self.assertEqual(state["research_id"], read_research(approved)["research_id"])
            self.assertEqual(state["status"], "descriptive")
            self.assertIsNone(state["approval"])
            self.assertIsNone(state["rules"])
            self.assertEqual(research_report(changed)["prioritization"]["status"], "criteria_require_review")
            clean = revise_research(changed, question="Нове питання", output=root / "clean.json")
            with self.assertRaises(PageviewsError):
                propose_criteria(clean, rules, output=root / "wrong.json")
            self.assertFalse((root / "wrong.json").exists())
            self.assertEqual(read_research(pending)["status"], "awaiting_confirmation")
            self.assertEqual(research_report(approved)["prioritization"]["counts"]["matches"], 1)

    def test_new_study_clears_rules_and_explicit_revise_discards_attachments(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, data = saved_study(root)
            analysis, _ = saved_analysis(root, data)
            initial = create_research(study, question=QUESTION, output=root / "initial.json", analysis_artifacts=(analysis,))
            self.assertEqual(research_summary(initial)["attachments"]["analyses"], 1)
            other, _ = saved_study(root / "other", {"en": (20, 20, 10, 10)}, unmatched=())
            changed = revise_research(initial, study=other, question=QUESTION, output=root / "changed.json")
            self.assertEqual(read_research(changed)["inputs"]["analyses"], [])
            report = research_report(changed)
            self.assertEqual(report["study_sha256"], other.sha256)
            self.assertEqual([row["language"] for row in report["evidence"]], ["en"])
            self.assertEqual(report["evidence"][0]["analysis"]["comparison"]["change_percent"], -50.0)

    def test_replacement_rule_proposal_needs_new_confirmation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _, _, _, approved = self.approved(root)
            low, _ = saved_rules(root, study, [rule(threshold=100)], name="lower.json")
            pending = propose_criteria(approved, low, output=root / "new-proposal.json")
            with self.assertRaises(PageviewsError):
                approve_criteria(pending, confirmation=approved.sha256, user_reply="Так.", output=root / "bad.json")
            self.assertFalse((root / "bad.json").exists())
            with self.assertRaises(PageviewsError):
                research_report(pending)
            new = approve_criteria(pending, confirmation=pending.sha256, user_reply="Новий поріг погоджено.", output=root / "new-approval.json")
            self.assertEqual(research_report(new)["prioritization"]["counts"]["matches"], 2)
            self.assertEqual(research_report(approved)["prioritization"]["counts"]["matches"], 1)

    def test_unstructured_conditions_must_be_resolved_not_silently_discarded(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            rules, _ = saved_rules(root, study)
            initial = create_research(study, question=QUESTION, criteria=["Якісний контент"], output=root / "initial.json")
            self.assertEqual(research_summary(initial)["next_action"], "clarify_criteria")
            with self.assertRaises(PageviewsError):
                propose_criteria(initial, rules, output=root / "hidden-condition.json")
            self.assertFalse((root / "hidden-condition.json").exists())
            self.assertEqual(read_research(initial)["inputs"]["criteria"], ["Якісний контент"])

    def test_confirmed_state_preserves_explicit_diagnostic_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, data = saved_study(root, {"cs": (10, 20, 30, 100, 30, 40, 50, 60)}, split=4, unmatched=())
            analysis, _ = saved_analysis(root, data)
            rules, _ = saved_rules(root, study, [rule("change_without_top_days_percent", 100, parameters={"top_days": 1})])
            initial = create_research(study, question=QUESTION, analysis_artifacts=(analysis,), output=root / "initial.json")
            pending = propose_criteria(initial, rules, output=root / "pending.json")
            confirmed = approve_criteria(pending, confirmation=pending.sha256, user_reply="Так.", output=root / "confirmed.json")
            report = research_report(confirmed)
            self.assertEqual(report["evidence"][0]["criteria_evaluation"]["status"], "matches")
            self.assertEqual(report["evidence"][0]["criteria_evaluation"]["checks"][0]["analysis_sha256"], analysis.sha256)
            analysis.path.write_bytes(analysis.path.read_bytes() + b"\n")
            with self.assertRaises(PageviewsError) as caught:
                research_report(confirmed)
            self.assertEqual(caught.exception.code, "report_attachment_error")

    def test_rehashed_approval_with_changed_inputs_or_rules_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _, _, _, approved = self.approved(root)
            lower, _ = saved_rules(root, study, [rule(threshold=100)], name="lower.json")
            original = read_research(approved)
            mutations = [(("inputs", "question"), "Changed"), (("revision",), original["revision"] + 1),
                         (("research_id",), "0" * 32), (("rules",), {"path": str(lower.path), "sha256": lower.sha256}),
                         (("approval", "proposal_sha256"), "0" * 64), (("approval", "user_reply"), "")]
            for index, (keys, value) in enumerate(mutations):
                with self.subTest(keys=keys):
                    state = deepcopy(original)
                    target = state
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = value
                    bad = save_json_artifact(state, root / f"tampered-{index}.json")
                    with self.assertRaises(PageviewsError) as caught:
                        research_report(bad)
                    self.assertEqual(caught.exception.code, "research_state_error")

    def test_changed_parent_or_referenced_data_blocks_resume_without_repair(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _, _, pending, approved = self.approved(root)
            body = pending.path.read_bytes()
            pending.path.write_bytes(body + b"\n")
            with self.assertRaises(PageviewsError):
                research_report(approved)
            pending.path.write_bytes(body)
            study.path.write_bytes(study.path.read_bytes() + b"\n")
            with self.assertRaises(PageviewsError):
                research_report(approved)

    def test_malformed_state_and_checksum_return_structured_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, initial, _, _ = self.approved(root)
            valid = initial.path.read_bytes()
            for checksum in ("é" * 64, "0", "0" * 64):
                with self.subTest(checksum=checksum), self.assertRaises(PageviewsError):
                    read_research(JsonArtifact(initial.path, checksum))
            for body in (b"[]", b"{}", b"{bad", b'{"state_version":1,' + valid[1:], b'{"x":NaN,' + valid[1:]):
                initial.path.write_bytes(body)
                with self.subTest(body=body[:20]), self.assertRaises(PageviewsError):
                    read_research(JsonArtifact(initial.path, hashlib.sha256(body).hexdigest()))

    def test_invalid_transition_or_reply_does_not_publish(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, initial, pending, approved = self.approved(root)
            for ref in (initial, approved):
                with self.subTest(status=read_research(ref)["status"]), self.assertRaises(PageviewsError):
                    approve_criteria(ref, confirmation=ref.sha256, user_reply="Так.", output=root / "bad.json")
            for reply in ("", "   ", "x" * 2001, "\ud800"):
                with self.subTest(reply=repr(reply)), self.assertRaises(PageviewsError):
                    approve_criteria(pending, confirmation=pending.sha256, user_reply=reply, output=root / "bad.json")
            self.assertFalse((root / "bad.json").exists())

    def test_revisions_store_references_not_an_ever_growing_transcript(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            ref = create_research(study, question=QUESTION, output=root / "initial.json")
            sizes = []
            for index in range(15):
                ref = revise_research(ref, question=f"Пов'язане питання {index}", output=root / f"revision-{index:02}.json")
                sizes.append(ref.path.stat().st_size)
            state = read_research(ref)
            self.assertEqual(state["revision"], 15)
            self.assertNotIn("history", state)
            self.assertLess(max(sizes) - min(sizes), 100)
            self.assertEqual(research_report(ref)["question"], "Пов'язане питання 14")

    def test_existing_output_and_oversized_state_do_not_overwrite(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            ref = create_research(study, question=QUESTION, output=root / "initial.json")
            body = ref.path.read_bytes()
            with self.assertRaises(PageviewsError):
                revise_research(ref, question="Changed", output=ref.path)
            self.assertEqual(ref.path.read_bytes(), body)
            with patch("tools.pageviews.research_state.MAX_STATE_BYTES", 10), self.assertRaises(PageviewsError):
                create_research(study, question=QUESTION, output=root / "too-large.json")
            self.assertFalse((root / "too-large.json").exists())