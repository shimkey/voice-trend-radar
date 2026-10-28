"""HTTP, time and text helpers. Stdlib only."""
import email.utils
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UA = "Mozilla/5.0 (compatible; voice-radar/1.0)"

STOPWORDS = set("""
a an and are as at be but by for from has have he her his i if in into is it its just me my new no not of on or our
out over she so than that the their them then there these they this to too up us was we were what when where who why
will with you your yours about after all also any back been before being can could day did do does done down even
every first get got gm gn going good great here how im it's know last like look make more most much need never now
one only other people really right say see should still take thing think time today want way week well yeah year
yes yet breaking just in live update news report reports said says via thread rt lol lmao fyi ser anon fren
monday tuesday wednesday thursday friday saturday sunday january february march april may june july august september
october november december
""".split())


def now():
    return time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    """ISO 8601 or RFC 822 (RSS) -> unix seconds, or None."""
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = s.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        return None


class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:200]!r}")
        self.status = status
        self.body = body


def http(method, url, headers=None, body=None, timeout=25, retries=2):
    """Returns response bytes. Retries network errors and 5xx, raises HttpError otherwise."""
    headers = {"User-Agent": UA, **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = HttpError(e.code, e.read().decode("utf-8", "replace"))
            if e.code < 500 and e.code != 429:
                raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise last


def get_json(url, headers=None, **kw):
    return json.loads(http("GET", url, headers, **kw))


def post_json(url, payload, headers=None, **kw):
    return json.loads(http("POST", url, headers, body=payload, **kw))


def qs(params):
    return urllib.parse.urlencode(params, doseq=True)


def short_hash(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def x_search_url(query, top=True):
    return "https://x.com/search?" + qs({"q": query, "f": "top" if top else "live"})


def tweet_url(username, tweet_id):
    return f"https://x.com/{username or 'i'}/status/{tweet_id}"


def clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"https?://\S+", "", s)
    return re.sub(r"\s+", " ", s).strip()


CASHTAG = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9]{1,9})\b")
HASHTAG = re.compile(r"(?<!\w)#([A-Za-z][A-Za-z0-9_]{2,40})")
PROPER = re.compile(r"\b([A-Z][a-zA-Z0-9]{2,}(?:\s+[A-Z][a-zA-Z0-9]{2,}){0,2})\b")


def topic_keys(text):
    """Cashtags, hashtags and capitalised names a tweet talks about, lowercased."""
    text = text or ""
    keys = {"$" + m.lower() for m in CASHTAG.findall(text)}
    keys |= {"#" + m.lower() for m in HASHTAG.findall(text)}
    plain = HASHTAG.sub(" ", CASHTAG.sub(" ", re.sub(r"https?://\S+|@\w+", " ", text)))
    for name in PROPER.findall(plain):
        if name.isupper() and len(name) > 6:  # shouting ("BREAKING"), not an acronym
            continue
        k = " ".join(w for w in name.lower().split() if w not in STOPWORDS)
        if len(k) >= 3:
            keys.add(k)
    return keys


def words(text):
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in STOPWORDS}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0
