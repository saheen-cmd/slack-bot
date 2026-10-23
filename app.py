import os
import re
import sys
import json
import time
import random
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

# --- Settings ---
DM_ONLY = True          # True = bot answers only in DMs. Set False if you also want it to answer in channels.
CACHE_SECONDS = 300     # Re-download the policy doc / employee sheet every 5 minutes (not on every message)

# Gemini models, tried in order. If the first one fails, the bot tries the next one.
GEMINI_MODELS = ["models/gemini-2.5-flash", "models/gemini-2.5-flash-lite"]


def log(message):
    """Print and flush right away, so the message always shows up in Render logs."""
    try:
        print(message, flush=True)
    except Exception:
        try:
            sys.stderr.write(str(message) + "\n")
            sys.stderr.flush()
        except Exception:
            pass

# Slack app
app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)

# Gemini client
client = genai.Client(api_key=GOOGLE_API_KEY)

# Google Sheets API setup
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

creds_info = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
service = build("sheets", "v4", credentials=creds)

# --- History buffer (per user, last 6 lines: user messages + bot answers) ---
user_histories = defaultdict(lambda: deque(maxlen=6))

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
    "onboarding":  ["joining formalities", "induction", "new hire", "orientation", "greythr"],
    "insurance":   ["mediclaim", "health cover", "gmc", "gpa", "insurance claim"],
    "payroll":     ["payslip", "salary credit", "reimbursement", "pf", "epf", "tds", "ctc", "increment", "payment"],
    "performance": ["performance review", "appraisal", "pip", "performance improvement"],
    "probation":   ["probation confirmation", "probation extension"],
}

DEFAULT_HR_CONTACT = "U06BW50M7NF"

LOKAL_CULTURE_TEXT = (
    "At Lokal, we act like owners who take initiative beyond job descriptions, "
    "get things done despite obstacles, and maintain a growth mindset focused on learning. "
    "We're outcome-driven with bias for action, believing speed matters in building products for billion Indians. "
    "Decision-making involves open debate followed by full commitment and we operate on context rather than control—"
    "managers share the \"why\" while you own execution. "
    "We aim to be an all-star team that thinks big and takes calculated risks: to achieve ambitious goals."
)

# --- SMALL TALK START ---
GREETINGS = {
    "hi", "hii", "hello", "hey", "heyy", "hola", "namaste",
    "good morning", "good afternoon", "good evening", "gm",
}
HOW_ARE_YOU = {
    "how are you", "how are you doing", "how r u", "hows it going", "whats up",
    "wassup", "how was your day", "how is your day", "hows your day",
}
THANKS = {
    "thanks", "thank you", "thankyou", "thx", "ty", "thanks a lot",
    "thank you so much", "many thanks",
}
ACKS = {
    "ok", "okay", "k", "kk", "okie", "got it", "noted", "done", "sure",
    "alright", "understood", "yes", "yep", "cool", "nice", "perfect",
}
POSITIVE_MOOD = {
    "good", "fine", "great", "awesome", "amazing", "superb", "all good",
    "pretty good", "not bad", "good thanks",
}
FILLER_WORDS = {
    "ok", "okay", "k", "kk", "done", "thanks", "thank", "thx", "you", "so", "much",
    "noted", "sure", "cool", "nice", "great", "perfect", "alright", "yes", "yep",
    "understood", "got", "it",
}
BYES = {"bye", "goodbye", "see you", "see ya", "cya", "good night", "gn", "take care"}
HELP_REQUESTS = {"help", "what can you do", "what can you help with", "what can i ask", "what can i ask you", "menu", "topics", "options"}

