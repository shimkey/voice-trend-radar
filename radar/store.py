"""state.json (radar memory) and feed.json (what people see). Both live in the data dir."""
import json
import os
import time

STATE_DEFAULT = {
    "version": 1,
    "born": None,
    "last_run": {},
    "budget": {},
    "tweets": {},
    "authors": {},
    "keys": {},
    "trends": {},
    "poly": {"top": [], "flagged": {}},
    "coins": {"seen": {}},
    "news": {"seen": {}, "flagged": {}},
    "cid_topic": {},
    "rejected": {},
    "alerts": [],
    "digests": {},
    "errors": {},
}


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    for k, v in default.items():
        data.setdefault(k, json.loads(json.dumps(v)))
    return data


def save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def due(state, name, every_min, now, force=False):
    last = state["last_run"].get(name, 0)
    if force or now - last >= every_min * 60 - 30:  # 30s slack so a 5-min cron hits a 10-min cadence
        state["last_run"][name] = now
        return True
    return False


def prune(state, feed, keep_days, now):
    day = 86400
    state["tweets"] = {k: t for k, t in state["tweets"].items() if now - t["ts"] < day}
    cutoff_h = int((now - 7 * day) // 3600)
    for key in list(state["keys"]):
        hours = {h: c for h, c in state["keys"][key].items() if int(h) >= cutoff_h}
        # one-off mentions older than a day are noise, drop them to keep state small
        if not hours or (sum(hours.values()) == 1 and int(max(hours)) < (now - day) // 3600):
            del state["keys"][key]
        else:
            state["keys"][key] = hours
    for bucket in ("rejected", "cid_topic"):
        state[bucket] = {k: v for k, v in state[bucket].items()
                         if now - (v["ts"] if isinstance(v, dict) else v) < 2 * day}
    state["news"]["seen"] = {k: v for k, v in state["news"]["seen"].items() if now - v["ts"] < 2 * day}
    state["news"]["flagged"] = {k: v for k, v in state["news"]["flagged"].items() if now - v < 2 * day}
    state["poly"]["flagged"] = {k: v for k, v in state["poly"]["flagged"].items() if now - v < 2 * day}
    state["coins"]["seen"] = {k: v for k, v in state["coins"]["seen"].items() if now - v < 3 * day}
    state["alerts"] = [t for t in state["alerts"] if now - t < 3600]
    state["digests"] = {k: v for k, v in state["digests"].items() if now - v < 3 * day}
    feed["topics"] = [t for t in feed["topics"] if now - t["last_seen"] < keep_days * day]


def stamp(feed, state, cfg, now, run_info):
    feed["updated_at"] = now
    feed["health"] = {
        "errors": state["errors"],
        "budget": state["budget"],
        "daily_budget": cfg["sorsa"]["daily_budget"],
        "x_enabled": run_info.get("x_enabled", False),
        "llm_enabled": run_info.get("llm_enabled", False),
        "run_seconds": round(time.time() - now, 1),
        "counts": run_info.get("counts", {}),
    }
