#!/usr/bin/env python3
"""Benchmark the direct service (CLAUDE.md). Needs the API running and CONSOLE_KEY exported.
  python bench/bench_direct.py [--api http://127.0.0.1:8080] [--testset bench/testset.csv]
Pass 1: BOM fresh=true (cold). Pass 2: BOM fresh=false (warm). Pass 3: GET /v1/part per line (warm).
"""
import argparse
import csv
import json
import os
import re
import statistics
import time
from pathlib import Path

import httpx

OUT = Path("results/direct")


def pct(xs, p):
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    return round(statistics.quantiles(xs, n=100)[p - 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("API", "http://127.0.0.1:8080"))
    ap.add_argument("--testset", default="bench/testset.csv")
    a = ap.parse_args()
    key = os.environ.get("CONSOLE_KEY") or ap.error("export CONSOLE_KEY first")
    OUT.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(open(a.testset, newline="", encoding="utf-8")))
    lines = [{"line_id": f"L{i + 1}", "mpn": r["mpn"], "qty": int(r["qty"]), "manufacturer": r["manufacturer"] or None}
             for i, r in enumerate(rows)]
    save = lambda name, data: (OUT / name).write_text(json.dumps(data, indent=2))  # noqa: E731

    with httpx.Client(base_url=a.api, headers={"X-API-Key": key}, timeout=900) as c:
        quota_before = c.get("/v1/quota").raise_for_status().json()
        save("quota_before.json", quota_before)

        timings = {}
        for name, fresh in (("bom_cold", True), ("bom_warm", False)):
            t = time.perf_counter()
            r = c.post("/v1/bom/price", json={"lines": lines, "fresh": fresh}).raise_for_status()
            timings[name] = round((time.perf_counter() - t) * 1000)
            save(f"{name}.json", r.json())
            print(f"{name}: {timings[name]} ms  summary={json.dumps(r.json()['summary'])}")

        part_ms = []
        for ln in lines:
            t = time.perf_counter()
            r = c.get("/v1/part", params={k: v for k, v in
                                          {"mpn": ln["mpn"], "qty": ln["qty"], "manufacturer": ln["manufacturer"]}.items() if v})
            ms = round((time.perf_counter() - t) * 1000)
            part_ms.append(ms)
            save(re.sub(r"[^A-Za-z0-9._-]", "_", ln["mpn"]) + ".json", r.json())
            best = (r.json().get("best") or {})
            print(f"part {ln['mpn']:<26} {ms:>5} ms  best={best.get('provider')} {best.get('unit_price')}")

        quota_after = c.get("/v1/quota").raise_for_status().json()
        save("quota_after.json", quota_after)
        save("stats.json", c.get("/v1/stats", params={"hours": 24}).raise_for_status().json())

    summary = {"bom_cold_ms": timings["bom_cold"], "bom_warm_ms": timings["bom_warm"],
               "part_warm_p50": pct(part_ms, 50), "part_warm_p95": pct(part_ms, 95),
               "quota_used": {p: quota_after["providers"][p]["used"] - quota_before["providers"][p]["used"]
                              for p in quota_after["providers"]}}
    save("summary.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