THANKS_REPLIES = [
    "You're welcome! 😊 Message me anytime if you have an HR question.",
    "Happy to help! Just message me if anything else comes up. 🙂",
    "Anytime! I'm here whenever you have an HR question.",
]
MOOD_REPLIES = [
    "Glad to hear that! 😊 If you have any HR questions, just ask me.",
    "Nice! 🙂 I'm here if you need anything HR-related.",
]
HELP_TEXT = (
    "I can help with HR questions like:\n"
    "• Leave and holiday policies\n"
    "• Payroll, payslips and reimbursements\n"
    "• Insurance (mediclaim, GMC, GPA)\n"
    "• Probation and performance reviews\n"
    "• Onboarding, equipment and office facilities\n\n"
    "Just type your question and I'll check the policy for you. 🙂"
)


def normalize(text):
    """Lowercase and strip emojis, punctuation, mentions so we can match short chat phrases."""
    text = text.lower()
    text = re.sub(r":[a-z0-9_+\-]+:", " ", text)   # Slack emoji codes like :thumbsup:
    text = re.sub(r"<@[^>]+>", " ", text)           # @mentions
    text = re.sub(r"['’]", "", text)                # hows, whats
    text = re.sub(r"[^\w\s]", " ", text)            # punctuation and unicode emojis
    text = re.sub(r"(.)\1{2,}", r"\1", text)        # thankssss -> thanks, heyyy -> hey
    text = re.sub(r"\s+", " ", text).strip()
    return text


def small_talk_reply(raw_text, first_name):
    """Return a friendly reply for greetings / thanks / chit-chat, or None if it's a real question."""
    text = normalize(raw_text)

    # "hi how are you" -> treat as "how are you"
    for g in ("hi", "hello", "hey", "hola", "namaste"):
        if text.startswith(g + " ") and text[len(g) + 1:] in HOW_ARE_YOU:
            text = text[len(g) + 1:]
            break

    if text in GREETINGS:
        return f"Hi {first_name}! 👋 How can I help you today?"
    if text in HOW_ARE_YOU:
        return "I'm doing great, thanks for asking! 😊 How can I help you with HR today?"
    if text in HELP_REQUESTS:
        return HELP_TEXT
    if text in BYES:
        return "Take care! 👋 I'm here whenever you need HR help."
    if text in POSITIVE_MOOD:
        return random.choice(MOOD_REPLIES)
    if text == "" or text in THANKS or text in ACKS:   # "" = emoji-only message like 👍
        return random.choice(THANKS_REPLIES)

    # Short combos like "ok done", "ok thanks", "thanks noted" (every word is just an acknowledgement)
    words = text.split()
    if 0 < len(words) <= 4 and all(w in FILLER_WORDS for w in words):
        return random.choice(THANKS_REPLIES)
    return None
# --- SMALL TALK END ---


def keyword_in_text(keyword, text):
    """Whole-word match, so 'pf' does not match inside other words."""
    return re.search(r"\b" + re.escape(keyword.lower()) + r"\b", text) is not None


def get_hr_contacts_from_question(user_question):
    question_lower = user_question.lower()
    matched_categories = []

    for category, keywords in HR_KEYWORDS.items():
        if any(keyword_in_text(kw, question_lower) for kw in keywords):
            matched_categories.append(category)

    if not matched_categories:
        return ""

    sentences = []
    for category in matched_categories:
        ids = HR_CONTACTS.get(category, [])
        if ids:
            mentions = " or ".join(f"<@{uid}>" for uid in ids)
            sentences.append(f"For {category.replace('_', ' ').title()} related queries, please reach out to {mentions}.")
    return "\n".join(sentences)


# --- Fetching with retries and caching ---

_cache = {
    "doc": (0, ""),
    "employees": (0, {}),
}


def fetch_doc_text(retries=3, delay=2):
    """Fetch Google Doc content as plain text with retry logic"""
    for attempt in range(retries):
        try:
            resp = requests.get(DOC_URL, timeout=10)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            log(f"Doc fetch attempt {attempt + 1} failed: {e}")
            time.sleep(delay)
    return ""  # fallback if all retries fail


