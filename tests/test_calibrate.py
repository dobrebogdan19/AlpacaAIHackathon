"""calibrate.py — threshold recalibration from forward evidence.

The point of the module is honesty about in-sample optimisation, so the tests
check exactly that: the holdout split is a deterministic partition, the search
is deterministic, and a run where calibration does NOT help is reported as
``no-improvement`` rather than silently returning the best fitted combination.
"""

import json
import os

import pytest

import calibrate
import db
from calibrate import Candidate, grid_search, split_holdout


# --- pure helpers -------------------------------------------------------


def _cand(sid, ins_ret, ins_dd, ins_trades, fwd_ret):
    return Candidate(
        strategy_id=sid, name=f"s{sid}", symbol="AAA",
        insample={"total_return_pct": ins_ret, "max_drawdown_pct": ins_dd,
                  "num_trades": ins_trades},
        forward_return_pct=fwd_ret,
    )


def test_holdout_split_is_a_deterministic_partition():
    cands = [_cand(sid, 10, 5, 12, sid) for sid in (7, 3, 9, 1, 5, 11, 2, 8, 6)]

    train_a, hold_a = split_holdout(cands, every=3)
    train_b, hold_b = split_holdout(list(reversed(cands)), every=3)

    # order of the input does not change the split
    assert [c.strategy_id for c in hold_a] == [c.strategy_id for c in hold_b]
    assert [c.strategy_id for c in train_a] == [c.strategy_id for c in train_b]

    # every candidate lands in exactly one side
    all_ids = {c.strategy_id for c in cands}
    assert {c.strategy_id for c in train_a} | {c.strategy_id for c in hold_a} == all_ids
    assert not ({c.strategy_id for c in train_a} & {c.strategy_id for c in hold_a})

    # holdout is every 3rd id in sorted order: 1, 5, 8
    assert [c.strategy_id for c in hold_a] == [1, 5, 8]


def test_search_is_deterministic():
    cands = [
        _cand(1, 30, 8, 20, 12.0),
        _cand(2, 5, 30, 4, -3.0),
        _cand(3, 20, 12, 15, 9.0),
        _cand(4, 2, 40, 2, -1.0),
        _cand(5, 25, 10, 18, 11.0),
        _cand(6, 1, 50, 1, 0.0),
        _cand(7, 22, 14, 16, 7.0),
        _cand(8, 0, 20, 0, 0.5),
    ]
    first = grid_search(cands, min_promoted=3)
    for _ in range(5):
        assert grid_search(cands, min_promoted=3) == first
    assert first[0] is not None


# --- the full record, against a temp DB -------------------------------


def _seed_asof_run(conn, specs):
    """specs: list of (ins_ret, ins_dd, ins_trades, fwd_ret)."""
    run_id = db.start_run(conn)
    db.set_run_as_of(conn, run_id, "2026-06-17")
    for i, (ins_ret, ins_dd, ins_trades, fwd_ret) in enumerate(specs, start=1):
        sid = db.insert_strategy(
            conn, name=f"strat {i}", symbol="AAA",
            schema_json=json.dumps({"symbol": "AAA"}), rationale="r",
            source="llm", status="rejected", dedup_key=f"k{i}",
        )
        db.insert_backtest(
            conn, strategy_id=sid, run_id=run_id,
            metrics_json=json.dumps({"total_return_pct": ins_ret,
                                     "max_drawdown_pct": ins_dd,
                                     "num_trades": ins_trades}),
            equity_curve_json="[]", kind="insample", as_of="2026-06-17",
        )
        db.insert_backtest(
            conn, strategy_id=sid, run_id=run_id,
            metrics_json=json.dumps({"total_return_pct": fwd_ret,
                                     "max_drawdown_pct": 5.0, "num_trades": 3}),
            equity_curve_json="[]", kind="forward", as_of="2026-06-17",
        )
        db.insert_decision(conn, strategy_id=sid, run_id=run_id,
                           outcome="rejected", reason="x")
    db.finish_run(conn, run_id, n_generated=len(specs), n_promoted=0,
                  n_rejected=len(specs))
    return run_id


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(calibrate, "MIN_PROMOTED", 2)
    c = db.connect(tmp_path / "cal.db")
    yield c
    c.close()


def test_no_improvement_is_reported_not_silently_fitted(conn):
    # Every candidate clears every grid combination (huge return, tiny drawdown,
    # many trades), so no threshold move can raise the promoted set's mean
    # forward return above what the current gate already gets.
    _seed_asof_run(conn, [(100.0, 2.0, 40, r) for r in
                          (3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0)])
    rec = calibrate.calibrate(conn)

    assert rec["verdict"] == "no-improvement"
    assert rec["proposed"]["delta_vs_current_pp"] < calibrate.MIN_IMPROVEMENT_PP
    # the proposal is not a tighter fitted combination — it matches current
    assert rec["proposed"]["thresholds"] == rec["current"]["thresholds"]
    assert "Keep the current thresholds" in rec["recommendation"]
    assert rec["applied"] is False


def test_in_sample_gain_that_reverses_on_holdout_is_flagged(conn):
    # Construct so a tighter gate (high in-sample return) looks great on the
    # training candidates but picks the forward losers among the held-out ones.
    # sorted ids 1..12 -> holdout = 1, 4, 7, 10 ; train = the rest.
    specs = []
    for i in range(1, 13):
        held_out = (i - 1) % 3 == 0
        high_ins = i % 2 == 0  # half the candidates have a high in-sample return
        ins_ret = 40.0 if high_ins else 1.0
        if held_out:
            fwd = -10.0 if high_ins else 5.0   # tighter gate loses on holdout
        else:
            fwd = 15.0 if high_ins else -2.0   # tighter gate wins on train
        specs.append((ins_ret, 5.0, 12, fwd))
    _seed_asof_run(conn, specs)

    rec = calibrate.calibrate(conn)
    assert rec["proposed"]["delta_vs_current_pp"] > 0          # fitted gain exists
    assert rec["holdout"]["survives"] is False
    assert rec["verdict"] == "does-not-survive-holdout"
    assert rec["holdout"]["improvement_survival_fraction"] is None \
        or rec["holdout"]["improvement_survival_fraction"] <= 0
    assert rec["applied"] is False


def test_run_and_store_persists_and_is_readable(conn):
    _seed_asof_run(conn, [(v, 10.0, 12, v / 3) for v in
                          (0, 4, 8, 12, 16, 20, 24, 28, 32, 36)])
    cal_id, rec = calibrate.run_and_store(conn)

    row = db.latest_calibration(conn)
    assert row["id"] == cal_id
    assert bool(row["applied"]) is False
    stored = json.loads(row["record_json"])
    assert stored["verdict"] == rec["verdict"]
    assert stored["run_id"] == rec["run_id"]


# --- integration against the committed seed --------------------------


@pytest.mark.skipif(not os.path.exists("seed.db"), reason="seed.db not present")
def test_against_committed_seed():
    """The frozen seed must produce a well-formed record; on this data the
    in-sample gain does not survive the holdout (D10)."""
    c = db.connect("seed.db")
    try:
        rec = calibrate.calibrate(c)
    finally:
        c.close()
    assert rec["run_id"] == 5 and rec["as_of"] == "2026-06-17"
    assert rec["n_candidates"] == 74
    assert rec["current"]["n_promoted"] == 8
    assert rec["proposed"]["delta_vs_current_pp"] > 0
    assert rec["verdict"] == "does-not-survive-holdout"
    assert rec["applied"] is False
