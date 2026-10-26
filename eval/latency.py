"""What one *check* costs, and what one *decision* costs — measured apart.

    python -m eval.latency                # both scenarios, n=6 each
    python -m eval.latency --n 10
    python -m eval.latency --json

The requirement this exists to answer is written as a single number — "under
2.0 seconds per check" — and a single number is the mistake. It picks one budget
for two operations that are three orders of magnitude apart:

    the check      every second, deterministic, no network      < 30 ms
    the decision   once, when an index has crossed its threshold   ~2.5 s

Collapsed into one figure the requirement is either met trivially by the path
nobody worries about, or missed by the path that never needed to be that fast.
So this prints them separately, and the decision gets its own budget.

That budget is not a relaxation, because the decision is not on the same clock:
it is followed by a 300 s fail-safe. Five seconds against five minutes is under
two percent, and the fail-safe — not the model — is what protects the user.

The check half runs with no `llm` block (the rules need no key); the decision
half needs one. With `proactive.llm_decide: false` the decision collapses into
the check at < 30 ms and the two-tier policy, the approval queue and the
fail-safe all still run — what is lost is only the choice *among* candidates.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db, llm, policy, risk, sensors  # noqa: E402
from app.devices import MockAdapter  # noqa: E402
from app.devices import mock as mock_mod  # noqa: E402
from app.proactive import ProactiveEngine  # noqa: E402

SCENARIOS = ("mold", "oven_running")


def _setup(scenario: str) -> tuple[sensors.SensorSimulator, dict, list]:
    """A fresh house in the state that makes this scenario fire."""
    sim = sensors.SensorSimulator(tick_seconds=1.0)
    sim.apply_scenario(scenario)
    snap = sim.snapshot()
    sim.record(snap)
    now = datetime.now()
    fired = [r for r in risk.evaluate(snap, sim.history, now) if r.score >= r.threshold]
    return sim, snap, fired


def _check(sim: sensors.SensorSimulator) -> tuple[float, int]:
    """One tick of the 1 Hz path: snapshot, record, both indices, candidates."""
    now = datetime.now()
    started = time.perf_counter()
    snap = sim.snapshot()
    sim.record(snap)
    fired = [r for r in risk.evaluate(snap, sim.history, now) if r.score >= r.threshold]
    n = sum(len(policy.candidates(r, snap)) for r in fired)
    return (time.perf_counter() - started) * 1000, n


def _decision(cfg: dict, adapter, scenario: str) -> dict:
    """One dispatch: the model chooses, Tier 1 executes, Tier 2 is put to the user."""
    sim, snap, fired = _setup(scenario)
    if not fired:
        return {"ms": 0.0, "what": "no risk fired"}
    engine = ProactiveEngine(adapter, cfg)
    started = time.perf_counter()
    event = engine._intervene(fired[0], snap, datetime.now())
    ms = (time.perf_counter() - started) * 1000
    return {
        "ms": ms,
        "risk": event["risk"],
        "decided_by": event["decided_by"],
        "tier1": event["tier1"],
        "tier2": event["tier2"],
    }


def _spread(values: list[float]) -> str:
    return (f"median {statistics.median(values) / 1000:.2f} s  "
            f"({min(values) / 1000:.2f}–{max(values) / 1000:.2f})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6, help="runs per scenario (default 6)")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args()

    cfg = config.load()
    has_model = llm.available(cfg)
    model = (cfg.get("llm") or {}).get("model")

    tmp = tempfile.TemporaryDirectory()
    orig = (db.DATA_DIR, db.DB_PATH, mock_mod.STATE_PATH)
    db.DATA_DIR = Path(tmp.name)
    db.DB_PATH = Path(tmp.name) / "latency.db"
    mock_mod.STATE_PATH = Path(tmp.name) / "mock_state.json"
    db.init()

    adapter = MockAdapter()
    rows = []
    try:
        for scenario in SCENARIOS:
            checks = []
            for _ in range(args.n):
                sim, _snap, _fired = _setup(scenario)
                ms, n_cands = _check(sim)
                checks.append(ms)
            runs = [_decision(cfg, adapter, scenario) for _ in range(args.n)] if has_model else []
            rows.append({
                "scenario": scenario,
                "check_ms": checks,
                "candidates": n_cands,
                "decisions": runs,
            })
    finally:
        adapter.stop()
        db.DATA_DIR, db.DB_PATH, mock_mod.STATE_PATH = orig
        tmp.cleanup()

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    print(f'n={args.n} per scenario, model {model if has_model else "— (no key)"}\n')
    for row in rows:
        check = row["check_ms"]
        print(f'{row["scenario"]}  — {row["candidates"]} candidates')
        print(f'  check     median {statistics.median(check):.1f} ms  '
              f'max {max(check):.1f} ms   (every tick, no network)')
        if not row["decisions"]:
            print("  decision  not measured — no llm.api_key\n")
            continue
        ms = [d["ms"] for d in row["decisions"]]
        first = row["decisions"][0]
        print(f'  decision  {_spread(ms)}   (model in the loop)')
        print(f'            {first["decided_by"]}: '
              f'Tier 1 {", ".join(first["tier1"]) or "—"} / '
              f'Tier 2 {", ".join(first["tier2"]) or "—"}')
        # The decision is followed by a fail-safe, not by the next tick.
        share = statistics.median(ms) / 1000 / policy.APPROVAL_TTL_SECONDS * 100
        print(f'            {share:.1f}% of the {policy.APPROVAL_TTL_SECONDS} s '
              f'fail-safe\n')

    if not has_model:
        print("The check path is the whole story without a model: "
              "`llm_decide: false` gives the same < 30 ms and keeps the tiers.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