def get_doc_text():
    """Policy doc, refreshed every CACHE_SECONDS. Falls back to the last good copy if a refresh fails."""
    fetched_at, text = _cache["doc"]
    if text and time.time() - fetched_at < CACHE_SECONDS:
        return text
    fresh = fetch_doc_text()
    if fresh:
        _cache["doc"] = (time.time(), fresh)
        return fresh
    return text  # old copy (or "" if we never had one)


def load_employment_map(retries=3, delay=2):
    """Read 'Master Data' once and return {SlackID: EmploymentType}. Returns None if it keeps failing."""
    for attempt in range(retries):
        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=SHEET_ID,
                range="Master Data"
            ).execute()
            values = result.get("values", [])
            if not values:
                log("Sheet empty or invalid range.")
                return {}

            headers = values[0]
            mapping = {}
            for row in values[1:]:
                row_dict = dict(zip(headers, row))
                slack_id = (row_dict.get("SlackID") or "").strip()
                if slack_id:
                    mapping[slack_id] = row_dict.get("EmploymentType") or "General"
            return mapping
        except Exception as e:
            log(f"Sheet lookup attempt {attempt + 1} failed: {e}")
            time.sleep(delay)
    return None


def lookup_employment_type(slack_id):
    """Employment type for a Slack user, using a cached copy of the sheet (refreshed every CACHE_SECONDS)."""
    fetched_at, mapping = _cache["employees"]
    if not (mapping and time.time() - fetched_at < CACHE_SECONDS):
        fresh = load_employment_map()
        if fresh is not None:
            _cache["employees"] = (time.time(), fresh)
            mapping = fresh
    return mapping.get(slack_id, "General")


def call_gemini_with_retry(prompt, retries=2, delay=3):
    """Try each Gemini model in GEMINI_MODELS, with retries, and log exactly why a call failed."""
    log(f"GEMINI: sending prompt of {len(prompt)} characters")
    for model in GEMINI_MODELS:
        for attempt in range(retries):
            try:
                response = client.models.generate_content(model=model, contents=prompt)
                ai_response = (getattr(response, "text", "") or "").strip()
                if ai_response:
                    return ai_response
                # Empty answer: log why (blocked, safety, etc.)
                reason = "unknown"
                try:
                    cand = response.candidates[0] if response.candidates else None
                    reason = f"finish_reason={getattr(cand, 'finish_reason', None)}, " \
                             f"prompt_feedback={getattr(response, 'prompt_feedback', None)}"
                except Exception:
                    pass
                log(f"GEMINI ERROR [{model}] attempt {attempt + 1}: empty response ({reason})")
            except Exception as e:
                log(f"GEMINI ERROR [{model}] attempt {attempt + 1}: {type(e).__name__}: {e}")
                # Rate limit: wait longer before trying again
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    time.sleep(delay * (attempt + 2))
                    continue
            time.sleep(delay)
        log(f"GEMINI: giving up on {model}, trying next model (if any)")
    return None  # all models failed


# --- Shortening helper ---

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


# --- Slack helpers ---

_name_cache = {}


def get_slack_name(user_id, logger):
    """Real name from Slack (cached, so we don't call Slack on every message)."""
    if user_id in _name_cache:
        return _name_cache[user_id]
    try:
        user_info = app.client.users_info(user=user_id)
        user = user_info.get("user", {})
        name = user.get("real_name") or user.get("profile", {}).get("display_name") or ""
    except Exception as e:
        logger.error(f"Error fetching Slack user info: {e}")
        return ""
    _name_cache[user_id] = name
    return name


def deliver(say, thinking, text):
    """Replace the 'Let me check...' message with the final answer. If that fails, send a new message."""
    if thinking:
        try:
            app.client.chat_update(channel=thinking["channel"], ts=thinking["ts"], text=text)
            return
        except Exception as e:
            log(f"Could not update message, sending a new one: {e}")
    say(text)


# --- Slack event handler ---

