"""
Run the eval cases against the agent, several times each, and report.

    python evals/run_evals.py                          # all cases, 3 repetitions each
    python evals/run_evals.py --reps 5 --label baseline
    python evals/run_evals.py --only 2,3 --show        # some cases; print failing answers
    python evals/run_evals.py --compare evals/results/A.json evals/results/B.json

WHY REPETITIONS
    The same input can behave differently from one run to the next (we saw
    "the friend" ask for clarification once and work other times). One run proves
    little. Pass RATES over several runs are the honest measure, and a case that
    passes only sometimes is flagged as FLAKY.

WHY INFRASTRUCTURE ERRORS ARE KEPT APART
    A Groq rate limit or a dropped connection says nothing about whether the
    agent is good. Those runs are retried with a wait, and if they still fail
    they are reported as "infra" and left out of the pass rates.
    (A malformed tool call rejected by Groq IS the model's fault, so it counts
    as a failure.)

SAFETY
    Every case uses the fake catalog and a fake TV (see eval_servers.py). Nothing
    touches your TV or your RapidAPI quota. It does use Groq calls.
"""
import _paths  # noqa: F401  (must come first: sets up sys.path)

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import sys
import time
from collections import Counter
from datetime import datetime

import groq

import agent
import config
from cases import CASES, CASES_BY_ID, normalize_answer

RESULTS_DIR = _paths.HERE / "results"

# Groq-side problems that say nothing about the agent: wait and retry.
INFRA_ERRORS = (groq.RateLimitError, groq.APIConnectionError, groq.APITimeoutError, groq.InternalServerError)


# ---------------------------------------------------------------- helpers

def leaves(exc):
    """Unwrap nested ExceptionGroups (the async library wraps real errors in them)."""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from leaves(sub)
    else:
        yield exc


def classify_error(exc) -> tuple[str, str]:
    """-> ("infra" | "model_error", description)"""
    found = list(leaves(exc))
    for e in found:
        if isinstance(e, groq.BadRequestError):
            return "model_error", f"Groq rejected the request (often a malformed tool call): {e}"
    for e in found:
        if isinstance(e, INFRA_ERRORS):
            return "infra", f"{type(e).__name__}: {e}"
    return "infra", "unexpected: " + "; ".join(f"{type(e).__name__}: {e}" for e in found)


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def fingerprint() -> dict:
    """What was under test. Two result files are only comparable if you know what differed."""
    tools_src = "".join((config.BASE_DIR / f).read_text(encoding="utf-8") for f in ("catalog_mcp.py", "device_mcp.py"))
    return {
        "model": agent.MODEL,
        "system_prompt": _sha(agent.SYSTEM),
        "tool_servers": _sha(tools_src),
        "max_turns": agent.MAX_TURNS,
    }


def tool_sequence(trace) -> str:
    return " -> ".join(t["tool"] + ("" if t["executed"] else "(blocked)") for t in trace) or "(no tools)"


# ---------------------------------------------------------------- scoring

def score(case, trace, answer) -> dict:
    checks = {}
    answered = bool(answer.strip()) and not answer.startswith("Stopped after")
    checks["final_answer"] = {"ok": answered, "soft": False,
                              "detail": "has an answer" if answered else "no final answer (hit the turn limit?)"}
    plain = normalize_answer(answer)       # checks see plain text; the saved answer stays as written
    for c in case.checks:
        try:
            ok, detail = c.fn(trace, plain)
        except Exception as e:  # noqa: BLE001   a broken check must not crash the suite
            ok, detail = False, f"check crashed: {e!r}"
        checks[c.name] = {"ok": bool(ok), "soft": c.soft, "detail": detail}
    return checks


def hard_failures(record) -> list[str]:
    return [n for n, c in record["checks"].items() if not c["ok"] and not c["soft"]]


def soft_failures(record) -> list[str]:
    return [n for n, c in record["checks"].items() if not c["ok"] and c["soft"]]


# ---------------------------------------------------------------- running

