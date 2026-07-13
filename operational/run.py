"""FloodNowcasting.eu 2.0 - operational runner.

Usage:
  python run.py --precompute        # build catchment + pixel-map cache (once)
  python run.py --once              # run a single cycle now
  python run.py --loop 30           # run every 30 min (clamped to [15,120])

The rolling store persists in operational/state/, so repeated --once calls (e.g.
from cron / Task Scheduler / GitHub Actions) accumulate a 6-10 h window over time.
"""
import argparse, time, sys
import config as C
import pipeline as P


def _clamp_cadence(mins):
    lo, hi = 15, 120
    m = max(lo, min(hi, int(mins)))
    if m != int(mins):
        print(f"cadence clamped to {m} min (allowed {lo}-{hi})")
    return m


def main():
    ap = argparse.ArgumentParser(description="FloodNowcasting.eu 2.0 operational pipeline")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--precompute", action="store_true", help="build catchment/pixel cache and exit")
    g.add_argument("--once", action="store_true", help="run a single cycle")
    g.add_argument("--loop", type=int, metavar="MIN", help="run every MIN minutes (15-120)")
    args = ap.parse_args()

    print(f"domain {C.DOMAIN_BBOX} | levels {C.LEVELS} | window {C.WINDOW_H} h")

    if args.precompute:
        P.precompute(force=True); return
    if args.once:
        P.run_once(); return

    cadence = _clamp_cadence(args.loop)
    print(f"looping every {cadence} min - Ctrl+C to stop")
    while True:
        t0 = time.time()
        try:
            P.run_once()
        except Exception as e:                      # keep the loop alive across transient errors
            print(f"cycle error: {type(e).__name__}: {e}", file=sys.stderr)
        sleep_s = max(5, cadence*60 - (time.time() - t0))
        print(f"sleeping {sleep_s/60:.1f} min ...\n")
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
