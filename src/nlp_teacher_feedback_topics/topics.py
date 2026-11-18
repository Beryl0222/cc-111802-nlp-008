"""主题口径、判断与报告所需的纯函数。

这里的函数不写入存储，便于在测试和命令行之外复用。
"""

import json
from datetime import datetime, timezone

from .text import normalize_text


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_timestamp(value, label="时间"):
    if not value:
        raise ValueError(f"{label}不能为空")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label}格式无效：{value}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label}必须包含时区")
    return str(value)


def _string_list(value, label):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label}必须是字符串列表")
    result = []
    for item in value:
        item = str(item).strip()
        if not item:
            raise ValueError(f"{label}不能包含空值")
        if item not in result:
            result.append(item)
    return result


def normalize_taxonomy(payload, existing_taxonomies, version=None):
    """校验并冻结一份主题口径快照。

    主题定义一次写入后不再修改；合并或拆分通过新的 ``taxonomy_id`` 表达，
    旧口径因此始终能解释旧报告。
    """
    if not isinstance(payload, dict):
        raise ValueError("主题口径必须是对象")
    taxonomy_id = str(payload.get("taxonomy_id", "")).strip()
    if not taxonomy_id:
        raise ValueError("主题口径缺少 taxonomy_id")
    topics_value = payload.get("topics")
    if not isinstance(topics_value, list) or not topics_value:
        raise ValueError("主题口径至少需要一个主题")

    supersedes = payload.get("supersedes")
    if supersedes is not None:
        supersedes = str(supersedes).strip() or None
    if supersedes is not None and supersedes not in existing_taxonomies:
        raise ValueError(f"上级主题口径不存在：{supersedes}")
    old_topics = set()
    if supersedes:
        old_topics = {topic["topic_id"] for topic in existing_taxonomies[supersedes]["topics"]}

    topics = []
    seen = set()
    for value in topics_value:
        if not isinstance(value, dict):
            raise ValueError("主题必须是对象")
        topic_id = str(value.get("topic_id", "")).strip()
        name = str(value.get("name", "")).strip()
        if not topic_id or not name:
            raise ValueError("主题缺少 topic_id 或名称")
        if topic_id in seen:
            raise ValueError(f"主题编号重复：{topic_id}")
        seen.add(topic_id)
        keywords = _string_list(value.get("keywords"), f"{topic_id} 的关键词")
        if not keywords:
            keywords = [name]
        sources = _string_list(
            value.get("source_topic_ids", value.get("derived_from")),
            f"{topic_id} 的来源主题",
        )
        if sources and not supersedes:
            raise ValueError(f"{topic_id} 声明了来源主题，但新口径未指定 supersedes")
        if supersedes:
            unknown = [item for item in sources if item not in old_topics]
            if unknown:
                raise ValueError(f"{topic_id} 引用了上级口径中不存在的主题：{', '.join(unknown)}")
        topics.append({
            "topic_id": topic_id,
            "name": name,
            "keywords": keywords,
            "source_topic_ids": sources,
        })

    if version is None:
        version = payload.get("version")
    if version is None:
        version = max((item["version"] for item in existing_taxonomies.values()), default=0) + 1
    try:
        version = int(version)
    except (TypeError, ValueError) as error:
        raise ValueError("主题口径版本必须是正整数") from error
    if version < 1:
        raise ValueError("主题口径版本必须是正整数")

    note = str(payload.get("note", "")).strip()
    created_at = payload.get("created_at") or utc_now()
    return {
        "taxonomy_id": taxonomy_id,
        "version": version,
        "supersedes": supersedes,
        "note": note,
        "created_at": validate_timestamp(created_at, "主题口径创建时间"),
        "topics": topics,
    }


def classify_text(text, topics):
    """返回文本命中的全部主题。

    多主题归属是刻意保留的：同一条意见可以同时涉及多个主题。报告层用
    record_id 去重统计总量，因此多归属不会放大意见总数。
    """
    normalized = normalize_text(text).casefold()
    matched = []
    for topic in topics:
        for keyword in topic["keywords"]:
            needle = normalize_text(keyword).casefold()
            if needle and needle in normalized:
                matched.append(topic["topic_id"])
                break
    return matched


def resolve_judgment(records, judgments, taxonomy_id):
    """按记录解析当前有效判断。

    人工判断优先于自动判断；只有与当前正文摘要一致的判断才是有效判断。
    返回 ``(assignments, pending, unjudged)``。
    """
    by_key = {}
    for judgment in judgments:
        if judgment["taxonomy_id"] != taxonomy_id:
            continue
        if judgment["status"] not in ("active", "needs_review"):
            continue
        by_key.setdefault(judgment["record_id"], []).append(judgment)

    assignments = {}
    pending = {}
    unjudged = {}
    for record in records:
        record_id = record["record_id"]
        current_digest = record["analysis"]["content_digest"]
        candidates = [
            item for item in by_key.get(record_id, [])
            if item["content_digest"] == current_digest and item["status"] == "active"
        ]
        manual = [item for item in candidates if item["origin"] == "manual"]
        if manual:
            chosen = max(manual, key=lambda item: item["judgment_id"])
            assignments[record_id] = chosen
            continue
        automatic = [item for item in candidates if item["origin"] == "automatic"]
        if automatic:
            assignments[record_id] = max(automatic, key=lambda item: item["judgment_id"])
            continue
        reviews = [
            item for item in by_key.get(record_id, [])
            if item["status"] == "needs_review"
        ]
        if reviews:
            pending[record_id] = max(reviews, key=lambda item: item["judgment_id"])
        else:
            unjudged[record_id] = record
    return assignments, pending, unjudged


def snapshot(value):
    return json.loads(json.dumps(value, ensure_ascii=False))
