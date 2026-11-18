import json
import tempfile
import unittest
from pathlib import Path

from nlp_teacher_feedback_topics.repository import RecordRepository
from nlp_teacher_feedback_topics.text import content_digest, normalize_text, tokenize
from nlp_teacher_feedback_topics.workflow import TextWorkflow, WorkflowError


V1 = {
    "taxonomy_id": "topics-v1",
    "version": 1,
    "created_at": "2026-11-18T10:00:00Z",
    "topics": [
        {"topic_id": "questioning", "name": "课堂提问", "keywords": ["课堂提问", "提问"]},
        {"topic_id": "question-chain", "name": "问题链设计", "keywords": ["问题链"]},
    ],
}

V2_MERGED = {
    "taxonomy_id": "topics-v2-merged",
    "version": 2,
    "supersedes": "topics-v1",
    "created_at": "2026-11-19T10:00:00Z",
    "topics": [
        {"topic_id": "question-design", "name": "问题设计", "keywords": ["提问", "问题链"],
         "source_topic_ids": ["questioning", "question-chain"]},
    ],
}


def record(record_id, text, revision=1):
    return {
        "record_id": record_id,
        "source": "school-synthetic",
        "revision": revision,
        "observed_at": "2026-11-18T09:00:00Z",
        "text": text,
        "context": "教研演练",
    }


