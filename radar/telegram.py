"""Pushes to Telegram through @voicefunreports_bot. HTML parse mode, URL buttons only (no webhook needed)."""
import html
import urllib.parse

from .util import post_json

CAT_LABEL = {
    "crypto": "КРИПТА", "solana": "SOLANA", "memecoins": "МЕМКОИНЫ", "markets": "РЫНКИ", "politics": "ПОЛИТИКА",
    "world": "МИР", "tech_ai": "AI / ТЕХ", "culture": "КУЛЬТУРА", "sports": "СПОРТ", "other": "ДРУГОЕ",
}
SRC_LABEL = {"x": "X", "cluster": "X, несколько аккаунтов", "trend": "тренды X", "polymarket": "Polymarket",
             "coingecko": "CoinGecko", "news": "новости"}


def esc(s):
    return html.escape(s or "", quote=False)


def quote_intent(text, url):
    params = {"text": text or ""}
    if url:
        params["url"] = url
    return "https://x.com/intent/post?" + urllib.parse.urlencode(params)


def preview_url(url):
    """fixupx renders tweets as a proper card inside Telegram."""
    if url and url.startswith("https://x.com/") and "/status/" in url:
        return url.replace("https://x.com/", "https://fixupx.com/", 1)
    return url


def alert_text(t):
    lines = [f"<b>{CAT_LABEL.get(t['category'], 'ТРЕНД')} · {t['relevance']}/10</b>  "
             f"<i>{esc(' + '.join(SRC_LABEL.get(s, s) for s in t['sources']))}</i>",
             f"<b>{esc(t['title'])}</b>"]
    if t.get("why"):
        lines.append(esc(t["why"]))
    if t.get("signals"):
        lines.append("<i>" + esc(" · ".join(t["signals"][:3])) + "</i>")
    if t.get("poll_question"):
        opts = " / ".join(t.get("poll_options") or [])
        lines.append(f"\n<b>Опрос:</b> {esc(t['poll_question'])}" + (f"\n{esc(opts)}" if opts else ""))
    if t.get("qt"):
        lines.append(f"\n<b>QT:</b> <code>{esc(t['qt'])}</code>")
    return "\n".join(lines)


def alert_buttons(t, feed_url):
    row1, row2 = [], []
    if t.get("main_url") and "/status/" in t["main_url"]:
        row1.append({"text": "Открыть пост", "url": t["main_url"]})
        row1.append({"text": "Квотнуть с черновиком", "url": quote_intent(t.get("qt"), t["main_url"])})
    elif t.get("main_url"):
        row1.append({"text": "Открыть источник", "url": t["main_url"]})
    if t.get("search_url"):
        row2.append({"text": "Топ постов в X", "url": t["search_url"]})
    if feed_url:
        row2.append({"text": "Лента трендов", "url": feed_url})
    return [r for r in (row1, row2) if r]


def send(token, chat_id, text, buttons=None, preview=None):
    body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if buttons:
        body["reply_markup"] = {"inline_keyboard": buttons}
    if preview:
        body["link_preview_options"] = {"url": preview, "prefer_large_media": True}
    else:
        body["link_preview_options"] = {"is_disabled": True}
    return post_json(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20)


def send_alert(token, chat_ids, topic, feed_url):
    for chat in chat_ids:
        send(token, chat, alert_text(topic), alert_buttons(topic, feed_url), preview_url(topic.get("main_url")))


def send_digest(token, chat_ids, topics, window_h, feed_url):
    lines = [f"<b>Главное за {window_h} ч</b>"]
    for i, t in enumerate(topics, 1):
        link = t.get("main_url") or t.get("search_url")
        title = f'<a href="{esc(link)}">{esc(t["title"])}</a>' if link else esc(t["title"])
        lines.append(f"{i}. {title}  <i>{CAT_LABEL.get(t['category'], '')} · {t['relevance']}/10</i>")
    buttons = [[{"text": "Открыть ленту", "url": feed_url}]] if feed_url else None
    for chat in chat_ids:
        send(token, chat, "\n".join(lines), buttons)
