import os
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
USER_ID = "U08Q2FCSHMJ"
MESSAGE = "Hi! How was your day? 🙂"

client = WebClient(token=SLACK_BOT_TOKEN)

try:
    client.chat_postMessage(channel=USER_ID, text=MESSAGE)
    print("Message sent.")
except SlackApiError as e:
    print(f"Failed to send message: {e.response.get('error')}")
