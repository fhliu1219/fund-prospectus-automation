"""CLI for fetching and evaluating the optional Milestone 6 corpus."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

from .corpus import CorpusCache, load_manifest
from .corpus_evaluator import CorpusEvaluator, save_report
from .sec_client import SECClient


DEFAULT_MANIFEST = "corpus/manifest.json"
DEFAULT_CACHE = "corpus/cache"
DEFAULT_REPORT = "corpus/reports/evaluation.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prospectus-corpus",
        description="Fetch and evaluate the checksum-verified validation corpus.",
    )
    parser.add_argument("command", choices=("fetch", "evaluate", "all"))
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--cache", default=DEFAULT_CACHE)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = load_manifest(args.manifest)
    cache = CorpusCache(SECClient(), args.cache)
    if args.command in {"fetch", "all"}:
        cache.ensure_manifest(manifest)
        print(f"Cached and verified {len(manifest.cases)} corpus cases in {args.cache}")
    if args.command in {"evaluate", "all"}:
        report = CorpusEvaluator(cache).evaluate(manifest)
        save_report(report, args.report)
        current = report["current_metrics"]
        shadow = report["shadow_metrics"]
        print(f"Evaluation report: {Path(args.report)}")
        print(
            "Current: "
            f"false-positive verification={current['false_positive_verification_count']}, "
            f"false-negative verification={current['false_negative_verification_count']}, "
            f"review rate={current['manual_review_rate']:.1%}"
        )
        print(
            "Shadow:  "
            f"false-positive verification={shadow['false_positive_verification_count']}, "
            f"false-negative verification={shadow['false_negative_verification_count']}, "
            f"review rate={shadow['manual_review_rate']:.1%}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
