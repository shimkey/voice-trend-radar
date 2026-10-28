"""Shows the chats the bot can see, to grab the group id after adding @voicefunreports_bot to a group.

    TELEGRAM_BOT_TOKEN=... python -m radar.tg_chats

Send any message in the group first, Telegram only reports chats with recent activity.
"""
import os
import sys

from .util import get_json


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    updates = get_json(f"https://api.telegram.org/bot{token}/getUpdates?limit=100").get("result") or []
    chats = {}
    for u in updates:
        for key in ("message", "my_chat_member", "channel_post", "edited_message"):
            chat = (u.get(key) or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name")
    for cid, name in chats.items():
        print(cid, name)
    if not chats:
        print("no chats yet: add the bot to the group and send a message there")


if __name__ == "__main__":
    main()
