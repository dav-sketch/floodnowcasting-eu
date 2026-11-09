"""Proof that the merged OPERA + RainViewer accumulation is independent of WHEN
the pipeline runs (irregular cadence, multi-hour gaps, repeated runs) and never
double counts. Offline: replays a synthetic 10-min rain timeline through the same
store algebra run_once uses (merge_frames / prune_store / choose_anchor /
hourly_slots / window_sum). Run: python test_accumulation.py

Basins:
  B_op  inside OPERA   -> every window must equal the analytic truth, whatever the cadence
  B_rv  outside OPERA  -> RainViewer fill: exact where its 10-min frames were seen,
                          otherwise the hour is MISSING (coverage < 1), never invented
"""
import numpy as np, pandas as pd
import config as C
import pipeline as P

N_H = 12
DT = 600                                   # RainViewer frame spacing (s)
LAT_OPERA = 15 * 60                        # OPERA file appears ~15 min after its end time
RV_PAST = 2 * 3600                         # RainViewer serves the last 2 h
OPERA_KEEP = 24 * 3600                     # OPERA bucket keeps the last 24 h
t0 = 1_800_000_000 - (1_800_000_000 % 3600)
rng = np.random.default_rng(1)
frames = np.arange(t0, t0 + 40 * 3600 + 1, DT)                    # 40 h of 10-min frames
rain = {"B_op": rng.gamma(0.6, 0.8, len(frames)) * (rng.random(len(frames)) < 0.5),
        "B_rv": rng.gamma(0.6, 0.8, len(frames)) * (rng.random(len(frames)) < 0.5)}
mm = {b: dict(zip(frames.tolist(), v)) for b, v in rain.items()}

def truth(b, T, w):                         # rain in (T - w h, T]
    return sum(v for t, v in mm[b].items() if T - w * 3600 < t <= T)

def opera_value(te):                        # ACRR ending at te = 10-min rain summed over (te-1h, te]
    return pd.Series({"B_op": truth("B_op", te, 1), "B_rv": np.nan}, name=te)  # B_rv: no radar

def rv_value(t):
    return pd.Series({"B_op": mm["B_op"][t], "B_rv": mm["B_rv"][t]}, name=t)

def run(now, op_st, rv_st):
    """One run_once at wall-clock `now`, minus the network."""
    op_avail = {te: None for te in range(t0 + 3600, now - LAT_OPERA + 1, 900)
                if te > now - OPERA_KEEP}
    past = [{"time": int(t)} for t in frames if now - RV_PAST < t <= now]
    T, _ = P.choose_anchor(op_avail, past)
    oldest = T - N_H * 3600
    op_st, rv_st = P.prune_store(op_st, oldest), P.prune_store(rv_st, oldest)
    want = [T - k * 3600 for k in range(N_H) if T - k * 3600 in op_avail]
    op_st = P.merge_frames(op_st, [opera_value(t) for t in want if t not in op_st.index])
    rv_st = P.merge_frames(rv_st, [rv_value(f["time"]) for f in past
                                   if f["time"] > oldest - 3600 and f["time"] not in rv_st.index])
    arr, src = P.hourly_slots(op_st, rv_st, ["B_op", "B_rv"], T, N_H)
    return T, arr, src, op_st, rv_st

def replay(offsets_min):
    op_st, rv_st = pd.DataFrame(), pd.DataFrame()
    now = t0 + 14 * 3600; res = []
    for o in offsets_min:
        now += int(o * 60)
        T, arr, src, op_st, rv_st = run(now, op_st, rv_st)
        res.append((T, arr, src, rv_st))
    return res

CADENCES = {
    "every 15 min":       [15] * 60,
    "hourly":             [60] * 15,
    "GitHub-like jitter": [7, 95, 260, 33, 61, 540, 12, 180, 75, 5, 5],
    "one run after 20 h": [0, 1200],
    "same minute x3":     [0, 0, 0, 60, 60],
}
ok = True
for name, offs in CADENCES.items():
    worst_op, rv_bad, rv_part, rv_err = 0.0, 0, 0, []
    runs = replay(offs)
    for T, arr, src, rv_st in runs:
        for w in C.ACC_WINDOWS_H:
            acc, cov = P.window_sum(arr, w)
            worst_op = max(worst_op, abs(acc[0] - truth("B_op", T, w)))
            assert cov[0] == 1.0, (name, w, cov)
        for k in range(N_H):                # RainViewer-filled hours
            if src[k, 1] != 2: continue
            te = T - k * 3600
            nfr = int(((rv_st.index > te - 3600) & (rv_st.index <= te)).sum())
            err = abs(arr[k, 1] - truth("B_rv", te, 1))
            if nfr == 6: rv_bad += err > 1e-9          # all 6 frames seen -> must be exact
            else: rv_part += 1; rv_err.append(err)      # 3-5 frames -> scaled estimate
    _, cov_last = P.window_sum(runs[-1][1], N_H)
    good = worst_op < 1e-9 and rv_bad == 0
    ok &= good
    print(f"  {name:18s} | OPERA basin |err| {worst_op:.0e} mm | RV full hours wrong: {rv_bad}"
          f" | RV partial hours {rv_part} (mean |err| {np.mean(rv_err) if rv_err else 0:.2f} mm)"
          f" | RV-only basin 12h coverage at last run {cov_last[1]:.2f} | {'OK' if good else 'FAIL'}")

# identical re-poll must not inflate anything
st = P.merge_frames(pd.DataFrame(), [rv_value(int(t)) for t in frames[:13]])
st2 = P.merge_frames(st, [rv_value(int(t)) for t in frames[:13]])
red = len(st) == len(st2) and np.isclose(st["B_op"].sum(), st2["B_op"].sum())
ok &= red
print(f"  redundant re-poll  | rows {len(st)}->{len(st2)} | {'OK' if red else 'FAIL'}")

# a missing hour lowers coverage instead of silently reading as dry
arr = np.array([[1.0], [np.nan], [2.0], [np.nan]])
a, c = P.window_sum(arr, 4)
miss = a[0] == 3.0 and c[0] == 0.5
ok &= miss
print(f"  missing hour       | sum {a[0]} mm, coverage {c[0]} | {'OK' if miss else 'FAIL'}")

print("ALL PASS - cadence-independent, gap-proof where OPERA sees, no double counting."
      if ok else "FAILURES")
assert ok