@app.event("message")
def handle_message_events(body, say, logger):
    thinking = None
    try:
        event = body.get("event", {})

        # Ignore bot messages, edits, deletions, channel joins and other system events
        if event.get("bot_id") or event.get("subtype"):
            return

        # Only answer in DMs (change DM_ONLY at the top if you want channels too)
        if DM_ONLY and event.get("channel_type") != "im":
            return

        user_question = (event.get("text") or "").strip()
        user_id = event.get("user")
        if not user_question or not user_id:
            return

        slack_name = get_slack_name(user_id, logger)
        first_name = slack_name.split()[0] if slack_name else "there"

        # 1) Friendly small talk: greetings, thanks, "ok", "how was your day", etc.
        reply = small_talk_reply(user_question, first_name)
        if reply:
            say(reply)
            return

        # 2) Culture / mission shortcut
        if re.search(r"\b(culture|mission)\b", user_question.lower()):
            say(LOKAL_CULTURE_TEXT)
            return

        # 3) Real HR question: remember it, tell the user we're checking, then answer
        user_histories[user_id].append(f"User: {user_question}")
        history_text = "\n".join(user_histories[user_id])

        try:
            thinking = say("Let me check that for you... 🔍")
        except Exception:
            thinking = None

        employment_type = lookup_employment_type(user_id)

        doc_text = get_doc_text()
        if not doc_text:
            deliver(say, thinking,
                    "Sorry, I'm having trouble reaching the FAQ right now. "
                    "Please try again in a few minutes, or contact HR directly.")
            return

        prompt = (
            f"You are a friendly HR assistant for {COMPANY_NAME}, chatting with {first_name} on Slack.\n\n"
            f"Policy document:\n\n{doc_text}\n\n"
            f"Recent conversation:\n{history_text}\n\n"
            f"{first_name} (employment type: {employment_type}) just asked: {user_question}\n\n"
            f"Rules:\n"
            f"- The user's employment type is '{employment_type}'.\n"
            f"- In the policy, find the section that starts with exactly '{employment_type}:' and answer ONLY from it if available. "
            f"If there is no such section but there is a general answer, use the general answer.\n"
            f"- If the policy explicitly says 'Please contact HR' or similar, reply with that wording.\n"
            f"- If the policy has no answer at all for {employment_type}, reply exactly: "
            f"'I have limited knowledge on this, please contact HR for clarification.'\n"
            f"- Sound like a warm, helpful colleague, not a robot. Use simple, natural words.\n"
            f"- Start with the answer directly. Never say 'According to the policy document' or 'As an AI'.\n"
            f"- You may use {first_name}'s name once, only if it feels natural.\n"
            f"- Keep it short: usually 1-3 sentences. Use bullet points only for steps or lists. "
            f"Maximum 7 lines or 150 words.\n"
            f"- Do not copy text verbatim from the policy. Paraphrase it clearly and conversationally.\n"
            f"- Use at most one emoji, and only if it fits.\n"
            f"- Never cut off mid-sentence. Always complete the full answer.\n"
        )

        ai_response = call_gemini_with_retry(prompt)
        if not ai_response:
            deliver(say, thinking,
                    "Sorry, I couldn't come up with an answer just now. Please try again in a bit 🙏")
            return

        # If Gemini says it doesn't know, point the user to the right HR person
        if "please contact hr for clarification" in ai_response.lower():
            hr_response = get_hr_contacts_from_question(user_question)
            if hr_response:
                final_text = f"I'm not fully sure about this one 🙂\n{hr_response}"
            else:
                final_text = (
                    f"I'm not fully sure about this one 🙂 "
                    f"Please reach out to <@{DEFAULT_HR_CONTACT}> and they'll be happy to help."
                )
        else:
            final_text = shorten_response(ai_response, max_lines=7, max_words=200)

        deliver(say, thinking, final_text)
        user_histories[user_id].append(f"Assistant: {final_text}")

    except Exception as e:
        logger.error(f"Error handling message: {e}")
        deliver(say, thinking,
                "Oops, something went wrong on my side. Please try again in a moment, "
                "and if it keeps happening, contact HR.")


# --- Run the bot ---
if __name__ == "__main__":
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
