#!/usr/bin/env python3
"""汇总一个 aider benchmark 结果目录（只读，不需要容器，不需要 aider）。

用法:
  python3 summarize_results.py <结果目录> [--vs <另一个结果目录>] [--tsv out.tsv]

输出:
  - 总体: 习题数 / 第一次通过 / 两次内通过(含第一次) / 模型一个 token 都没返回的习题数 / 崩溃数
  - 分语言
  - 超时相关: aider 自己记的 num_error_outputs 之和
  - 需要人工留意的: 零 token 却"通过"的习题（多半是起始代码本来就能过）、崩溃的习题和最后一行异常
  - --vs: 两个目录逐题并排
口径与官方 `benchmark.py --stats` 一致: pass_rate_1 = 第一次尝试就通过, pass_rate_2 = 任意一次通过。
"""
import argparse, collections, glob, json, os, sys


def load(d):
    rows = []
    for f in sorted(glob.glob(os.path.join(d, "*", "exercises", "practice", "*", ".aider.results.json"))):
        lang = os.path.relpath(f, d).split(os.sep)[0]
        name = os.path.basename(os.path.dirname(f))
        r = json.load(open(f))
        if "tests_outcomes" not in r:  # run_test 捕获异常时只写 exception
            last = (r.get("exception") or "").strip().splitlines()
            rows.append(dict(lang=lang, name=name, crash=(last[-1] if last else "exception")))
            continue
        o = r["tests_outcomes"]
        rows.append(dict(lang=lang, name=name, outcomes=o, p1=bool(o and o[0]), p2=any(o),
                         dur=r.get("duration", 0), ctok=r.get("completion_tokens", 0), ptok=r.get("prompt_tokens", 0),
                         err=r.get("num_error_outputs", 0), malformed=r.get("num_malformed_responses", 0)))
    return rows


def summary(rows, title):
    ok = [r for r in rows if "crash" not in r]
    n = len(rows)
    print("== %s  (%d 个结果文件)" % (title, n))
    if not n:
        return
    p1 = sum(r["p1"] for r in ok)
    p2 = sum(r["p2"] for r in ok)
    zero = [r for r in ok if r["ctok"] == 0]
    print("  pass_rate_1 %.1f%% (%d)   pass_rate_2 %.1f%% (%d)   崩溃 %d   零 token %d   num_error_outputs 合计 %d"
          % (100 * p1 / n, p1, 100 * p2 / n, p2, n - len(ok), len(zero), sum(r["err"] for r in ok)))
    if ok:
        print("  平均耗时 %.0fs   completion_tokens 合计 %d" % (sum(r["dur"] for r in ok) / len(ok), sum(r["ctok"] for r in ok)))
    by = collections.defaultdict(list)
    for r in rows:
        by[r["lang"]].append(r)
    print("  %-11s %4s %7s %7s %6s" % ("语言", "题数", "一次过", "两次内", "零token"))
    for k in sorted(by):
        v = by[k]
        print("  %-11s %4d %7d %7d %6d" % (k, len(v), sum(1 for r in v if r.get("p1")), sum(1 for r in v if r.get("p2")),
                                           sum(1 for r in v if r.get("ctok") == 0 and "crash" not in r)))
    sus = [r for r in zero if r["p2"]]
    if sus:
        print("  零 token 却通过（多半是起始代码本来就能过测试，不是模型的功劳）: " + ", ".join("%s/%s" % (r["lang"], r["name"]) for r in sus))
    for r in rows:
        if "crash" in r:
            print("  崩溃 %s/%s: %s" % (r["lang"], r["name"], r["crash"][:120]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--vs")
    ap.add_argument("--tsv")
    a = ap.parse_args()
    A = load(a.dir)
    summary(A, os.path.basename(a.dir.rstrip("/")))
    if a.tsv:
        with open(a.tsv, "w") as f:
            f.write("lang\texercise\toutcomes\tduration_s\tcompletion_tokens\tprompt_tokens\tnum_error_outputs\tmalformed\n")
            for r in A:
                if "crash" in r:
                    f.write("%s\t%s\tCRASH: %s\t\t\t\t\t\n" % (r["lang"], r["name"], r["crash"][:100]))
                else:
                    f.write("%s\t%s\t%s\t%.0f\t%d\t%d\t%d\t%d\n" % (r["lang"], r["name"], json.dumps(r["outcomes"]), r["dur"],
                                                                   r["ctok"], r["ptok"], r["err"], r["malformed"]))
    if a.vs:
        B = load(a.vs)
        summary(B, os.path.basename(a.vs.rstrip("/")))
        ia = {(r["lang"], r["name"]): r for r in A}
        ib = {(r["lang"], r["name"]): r for r in B}
        keys = sorted(set(ia) & set(ib))
        print("== 逐题对照 (%d 题共有)" % len(keys))
        print("  %-30s | %-16s %6s %6s %3s | %-16s %6s %6s %3s" % ("题", "A 结果", "耗时", "ctok", "err", "B 结果", "耗时", "ctok", "err"))
        for k in keys:
            def cell(r):
                return ("CRASH", 0, 0, 0) if "crash" in r else (json.dumps(r["outcomes"]), r["dur"], r["ctok"], r["err"])
            x, y = cell(ia[k]), cell(ib[k])
            print("  %-30s | %-16s %6.0f %6d %3d | %-16s %6.0f %6d %3d" % (k[0] + "/" + k[1], x[0], x[1], x[2], x[3], y[0], y[1], y[2], y[3]))


if __name__ == "__main__":
    sys.exit(main())
