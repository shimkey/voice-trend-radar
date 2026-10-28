"""Everything the radar reads from the outside world.

Each fetcher returns plain dicts ("items"). An item always has:
  id, source, kind, title, text, url, ts (unix seconds)
plus source-specific fields (metrics, author, region, ...).
"""
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .util import (HttpError, clean_text, get_json, http, parse_time, post_json, qs, short_hash,
                   tweet_url, x_search_url)


# ---------------------------------------------------------------- Sorsa (X)

class BudgetExceeded(Exception):
    pass


class Sorsa:
    """Thin Sorsa v3 client. Every call costs 1 request; a daily budget keeps the plan alive all month."""

    def __init__(self, key, base, budget_state, daily_budget):
        self.key = key
        self.base = base.rstrip("/")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if budget_state.get("day") != today:
            budget_state.clear()
            budget_state.update(day=today, used=0)
        self.budget = budget_state
        self.daily_budget = daily_budget

    @property
    def enabled(self):
        return bool(self.key)

    def left(self):
        return self.daily_budget - self.budget["used"]

    def _call(self, method, path, params=None, body=None):
        if not self.key:
            raise BudgetExceeded("no SORSA_API_KEY")
        if self.left() <= 0:
            raise BudgetExceeded(f"daily budget {self.daily_budget} spent")
        self.budget["used"] += 1
        url = f"{self.base}{path}" + (f"?{qs(params)}" if params else "")
        headers = {"ApiKey": self.key}
        if method == "GET":
            return get_json(url, headers)
        return post_json(url, body or {}, headers)

    def list_tweets(self, list_id, cursor=None):
        params = {"list_id": list_id}
        if cursor:
            params["next_cursor"] = cursor
        return self._call("GET", "/list-tweets", params)

    def search(self, query, order="latest", cursor=None):
        body = {"query": query, "order": order}
        if cursor:
            body["next_cursor"] = cursor
        return self._call("POST", "/search-tweets", body=body)

    def tweets_bulk(self, ids):
        return self._call("POST", "/tweet-info-bulk", body={"tweet_links": list(ids)[:100]})

    def usage(self):
        return self._call("GET", "/key-usage-info")

    def info_batch(self, usernames):
        return self._call("GET", "/info-batch", {"usernames": list(usernames)[:100]})


def tweet_item(t, via):
    """Sorsa tweet object -> item. Retweets resolve to the original tweet."""
    if t.get("retweeted_status"):
        t = t["retweeted_status"]
    user = t.get("user") or {}
    handle = user.get("username") or user.get("screen_name") or ""
    tid = str(t.get("id") or t.get("id_str") or "")
    m = {
        "views": int(t.get("view_count") or 0),
        "likes": int(t.get("likes_count") or 0),
        "rts": int(t.get("retweet_count") or 0),
        "replies": int(t.get("reply_count") or 0),
        "quotes": int(t.get("quote_count") or 0),
        "bookmarks": int(t.get("bookmark_count") or 0),
    }
    text = clean_text(t.get("full_text") or t.get("text") or "")
    quoted = t.get("quoted_status") or {}
    return {
        "id": f"x:{tid}",
        "tweet_id": tid,
        "source": "x",
        "kind": "tweet",
        "via": via,
        "title": text[:140],
        "text": text,
        "quoted_text": clean_text(quoted.get("full_text") or "")[:280],
        "url": tweet_url(handle, tid),
        "author": handle,
        "author_name": user.get("display_name") or user.get("name") or handle,
        "followers": int(user.get("followers_count") or 0),
        "is_reply": bool(t.get("is_reply")),
        "lang": t.get("lang") or "",
        "media": [e.get("preview") or e.get("link") for e in (t.get("entities") or [])
                  if e.get("type") in ("photo", "video", "animated_gif")][:1],
        "ts": parse_time(t.get("created_at")) or time.time(),
        "metrics": m,
    }


def engagement(m):
    return m["likes"] + 2 * m["rts"] + 3 * m["quotes"] + m["replies"]


# ---------------------------------------------------------------- X trends (trends24.in, free)

T24_BLOCK = re.compile(r'<h3 class=title data-timestamp=([\d.]+)>.*?<ol class=trend-card__list>(.*?)</ol>', re.S)
T24_TREND = re.compile(r'class=trend-link>(.*?)</a><span class=tweet-count data-count="?(\d*)"?')


