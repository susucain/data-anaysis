#!/usr/bin/env python3
"""复现 sample_requests.csv 的数据核验、根因指标和策略成本回放。"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable


CSV_PATH = Path(__file__).with_name("sample_requests.csv")
FAILURE_COST = 75
SOLD_OUT_COST = 10
EXPECTED_COLUMNS = {
    "request_id",
    "request_time",
    "quota_synced_at",
    "region",
    "category",
    "remaining_quota",
    "page_duration_ms",
    "is_success",
}


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_rows() -> tuple[list[dict], list[dict]]:
    with CSV_PATH.open(encoding="utf-8", newline="") as source:
        raw_rows = list(csv.DictReader(source))

    if not raw_rows or set(raw_rows[0]) != EXPECTED_COLUMNS:
        raise ValueError("CSV 字段与题目口径不一致")

    unique_by_id: dict[str, dict] = {}
    for row in raw_rows:
        request_id = row["request_id"]
        if request_id in unique_by_id and row != unique_by_id[request_id]:
            raise ValueError(f"同一 request_id 存在冲突记录: {request_id}")
        unique_by_id.setdefault(request_id, row)

    rows = []
    for source in unique_by_id.values():
        row = dict(source)
        row["remaining_quota"] = int(row["remaining_quota"])
        row["page_duration_ms"] = int(row["page_duration_ms"])
        row["is_success"] = (
            None if row["is_success"] == "" else int(float(row["is_success"]))
        )
        request_time = parse_time(row["request_time"])
        synced_at = parse_time(row["quota_synced_at"])
        row["lag_min"] = (request_time - synced_at).total_seconds() / 60
        row["date"] = request_time.date().isoformat()
        rows.append(row)
    return raw_rows, rows


def rate(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:.2%}" if denominator else "n.a."


def evaluate(
    rows: Iterable[dict], name: str, rule: Callable[[dict], bool]
) -> dict[str, int | str]:
    rows = list(rows)
    failures = sum(row["is_success"] == 0 for row in rows)
    flagged = sum(rule(row) for row in rows)
    captured = sum(rule(row) and row["is_success"] == 0 for row in rows)
    residual = failures - captured
    baseline_cost = failures * FAILURE_COST
    policy_cost = flagged * SOLD_OUT_COST + residual * FAILURE_COST
    return {
        "name": name,
        "requests": len(rows),
        "failures": failures,
        "flagged": flagged,
        "captured": captured,
        "residual": residual,
        "capture_rate": rate(captured, failures),
        "baseline_cost": baseline_cost,
        "policy_cost": policy_cost,
        "saving": baseline_cost - policy_cost,
        "saving_rate": rate(baseline_cost - policy_cost, baseline_cost),
    }


def print_markdown_table(headers: list[str], values: list[list[object]]) -> None:
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join("---" for _ in headers) + " |")
    for row in values:
        print("| " + " | ".join(str(value) for value in row) + " |")


def main() -> None:
    raw_rows, rows = load_rows()
    observed = [row for row in rows if row["is_success"] is not None]
    failures = [row for row in observed if row["is_success"] == 0]
    missing = [row for row in rows if row["is_success"] is None]
    dates = [row["date"] for row in rows]

    print("# 数据质量")
    print(f"- CSV 原始行数：{len(raw_rows)}")
    print(f"- 唯一 request_id：{len(rows)}")
    print(f"- 完全重复的额外行：{len(raw_rows) - len(rows)}")
    print(f"- 已回传结果：{len(observed)}")
    print(f"- 未回传结果：{len(missing)}")
    print(f"- 数据日期：{min(dates)} 至 {max(dates)}")
    print(f"- 去重后落空：{len(failures)}，落空率 {rate(len(failures), len(observed))}")

    fresh = [row for row in observed if row["lag_min"] < 20]
    stale = [row for row in observed if row["lag_min"] >= 20]
    fresh_failures = sum(row["is_success"] == 0 for row in fresh)
    stale_failures = sum(row["is_success"] == 0 for row in stale)

    print("\n# 同步时延与落空")
    print_markdown_table(
        ["配额同步时延", "请求数", "落空数", "落空率"],
        [
            ["< 20 分钟", len(fresh), fresh_failures, rate(fresh_failures, len(fresh))],
            [
                ">= 20 分钟",
                len(stale),
                stale_failures,
                rate(stale_failures, len(stale)),
            ],
        ],
    )
    relative_risk = (stale_failures / len(stale)) / (fresh_failures / len(fresh))
    print(f"\n时延达到 20 分钟后的落空风险是未达到时的 {relative_risk:.1f} 倍。")

    policies = [
        evaluate(observed, "不干预", lambda _: False),
        evaluate(observed, "同步时延 >= 20 分钟", lambda row: row["lag_min"] >= 20),
        evaluate(
            observed,
            "同步时延 >= 20 分钟或展示配额 = 0",
            lambda row: row["lag_min"] >= 20 or row["remaining_quota"] == 0,
        ),
        evaluate(observed, "展示配额 <= 3", lambda row: row["remaining_quota"] <= 3),
    ]

    print("\n# 策略历史回放")
    print(
        f"成本公式：标记数 x {SOLD_OUT_COST} + 未拦截落空数 x {FAILURE_COST}（元）"
    )
    print_markdown_table(
        [
            "策略",
            "标记数",
            "拦截落空",
            "剩余落空",
            "落空拦截率",
            "总成本（元）",
            "节省（元）",
            "节省率",
        ],
        [
            [
                policy["name"],
                policy["flagged"],
                policy["captured"],
                policy["residual"],
                policy["capture_rate"],
                policy["policy_cost"],
                policy["saving"],
                policy["saving_rate"],
            ]
            for policy in policies
        ],
    )

    print("\n# 按日验证：同步时延 >= 20 分钟")
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in observed:
        by_date[row["date"]].append(row)
    daily = [
        evaluate(day_rows, date, lambda row: row["lag_min"] >= 20)
        for date, day_rows in sorted(by_date.items())
    ]
    print_markdown_table(
        ["日期", "请求数", "落空数", "策略成本（元）", "节省（元）"],
        [
            [
                day["name"],
                day["requests"],
                day["failures"],
                day["policy_cost"],
                day["saving"],
            ]
            for day in daily
        ],
    )

    train = [row for row in observed if row["date"] <= "2026-09-05"]
    holdout = [row for row in observed if row["date"] >= "2026-09-06"]
    split_results = [
        evaluate(train, "前 5 日", lambda row: row["lag_min"] >= 20),
        evaluate(holdout, "后 2 日留出验证", lambda row: row["lag_min"] >= 20),
    ]
    print("\n# 时间留出验证：同步时延 >= 20 分钟")
    print_markdown_table(
        ["样本", "请求数", "落空数", "策略成本（元）", "节省（元）", "节省率"],
        [
            [
                result["name"],
                result["requests"],
                result["failures"],
                result["policy_cost"],
                result["saving"],
                result["saving_rate"],
            ]
            for result in split_results
        ],
    )

    status_counts = Counter(row["is_success"] for row in rows)
    assert status_counts == Counter({1: 778, 0: 92, None: 39})
    assert all(day["saving"] > 0 for day in daily)


if __name__ == "__main__":
    main()
