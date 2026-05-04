#!/usr/bin/env python3
"""
Build labeled policy dataset from telemetry JSONL.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from kernell_sdk.router.offline_labeler import LabelConfig, OfflineLabeler


def _read_jsonl(path: Path):
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def main() -> int:
    p = argparse.ArgumentParser(description="Create labeled policy dataset from telemetry")
    p.add_argument("--input", required=True, help="Telemetry JSONL input path")
    p.add_argument("--output", required=True, help="Labeled JSONL output path")
    p.add_argument("--quality-weight", type=float, default=1.0)
    p.add_argument("--cost-weight", type=float, default=10.0)
    p.add_argument("--latency-weight", type=float, default=0.1)
    p.add_argument("--escalation-penalty", type=float, default=0.3)
    p.add_argument("--min-confidence", type=float, default=0.5)
    args = p.parse_args()

    cfg = LabelConfig(
        quality_weight=args.quality_weight,
        cost_weight=args.cost_weight,
        latency_weight=args.latency_weight,
        escalation_penalty=args.escalation_penalty,
        min_confidence=args.min_confidence,
    )
    labeler = OfflineLabeler(cfg)
    events = list(_read_jsonl(Path(args.input)))
    labeled = labeler.label_batch(events)
    n = labeler.export_jsonl(labeled, Path(args.output))

    print(json.dumps({
        "input_events": len(events),
        "labeled_examples": n,
        "stats": labeler.get_stats(),
        "balance": labeler.get_balance_report(labeled),
        "output": args.output,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