class TextWorkflowTests(unittest.TestCase):
    def test_normalization_and_tokenization(self):
        self.assertEqual(normalize_text("  Ａ  B\nC  "), "A B C")
        self.assertEqual(tokenize("语文课 A2。"), ["语", "文", "课", "A", "2", "。"])

    def test_ingest_persists_analysis_and_uses_newer_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = TextWorkflow(RecordRepository(Path(directory) / "records.json"))
            first = {"record_id": "lesson-1", "source": "synthetic", "revision": 1,
                     "observed_at": "2026-11-18T09:00:00Z", "text": "原稿 甲乙", "context": "课堂"}
            second = {**first, "revision": 2, "text": "修订稿 乙丙"}
            self.assertEqual(workflow.ingest([first]), {"received": 1, "stored": 1})
            workflow.ingest([second, first])
            result = workflow.list_records()[0]
            self.assertEqual(result["revision"], 2)
            self.assertEqual(result["text"], "修订稿 乙丙")
            self.assertEqual(result["analysis"]["token_count"], 5)

    def test_invalid_records_are_rejected_without_partial_write(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = RecordRepository(Path(directory) / "records.json")
            workflow = TextWorkflow(repository)
            valid = {"record_id": "one", "source": "synthetic", "revision": 1,
                     "observed_at": "2026-11-18T09:00:00Z", "text": "有效文本"}
            workflow.ingest([valid])
            invalid = {**valid, "record_id": "two", "observed_at": "bad-time"}
            with self.assertRaises(ValueError):
                workflow.ingest([valid, invalid])
            self.assertEqual([row["record_id"] for row in repository.read()], ["one"])

    def test_source_filter_and_fixture_are_local(self):
        fixture = json.loads(Path("fixtures/records.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            workflow = TextWorkflow(RecordRepository(Path(directory) / "records.json"))
            workflow.ingest(fixture)
            self.assertEqual(len(workflow.list_records("school-synthetic")), 3)
            self.assertEqual(workflow.list_records("missing"), [])


class TopicTraceabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = RecordRepository(Path(self.directory.name) / "records.json")
        self.workflow = TextWorkflow(self.repository)
        self.workflow.ingest([
            record("a", "课堂提问要留出等待时间。"),
            record("b", "问题链设计要有层次。"),
            record("c", "提问和问题链都要贴近课文语境。"),
            record("d", "作业量需要协调。"),
        ])
        self.workflow.define_taxonomy(V1)

    def test_automatic_judgments_keep_source_text_and_multi_membership(self):
        self.workflow.analyze()
        judgments = {row["record_id"]: row for row in self.workflow.list_judgments("c")}
        self.assertEqual(judgments["c"]["topic_ids"], ["questioning", "question-chain"])
        self.assertEqual(judgments["c"]["origin"], "automatic")
        digest = content_digest("提问和问题链都要贴近课文语境。")
        self.assertEqual(judgments["c"]["content_digest"], digest)

    def test_report_deduplicates_opinion_total_across_topics(self):
        self.workflow.analyze()
        report = self.workflow.build_report()
        counts = {row["topic_id"]: row["count"] for row in report["topics"]}
        self.assertEqual(counts["questioning"], 2)
        self.assertEqual(counts["question-chain"], 2)
        self.assertEqual(report["totals"]["topic_assignments"], 4)
        self.assertEqual(report["totals"]["opinions_in_topic"], 3)
        self.assertEqual(report["totals"]["records"], 4)
        self.assertEqual(report["totals"]["opinions_without_topic"], 1)
        self.assertEqual([row["record_id"] for row in report["assignments"]], ["a", "b", "c", "d"])
        self.assertEqual(report["assignments"][3]["topic_ids"], [])

    def test_manual_judgment_wins_over_reanalysis(self):
        self.workflow.analyze()
        manual = self.workflow.judge("d", ["question-chain"], reviewer="教研员甲", note="语境补充")
        self.assertEqual(manual["origin"], "manual")
        self.workflow.analyze()
        report = self.workflow.build_report()
        counts = {row["topic_id"]: row["count"] for row in report["topics"]}
        self.assertEqual(counts["question-chain"], 3)
        assignment = next(row for row in report["assignments"] if row["record_id"] == "d")
        self.assertEqual(assignment["judgment_id"], manual["judgment_id"])
        self.assertEqual(assignment["origin"], "manual")

    def test_manual_judgment_can_exclude_record_from_all_topics(self):
        self.workflow.analyze()
        self.workflow.judge("c", [], reviewer="教研员甲", note="重复意见")
        report = self.workflow.build_report()
        self.assertEqual(report["totals"]["opinions_in_topic"], 2)
        self.assertEqual(report["totals"]["topic_assignments"], 2)
        self.assertEqual(report["totals"]["excluded"], 1)
        self.assertEqual(report["totals"]["opinions_without_topic"], 2)
        self.assertEqual(report["excluded_record_ids"], ["c"])
        self.assertEqual(report["unassigned_record_ids"], [])

    def test_source_revision_sends_judgments_back_to_review(self):
        self.workflow.analyze()
        self.workflow.judge("a", ["question-chain"], reviewer="教研员甲")
        self.workflow.ingest([record("a", "课堂提问需要更明确的指向。", revision=2)])
        queue = {row["record_id"]: row for row in self.workflow.review_queue()}
        self.assertIn("a", queue)
        self.assertEqual(queue["a"]["previous_topic_ids"], ["question-chain"])
        self.assertEqual(queue["a"]["suggested_topic_ids"], ["questioning"])
        self.assertEqual(queue["a"]["revision"], 2)
        self.workflow.analyze()
        self.assertIn("a", {row["record_id"] for row in self.workflow.review_queue()})
        report = self.workflow.build_report()
        self.assertEqual(report["totals"]["pending_review"], 1)
        self.assertNotIn("a", {row["record_id"] for row in report["assignments"]})

    def test_unchanged_text_across_revision_does_not_invalidate(self):
        self.workflow.analyze()
        self.workflow.ingest([record("a", "课堂提问要留出等待时间。", revision=2)])
        self.assertEqual(self.workflow.review_queue(), [])

    def test_taxonomy_versions_are_immutable_and_explain_merge(self):
        with self.assertRaises(WorkflowError):
            self.workflow.define_taxonomy(V1)
        merged = self.workflow.define_taxonomy(V2_MERGED)
        self.assertEqual(merged["supersedes"], "topics-v1")
        self.assertEqual(merged["topics"][0]["source_topic_ids"], ["questioning", "question-chain"])
        self.assertEqual(len(self.workflow.list_taxonomies()), 2)

    def test_merge_reanalysis_links_new_topics_to_old_sources(self):
        self.workflow.analyze()
        self.workflow.define_taxonomy(V2_MERGED)
        self.workflow.analyze()
        report = self.workflow.build_report()
        self.assertEqual(report["taxonomy_id"], "topics-v2-merged")
        self.assertEqual(report["topics"][0]["topic_id"], "question-design")
        self.assertEqual(report["topics"][0]["count"], 3)
        old_ids = set(report["topics"][0]["source_topic_ids"])
        self.assertEqual(old_ids, {"questioning", "question-chain"})
        self.assertEqual(report["totals"]["topic_assignments"], 3)
        self.assertEqual(report["totals"]["opinions_in_topic"], 3)

    def test_issued_report_keeps_old_taxonomy_after_new_analysis(self):
        self.workflow.analyze()
        old_report = self.workflow.build_report(title="旧口径报告")
        self.workflow.define_taxonomy(V2_MERGED)
        self.workflow.analyze()
        self.workflow.build_report(title="新口径报告")
        frozen = self.workflow.get_report(old_report["report_id"])
        self.assertEqual(frozen["taxonomy_id"], "topics-v1")
        self.assertEqual([topic["topic_id"] for topic in frozen["topics"]],
                         ["questioning", "question-chain"])
        self.assertEqual(frozen["totals"], old_report["totals"])
        self.assertEqual(frozen["assignments"], old_report["assignments"])
        self.assertEqual(frozen["taxonomy"]["taxonomy_id"], "topics-v1")
        self.assertEqual(frozen["taxonomy"]["version"], 1)
        self.assertEqual(frozen["taxonomy"]["supersedes"], None)

    def test_split_does_not_double_count_shared_opinions(self):
        split = {
            "taxonomy_id": "topics-v2-split",
            "version": 2,
            "supersedes": "topics-v1",
            "created_at": "2026-11-19T10:00:00Z",
            "topics": [
                {"topic_id": "classroom-questioning", "name": "课堂提问",
                 "keywords": ["课堂提问"], "source_topic_ids": ["questioning"]},
                {"topic_id": "question-chain-design", "name": "问题链设计",
                 "keywords": ["问题链"], "source_topic_ids": ["question-chain"]},
            ],
        }
        self.workflow.analyze()
        self.workflow.define_taxonomy(split)
        self.workflow.analyze()
        report = self.workflow.build_report()
        counts = {row["topic_id"]: row["count"] for row in report["topics"]}
        self.assertEqual(counts["classroom-questioning"], 1)
        self.assertEqual(counts["question-chain-design"], 2)
        self.assertEqual(report["totals"]["topic_assignments"], 3)
        self.assertEqual(report["totals"]["opinions_in_topic"], 3)

    def test_manual_assignment_carries_across_merged_taxonomy(self):
        self.workflow.analyze()
        manual = self.workflow.judge("d", ["question-chain"], reviewer="教研员甲")
        self.workflow.define_taxonomy(V2_MERGED)
        result = self.workflow.analyze()
        self.assertEqual(result["manual_carried_forward"], 1)
        report = self.workflow.build_report()
        self.assertEqual(report["topics"][0]["count"], 4)
        assignment = next(row for row in report["assignments"] if row["record_id"] == "d")
        self.assertEqual(assignment["origin"], "manual")
        history = self.workflow.list_judgments("d", "topics-v2-merged")
        self.assertEqual(assignment["judgment_id"], history[0]["judgment_id"])
        self.assertEqual(history[0]["carried_from"], manual["judgment_id"])
        self.assertEqual(history[0]["source_taxonomy_id"], "topics-v1")
        self.assertEqual(history[0]["topic_ids"], ["question-design"])

    def test_manual_assignment_waits_for_confirmation_when_split_is_ambiguous(self):
        split = {
            "taxonomy_id": "topics-v2-split-ambiguous",
            "version": 2,
            "supersedes": "topics-v1",
            "created_at": "2026-11-19T10:00:00Z",
            "topics": [
                {"topic_id": "questioning-a", "name": "课堂提问", "keywords": ["课堂提问"],
                 "source_topic_ids": ["questioning", "question-chain"]},
                {"topic_id": "questioning-b", "name": "问题链设计", "keywords": ["问题链"],
                 "source_topic_ids": ["questioning", "question-chain"]},
            ],
        }
        self.workflow.analyze()
        self.workflow.judge("c", ["questioning", "question-chain"], reviewer="教研员甲")
        self.workflow.define_taxonomy(split)
        result = self.workflow.analyze()
        self.assertEqual(result["manual_needs_confirmation"], 1)
        queue = {row["record_id"]: row for row in self.workflow.review_queue()}
        self.assertIn("c", queue)
        self.assertEqual(queue["c"]["previous_topic_ids"], ["questioning", "question-chain"])
        report = self.workflow.build_report()
        self.assertEqual(report["totals"]["pending_review"], 1)

    def test_stale_manual_decision_stays_in_review_after_taxonomy_change(self):
        self.workflow.analyze()
        self.workflow.judge("a", ["question-chain"], reviewer="教研员甲")
        self.workflow.ingest([record("a", "课堂提问需要更明确的指向。", revision=2)])
        self.workflow.define_taxonomy(V2_MERGED)
        result = self.workflow.analyze()
        self.assertEqual(result["manual_needs_confirmation"], 1)
        queue = {row["record_id"]: row for row in self.workflow.review_queue()}
        self.assertIn("a", queue)
        report = self.workflow.build_report()
        self.assertEqual(report["totals"]["pending_review"], 1)
        self.assertNotIn("a", {row["record_id"] for row in report["assignments"]})

    def test_manual_decision_follows_multi_level_lineage(self):
        self.workflow.analyze()
        manual = self.workflow.judge("d", ["question-chain"], reviewer="教研员甲")
        self.workflow.define_taxonomy(V2_MERGED)
        v3 = {
            "taxonomy_id": "topics-v3",
            "version": 3,
            "supersedes": "topics-v2-merged",
            "created_at": "2026-11-20T10:00:00Z",
            "topics": [
                {"topic_id": "design-and-waiting", "name": "问题设计与等待时间",
                 "keywords": ["提问", "问题链", "等待"],
                 "source_topic_ids": ["question-design"]},
            ],
        }
        self.workflow.define_taxonomy(v3)
        result = self.workflow.analyze()
        self.assertEqual(result["manual_carried_forward"], 1)
        report = self.workflow.build_report()
        assignment = next(row for row in report["assignments"] if row["record_id"] == "d")
        self.assertEqual(assignment["topic_ids"], ["design-and-waiting"])
        history = self.workflow.list_judgments("d", "topics-v3")
        self.assertEqual(history[0]["carried_from"], manual["judgment_id"])
        self.assertEqual(report["taxonomy_lineage"],
                         ["topics-v1", "topics-v2-merged", "topics-v3"])

    def test_competing_taxonomies_can_be_compared_and_switched(self):
        split = json.loads(
            Path("fixtures/taxonomy-v2-split.json").read_text(encoding="utf-8")
        )
        self.workflow.analyze()
        self.workflow.define_taxonomy(V2_MERGED)
        self.workflow.define_taxonomy(split)
        merged_report = None
        for taxonomy_id in ("topics-v2-split", "topics-v2-merged"):
            self.workflow.activate_taxonomy(taxonomy_id)
            self.workflow.analyze()
            report = self.workflow.build_report()
            if taxonomy_id == "topics-v2-merged":
                merged_report = report
                self.assertEqual(report["topics"][0]["count"], 3)
            else:
                self.assertEqual(
                    {row["topic_id"]: row["count"] for row in report["topics"]},
                    {"classroom-questioning": 1, "question-chain-design": 2},
                )
        self.assertEqual(
            self.workflow.get_taxonomy()["taxonomy_id"], "topics-v2-merged"
        )
        self.assertEqual(merged_report["supersedes_taxonomy_id"], "topics-v1")
        self.assertEqual(len(self.workflow.list_reports()), 2)

    def test_same_revision_different_text_is_recorded_as_conflict(self):
        self.workflow.analyze()
        self.workflow.ingest([record("c", "问题链设计要有层次。")])
        self.assertEqual(len(self.workflow.revisions("c")), 2)
        current = self.workflow.revisions("c")[-1]
        self.assertEqual(current["text"], "问题链设计要有层次。")
        self.assertIn("conflicts_with", current)
        queue = {row["record_id"]: row for row in self.workflow.review_queue()}
        self.assertIn("c", queue)
        self.assertIn("修订 1 下发生变化", queue["c"]["reason"])

    def test_issued_report_keeps_original_text_after_later_revision(self):
        self.workflow.analyze()
        issued = self.workflow.build_report()
        self.workflow.ingest([record("a", "课堂提问需要更明确的指向。", revision=2)])
        self.workflow.analyze()
        frozen = self.workflow.get_report(issued["report_id"])
        row = next(item for item in frozen["assignments"] if item["record_id"] == "a")
        self.assertEqual(row["text"], "课堂提问要留出等待时间。")
        self.assertEqual(row["revision"], 1)
        self.assertEqual(frozen["pending_review"], [])
        self.assertEqual(frozen["totals"]["pending_review"], 0)
        self.assertEqual(self.workflow.review_queue()[0]["record_id"], "a")

    def test_judgment_history_is_append_only(self):
        self.workflow.analyze()
        first = self.workflow.list_judgments("a")
        self.workflow.judge("a", ["question-chain"], reviewer="教研员甲")
        self.workflow.judge("a", ["questioning"], reviewer="教研员乙")
        history = self.workflow.list_judgments("a")
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0]["judgment_id"], first[0]["judgment_id"])
        self.assertEqual(history[0]["status"], "superseded")
        self.assertEqual(history[-1]["status"], "active")
        self.assertEqual(history[-1]["reviewer"], "教研员乙")

    def test_invalid_manual_topic_is_rejected_without_write(self):
        self.workflow.analyze()
        before = self.workflow.list_judgments()
        with self.assertRaises(WorkflowError):
            self.workflow.judge("a", ["missing-topic"], reviewer="教研员甲")
        self.assertEqual(self.workflow.list_judgments(), before)

    def test_local_state_is_single_atomic_document(self):
        self.workflow.analyze()
        self.workflow.build_report()
        state = json.loads(self.repository.path.read_text(encoding="utf-8"))
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["active_taxonomy"], "topics-v1")
        self.assertEqual(len(state["records"]), 4)
        self.assertEqual(len(state["reports"]), 1)
        self.assertEqual(self.repository.read(), state["records"])

    def test_legacy_record_only_file_is_upgraded(self):
        legacy = Path(self.directory.name) / "legacy.json"
        legacy.write_text(json.dumps({"records": [record("old", "课堂提问")]}), encoding="utf-8")
        workflow = TextWorkflow(RecordRepository(legacy))
        workflow.ingest([record("old", "课堂提问", revision=2)])
        state = json.loads(legacy.read_text(encoding="utf-8"))
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["records"][0]["revision"], 2)


if __name__ == "__main__":
    unittest.main()
