"""Turns raw items into candidates: things that look like they are blowing up right now.

A candidate is a dict:
  cid      stable id, so the same story maps to the same topic run after run
  source   x | cluster | trend | polymarket | coingecko | news
  title, text, url (main post or page), search_url (X search for the topic)
  signals  short human-readable reasons, in Russian (they go to Telegram as is)
  heat     0..100, how loud it is right now (not how relevant, that is the LLM's job)
"""
import math
import re
import statistics

from .sources import engagement
from .util import jaccard, topic_keys, words, x_search_url

MAJOR_COINS = {"BTC", "ETH", "SOL", "USDT", "USDC", "BNB", "XRP", "DOGE", "TRX", "ADA", "WBTC", "STETH", "DAI"}
SPORTS_TAGS = {"sports", "nba", "nfl", "mlb", "nhl", "soccer", "tennis", "golf", "ufc", "basketball",
               "football", "baseball", "hockey", "cricket", "f1", "games"}
NON_LATIN = re.compile(r"[؀-ۿ฀-๿぀-ヿ㐀-鿿가-힯]")


def fmt(n):
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suf
    return str(int(n))


def ago(minutes):
    return f"{int(minutes)} мин" if minutes < 90 else f"{minutes / 60:.1f} ч".replace(".0 ", " ")


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- tweets

