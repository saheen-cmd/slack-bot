import os
import json
import time
import requests
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
COMPANY_NAME = os.getenv("COMPANY_NAME", "MyCompany")
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

# --- Retry wrappers ---

def fetch_doc_text(retries=3, delay=2):
    """Fetch Google Doc content as plain text with retry logic"""
    for attempt in range(retries):
        try:
            resp = requests.get(DOC_URL, timeout=10)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            print(f"Doc fetch attempt {attempt+1} failed: {e}")
            time.sleep(delay)
    return ""  # fallback if all retries fail


def lookup_employment_type(slack_id, retries=3, delay=2):
    """Lookup employment type from Google Sheet using SlackID with retry logic"""
    for attempt in range(retries):
        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=SHEET_ID,
                range="Master Data"
            ).execute()
            values = result.get("values", [])
            if not values:
                print("Sheet empty or invalid range.")
                return "General"

            headers = values[0]
            rows = values[1:]

            for row in rows:
                row_dict = dict(zip(headers, row))
                if row_dict.get("SlackID") == slack_id:
                    return row_dict.get("EmploymentType", "General")
            print("SlackID not found in sheet.")
            return "General"
        except Exception as e:
            print(f"Sheet lookup attempt {attempt+1} failed: {e}")
            time.sleep(delay)
    return "General"  # fallback if all retries fail


def call_gemini_with_retry(prompt, retries=3, delay=2):
    """Call Gemini API with retry logic"""
    for attempt in range(retries):
        try:
            response = client.models.generate_content(
                model="models/gemini-2.5-flash",
                contents=prompt
            )
            ai_response = getattr(response, "text", "").strip()
            if ai_response:
                return ai_response
            else:
                print("Gemini returned empty response.")
        except Exception as e:
            print(f"Gemini API attempt {attempt+1} failed: {e}")
            time.sleep(delay)
    return None  # fallback if all retries fail

# --- Shortening helper (preserve bullet/numbered lists up to 7 items) ---

def shorten_response(text, max_lines=7, max_words=200):
    """Trim response to a maximum number of bullet/numbered lines and words"""
    # Split by line breaks first (preserves bullet/numbered formatting)
    parts = text.splitlines()
    shortened = "\n".join(parts[:max_lines]).strip()

    # Word limit
    words = shortened.split()
    if len(words) > max_words:
        shortened = " ".join(words[:max_words]) + "..."
    return shortened

# --- Slack event handler ---

@app.event("message")
def handle_message_events(body, say, logger):
    try:
        event = body.get("event", {})
        user_question = event.get("text")
        if not user_question:
            return  # Ignore non-text events

        user_id = event.get("user")
        if not user_id:
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

        # ✅ Handle greetings directly
        greetings = ["hi", "hello", "hey"]
        if user_question.lower().strip() in greetings:
            say(f"Hi {slack_name}, how can I help you today?")
            return

        # ✅ Fetch doc content with fallback
        doc_text = fetch_doc_text()
        if not doc_text:
            say("FAQ document unavailable right now. Please contact HR.")
            return

        # ✅ Build prompt for Gemini (with concise rule + history + new fallback)
        prompt = (
            f"You are an assistant for {COMPANY_NAME}. "
            f"Here is the policy document:\n\n{doc_text}\n\n"
            f"Conversation history (last 5 messages):\n{history_text}\n\n"
            f"User ({slack_name}, {employment_type}) just asked: {user_question}\n\n"
            f"Rules:\n"
            f"- If the policy has an answer for {employment_type}, use that.\n"
            f"- If not, use 'General'.\n"
            f"- If the policy explicitly says 'Please contact HR' or similar, reply exactly with that wording.\n"
            f"- If the policy has no answer at all for {employment_type} and no 'General' section, then generate a short, natural, slightly    funny answer (max 25 words) "
            f"that politely says the info isn't in the policy and suggests asking about company policies.\n"
            f"- Always reply concisely (max 7 bullet/numbered lines, under 200 words)."
        )

        # ✅ Call Gemini with retry wrapper
        ai_response = call_gemini_with_retry(prompt)
        if not ai_response:
            say("Gemini could not generate a response after multiple attempts. Please try again later.")
            return

        # ✅ Shorten response before sending
        short_response = shorten_response(ai_response, max_lines=7, max_words=200)
        say(short_response)

    except Exception as e:
        logger.error(f"Error handling message: {e}")
        say("An unexpected error occurred while processing your request.")

# --- Run the bot ---
if __name__ == "__main__":
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()