import os
import json
import time
import requests
import threading
from collections import defaultdict, deque
from googleapiclient.discovery import build
from google.oauth2 import service_account
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import google.genai as genai
from dotenv import load_dotenv

load_dotenv()

# Environment variables
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
DOC_URL = os.getenv("DOC_URL")
COMPANY_NAME = os.getenv("COMPANY_NAME", "Lokal")
SHEET_ID = os.getenv("SHEET_ID")

# Slack app
app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)

# Gemini client
client = genai.Client(api_key=GOOGLE_API_KEY)

# Google Sheets API setup
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

creds_info = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
service = build("sheets", "v4", credentials=creds)

# --- History buffer (per user, last 5 messages) ---
user_histories = defaultdict(lambda: deque(maxlen=5))

# --- Inactivity feedback timer ---
user_timers = {}

def schedule_feedback(user_id, channel_id):
    # Cancel any existing timer for this user
    if user_id in user_timers:
        user_timers[user_id].cancel()

    # Start a new 3-minute timer
    def send_feedback():
        try:
            app.client.chat_postMessage(
                channel=channel_id,
                text="*Please fill the form for any suggestion or issue faced in response: https://forms.gle/gjcFHFs1ubsaqeSv5*"
            )
        except Exception as e:
            print(f"Error sending feedback message: {e}")
        user_timers.pop(user_id, None)  # remove after firing

    timer = threading.Timer(180, send_feedback)  # 180 seconds = 3 minutes
    user_timers[user_id] = timer
    timer.start()

# --- Slack event handler ---
@app.event("message")
def handle_message_events(body, say, logger):
    try:
        event = body.get("event", {})
        user_question = event.get("text")
        if not user_question:
            return  # Ignore non-text events

        user_id = event.get("user")
        channel_id = event.get("channel")
        if not user_id or not channel_id:
            return  # Ignore system/bot messages

        # ✅ Update user history
        user_histories[user_id].append(user_question)
        history_text = "\n".join(user_histories[user_id])

        # ✅ Get Slack user info safely
        try:
            user_info = app.client.users_info(user=user_id)
            slack_name = user_info.get("user", {}).get("real_name", "User")
        except Exception as e:
            logger.error(f"Error fetching Slack user info: {e}")
            slack_name = "User"

        # ✅ Lookup employment type with fallback
        employment_type = lookup_employment_type(user_id) or "General"

        # ✅ Handle greetings directly (no feedback timer here)
        greetings = ["hi", "hello", "hey"]
        if user_question.lower().strip() in greetings:
            say(f"Hi {slack_name}, how can I help you today?")
            return

        policy_keywords = ["culture", "mission"]

        lokal_culture_text = (
            "At Lokal, we act like owners who take initiative beyond job descriptions, "
            "get things done despite obstacles, and maintain a growth mindset focused on learning. "
            "We're outcome-driven with bias for action, believing speed matters in building products for billion Indians. "
            "Decision-making involves open debate followed by full commitment and we operate on context rather than control—"
            "managers share the \"why\" while you own execution. "
            "We aim to be an all-star team that thinks big and takes calculated risks: to achieve ambitious goals."
        )

        if any(word in user_question.lower() for word in policy_keywords):
            say(lokal_culture_text)
            schedule_feedback(user_id, channel_id)
            return

        # ✅ Fetch doc content with fallback
        doc_text = fetch_doc_text()
        if not doc_text:
            say("FAQ document unavailable right now. Please contact HR.")
            schedule_feedback(user_id, channel_id)
            return

        # ✅ Build prompt for Gemini
        prompt = (
            f"You are an assistant for {COMPANY_NAME}. "
            f"Here is the policy document:\n\n{doc_text}\n\n"
            f"Conversation history (last 5 messages):\n{history_text}\n\n"
            f"User ({slack_name}, employment type: {employment_type}) just asked: {user_question}\n\n"
            f"Rules:\n"
            f"- The user's employment type is '{employment_type}'.\n"
            f"- In the policy, find the section that starts with exactly '{employment_type}:' and return ONLY that answer if its available. Else give the same answer.\n"
            f"- If the policy explicitly says 'Please contact HR' or similar, reply exactly with that wording.\n"
            f"- Keep the response consolidated if word count of answer in policy is above 150 words.\n"
            f"- If the policy has no answer at all for {employment_type}, then say in a human way: "
            f"'I have limited knowledge on this, please contact HR for clarification.'\n"
            f"- Use short bullet points or concise sentences.\n"
            f"- Never cut off mid-sentence.\n"
            f"- Maximum 10 bullet points or 150 words.\n"
            f"- Always phrase answers in a natural, human‑like way.\n"
        )

        # ✅ Call Gemini with retry wrapper
        ai_response = call_gemini_with_retry(prompt)
        if not ai_response:
            say("Gemini could not generate a response after multiple attempts. Please try again later.")
            schedule_feedback(user_id, channel_id)
            return
        
        # ✅ Only override if Gemini says "Please contact HR for clarification."
        if "please contact hr for clarification" in ai_response.lower():
            hr_response = get_hr_contacts_from_question(user_question)
            if hr_response:
                say(hr_response)
            else:
                say("Please contact <@U06BW50M7NF> for further assistance.")
            schedule_feedback(user_id, channel_id)
            return

        # ✅ Otherwise, shorten and send Gemini’s answer
        short_response = shorten_response(ai_response, max_lines=7, max_words=200)
        say(short_response)
        schedule_feedback(user_id, channel_id)

    except Exception as e:
        logger.error(f"Error handling message: {e}")
        # Only send error message if we still have context
        if "event" in body:
            channel_id = body["event"].get("channel")
            if channel_id:
                app.client.chat_postMessage(
                    channel=channel_id,
                    text="An unexpected error occurred while processing your request."
                )
                user_id = body["event"].get("user")
                if user_id:
                    schedule_feedback(user_id, channel_id)

# --- Run the bot ---
if __name__ == "__main__":
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