def fetch_trends24(location_slug):
    """Hourly snapshots for one location, newest first: [(ts, [(rank, name, count), ...]), ...]."""
    url = f"https://trends24.in/{location_slug}/" if location_slug else "https://trends24.in/"
    page = http("GET", url, timeout=30).decode("utf-8", "replace")
    snaps = []
    for ts, block in T24_BLOCK.findall(page):
        trends = [(i + 1, html.unescape(name).strip(), int(c) if c else 0)
                  for i, (name, c) in enumerate(T24_TREND.findall(block))]
        if trends:
            snaps.append((float(ts), trends))
    snaps.sort(key=lambda s: -s[0])
    return snaps


# ---------------------------------------------------------------- Polymarket (free)

def fetch_polymarket(limit=40):
    events = get_json("https://gamma-api.polymarket.com/events?" + qs({
        "active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": limit}))
    items = []
    for rank, e in enumerate(events, 1):
        markets = e.get("markets") or []
        # multi-outcome events (seat counts, price ladders) swing on tiny buckets, so judge the busiest market only
        lead = max(markets, key=lambda m: float(m.get("volume24hr") or 0), default={})
        price = None
        try:
            prices = lead.get("outcomePrices")
            prices = prices if isinstance(prices, list) else json.loads(prices or "[]")
            price = float(prices[0]) if prices else None
        except (ValueError, TypeError):
            pass
        items.append({
            "id": f"poly:{e.get('slug')}",
            "source": "polymarket",
            "kind": "market",
            "title": e.get("title") or lead.get("question") or "",
            "text": (e.get("description") or "")[:400],
            "url": f"https://polymarket.com/event/{e.get('slug')}",
            "ts": parse_time(e.get("startDate")) or time.time(),
            "rank": rank,
            "volume24": float(e.get("volume24hr") or 0),
            "volume": float(e.get("volume") or 0),
            "max_move": abs(float(lead.get("oneDayPriceChange") or 0)),
            "closed_soon": bool(lead.get("closed")),
            "lead_question": lead.get("question") or "",
            "lead_yes": price,
            "tags": [t.get("label") for t in (e.get("tags") or []) if t.get("label")][:5],
        })
    return items


# ---------------------------------------------------------------- CoinGecko trending (free)

def fetch_coingecko():
    data = get_json("https://api.coingecko.com/api/v3/search/trending")
    items = []
    for rank, c in enumerate(data.get("coins") or [], 1):
        c = c.get("item") or {}
        d = c.get("data") or {}
        change = (d.get("price_change_percentage_24h") or {}).get("usd")
        items.append({
            "id": f"cg:{c.get('id')}",
            "source": "coingecko",
            "kind": "coin",
            "title": f"{c.get('name')} (${(c.get('symbol') or '').upper()}) trending on CoinGecko",
            "text": f"rank {rank} in CoinGecko search trending, market cap rank {c.get('market_cap_rank')}, "
                    f"24h {change:+.1f}%" if isinstance(change, (int, float)) else f"rank {rank} in CoinGecko trending",
            "symbol": (c.get("symbol") or "").upper(),
            "url": f"https://www.coingecko.com/en/coins/{c.get('id')}",
            "search_url": x_search_url("$" + (c.get("symbol") or "")),
            "ts": time.time(),
            "rank": rank,
            "change24": change,
        })
    return items


# ---------------------------------------------------------------- News RSS (free)

def fetch_rss(name, category, url):
    root = ET.fromstring(http("GET", url, timeout=30))
    items = []
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        # ElementTree elements without children are falsy, so compare with None explicitly
        def text_of(*tags):
            for t in tags:
                el = next((c for c in node if c.tag.split("}")[-1] == t), None)
                if el is not None:
                    return el
            return None
        title_el, link_el = text_of("title"), text_of("link")
        title = clean_text(html.unescape((title_el.text if title_el is not None else "") or ""))
        link = (link_el.get("href") or link_el.text or "").strip() if link_el is not None else ""
        when = text_of("pubDate", "published", "updated")
        summary = text_of("description", "summary")
        outlet = name
        if name == "Google News" and " - " in title:  # "Headline - Source"
            title, outlet = title.rsplit(" - ", 1)
        if not title or not link:
            continue
        items.append({
            "id": f"news:{short_hash(link)}",
            "source": "news",
            "kind": "article",
            "category": category,
            "outlet": outlet,
            "feed": name,
            "title": title,
            "text": clean_text(html.unescape((summary.text if summary is not None else "") or ""))[:400],
            "url": link,
            "ts": parse_time(when.text if when is not None else None) or time.time(),
        })
    return items


__all__ = ["Sorsa", "BudgetExceeded", "HttpError", "tweet_item", "engagement", "fetch_trends24",
           "fetch_polymarket", "fetch_coingecko", "fetch_rss"]
