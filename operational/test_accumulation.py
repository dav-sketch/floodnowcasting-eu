"""Proof that rolling accumulation is cadence-independent and never double-counts.

Simulates the SAME underlying 10-min radar-frame timeline polled at several cron
cadences (10/15/30/60/120 min), each poll seeing a 2 h 'past' window (as RainViewer
does), and checks the 6 h and 10 h per-basin totals are identical across cadences
and equal to the analytic sum of the distinct frames. Run: python test_accumulation.py
"""
import numpy as np, pandas as pd
import pipeline as P

WINDOW_H = 10.0
DT = 600                      # radar frames every 10 min
NFR = 84                      # 14 h of frames (so pruning to WINDOW_H is exercised)
PAST_H = 2.0                  # RainViewer exposes ~2 h of past per poll
t0 = 1_700_000_000
times = [t0 + i * DT for i in range(NFR)]
mm = {t: round(0.5 + (i % 7) * 0.3, 3) for i, t in enumerate(times)}   # known mm/frame

def _row(t): return pd.Series({"B1": mm[t]}, name=t)

def simulate(cadence_min):
    """Replay the timeline polled every cadence_min, exactly as run_once would."""
    cad, past = cadence_min * 60, int(PAST_H * 3600)
    store = pd.DataFrame()
    tc = t0
    while tc <= times[-1] + cad:
        seen = {int(x) for x in store.index}
        rows = [_row(t) for t in times if tc - past < t <= tc and t not in seen]
        store = P.prune_store(P.merge_frames(store, rows), WINDOW_H)
        tc += cad
    return store

def analytic(D_h):
    now = times[-1]
    kept = [t for t in times if t >= now - WINDOW_H * 3600]     # survives pruning
    return sum(mm[t] for t in kept if now - t <= D_h * 3600 + 1e-6)

ok = True
for D in (6.0, 10.0):
    truth = analytic(D)
    vals = {}
    for c in (10, 15, 30, 60, 120):
        acc, _, _ = P.window_accumulate(simulate(c), ["B1"], np.array([D]))
        vals[c] = round(float(acc[0]), 6)
    same = len(set(vals.values())) == 1
    correct = abs(next(iter(vals.values())) - truth) < 1e-6
    ok &= same and correct
    print(f"  {D:>4.0f} h window | cadences(min)->mm {vals} | analytic {truth:.3f} "
          f"| {'OK' if same and correct else 'FAIL'}")

# explicit: re-seeing the identical 2 h 'past' must not inflate anything
st = P.merge_frames(pd.DataFrame(), [_row(t) for t in times[:13]])
n1, s1 = len(st), float(st["B1"].sum())
st = P.merge_frames(st, [_row(t) for t in times[:13]])          # same frames again
n2, s2 = len(st), float(st["B1"].sum())
red_ok = n1 == n2 and abs(s1 - s2) < 1e-9
ok &= red_ok
print(f"  redundant re-poll | rows {n1}->{n2}, sum {s1:.3f}->{s2:.3f} | {'OK' if red_ok else 'FAIL'}")

print("ALL PASS - accumulation is cadence-independent, no double counting." if ok else "FAILURES")
assert ok
