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

HR_CONTACTS = {
    "admin":       ["U079CLY03T9"],
    "onboarding":  ["U09HZE7BBL5"],
    "insurance":   ["U04KZK0SF2T", "U09HZE7BBL5"],
    "payroll":     ["U09GTSUR48N"],
    "performance": ["U09RQQS0AP5", "U0A7LTEKHB3"],
    "probation":   ["U09RQQS0AP5", "U0A7LTEKHB3"],
}

HR_KEYWORDS = {
    "admin":       ["access card", "laptop", "equipment", "seating", "facility", "office supplies", "food", "snack"],
    "onboarding":  ["joining formalities", "induction", "new hire", "orientation", "GreytHR"],
    "insurance":   ["mediclaim", "health cover", "gmc", "gpa", "insurance claim"],
    "payroll":     ["payslip", "salary credit", "reimbursement", "pf", "epf", "tds", "ctc", "increment", "payment"],
    "performance": ["performance review", "appraisal", "pip", "performance improvement"],
    "probation":   ["probation confirmation", "probation extension"],
}

def get_hr_contacts_from_question(user_question):
    question_lower = user_question.lower()
    matched_categories = []

    for category, keywords in HR_KEYWORDS.items():
        for kw in keywords:
            if kw in question_lower:
                matched_categories.append(category)
                break  # stop after first keyword match for this category

    if not matched_categories:
        return ""

    sentences = []
    for category in matched_categories:
        ids = HR_CONTACTS.get(category, [])
        if ids:
            mentions = " or ".join(f"<@{uid}>" for uid in ids)
            sentences.append(f"For {category.replace('_',' ').title()} related queries, please reach out to {mentions}.")
    return "\n".join(sentences)

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

            print(f"DEBUG: Sheet headers = {headers}")

            for row in rows:
                row_dict = dict(zip(headers, row))
                print(f"DEBUG: Checking row SlackID={row_dict.get('SlackID')} against {slack_id}")
                if row_dict.get("SlackID") == slack_id:
                    print(f"DEBUG: Match found for {slack_id}, EmploymentType={row_dict.get('EmploymentType')}")
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

def shorten_response(text, max_lines=10, max_words=150):
    """Trim response without cutting mid-sentence"""
    parts = text.splitlines()
    shortened = "\n".join(parts[:max_lines]).strip()

    words = shortened.split()
    if len(words) > max_words:
        truncated = " ".join(words[:max_words])
        if "." in truncated:
            truncated = truncated.rsplit(".", 1)[0] + "."
        shortened = truncated
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
            f"User ({slack_name}, employment type: {employment_type}) just asked: {user_question}\n\n"
            f"Rules:\n"
            f"- The user's employment type is '{employment_type}'.\n"
            f"- In the policy, find the section that starts with exactly '{employment_type}:' and return ONLY that answer if its available. Else give the same answer.\n"
            f"- If the policy explicitly says 'Please contact HR' or similar, reply exactly with that wording.\n"
            f"- Keep the response consolidated if word count of answer in policy is above 150 words— no long explanations or full paragraphs if wordcount in policy is above 150 words.\n"
            f"- If the policy has no answer at all for {employment_type}, then say in a human way: "
            f"'I have limited knowledge on this, please contact HR for clarification.'\n"
            f"- Use short bullet points (2-3 words per point where possible) or concise sentences. Mostly prefer concise sentences.\n"
            f"- Never cut off mid-sentence. Always complete the full answer.\n"
            f"- Maximum 10 bullet points or 150 words. If answer is short, keep it short.\n"
            f"- Always phrase answers in a natural, human‑like way. Do not copy text verbatim from the policy; instead, paraphrase clearly and conversationally.\n"

        )

        # ✅ Call Gemini with retry wrapper
        ai_response = call_gemini_with_retry(prompt)
        if not ai_response:
            say("Gemini could not generate a response after multiple attempts. Please try again later.")
            return
        
        # ✅ Only override if Gemini says "Please contact HR for clarification."
        if "please contact hr for clarification" in ai_response.lower():
            hr_response = get_hr_contacts_from_question(user_question)
            if hr_response:
                say(hr_response)
            else:
                # Default HR contact if no keyword matched
                say("Please contact <@U06BW50M7NF> for further assistance.")
            return

        # Otherwise, just shorten and send Gemini’s answer
        short_response = shorten_response(ai_response, max_lines=7, max_words=200)
        say(short_response)


    except Exception as e:
        logger.error(f"Error handling message: {e}")
        say("An unexpected error occurred while processing your request.")

# --- Run the bot ---
if __name__ == "__main__":
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()