def ingest_tweets(state, items, now):
    """Store/refresh tweets, count topic keys once per tweet, learn each author's normal engagement."""
    fresh = 0
    for it in items:
        if not it["tweet_id"] or it["is_reply"]:
            continue
        tid = it["tweet_id"]
        eng = engagement(it["metrics"])
        t = state["tweets"].get(tid)
        if t is None:
            fresh += 1
            keys = sorted(topic_keys(it["text"] + " " + it.get("quoted_text", "")))
            t = state["tweets"][tid] = {
                "author": it["author"], "followers": it["followers"], "ts": it["ts"], "via": it["via"],
                "text": it["text"][:400], "url": it["url"], "media": it["media"], "keys": keys, "obs": [],
            }
            hour = str(int(now // 3600))
            for k in keys:
                state["keys"].setdefault(k, {})
                state["keys"][k][hour] = state["keys"][k].get(hour, 0) + 1
        t["followers"] = it["followers"] or t["followers"]
        t["views"] = it["metrics"]["views"]
        t["obs"] = (t["obs"] + [[round(now), eng, it["metrics"]["views"]]])[-6:]
        age = (now - t["ts"]) / 60
        if age >= 60 and not t.get("based") and t["via"] == "list":
            samples = state["authors"].setdefault(t["author"], [])
            samples.append(eng)
            del samples[:-30]
            t["based"] = True
    return fresh


def recheck_ids(state, cfg, now, limit=100):
    """Which tweets deserve a fresh metrics pull: young list tweets with early traction or needing a baseline."""
    max_age = cfg["detect"]["max_age_h"] * 3600
    picks = []
    for tid, t in state["tweets"].items():
        age = now - t["ts"]
        if t["via"] != "list" or age > max_age or age < 15 * 60:
            continue
        last_obs = t["obs"][-1][0] if t["obs"] else 0
        if now - last_obs < 15 * 60:
            continue
        eng = t["obs"][-1][1] if t["obs"] else 0
        rate = eng / max(age / 60, 1)
        need_base = age >= 60 * 60 and not t.get("based")
        picks.append((rate + (5 if need_base else 0), tid))
    return [tid for _, tid in sorted(picks, reverse=True)[:limit]]


def tweet_candidates(state, cfg, now):
    d = cfg["detect"]
    out = []
    for tid, t in state["tweets"].items():
        age_min = (now - t["ts"]) / 60
        if age_min > d["max_age_h"] * 60 or not t["obs"]:
            continue
        eng = t["obs"][-1][1]
        if eng < d["min_eng"]:
            continue
        rate = eng / max(age_min, 5)
        signals = []
        if t["via"] in ("list", "list-rt"):
            base = state["authors"].get(t["author"]) or []
            if len(base) >= 5:
                ratio = eng / max(statistics.median(base), 20)
                if ratio < d["outperform_x"]:
                    continue
                signals.append(f"×{ratio:.1f} к обычным постам @{t['author']}")
            else:
                per1k = eng / max(t["followers"], 1000) * 1000
                if per1k < d["cold_eng_per_1k_followers"]:
                    continue
                ratio = per1k / d["cold_eng_per_1k_followers"] * d["outperform_x"]
                signals.append(f"{per1k:.0f} реакций на 1k подписчиков @{t['author']}")
        else:  # discovery / firehose search already filtered by min_faves
            ratio = d["outperform_x"]
            signals.append(f"залетел в поиске X ({t['via']})")
        signals.append(f"{fmt(t.get('views', 0))} просмотров, {fmt(eng)} реакций за {ago(age_min)}")
        heat = clamp(22 * math.log2(max(ratio, 1)) + 18 * math.log10(1 + rate))
        out.append({
            "cid": f"x:{tid}", "source": "x", "title": t["text"][:140], "text": t["text"],
            "url": t["url"], "author": t["author"], "media": t["media"],
            "search_url": None, "signals": signals, "heat": round(heat, 1), "ts": t["ts"],
        })
    return out


def cluster_candidates(state, cfg, now):
    """Same name/cashtag from several watched accounts within a few hours, well above its usual chatter."""
    d = cfg["detect"]
    window = d["cluster_window_h"] * 3600
    min_authors = d["cluster_min_authors"] + (2 if now - (state.get("born") or now) < 86400 else 0)
    recent = [t for t in state["tweets"].values() if now - t["ts"] < window]
    by_key = {}
    for t in recent:
        for k in t["keys"]:
            by_key.setdefault(k, []).append(t)
    out = []
    now_h = int(now // 3600)
    for k, ts in by_key.items():
        authors = {t["author"] for t in ts}
        if len(authors) < min_authors:
            continue
        hours = state["keys"].get(k, {})
        past = sum(c for h, c in hours.items() if int(h) <= now_h - d["cluster_window_h"])
        slots = (7 * 24 - d["cluster_window_h"]) / d["cluster_window_h"]
        baseline = past / slots
        spike = len(ts) / max(baseline, 0.5)
        if spike < d["cluster_spike_x"]:
            continue
        top = max(ts, key=lambda t: t["obs"][-1][1] if t["obs"] else 0)
        names = ", ".join("@" + a for a in sorted(authors)[:5])
        heat = clamp(15 * len(authors) + 12 * math.log2(spike))
        out.append({
            "cid": f"k:{k}", "source": "cluster", "title": k, "text": " | ".join(t["text"][:160] for t in ts[:6]),
            "url": top["url"], "author": top["author"], "media": top["media"],
            "search_url": x_search_url(k), "heat": round(heat, 1), "ts": min(t["ts"] for t in ts),
            "signals": [f"«{k}»: {len(authors)} аккаунтов за {d['cluster_window_h']} ч ({names})",
                        f"×{spike:.0f} к обычному фону"],
        })
    return out


# ---------------------------------------------------------------- X trends by country

def trend_candidates(state, snaps_by_loc, cfg, now):
    top_n = cfg["trends"]["top_n"]
    current = {}
    for loc, snaps in snaps_by_loc.items():
        if not snaps:
            continue
        st = state["trends"].setdefault(loc, {"last_ts": 0})
        latest_ts, latest = snaps[0]
        if latest_ts <= st["last_ts"]:
            continue
        st["last_ts"] = latest_ts
        prev_sets = [{n.lower() for _, n, _ in tr[:30]} for _, tr in snaps[1:7]]
        prev_rank = {n.lower(): r for r, n, _ in (snaps[1][1] if len(snaps) > 1 else [])}
        for rank, name, _count in latest[:top_n]:
            low = name.lower()
            if len(NON_LATIN.findall(name)) > len(name) / 3:
                continue
            hours_on = sum(1 for _, tr in snaps if any(n.lower() == low for _, n, _ in tr))
            fresh = not any(low in s for s in prev_sets)
            climbed = prev_rank.get(low, 99) - rank >= 10 and rank <= 10
            if not (fresh or climbed):
                continue
            c = current.setdefault(low, {"name": name, "locs": [], "best": 99, "hours": hours_on, "fresh": fresh})
            c["locs"].append(loc.replace("-", " ") or "worldwide")
            c["best"] = min(c["best"], rank)
    out = []
    for low, c in current.items():
        heat = clamp((top_n + 1 - c["best"]) / top_n * 60 + 15 * (len(c["locs"]) - 1) + (10 if c["fresh"] else 0))
        where = ", ".join(c["locs"])
        out.append({
            "cid": f"t:{low}", "source": "trend", "title": c["name"], "text": f"X trend in {where}",
            "url": None, "search_url": x_search_url(c["name"]), "heat": round(heat, 1), "ts": now,
            "signals": [f"тренд X #{c['best']} ({where})",
                        "только что ворвался" if c["fresh"] else f"резко вырос в топ-10, в трендах {c['hours']} ч"],
        })
    return out


# ---------------------------------------------------------------- Polymarket

def poly_candidates(state, items, cfg, now):
    p = cfg["polymarket"]
    prev_top = set(state["poly"]["top"])
    top20 = [i["id"] for i in items[:20]]
    state["poly"]["top"] = top20
    out = []
    for it in items:
        tags = {t.lower() for t in it["tags"]}
        # game markets swing to 0/100 every night, that is a score, not a trend
        if tags & SPORTS_TAGS or " vs. " in it["title"] or it["closed_soon"]:
            continue
        reasons = []
        if it["rank"] <= 20 and prev_top and it["id"] not in prev_top:
            reasons.append(f"ворвался в топ-20 Polymarket по объёму за сутки (#{it['rank']})")
        if it["max_move"] >= p["move_threshold"] and it["volume24"] >= p["min_volume24"]:
            reasons.append(f"цена исхода сдвинулась на {it['max_move'] * 100:.0f} п.п. за сутки")
        if not reasons or now - state["poly"]["flagged"].get(it["id"], 0) < 12 * 3600:
            continue
        state["poly"]["flagged"][it["id"]] = now
        reasons.append(f"объём за 24ч ${fmt(it['volume24'])}")
        if it["lead_yes"] is not None:
            reasons.append(f"«{it['lead_question'][:80]}»: {it['lead_yes'] * 100:.0f}%")
        heat = clamp(25 + 10 * math.log10(max(it["volume24"], 1) / 1e5 + 1) * 3 + it["max_move"] * 60)
        out.append({
            "cid": it["id"], "source": "polymarket", "title": it["title"],
            "text": (it["lead_question"] + ". " + it["text"])[:500], "url": it["url"],
            "search_url": x_search_url(it["title"][:60]), "heat": round(heat, 1), "ts": now,
            "signals": reasons, "tags": it["tags"],
        })
    return out


# ---------------------------------------------------------------- CoinGecko trending

def coin_candidates(state, items, cfg, now):
    seen = state["coins"]["seen"]
    first_run = not seen
    out = []
    for it in items[: cfg["coingecko"]["top_n"]]:
        sym = it["symbol"]
        if sym in MAJOR_COINS:
            continue
        new = now - seen.get(sym, 0) > 24 * 3600
        seen[sym] = now
        if not new or first_run:
            continue
        sig = [f"#{it['rank']} в трендах поиска CoinGecko"]
        if isinstance(it["change24"], (int, float)):
            sig.append(f"цена {it['change24']:+.0f}% за сутки")
        out.append({
            "cid": f"cg:{sym}", "source": "coingecko", "title": it["title"], "text": it["text"],
            "url": it["url"], "search_url": it["search_url"], "ts": now, "signals": sig,
            "heat": round(clamp(70 - 6 * it["rank"] + min(abs(it["change24"] or 0), 100) / 4), 1),
        })
    return out


# ---------------------------------------------------------------- news

def news_candidates(state, items, cfg, now):
    n = cfg["news"]
    seen = state["news"]["seen"]
    for it in items:
        if now - it["ts"] < n["max_age_h"] * 3600:
            seen.setdefault(it["id"], {"ts": it["ts"], "title": it["title"], "outlet": it["outlet"],
                                       "url": it["url"], "category": it["category"], "text": it["text"][:300]})
    recent = [dict(v, id=k) for k, v in seen.items() if now - v["ts"] < n["max_age_h"] * 3600]
    for r in recent:
        r["w"] = words(r["title"])
    out = []
    flagged = state["news"]["flagged"]
    for r in recent:
        if r["id"] in flagged:
            continue
        same = [o for o in recent if jaccard(r["w"], o["w"]) >= 0.3]
        outlets = {o["outlet"] for o in same}
        title_low = " " + r["title"].lower() + " "
        breaking = r["category"] == "crypto" and now - r["ts"] < 3 * 3600 and any(
            w in title_low for w in n["breaking_words"])
        if len(outlets) < n["min_outlets"] and not breaking:
            continue
        for o in same:
            flagged[o["id"]] = now
        sig = [f"пишут {len(outlets)} изданий: {', '.join(sorted(outlets)[:4])}"] if len(outlets) > 1 else \
              [f"срочное от {r['outlet']}"]
        out.append({
            "cid": f"n:{r['id']}", "source": "news", "title": r["title"], "text": r["text"],
            "url": r["url"], "search_url": x_search_url(" ".join(sorted(r["w"], key=len, reverse=True)[:4])),
            "ts": r["ts"], "signals": sig, "category_hint": r["category"],
            "heat": round(clamp(20 + 15 * len(outlets) + (20 if breaking else 0)), 1),
        })
    return sorted(out, key=lambda c: -c["heat"])[:6]
