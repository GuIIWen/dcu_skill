#!/usr/bin/env python3
"""Evaluate prepared evidence, never execute service operations."""
import argparse
import json
import sys
from pathlib import Path


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def evaluate(case):
    raw_id = case.get("case_id") if isinstance(case, dict) else None
    cid = raw_id if nonempty(raw_id) else "<invalid-case-id>"
    def result(status, reasons):
        return dict(case_id=cid, status=status, reasons=reasons)
    try:
        if not isinstance(case, dict) or not nonempty(case.get("case_id")):
            raise ValueError("case object with case_id required")
        if case.get("scope") not in ("offline", "live", "historical"):
            raise ValueError("scope required")
        t = case.get("trigger")
        if not isinstance(t, dict):
            raise ValueError("trigger required")
        if type(t.get("required")) is not int or t["required"] < 1:
            raise ValueError("required trigger count must be >=1")
        if type(t.get("observed")) is not int or t["observed"] < 0 or not nonempty(t.get("evidence")):
            raise ValueError("observed count and trigger evidence required")
        signals, assertions = case.get("signals"), case.get("assertions")
        if not isinstance(signals, list) or not signals or not isinstance(assertions, list) or not assertions:
            raise ValueError("nonempty signals and assertions required")
        by_name = {}
        for signal in signals:
            if not isinstance(signal, dict) or not nonempty(signal.get("name")):
                raise ValueError("signal name required")
            if signal["name"] in by_name:
                raise ValueError("duplicate signal")
            if any(type(signal.get(k)) is not bool for k in ("available", "valid")):
                raise ValueError("signal flags must be bool")
            if not nonempty(signal.get("evidence")):
                raise ValueError("signal evidence required")
            by_name[signal["name"]] = signal
        seen = set()
        for assertion in assertions:
            if not isinstance(assertion, dict) or not nonempty(assertion.get("name")):
                raise ValueError("assertion name required")
            if assertion["name"] in seen:
                raise ValueError("duplicate assertion")
            seen.add(assertion["name"])
            if "passed" not in assertion or (assertion["passed"] is not None and type(assertion["passed"]) is not bool):
                raise ValueError("assertion passed must be bool or null")
            if not nonempty(assertion.get("evidence")):
                raise ValueError("assertion evidence required")
            deps = assertion.get("requires")
            if not isinstance(deps, list) or not deps or any(not nonempty(d) or d not in by_name for d in deps):
                raise ValueError("assertion requires known signals")
        error = case.get("execution_error")
        if error is not None and not nonempty(error):
            raise ValueError("execution_error must be nonempty string or null")
        reasons = []
        triggered = t["observed"] >= t["required"]
        if not triggered:
            reasons.append("insufficient effective triggers")
        if error:
            reasons.append("execution error: "+error)
        invalid = [name for name, s in by_name.items() if not s["available"] or not s["valid"]]
        reasons.extend("unavailable or invalid signal: "+n for n in invalid)
        proven_failures = [a["name"] for a in assertions if a["passed"] is False and
                           all(d not in invalid for d in a["requires"])]
        if triggered and proven_failures:
            return result("FAIL", ["failed assertion: "+n for n in proven_failures]+reasons)
        reasons.extend("unresolved assertion: "+a["name"] for a in assertions
                       if a["passed"] is None or a["passed"] is False)
        if reasons:
            return result("INCONCLUSIVE", reasons)
        return result("PASS", ["triggered; required evidence valid; assertions passed"])
    except (ValueError, TypeError, KeyError) as exc:
        invalid = result("INCONCLUSIVE", ["invalid input: "+str(exc)])
        invalid["input_error"] = True
        return invalid


def suite(cases):
    if not isinstance(cases, list) or not cases:
        return dict(status="INCONCLUSIVE", cases=[], reasons=["empty or invalid suite"]), 2
    verdicts = [evaluate(c) for c in cases]
    invalid_count = sum(bool(v.get("input_error")) for v in verdicts)
    ids = [v["case_id"] for v in verdicts]
    if invalid_count:
        return dict(status="INCONCLUSIVE", cases=verdicts, invalid_case_count=invalid_count,
                    reasons=["malformed cases; valid case failures remain in cases"]), 2
    if len(set(ids)) != len(ids):
        return dict(status="INCONCLUSIVE", cases=verdicts, invalid_case_count=0,
                    reasons=["duplicate case_id"]), 2
    status = "FAIL" if any(v["status"] == "FAIL" for v in verdicts) else (
        "INCONCLUSIVE" if any(v["status"] == "INCONCLUSIVE" for v in verdicts) else "PASS")
    return dict(status=status, cases=verdicts, invalid_case_count=0), {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}[status]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        report, code = suite(json.loads(args.cases.read_text(encoding="utf-8-sig")))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "out": str(args.out.resolve())}))
        return code
    except (ValueError, OSError) as exc:
        print("verdict error: "+str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