async def run_once(case, rep: int, retries: int, backoff: float) -> dict:
    agent.SERVERS = {
        "catalog": f"evals/srv_catalog_{case.catalog}.py",
        "device": f"evals/srv_device_{case.device}.py",
    }
    started = time.monotonic()
    record = {"case": case.id, "name": case.name, "rep": rep, "request": case.request}

    for attempt in range(retries + 1):
        trace, buf = [], io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                answer = await agent.run_agent(case.request, trace)
        except Exception as e:  # noqa: BLE001
            kind, description = classify_error(e)
            if kind == "infra" and attempt < retries:
                wait = backoff * (2 ** attempt)
                print(f"      (infra error, waiting {wait:.0f}s then retrying: {description[:110]})", flush=True)
                await asyncio.sleep(wait)
                continue
            record.update(status="infra_error" if kind == "infra" else "fail", answer="", trace=trace,
                          transcript=buf.getvalue(), error=description, attempts=attempt + 1)
            record["checks"] = ({} if kind == "infra" else
                                {"model_call_failed": {"ok": False, "soft": False, "detail": description}})
            break
        else:
            record.update(answer=answer, trace=trace, transcript=buf.getvalue(), attempts=attempt + 1)
            record["checks"] = score(case, trace, answer)
            record["status"] = "fail" if hard_failures(record) else "pass"
            break

    record["seconds"] = round(time.monotonic() - started, 1)
    return record


async def run_suite(cases, reps: int, delay: float = 0, retries: int = 3, backoff: float = 15, label: str = "run") -> dict:
    results = {
        "meta": {"label": label, "when": datetime.now().isoformat(timespec="seconds"),
                 "reps": reps, "cases": [c.id for c in cases], "fingerprint": fingerprint()},
        "runs": [],
    }
    for case in cases:
        for rep in range(1, reps + 1):
            rec = await run_once(case, rep, retries, backoff)
            results["runs"].append(rec)
            failed = hard_failures(rec)
            note = f"  FAILED: {', '.join(failed)}" if failed else ""
            print(f"  #{case.id:<2} rep {rep}/{reps}  {rec['status'].upper():<11} {rec['seconds']:>5.1f}s  "
                  f"{tool_sequence(rec['trace'])}{note}", flush=True)
            if delay:
                await asyncio.sleep(delay)
    return results


# ---------------------------------------------------------------- reporting

def summarize(results) -> dict:
    per_case = {}
    for r in results["runs"]:
        d = per_case.setdefault(r["case"], {"name": r["name"], "pass": 0, "fail": 0, "infra": 0,
                                            "hard": Counter(), "soft": Counter(), "detail": {}})
        if r["status"] == "infra_error":
            d["infra"] += 1
            continue
        d["pass" if r["status"] == "pass" else "fail"] += 1
        for name in hard_failures(r):
            d["hard"][name] += 1
            d["detail"].setdefault(name, r["checks"][name]["detail"])
        for name in soft_failures(r):
            d["soft"][name] += 1
    return per_case


def rate_text(d) -> str:
    n = d["pass"] + d["fail"]
    return f"{d['pass']}/{n}" if n else "n/a"


def report(results, show: bool = False) -> int:
    per_case = summarize(results)
    m = results["meta"]
    print(f"\n=== {m['label']}  ({m['when']}, {m['reps']} reps per case, model {m['fingerprint']['model']}) ===\n")
    total_pass = total_runs = infra = 0
    for cid in sorted(per_case):
        d = per_case[cid]
        n = d["pass"] + d["fail"]
        total_pass += d["pass"]; total_runs += n; infra += d["infra"]
        flag = ""
        if n and 0 < d["pass"] < n:
            flag = "  FLAKY (passes only some of the time)"
        elif n and d["pass"] == 0:
            flag = "  FAILING"
        extra = f"  [{d['infra']} infra error(s) excluded]" if d["infra"] else ""
        print(f"#{cid:<2} {d['name']:<42} {rate_text(d):>5}{flag}{extra}")
        for name, count in d["hard"].most_common():
            print(f"       fails {name}  x{count}   ({d['detail'][name]})")
        if d["soft"]:
            print("       warnings: " + ", ".join(f"{k} x{v}" for k, v in d["soft"].most_common()))
    print(f"\nOverall: {total_pass}/{total_runs} runs passed all hard checks"
          + (f"  ({infra} infra run(s) excluded)" if infra else ""))
    if m["reps"] < 5:
        print("Note: with few repetitions, a difference of one run is within noise.")

    if show:
        for r in results["runs"]:
            if r["status"] == "pass":
                continue
            print(f"\n--- case #{r['case']} rep {r['rep']}: {r['status']}  request: {r['request']!r}")
            print(f"    tools: {tool_sequence(r['trace'])}")
            if r.get("error"):
                print(f"    error: {r['error'][:300]}")
            print(f"    answer: {' '.join((r['answer'] or '').split())[:500]}")
            for name in hard_failures(r):
                print(f"    FAILED {name}: {r['checks'][name]['detail']}")
    return 1 if total_runs and total_pass < total_runs else 0


