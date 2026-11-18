import argparse
import json
from pathlib import Path

from .repository import RecordRepository
from .workflow import TextWorkflow


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(description="本地教研文本处理工具")
    parser.add_argument("--state", default="var/records.json")
    subparsers = parser.add_subparsers(dest="action", required=True)

    ingest = subparsers.add_parser("ingest", help="导入记录")
    ingest.add_argument("path", nargs="?", default="fixtures/records.json")

    list_parser = subparsers.add_parser("list", help="查看当前记录")
    list_parser.add_argument("--source")

    subparsers.add_parser("revisions", help="查看来源修订历史")

    define = subparsers.add_parser("define-taxonomy", help="定义并冻结一份主题口径")
    define.add_argument("path")
    define.add_argument("--no-activate", action="store_true")

    activate = subparsers.add_parser("activate-taxonomy", help="切换当前主题口径")
    activate.add_argument("taxonomy_id")

    subparsers.add_parser("taxonomies", help="列出口径版本")

    analyze = subparsers.add_parser("analyze", help="按当前口径重新计算自动判断")
    analyze.add_argument("--taxonomy")
    analyze.add_argument("--reviewer", default="automatic")

    judge = subparsers.add_parser("judge", help="人工确认某条意见的主题")
    judge.add_argument("record_id")
    judge.add_argument("topics", help="逗号分隔的主题编号；留空表示不属于任何主题")
    judge.add_argument("--taxonomy")
    judge.add_argument("--reviewer", required=True)
    judge.add_argument("--note", default="")

    review = subparsers.add_parser("review-queue", help="查看待复核意见")
    review.add_argument("--taxonomy")

    judgments = subparsers.add_parser("judgments", help="查看判断历史")
    judgments.add_argument("--record")
    judgments.add_argument("--taxonomy")

    report = subparsers.add_parser("report", help="按当前口径生成冻结报告")
    report.add_argument("--taxonomy")
    report.add_argument("--title", default="")

    subparsers.add_parser("reports", help="列出已生成报告")

    show = subparsers.add_parser("show-report", help="查看某份报告的当时口径")
    show.add_argument("report_id")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    workflow = TextWorkflow(RecordRepository(args.state))
    if args.action == "ingest":
        result = workflow.ingest(_read_json(args.path))
    elif args.action == "list":
        result = workflow.list_records(args.source)
    elif args.action == "revisions":
        result = workflow.revisions()
    elif args.action == "define-taxonomy":
        result = workflow.define_taxonomy(
            _read_json(args.path), activate=not args.no_activate
        )
    elif args.action == "activate-taxonomy":
        result = workflow.activate_taxonomy(args.taxonomy_id)
    elif args.action == "taxonomies":
        result = workflow.list_taxonomies()
    elif args.action == "analyze":
        result = workflow.analyze(args.taxonomy, reviewer=args.reviewer)
    elif args.action == "judge":
        result = workflow.judge(
            args.record_id, args.topics, taxonomy_id=args.taxonomy,
            reviewer=args.reviewer, note=args.note,
        )
    elif args.action == "review-queue":
        result = workflow.review_queue(args.taxonomy)
    elif args.action == "judgments":
        result = workflow.list_judgments(args.record, args.taxonomy)
    elif args.action == "report":
        result = workflow.build_report(args.taxonomy, title=args.title)
    elif args.action == "reports":
        result = workflow.list_reports()
    else:
        result = workflow.get_report(args.report_id)
    _print(result)


if __name__ == "__main__":
    main()
