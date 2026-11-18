#!/usr/bin/env python3
"""Aggregate explicitly adapted cumulative usage snapshots; stdlib only."""
import argparse
import csv
import json
import sys
from pathlib import Path

METRICS = ("input_total", "cache_read", "cache_creation", "input_uncached", "output")
IDS = ("run_id", "trial_id", "session_id", "request_id", "attempt_id")


def number(v):
    if v is not None and (type(v) is not int or v < 0):
        raise ValueError("token counts must be nonnegative integers or null")
    return v


def add(*values):
    return None if any(v is None for v in values) else sum(values)


def normalize(row):
    u = row["usage"]
    if u is None or not u:
        return dict.fromkeys(METRICS)
    fmt = row["format"]
    if fmt == "openai":
        details = u.get("prompt_tokens_details")
        if details is not None and not isinstance(details, dict):
            raise ValueError("prompt_tokens_details must be object or null")
        total = number(u.get("prompt_tokens"))
        read = number((details or {}).get("cached_tokens"))
        result = dict(input_total=total, cache_read=read, cache_creation=None,
                      input_uncached=None if total is None or read is None else total-read,
                      output=number(u.get("completion_tokens")))
    elif fmt == "anthropic":
        inp, read, create = (number(u.get(k)) for k in
                            ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        result = dict(input_total=add(inp, read, create), cache_read=read, cache_creation=create,
                      input_uncached=add(inp, create), output=number(u.get("output_tokens")))
    else:
        result = {k: number(u.get(k)) for k in METRICS}
    for v in result.values():
        number(v)
    total, read, create, uncached = (result[k] for k in METRICS[:4])
    for name, component in (("cache_read", read), ("input_uncached", uncached), ("cache_creation", create)):
        if total is not None and component is not None and component > total:
            raise ValueError(name + " exceeds input_total")
    if all(v is not None for v in (total, read, create)) and read + create > total:
        raise ValueError("cache_read + cache_creation exceeds input_total")
    if all(v is not None for v in (total, read, uncached)) and total != read + uncached:
        raise ValueError("input_total != cache_read + input_uncached")
    if create is not None and uncached is not None and create > uncached:
        raise ValueError("cache_creation exceeds input_uncached")
    return result


def validate(row):
    if not isinstance(row, dict):
        raise ValueError("record must be an object")
    for k in IDS:
        if not isinstance(row.get(k), str) or not row[k].strip():
            raise ValueError("missing nonempty string: " + k)
    if type(row.get("revision")) is not int or row["revision"] < 0:
        raise ValueError("revision must be a nonnegative integer")
    if row.get("kind") not in ("main", "subagent", "compaction", "unknown"):
        raise ValueError("invalid kind")
    if row.get("status") not in ("success", "failed", "cancelled", "unknown"):
        raise ValueError("invalid status")
    if row.get("format") not in ("openai", "anthropic", "normalized"):
        raise ValueError("invalid format")
    if "usage" not in row or (row["usage"] is not None and not isinstance(row["usage"], dict)):
        raise ValueError("usage must be object or null")
    source = row.get("source")
    if not isinstance(source, dict) or any(not isinstance(source.get(k), str) or not source[k].strip()
                                          for k in ("path", "locator")):
        raise ValueError("source.path and source.locator required")
    normalize(row)


def deduplicate(rows):
    groups = {}
    for row in rows:
        validate(row)
        key = tuple(row[k] for k in IDS)
        groups.setdefault(key, []).append(row)
    output = []
    for key in sorted(groups):
        group = groups[key]
        by_revision = {}
        for row in group:
            canonical = {k: row[k] for k in ("kind", "status", "format", "usage")}
            rev = row["revision"]
            if rev in by_revision and by_revision[rev][0] != canonical:
                raise ValueError("conflicting snapshot revision: " + repr((key, rev)))
            by_revision[rev] = (canonical, row)
        if len({(r["kind"], r["format"]) for r in group}) != 1:
            raise ValueError("kind/format changed within one attempt: " + repr(key))
        ordered = [by_revision[i][1] for i in sorted(by_revision)]
        latest = ordered[-1]
        chosen = next((r for r in reversed(ordered) if r["usage"]), latest)
        out = {k: latest[k] for k in IDS + ("kind", "status", "format")}
        out.update(normalize(chosen))
        out["usage_revision"] = chosen["revision"]
        out["latest_revision"] = latest["revision"]
        out["usage_source"] = chosen["source"]
        out["sources"] = sorted({json.dumps(r["source"], sort_keys=True, ensure_ascii=False)
                                 for r in group})
        out["sources"] = [json.loads(s) for s in out["sources"]]
        output.append(out)
    return output


def aggregate(rows):
    metrics = {}
    for k in METRICS:
        known = [r[k] for r in rows if r[k] is not None]
        metrics[k] = dict(known_subtotal=sum(known), known_requests=len(known),
                          total_requests=len(rows),
                          complete_total=sum(known) if rows and len(known) == len(rows) else None)
    eligible = [r for r in rows if r["input_total"] is not None and r["cache_read"] is not None]
    denominator = sum(r["input_total"] for r in eligible)
    return dict(requests=len(rows), metrics=metrics,
                requests_without_any_usage=sum(all(r[k] is None for k in METRICS) for r in rows),
                status_counts={s: sum(r["status"] == s for r in rows)
                               for s in ("success", "failed", "cancelled", "unknown")},
                cache_hit_rate_known_subset=sum(r["cache_read"] for r in eligible)/denominator
                if denominator else None,
                eligible_requests=len(eligible), eligible_input_total=denominator)


def build(rows):
    requests = deduplicate(rows)
    runs = {}
    for run_id in sorted({r["run_id"] for r in requests}):
        subset = [r for r in requests if r["run_id"] == run_id]
        runs[run_id] = aggregate(subset)
        runs[run_id]["by_kind"] = {k: aggregate([r for r in subset if r["kind"] == k])
                                  for k in sorted({r["kind"] for r in subset})}
    unique_snapshots = len({tuple(r[k] for k in IDS) + (r["revision"],) for r in rows})
    return requests, dict(schema_version=1, raw_records=len(rows), unique_requests=len(requests),
                          duplicate_records=len(rows)-unique_snapshots,
                          superseded_snapshots=unique_snapshots-len(requests),
                          collapsed_records=len(rows)-len(requests), runs=runs,
                          scope="logged requests only; trials with no records require external inventory")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        rows = [json.loads(line) for line in args.records.read_text(encoding="utf-8-sig").splitlines()
                if line.strip()]
        requests, summary = build(rows)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out/"requests.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False)+"\n"
                                                     for r in requests), encoding="utf-8")
        (args.out/"summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n",
                                            encoding="utf-8")
        with (args.out/"trials.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["run_id", "trial_id", "requests"] +
                                    [k+s for k in METRICS for s in ("_known_subtotal", "_known_requests", "_complete_total")])
            writer.writeheader()
            for run, trial in sorted({(r["run_id"], r["trial_id"]) for r in requests}):
                agg = aggregate([r for r in requests if (r["run_id"], r["trial_id"]) == (run, trial)])
                flat = dict(run_id=run, trial_id=trial, requests=agg["requests"])
                for k in METRICS:
                    for suffix in ("known_subtotal", "known_requests", "complete_total"):
                        flat[k+"_"+suffix] = agg["metrics"][k][suffix]
                writer.writerow(flat)
        print(json.dumps({"unique_requests": len(requests), "runs": list(summary["runs"]),
                          "out": str(args.out.resolve())}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print("ledger error: "+str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