def compare(a: dict, b: dict):
    fa, fb = a["meta"]["fingerprint"], b["meta"]["fingerprint"]
    print(f"\nA = {a['meta']['label']} ({a['meta']['when']})   B = {b['meta']['label']} ({b['meta']['when']})\n")
    changed = {k: (fa.get(k), fb.get(k)) for k in set(fa) | set(fb) if fa.get(k) != fb.get(k)}
    print("What differs between the two runs:")
    if changed:
        for k, (x, y) in sorted(changed.items()):
            print(f"  {k}: {x}  ->  {y}")
        if len(changed) > 1:
            print("  WARNING: more than one thing changed, so you cannot tell which one caused any difference.")
    else:
        print("  nothing in the fingerprint (differences are run-to-run variation)")

    sa, sb = summarize(a), summarize(b)
    print(f"\n{'case':<46} {'A':>6} {'B':>6}")
    for cid in sorted(set(sa) | set(sb)):
        da = sa.get(cid, {"pass": 0, "fail": 0, "infra": 0, "name": "?"})
        db = sb.get(cid, {"pass": 0, "fail": 0, "infra": 0, "name": "?"})
        na, nb = da["pass"] + da["fail"], db["pass"] + db["fail"]
        ra = da["pass"] / na if na else None
        rb = db["pass"] / nb if nb else None
        arrow = ""
        if ra is not None and rb is not None:
            arrow = "  better" if rb > ra else "  WORSE" if rb < ra else ""
        print(f"#{cid:<2} {(da['name'] if da['name'] != '?' else db['name']):<42} {rate_text(da):>6} {rate_text(db):>6}{arrow}")

    def check_failures(s):
        c = Counter()
        for cid, d in s.items():
            for name, count in d["hard"].items():
                c[f"#{cid} {name}"] += count
        return c

    ca, cb = check_failures(sa), check_failures(sb)
    keys = sorted(set(ca) | set(cb))
    if keys:
        print("\nHard-check failures (count in A -> count in B):")
        for k in keys:
            print(f"  {k:<34} {ca.get(k, 0)} -> {cb.get(k, 0)}")
    print("\nTreat differences of a single run as noise unless you used 5 or more repetitions.")


# ---------------------------------------------------------------- command line

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Run the agent evals.")
    p.add_argument("--reps", type=int, default=3, help="repetitions per case (default 3)")
    p.add_argument("--only", default="", help="comma-separated case ids, e.g. 2,3,9")
    p.add_argument("--label", default="run", help="name for this run, used in the results file name")
    p.add_argument("--delay", type=float, default=0, help="seconds to wait between runs (eases rate limits)")
    p.add_argument("--retries", type=int, default=3, help="retries for Groq rate limits / connection errors")
    p.add_argument("--backoff", type=float, default=15, help="first wait in seconds, doubled each retry")
    p.add_argument("--show", action="store_true", help="print the answers of failing runs")
    p.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"), help="compare two saved result files")
    args = p.parse_args(argv)

    if args.compare:
        a, b = (json.loads(open(f, encoding="utf-8").read()) for f in args.compare)
        compare(a, b)
        return 0

    config.require("GROQ_API_KEY")
    if not agent.MODEL:
        sys.exit("GROQ_MODEL is not set. Add GROQ_MODEL=<id> to .env (see `python agent.py --models`).")

    if args.only:
        ids = [int(x) for x in args.only.split(",") if x.strip()]
        unknown = [i for i in ids if i not in CASES_BY_ID]
        if unknown:
            sys.exit(f"Unknown case id(s): {unknown}. Valid: {sorted(CASES_BY_ID)}")
        cases = [CASES_BY_ID[i] for i in ids]
    else:
        cases = CASES

    runs = len(cases) * args.reps
    print(f"{len(cases)} case(s) x {args.reps} reps = {runs} runs, roughly {runs * 3} Groq calls. "
          "Check your Groq rate limits; use --delay if you hit them.")
    print("Fake catalog and fake TV: nothing touches your TV or RapidAPI.\n")

    results = asyncio.run(run_suite(cases, args.reps, args.delay, args.retries, args.backoff, args.label))
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{args.label}.json"
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    code = report(results, args.show)
    print(f"\nSaved: {path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
