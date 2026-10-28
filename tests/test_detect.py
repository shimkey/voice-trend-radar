"""python -m unittest discover -s tests"""
import json
import os
import unittest

from radar import detect, store
from radar.util import topic_keys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
NOW = 1_800_000_000.0


def state():
    s = json.loads(json.dumps(store.STATE_DEFAULT))
    s["born"] = NOW - 10 * 86400
    return s


def tweet(tid, author, text, likes, age_min, followers=50_000, via="list"):
    return {"tweet_id": str(tid), "is_reply": False, "author": author, "followers": followers, "via": via,
            "ts": NOW - age_min * 60, "text": text, "quoted_text": "", "url": f"https://x.com/{author}/status/{tid}",
            "media": [], "metrics": {"views": likes * 40, "likes": likes, "rts": 0, "replies": 0, "quotes": 0}}


class TweetSignals(unittest.TestCase):
    def test_outperforming_tweet_is_a_candidate_ordinary_one_is_not(self):
        s = state()
        s["authors"]["dev"] = [100] * 10  # dev usually gets ~100 engagements
        detect.ingest_tweets(s, [tweet(1, "dev", "gm", 120, 90), tweet(2, "dev", "we got hacked", 900, 90)], NOW)
        cids = {c["cid"] for c in detect.tweet_candidates(s, CFG, NOW)}
        self.assertEqual(cids, {"x:2"})

    def test_cold_author_judged_by_followers(self):
        s = state()
        detect.ingest_tweets(s, [tweet(3, "new", "big news", 2000, 60, followers=20_000)], NOW)
        self.assertEqual(len(detect.tweet_candidates(s, CFG, NOW)), 1)

    def test_old_tweets_and_replies_are_ignored(self):
        s = state()
        old = tweet(4, "dev", "old", 5000, 60 * 10)
        reply = dict(tweet(5, "dev", "reply", 5000, 30), is_reply=True)
        detect.ingest_tweets(s, [old, reply], NOW)
        self.assertEqual(detect.tweet_candidates(s, CFG, NOW), [])


class Clusters(unittest.TestCase):
    def test_spike_across_authors(self):
        s = state()
        items = [tweet(10 + i, f"kol{i}", "Pengu is sending, $PENGU everywhere", 50, 30) for i in range(4)]
        detect.ingest_tweets(s, items, NOW)
        keys = {c["title"] for c in detect.cluster_candidates(s, CFG, NOW)}
        self.assertIn("$pengu", keys)

    def test_always_on_chatter_is_not_a_spike(self):
        s = state()
        h = int(NOW // 3600)
        s["keys"]["$sol"] = {str(h - k): 6 for k in range(4, 160)}  # mentioned all week long
        detect.ingest_tweets(s, [tweet(20 + i, f"kol{i}", "$SOL looks fine", 50, 30) for i in range(4)], NOW)
        self.assertNotIn("$sol", {c["title"] for c in detect.cluster_candidates(s, CFG, NOW)})


class Trends(unittest.TestCase):
    def test_fresh_breakout_flagged_once(self):
        s = state()
        old = [(r, f"old{r}", 0) for r in range(1, 31)]
        snaps = [(NOW, [(1, "Pengu Season", 0)] + old[1:]), (NOW - 3600, old), (NOW - 7200, old)]
        first = detect.trend_candidates(s, {"united-states": snaps}, CFG, NOW)
        self.assertEqual([c["title"] for c in first], ["Pengu Season"])
        self.assertEqual(detect.trend_candidates(s, {"united-states": snaps}, CFG, NOW + 60), [])


class Keys(unittest.TestCase):
    def test_topic_keys(self):
        keys = topic_keys("BREAKING: Coinbase lists $PENGU, #Solana summer is back https://t.co/x @frank")
        self.assertIn("$pengu", keys)
        self.assertIn("#solana", keys)
        self.assertIn("coinbase", keys)
        self.assertNotIn("breaking", keys)


if __name__ == "__main__":
    unittest.main()
