"""Checks every watchlist handle through Sorsa /info-batch (2 requests for ~150 handles).

    SORSA_API_KEY=... python -m radar.verify_watchlist

Prints dead/renamed handles and follower counts, so the X List can be built from live accounts only.
"""
import json
import os
import sys

from .run import ROOT
from .sources import Sorsa


def main():
    with open(os.path.join(ROOT, "watchlist.json"), encoding="utf-8") as f:
        watch = json.load(f)
    handles = [h for g in watch["groups"].values() for h in g["handles"]]
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    sorsa = Sorsa(os.environ.get("SORSA_API_KEY"), cfg["sorsa"]["base"], {}, 10)
    found = {}
    for i in range(0, len(handles), 100):
        for u in sorsa.info_batch(handles[i:i + 100]).get("users") or []:
            found[(u.get("username") or "").lower()] = u
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    missing = [h for h in handles if h.lower() not in found]
    for h in handles:
        u = found.get(h.lower())
        if u:
            print(f"ok   @{h:<18} {int(u.get('followers_count') or 0):>10,}")
    print(f"\n{len(handles) - len(missing)}/{len(handles)} live. Not found: {', '.join('@' + m for m in missing) or 'none'}")


if __name__ == "__main__":
    main()
