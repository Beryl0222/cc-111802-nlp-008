from .records import TextRecord
from .text import annotate, normalize_text
from .topics import (
    classify_text,
    normalize_taxonomy,
    resolve_judgment,
    snapshot,
    utc_now,
    validate_timestamp,
)


class WorkflowError(ValueError):
    """工作流输入不满足口径或复核约束。"""


class TextWorkflow:
    def __init__(self, repository):
        self.repository = repository

    # ------------------------------------------------------------------ 记录
    def _state(self):
        return self.repository.read_state()

    def _save(self, state):
        self.repository.write_state(state)

    def ingest(self, values):
        if not isinstance(values, list):
            raise TypeError("导入内容必须是记录列表")
        incoming = [TextRecord.from_dict(value).to_dict() for value in values]
        state = self._state()
        current = {record["record_id"]: record for record in state["records"]}
        revisions = {item["revision_key"]: item for item in state["revisions"]}
        for record in incoming:
            previous = current.get(record["record_id"])
            if previous is not None and record["revision"] < previous["revision"]:
                continue
            normalized = normalize_text(record["text"])
            record["text"] = normalized
            record["analysis"] = annotate(record)
            current[record["record_id"]] = record
            revision_key = f"{record['record_id']}@{record['revision']}"
            entry = {
                "revision_key": revision_key,
                "record_id": record["record_id"],
                "revision": record["revision"],
                "content_digest": record["analysis"]["content_digest"],
                "text": normalized,
                "observed_at": record["observed_at"],
            }
            prior = revisions.get(revision_key)
            if prior is not None and prior["content_digest"] != entry["content_digest"]:
                # 同一修订号收到不同正文：保留冲突痕迹，不静默覆盖历史。
                entry["conflicts_with"] = prior["content_digest"]
                suffix = 1
                while f"{revision_key}#{suffix}" in revisions:
                    suffix += 1
                entry["revision_key"] = f"{revision_key}#{suffix}"
            revisions[entry["revision_key"]] = entry
            if previous is not None and (
                previous["analysis"].get("content_digest") != record["analysis"]["content_digest"]
            ):
                self._mark_stale(
                    state, record["record_id"], previous["revision"], record["revision"]
                )

        state["records"] = sorted(current.values(), key=lambda row: row["record_id"])
        state["revisions"] = sorted(revisions.values(), key=lambda row: row["revision_key"])
        self._save(state)
        return {"received": len(incoming), "stored": len(state["records"])}

    @staticmethod
    def _mark_stale(state, record_id, previous_revision, current_revision):
        for judgment in state["judgments"]:
            if judgment["record_id"] != record_id:
                continue
            if judgment["status"] in ("active", "needs_review"):
                judgment["status"] = "needs_review"
                if previous_revision == current_revision:
                    judgment["stale_reason"] = (
                        f"来源文本在修订 {current_revision} 下发生变化，相关判断需重新复核"
                    )
                else:
                    judgment["stale_reason"] = (
                        f"来源文本已从修订 {judgment['revision']} 更新到修订 "
                        f"{current_revision}（此前显示为修订 {previous_revision}），"
                        "相关判断需重新复核"
                    )

    def list_records(self, source=None):
        records = self._state()["records"]
        if source:
            records = [record for record in records if record["source"] == source]
        return records

    def revisions(self, record_id=None):
        values = self._state()["revisions"]
        if record_id:
            values = [row for row in values if row["record_id"] == record_id]
        return sorted(values, key=lambda row: (row["record_id"], row["revision"]))

    # ------------------------------------------------------------------ 口径
    def define_taxonomy(self, payload, activate=True):
        state = self._state()
        taxonomy = normalize_taxonomy(payload, state["taxonomies"])
        if taxonomy["taxonomy_id"] in state["taxonomies"]:
            raise WorkflowError(f"主题口径已存在且不可修改：{taxonomy['taxonomy_id']}")
        state["taxonomies"][taxonomy["taxonomy_id"]] = taxonomy
        if activate or state["active_taxonomy"] is None:
            state["active_taxonomy"] = taxonomy["taxonomy_id"]
        self._save(state)
        return snapshot(taxonomy)

    def activate_taxonomy(self, taxonomy_id):
        state = self._state()
        if taxonomy_id not in state["taxonomies"]:
            raise WorkflowError(f"主题口径不存在：{taxonomy_id}")
        state["active_taxonomy"] = taxonomy_id
        self._save(state)
        return snapshot(state["taxonomies"][taxonomy_id])

    def list_taxonomies(self):
        state = self._state()
        return [
            snapshot(state["taxonomies"][taxonomy_id])
            for taxonomy_id in sorted(state["taxonomies"])
        ]

    def get_taxonomy(self, taxonomy_id=None):
        state = self._state()
        taxonomy_id = taxonomy_id or state["active_taxonomy"]
        if not taxonomy_id:
            raise WorkflowError("尚未定义主题口径")
        if taxonomy_id not in state["taxonomies"]:
            raise WorkflowError(f"主题口径不存在：{taxonomy_id}")
        return snapshot(state["taxonomies"][taxonomy_id])

    def _taxonomy(self, state, taxonomy_id):
        taxonomy_id = taxonomy_id or state["active_taxonomy"]
        if not taxonomy_id:
            raise WorkflowError("尚未定义主题口径")
        taxonomy = state["taxonomies"].get(taxonomy_id)
        if taxonomy is None:
            raise WorkflowError(f"主题口径不存在：{taxonomy_id}")
        return taxonomy

    # ------------------------------------------------------------------ 判断
    @staticmethod
    def _next_judgment_id(state):
        numbers = []
        for judgment in state["judgments"]:
            value = judgment["judgment_id"]
            if value.startswith("J") and value[1:].isdigit():
                numbers.append(int(value[1:]))
        return f"J{max(numbers, default=0) + 1:06d}"

    def _new_judgment(self, state, record, taxonomy_id, topic_ids, origin, **extra):
        judgment = {
            "judgment_id": self._next_judgment_id(state),
            "record_id": record["record_id"],
            "taxonomy_id": taxonomy_id,
            "topic_ids": list(topic_ids),
            "origin": origin,
            "status": extra.pop("status", "active"),
            "content_digest": record["analysis"]["content_digest"],
            "revision": record["revision"],
            "created_at": extra.pop("created_at", None) or utc_now(),
            "reviewer": extra.pop("reviewer", ""),
            "note": extra.pop("note", ""),
            "supersedes": extra.pop("supersedes", None),
            "source_taxonomy_id": extra.pop("source_taxonomy_id", None),
            "carried_from": extra.pop("carried_from", None),
            "previous_topic_ids": extra.pop("previous_topic_ids", None),
            "stale_reason": extra.pop("stale_reason", None),
        }
        if extra:
            raise WorkflowError(f"未知判断字段：{', '.join(sorted(extra))}")
        state["judgments"].append(judgment)
        return judgment

    def _active_manual(self, state, record, taxonomy_id):
        for judgment in reversed(state["judgments"]):
            if (
                judgment["record_id"] == record["record_id"]
                and judgment["taxonomy_id"] == taxonomy_id
                and judgment["origin"] == "manual"
                and judgment["status"] == "active"
                and judgment["content_digest"] == record["analysis"]["content_digest"]
            ):
                return judgment
        return None

    def _needs_review(self, state, record, taxonomy_id):
        return [
            judgment for judgment in state["judgments"]
            if judgment["record_id"] == record["record_id"]
            and judgment["taxonomy_id"] == taxonomy_id
            and judgment["status"] == "needs_review"
        ]

    def _supersede_active(self, state, record_id, taxonomy_id):
        for judgment in state["judgments"]:
            if (
                judgment["record_id"] == record_id
                and judgment["taxonomy_id"] == taxonomy_id
                and judgment["status"] in ("active", "needs_review")
            ):
                judgment["status"] = "superseded"

    @staticmethod
    def _ancestry(state, taxonomy_id):
        """从直接上级开始，返回口径的继承链。"""
        chain = []
        seen = set()
        current = state["taxonomies"].get(taxonomy_id)
        while current is not None and current.get("supersedes"):
            parent_id = current["supersedes"]
            if parent_id in seen or parent_id not in state["taxonomies"]:
                break
            seen.add(parent_id)
            parent = state["taxonomies"][parent_id]
            chain.append(parent)
            current = parent
        return chain

    @classmethod
    def _topic_sources(cls, state, taxonomy_id):
        """目标口径中每个主题对应的全部上级主题编号（跨多层追溯）。"""
        lineage = [state["taxonomies"][taxonomy_id]] + list(
            reversed(cls._ancestry(state, taxonomy_id))
        )
        mapping = {}
        for topic in lineage[0]["topics"]:
            sources = set(topic["source_topic_ids"])
            frontier = list(sources)
            while frontier:
                source_id = frontier.pop()
                for parent in lineage[1:]:
                    for parent_topic in parent["topics"]:
                        if parent_topic["topic_id"] != source_id:
                            continue
                        for inherited in parent_topic["source_topic_ids"]:
                            if inherited not in sources:
                                sources.add(inherited)
                                frontier.append(inherited)
            mapping[topic["topic_id"]] = sources
        return mapping

    @classmethod
    def _translate_topics(cls, state, taxonomy_id, old_topic_ids):
        """按主题来源把旧口径的人工归类映射到新口径。

        返回 ``(translated, ambiguous)``：旧主题在新口径中对应多个主题时视为
        歧义，必须人工确认，不能悄悄沿用。
        """
        if not old_topic_ids:
            return [], False
        mapping = cls._topic_sources(state, taxonomy_id)
        translated = []
        ambiguous = False
        for old_topic_id in old_topic_ids:
            candidates = [
                topic_id for topic_id, sources in mapping.items()
                if old_topic_id in sources
            ]
            if len(candidates) != 1:
                ambiguous = True
                continue
            if candidates[0] not in translated:
                translated.append(candidates[0])
        return translated, ambiguous

    def _carry_manual(self, state, record, taxonomy, current_digest):
        """把上级口径中仍匹配当前正文的人工确认带入本口径。"""
        ancestors = {item["taxonomy_id"] for item in self._ancestry(
            state, taxonomy["taxonomy_id"]
        )}
        if not ancestors:
            return None, False
        candidates = [
            judgment for judgment in state["judgments"]
            if judgment["record_id"] == record["record_id"]
            and judgment["taxonomy_id"] in ancestors
            and judgment["origin"] == "manual"
            and judgment["status"] in ("active", "needs_review")
            and (
                judgment["status"] == "needs_review"
                or judgment["content_digest"] == current_digest
            )
        ]
        if not candidates:
            return None, False
        previous = max(candidates, key=lambda item: item["judgment_id"])
        translated, ambiguous = self._translate_topics(
            state, taxonomy["taxonomy_id"], previous["topic_ids"]
        )
        # 上级口径中的人工确认优先于本口径既有的自动判断。
        self._supersede_active(state, record["record_id"], taxonomy["taxonomy_id"])
        # 新口径下的判断只保存本口径的主题编号；旧主题编号单独留档。
        carried_topics = translated if not ambiguous else []
        if previous["status"] == "needs_review":
            judgment = self._new_judgment(
                state, record, taxonomy["taxonomy_id"], carried_topics, "manual",
                status="needs_review",
                reviewer=previous["reviewer"],
                note="来源文本已修订，原人工归类需要重新确认",
                supersedes=previous["judgment_id"],
                source_taxonomy_id=previous["taxonomy_id"],
                carried_from=previous["judgment_id"],
                previous_topic_ids=list(previous["topic_ids"]),
                stale_reason=previous.get("stale_reason") or (
                    "来源文本已修订或口径已调整，原人工归类需要重新确认"
                ),
            )
            return judgment, False
        if ambiguous:
            judgment = self._new_judgment(
                state, record, taxonomy["taxonomy_id"], [], "manual",
                status="needs_review",
                reviewer=previous["reviewer"],
                note="口径调整后原人工归类存在多种对应，需要重新确认",
                supersedes=previous["judgment_id"],
                source_taxonomy_id=previous["taxonomy_id"],
                carried_from=previous["judgment_id"],
                previous_topic_ids=list(previous["topic_ids"]),
                stale_reason=(
                    f"主题口径已从 {previous['taxonomy_id']} 调整为 "
                    f"{taxonomy['taxonomy_id']}，原人工归类需重新确认"
                ),
            )
            return judgment, False
        judgment = self._new_judgment(
            state, record, taxonomy["taxonomy_id"], translated, "manual",
            reviewer=previous["reviewer"],
            note=f"沿用人工资归：{previous['judgment_id']}",
            supersedes=previous["judgment_id"],
            source_taxonomy_id=previous["taxonomy_id"],
            carried_from=previous["judgment_id"],
            previous_topic_ids=list(previous["topic_ids"]),
        )
        return judgment, True

    def analyze(self, taxonomy_id=None, reviewer="automatic"):
        """按当前口径重新计算自动判断；人工判断与待复核判断不被覆盖。"""
        state = self._state()
        taxonomy = self._taxonomy(state, taxonomy_id)
        taxonomy_id = taxonomy["taxonomy_id"]
        created = 0
        skipped_manual = 0
        carried = 0
        awaiting_manual_review = 0
        waiting = 0
        for record in state["records"]:
            current_digest = record["analysis"]["content_digest"]
            if self._active_manual(state, record, taxonomy_id):
                skipped_manual += 1
                continue
            target_reviews = self._needs_review(state, record, taxonomy_id)
            if target_reviews:
                waiting += 1
                continue
            carried_judgment, translated = self._carry_manual(
                state, record, taxonomy, current_digest
            )
            if carried_judgment is not None:
                if translated:
                    carried += 1
                else:
                    awaiting_manual_review += 1
                continue
            topic_ids = classify_text(record["text"], taxonomy["topics"])
            existing = [
                judgment for judgment in state["judgments"]
                if judgment["record_id"] == record["record_id"]
                and judgment["taxonomy_id"] == taxonomy_id
                and judgment["origin"] == "automatic"
                and judgment["status"] == "active"
                and judgment["content_digest"] == current_digest
            ]
            if existing:
                if existing[-1]["topic_ids"] == topic_ids:
                    continue
                self._supersede_active(state, record["record_id"], taxonomy_id)
            self._new_judgment(
                state, record, taxonomy_id, topic_ids, "automatic", reviewer=reviewer
            )
            created += 1
        self._save(state)
        return {
            "taxonomy_id": taxonomy_id,
            "analyzed": len(state["records"]),
            "automatic_judgments": created,
            "manual_preserved": skipped_manual,
            "manual_carried_forward": carried,
            "manual_needs_confirmation": awaiting_manual_review,
            "awaiting_review": waiting,
        }

    def judge(self, record_id, topic_ids, taxonomy_id=None, reviewer="", note="", created_at=None):
        """人工确认归类；立即成为该记录在当前口径下的有效判断。"""
        state = self._state()
        taxonomy = self._taxonomy(state, taxonomy_id)
        taxonomy_id = taxonomy["taxonomy_id"]
        record = next(
            (item for item in state["records"] if item["record_id"] == record_id), None
        )
        if record is None:
            raise WorkflowError(f"记录不存在：{record_id}")
        known = {topic["topic_id"] for topic in taxonomy["topics"]}
        if isinstance(topic_ids, str):
            topic_ids = topic_ids.split(",")
        normalized_topic_ids = []
        for item in topic_ids or []:
            topic_id = str(item).strip()
            if topic_id and topic_id not in normalized_topic_ids:
                normalized_topic_ids.append(topic_id)
        unknown = [item for item in normalized_topic_ids if item not in known]
        if unknown:
            raise WorkflowError(f"主题不在当前口径中：{', '.join(unknown)}")
        reviewer = str(reviewer).strip()
        if not reviewer:
            raise WorkflowError("人工确认必须记录复核人")
        if created_at is not None:
            validate_timestamp(created_at, "判断时间")
        self._supersede_active(state, record_id, taxonomy_id)
        judgment = self._new_judgment(
            state, record, taxonomy_id, normalized_topic_ids, "manual",
            reviewer=reviewer, note=str(note).strip(), created_at=created_at,
        )
        self._save(state)
        return snapshot(judgment)

    def review_queue(self, taxonomy_id=None):
        state = self._state()
        taxonomy = self._taxonomy(state, taxonomy_id)
        taxonomy_id = taxonomy["taxonomy_id"]
        assignments, pending, unjudged = resolve_judgment(
            state["records"], state["judgments"], taxonomy_id
        )
        rows = []
        for record in state["records"]:
            record_id = record["record_id"]
            if record_id in pending:
                stale = pending[record_id]
                rows.append({
                    "record_id": record_id,
                    "revision": record["revision"],
                    "text": record["text"],
                    "content_digest": record["analysis"]["content_digest"],
                    "reason": stale.get("stale_reason", "判断等待复核"),
                    "previous_topic_ids": stale.get("previous_topic_ids") or stale["topic_ids"],
                    "previous_origin": stale["origin"],
                    "previous_judgment_id": stale["judgment_id"],
                    "previous_taxonomy_id": stale.get("source_taxonomy_id"),
                    "suggested_topic_ids": classify_text(record["text"], taxonomy["topics"]),
                })
            elif record_id in unjudged:
                rows.append({
                    "record_id": record_id,
                    "revision": record["revision"],
                    "text": record["text"],
                    "content_digest": record["analysis"]["content_digest"],
                    "reason": "尚未判断",
                    "previous_topic_ids": [],
                    "previous_origin": None,
                    "previous_judgment_id": None,
                    "previous_taxonomy_id": None,
                    "suggested_topic_ids": classify_text(record["text"], taxonomy["topics"]),
                })
        return rows

    def list_judgments(self, record_id=None, taxonomy_id=None):
        values = self._state()["judgments"]
        if record_id:
            values = [row for row in values if row["record_id"] == record_id]
        if taxonomy_id:
            values = [row for row in values if row["taxonomy_id"] == taxonomy_id]
        return sorted(snapshot(values), key=lambda row: row["judgment_id"])

    # ------------------------------------------------------------------ 报告
    def build_report(self, taxonomy_id=None, title="", created_at=None):
        """冻结当前口径下的统计与逐条依据。

        报告保存口径快照和每条意见的判断摘要，之后主题合并、拆分或重新
        分析都不会改写已经发出的报告。
        """
        state = self._state()
        taxonomy = self._taxonomy(state, taxonomy_id)
        taxonomy_id = taxonomy["taxonomy_id"]
        if created_at is not None:
            validate_timestamp(created_at, "报告时间")
        assignments, pending, unjudged = resolve_judgment(
            state["records"], state["judgments"], taxonomy_id
        )
        topics = {topic["topic_id"]: topic for topic in taxonomy["topics"]}
        counts = {topic_id: 0 for topic_id in topics}
        assignment_rows = []
        opinions_in_topic = 0
        opinions_without_topic = 0
        for record in state["records"]:
            record_id = record["record_id"]
            judgment = assignments.get(record_id)
            if judgment is None:
                continue
            topic_ids = [topic_id for topic_id in judgment["topic_ids"] if topic_id in topics]
            if topic_ids:
                opinions_in_topic += 1
            else:
                opinions_without_topic += 1
            for topic_id in topic_ids:
                counts[topic_id] += 1
            assignment_rows.append({
                "record_id": record_id,
                "revision": record["revision"],
                "content_digest": record["analysis"]["content_digest"],
                "text": record["text"],
                "observed_at": record["observed_at"],
                "topic_ids": topic_ids,
                "topic_names": [topics[topic_id]["name"] for topic_id in topic_ids],
                "origin": judgment["origin"],
                "judgment_id": judgment["judgment_id"],
                "reviewer": judgment["reviewer"],
            })
        pending_rows = []
        for record in state["records"]:
            record_id = record["record_id"]
            if record_id not in pending:
                continue
            stale = pending[record_id]
            pending_rows.append({
                "record_id": record_id,
                "revision": record["revision"],
                "content_digest": record["analysis"]["content_digest"],
                "text": record["text"],
                "reason": stale.get("stale_reason", "判断等待复核"),
                "previous_topic_ids": stale.get("previous_topic_ids") or stale["topic_ids"],
                "previous_judgment_id": stale["judgment_id"],
                "previous_taxonomy_id": stale.get("source_taxonomy_id"),
            })
        unassigned = list(unjudged)
        excluded = [
            row["record_id"] for row in assignment_rows
            if not row["topic_ids"] and row["origin"] == "manual"
        ]
        report_id = self._next_report_id(state)
        report = {
            "report_id": report_id,
            "title": str(title).strip() or f"主题统计 {taxonomy_id}",
            "created_at": created_at or utc_now(),
            "taxonomy_id": taxonomy_id,
            "taxonomy_version": taxonomy["version"],
            "supersedes_taxonomy_id": taxonomy["supersedes"],
            "taxonomy_lineage": [
                item["taxonomy_id"] for item in reversed(self._ancestry(state, taxonomy_id))
            ] + [taxonomy_id],
            "taxonomy": snapshot(taxonomy),
            "totals": {
                "records": len(state["records"]),
                "opinions_in_topic": opinions_in_topic,
                "opinions_without_topic": opinions_without_topic,
                "excluded": len(excluded),
                "pending_review": len(pending_rows),
                "unjudged": len(unassigned),
                "topic_assignments": sum(counts.values()),
            },
            "topics": [
                {
                    "topic_id": topic["topic_id"],
                    "name": topic["name"],
                    "count": counts[topic["topic_id"]],
                    "source_topic_ids": list(topic["source_topic_ids"]),
                    "keywords": list(topic["keywords"]),
                }
                for topic in taxonomy["topics"]
            ],
            "assignments": assignment_rows,
            "excluded_record_ids": sorted(excluded),
            "pending_review": pending_rows,
            "unassigned_record_ids": sorted(unassigned),
        }
        state["reports"].append(report)
        self._save(state)
        return snapshot(report)

    @staticmethod
    def _next_report_id(state):
        numbers = []
        for report in state["reports"]:
            value = report["report_id"]
            if value.startswith("R") and value[1:].isdigit():
                numbers.append(int(value[1:]))
        return f"R{max(numbers, default=0) + 1:04d}"

    def list_reports(self):
        state = self._state()
        return [
            {
                "report_id": report["report_id"],
                "title": report["title"],
                "created_at": report["created_at"],
                "taxonomy_id": report["taxonomy_id"],
                "taxonomy_version": report["taxonomy_version"],
                "totals": snapshot(report["totals"]),
            }
            for report in state["reports"]
        ]

    def get_report(self, report_id):
        for report in self._state()["reports"]:
            if report["report_id"] == report_id:
                return snapshot(report)
        raise WorkflowError(f"报告不存在：{report_id}")
