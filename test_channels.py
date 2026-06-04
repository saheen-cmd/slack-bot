import os
from slack_sdk import WebClient
from dotenv import load_dotenv
load_dotenv()

client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))

response = client.conversations_list(types="im", limit=200)
channels = response["channels"]

print("Checking which channels have actual messages...")
for c in channels[:20]:
    hist = client.conversations_history(channel=c["id"], limit=5)
    msgs = [m for m in hist.get("messages", []) if m.get("user") and not m.get("subtype")]
    if msgs:
        print(f"Channel {c['id']} | User {c['user']} | Messages: {len(msgs)}")
        for m in msgs[:2]:
            print(f"   text: {m.get('text', '')[:50]}")
