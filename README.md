# 教研反馈主题台

教研反馈主题台用于处理本地合成教研文本，保存来源版本、文本片段、主题判断和处理报告，供校内复核使用。

核心约束：

- **原文可追溯**：每条记录保留当前正文、内容摘要和历次修订；同一修订号收到不同正文时保留冲突痕迹。
- **主题口径有版本**：主题定义冻结后不可修改。合并、拆分或新需求都通过新的 `taxonomy_id` 和 `supersedes` 表达，旧口径始终可解释旧报告。
- **判断可追溯**：自动判断和人工确认都按追加方式保存，记录判断编号、正文摘要、修订号、复核人和来源口径；人工确认优先于自动重算。
- **来源修订触发复核**：正文变化后，相关判断进入待复核队列，不会被自动结果直接顶替。
- **跨主题多归属不重复计量**：一条意见可以同时归入多个主题；报告同时给出按主题的归属数（`topic_assignments`）和去重后的意见数（`opinions_in_topic`）。
- **报告冻结**：报告保存生成时的口径快照、逐条判断依据和待复核清单；之后重新分析不会改写已发出的报告。

## 环境

需要 Python 3.11 或更新版本。应用运行不依赖第三方包；运行测试和构建前，安装固定版本的开发工具：`python -m pip install -r requirements-dev.txt`。

## 运行

运行数据保存在 `var/records.json`（可用 `--state` 指定）。示例子命令：

```bash
PYTHONPATH=src python -m nlp_teacher_feedback_topics.cli ingest fixtures/records.json
PYTHONPATH=src python -m nlp_teacher_feedback_topics.cli define-taxonomy fixtures/taxonomy-v1.json
PYTHONPATH=src python -m nlp_teacher_feedback_topics.cli analyze
PYTHONPATH=src python -m nlp_teacher_feedback_topics.cli judge <record_id> question-chain --reviewer 教研员甲
PYTHONPATH=src python -m nlp_teacher_feedback_topics.cli report --title 首轮反馈
```

`fixtures/` 同时提供合并口径 `taxonomy-v2-merged.json` 与拆分口径 `taxonomy-v2-split.json`，可分别 `define-taxonomy` 后用 `activate-taxonomy` 切换比较。其他子命令：`list`、`revisions`、`taxonomies`、`review-queue`、`judgments`、`reports`、`show-report`。

## 数据模型

`var/records.json` 是单一原子写入的文档：

- `records`：当前记录（含 `analysis.content_digest`）。
- `revisions`：历次修订快照，按 `record_id@revision` 归档。
- `taxonomies`：冻结的主题口径版本，`topics[].source_topic_ids` 指向被合并或拆分的旧主题。
- `judgments`：只追加的判断事件，`origin` 为 `automatic` 或 `manual`，`status` 为 `active`、`needs_review` 或 `superseded`。
- `reports`：冻结报告，含口径快照、逐条意见证据、待复核清单和 `taxonomy_lineage`。

## 测试

执行 `python -m pytest -q` 检查文本规范化、记录处理、主题版本、人工优先、修订复核、多归属去重和报告冻结。
