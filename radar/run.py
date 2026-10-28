"""One radar tick: fetch what is due, detect, judge with Claude, alert, save.

    python -m radar.run --data data            # normal tick (cron calls this every ~5 min)
    python -m radar.run --data data --dry      # no Telegram, print alerts instead
    python -m radar.run --data data --force    # ignore cadences, fetch everything now
"""
import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import detect, llm, store, telegram
from .sources import (BudgetExceeded, Sorsa, fetch_coingecko, fetch_polymarket, fetch_rss, fetch_trends24,
                      tweet_item)
from .util import short_hash, x_search_url

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MSK = timezone(timedelta(hours=3))


def load_json(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return json.load(f)


def chunks_of_handles(handles, extra, max_len=420):
    """Split handles into `(from:a OR from:b ...) extra` queries that stay under X's query length."""
    out, cur = [], []
    for h in handles:
        trial = cur + [h]
        q = "(" + " OR ".join("from:" + x for x in trial) + ") " + extra
        if len(q) > max_len and cur:
            out.append(cur)
            cur = [h]
        else:
            cur = trial
    if cur:
        out.append(cur)
    return ["(" + " OR ".join("from:" + x for x in c) + ") " + extra for c in out]


class Run:
    def __init__(self, args):
        self.args = args
        self.cfg = load_json("config.json")
        self.watch = load_json("watchlist.json")
        self.now = time.time()
        self.state_path = os.path.join(args.data, "state.json")
        self.feed_path = os.path.join(args.data, "feed.json")
        self.state = store.load(self.state_path, store.STATE_DEFAULT)
        self.feed = store.load(self.feed_path, {"topics": []})
        self.state["born"] = self.state["born"] or self.now
        env = os.environ.get
        self.list_id = env("RADAR_LIST_ID") or self.cfg["sorsa"]["list_id"]
        self.feed_url = env("RADAR_FEED_URL") or self.cfg["feed_url"]
        self.tg_token = env("TELEGRAM_BOT_TOKEN")
        chats = env("TELEGRAM_CHAT_IDS") or env("TELEGRAM_REPORT_CHAT_ID") or ""
        self.tg_chats = [c.strip() for c in chats.split(",") if c.strip()]
        self.anthropic_key = None if args.no_llm else env("ANTHROPIC_API_KEY")
        self.sorsa = Sorsa(env("SORSA_API_KEY"), self.cfg["sorsa"]["base"], self.state["budget"],
                           self.cfg["sorsa"]["daily_budget"])
        self.candidates = []
        self.counts = {}

    # ------------------------------------------------------------ helpers

    def due(self, name, every):
        return store.due(self.state, name, every, self.now, self.args.force)

    def guarded(self, name, fn):
        try:
            result = fn()
            self.state["errors"].pop(name, None)
            return result
        except BudgetExceeded as e:
            self.state["errors"][name] = f"skip: {e}"
        except Exception as e:  # one broken source must not kill the tick
            self.state["errors"][name] = f"{type(e).__name__}: {str(e)[:200]}"
            if self.args.verbose:
                traceback.print_exc()
        return None

    def ingest(self, raw, via):
        items = [tweet_item(t, via) for t in raw or []]
        n = detect.ingest_tweets(self.state, items, self.now)
        self.counts[via] = self.counts.get(via, 0) + n
        return items

    # ------------------------------------------------------------ X via Sorsa

    def fetch_x(self):
        if not self.sorsa.enabled:
            self.state["errors"]["x"] = "нет SORSA_API_KEY, X-слой выключен"
            return
        s = self.cfg["sorsa"]
        since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        if self.due("list", s["every_min"]["list"]):
            self.guarded("x_list", self._fetch_list if self.list_id else self._fetch_list_fallback)
        if self.due("recheck", s["every_min"]["recheck"]):
            ids = detect.recheck_ids(self.state, self.cfg, self.now)
            if ids:
                self.guarded("x_recheck", lambda: self.ingest(self.sorsa.tweets_bulk(ids).get("tweets"), "list"))
        if self.due("firehose", s["every_min"]["firehose"]):
            for name, g in self.watch["groups"].items():
                if g["mode"] != "firehose":
                    continue
                for q in chunks_of_handles(g["handles"], f"min_faves:{g['min_faves']} -filter:replies since:{since}"):
                    self.guarded(f"x_{name}", lambda q=q: self.ingest(self.sorsa.search(q).get("tweets"), "firehose"))
        if self.due("discovery", s["every_min"]["discovery"]):
            for i, q in enumerate(s["discovery_queries"]):
                self.guarded(f"x_discovery{i}",
                             lambda q=q: self.ingest(self.sorsa.search(f"{q} since:{since}").get("tweets"), "discovery"))
        if self.due("usage", s["every_min"]["usage"]):
            self.guarded("x_usage", lambda: self.state["budget"].update(plan=self.sorsa.usage()))

    def _fetch_list(self):
        cursor, known = None, set(self.state["tweets"])
        for _ in range(self.cfg["sorsa"]["list_pages"]):
            page = self.sorsa.list_tweets(self.list_id, cursor)
            raw = page.get("tweets") or []
            items = self.ingest([t for t in raw if not t.get("retweeted_status")], "list")
            self.ingest([t for t in raw if t.get("retweeted_status")], "list-rt")
            cursor = page.get("next_cursor")
            # stop once the page reaches tweets we already had
            if not cursor or any(i["tweet_id"] in known for i in items):
                break

    def _fetch_list_fallback(self):
        """No X List yet: walk the list accounts round-robin with from: searches (slower, costs more)."""
        handles = [h for g in self.watch["groups"].values() if g["mode"] == "list" for h in g["handles"]]
        since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        queries = chunks_of_handles(handles, f"-filter:replies since:{since}")
        rr = self.state.setdefault("rr", 0) % len(queries)
        for q in (queries[rr], queries[(rr + 1) % len(queries)]):
            self.ingest(self.sorsa.search(q).get("tweets"), "list")
        self.state["rr"] = rr + 2

    # ------------------------------------------------------------ free sources

    def fetch_free(self):
        c = self.cfg
        if self.due("trends", c["trends"]["every_min"]):
            snaps = {}
            for loc, slug in c["trends"]["locations"].items():
                got = self.guarded(f"trends_{loc}", lambda slug=slug: fetch_trends24(slug))
                if got:
                    snaps[loc] = got
            self.candidates += detect.trend_candidates(self.state, snaps, c, self.now)
        if self.due("polymarket", c["polymarket"]["every_min"]):
            items = self.guarded("polymarket", lambda: fetch_polymarket(c["polymarket"]["top_n"]))
            if items:
                self.candidates += detect.poly_candidates(self.state, items, c, self.now)
        if self.due("coingecko", c["coingecko"]["every_min"]):
            items = self.guarded("coingecko", fetch_coingecko)
            if items:
                self.candidates += detect.coin_candidates(self.state, items, c, self.now)
        if self.due("news", c["news"]["every_min"]):
            items = []
            for name, (cat, url) in c["news"]["feeds"].items():
                items += self.guarded(f"news_{name}", lambda n=name, cat=cat, url=url: fetch_rss(n, cat, url)) or []
            self.candidates += detect.news_candidates(self.state, items, c, self.now)

    # ------------------------------------------------------------ topics

    def topic_by_key(self):
        return {t["key"]: t for t in self.feed["topics"]}

    def refresh_existing(self, topics):
        """Candidates already mapped to a topic just refresh its numbers, no LLM call."""
        new = []
        for c in self.candidates:
            link = self.state["cid_topic"].get(c["cid"])
            t = topics.get(link["key"]) if link else None
            if t:
                self.merge(t, c)
                link["ts"] = self.now
            elif self.now - self.state["rejected"].get(c["cid"], 0) > 24 * 3600:
                new.append(c)
        return new

    def merge(self, t, c):
        t["last_seen"] = self.now
        t["heat"] = c["heat"]
        t["peak_heat"] = max(t.get("peak_heat", 0), c["heat"])
        t["signals"] = c["signals"]
        if c["source"] not in t["sources"]:
            t["sources"].append(c["source"])
        if c.get("url") and "/status/" in (c["url"] or "") and "/status/" not in (t.get("main_url") or ""):
            t["main_url"], t["media"] = c["url"], c.get("media")
        t["main_url"] = t.get("main_url") or c.get("url")
        t["search_url"] = t.get("search_url") or c.get("search_url")
        if not any(i["cid"] == c["cid"] for i in t["items"]):
            t["items"] = (t["items"] + [{"cid": c["cid"], "source": c["source"], "title": c["title"][:160],
                                         "url": c.get("url"), "author": c.get("author")}])[-8:]
        t["score"] = round(t["relevance"] * 7 + t["peak_heat"] * 0.3, 1)

    def judge_new(self, new, topics):
        new = sorted(new, key=lambda c: -c["heat"])[: self.cfg["detect"]["max_candidates_per_run"]]
        if not new:
            return
        recent = sorted((t for t in topics.values() if self.now - t["last_seen"] < 12 * 3600),
                        key=lambda t: -t["last_seen"])[:40]
        verdicts = None
        if self.anthropic_key:
            got = self.guarded("llm", lambda: llm.judge(new, recent, self.cfg, self.anthropic_key))
            if got:
                verdicts, usage = got
                self.counts["llm_in"] = getattr(usage, "input_tokens", 0)
                self.counts["llm_out"] = getattr(usage, "output_tokens", 0)
        else:
            self.state["errors"]["llm"] = "нет ANTHROPIC_API_KEY: без черновиков, релевантность по ключевым словам"
        if verdicts is None:
            if self.anthropic_key:  # API failed this tick: retry these candidates next tick
                return
            verdicts = {c["cid"]: llm.heuristic(c) for c in new}
        for c in new:
            v = verdicts.get(c["cid"])
            if not v:
                continue
            if not v["keep"]:
                self.state["rejected"][c["cid"]] = self.now
                continue
            t = topics.get(v["same_as"]) if v["same_as"] else None
            if t is None:
                t = {
                    "key": short_hash(c["cid"], self.now), "first_seen": self.now, "sources": [], "items": [],
                    "alerted_at": None, "main_url": None, "search_url": None, "media": None,
                }
                self.feed["topics"].append(t)
                topics[t["key"]] = t
                t.update(title=v["title"], why=v["why"], category=v["category"], relevance=v["relevance"],
                         urgency=v["urgency"], pollable=v["pollable"], poll_question=v["poll_question"],
                         poll_options=v["poll_options"], qt=v["qt"], x_query=v["x_query"])
            if v["x_query"] and not t.get("search_url"):
                t["search_url"] = x_search_url(v["x_query"])
            self.merge(t, c)
            self.state["cid_topic"][c["cid"]] = {"key": t["key"], "ts": self.now}
        self.counts["new_candidates"] = len(new)

    # ------------------------------------------------------------ alerts

    def should_alert(self, t):
        a = self.cfg["alerts"]
        if t.get("alerted_at"):
            return False
        return t["relevance"] >= a["min_relevance"] or (
            t["relevance"] >= a["min_relevance_urgent"] and t["urgency"] >= a["min_urgency"])

    def find_main_post(self, t):
        """Trend / news / market without a tweet: spend 1 Sorsa request to find the post to quote."""
        if "/status/" in (t.get("main_url") or "") or not t.get("x_query") or not self.sorsa.enabled \
                or t.get("mainpost_tried"):
            return
        t["mainpost_tried"] = True
        res = self.guarded("x_mainpost", lambda: self.sorsa.search(
            f"{t['x_query']} min_faves:200 -filter:replies", order="popular"))
        tweets = [tweet_item(x, "mainpost") for x in (res or {}).get("tweets") or []]
        if tweets:
            top = max(tweets, key=lambda i: i["metrics"]["likes"])
            t["source_url"] = t.get("main_url")
            t["main_url"], t["media"] = top["url"], top["media"]
            t["signals"] = t["signals"] + [f"главный пост: @{top['author']}, {detect.fmt(top['metrics']['likes'])} лайков"]

    def alerts(self):
        a = self.cfg["alerts"]
        todo = sorted((t for t in self.feed["topics"] if self.now - t["last_seen"] < 600 and self.should_alert(t)),
                      key=lambda t: -t["score"])
        sent = 0
        for t in todo:
            if len(self.state["alerts"]) >= a["max_per_hour"]:
                break
            self.find_main_post(t)
            if self.args.dry or not (self.tg_token and self.tg_chats):
                # nothing was sent, so leave the topic eligible for when Telegram is configured
                print("\n--- ALERT (not sent) ---\n" + telegram.alert_text(t))
                print("buttons:", json.dumps(telegram.alert_buttons(t, self.feed_url), ensure_ascii=False)[:400])
                continue
            self.guarded("telegram", lambda t=t: telegram.send_alert(self.tg_token, self.tg_chats, t, self.feed_url))
            if "telegram" in self.state["errors"]:
                break
            t["alerted_at"] = self.now
            self.state["alerts"].append(self.now)
            sent += 1
        self.counts["alerts"] = sent

    def digest(self):
        a = self.cfg["alerts"]
        msk = datetime.now(MSK)
        slot = msk.strftime("%Y-%m-%d-") + str(msk.hour)
        if msk.hour not in a["digest_hours_msk"] or slot in self.state["digests"]:
            return
        window = a["digest_window_h"] * 3600
        top = sorted((t for t in self.feed["topics"] if self.now - t["last_seen"] < window and t["relevance"] >= 5),
                     key=lambda t: -t["score"])[: a["digest_size"]]
        self.state["digests"][slot] = self.now
        if not top:
            return
        if self.args.dry or not (self.tg_token and self.tg_chats):
            print("\n--- DIGEST ---\n" + "\n".join(f"{t['score']:5.1f} {t['title']}" for t in top))
        else:
            self.guarded("telegram_digest", lambda: telegram.send_digest(
                self.tg_token, self.tg_chats, top, a["digest_window_h"], self.feed_url))

    # ------------------------------------------------------------ main

    def go(self):
        self.fetch_x()
        self.fetch_free()
        self.candidates += detect.tweet_candidates(self.state, self.cfg, self.now)
        self.candidates += detect.cluster_candidates(self.state, self.cfg, self.now)
        self.counts["candidates"] = len(self.candidates)
        topics = self.topic_by_key()
        new = self.refresh_existing(topics)
        self.judge_new(new, topics)
        self.alerts()
        self.digest()
        store.prune(self.state, self.feed, self.cfg["keep_days"], self.now)
        self.feed["topics"].sort(key=lambda t: -t["last_seen"])
        store.stamp(self.feed, self.state, self.cfg, self.now, {
            "x_enabled": self.sorsa.enabled, "llm_enabled": bool(self.anthropic_key), "counts": self.counts})
        store.save(self.state_path, self.state)
        store.save(self.feed_path, self.feed)
        print(json.dumps({"counts": self.counts, "budget": self.state["budget"],
                          "errors": self.state["errors"]}, ensure_ascii=False))


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=os.path.join(ROOT, "data"))
    p.add_argument("--dry", action="store_true", help="print alerts instead of sending to Telegram")
    p.add_argument("--force", action="store_true", help="ignore cadences")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    Run(args).go()


if __name__ == "__main__":
    main()
