import os
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import google.genai as genai
from dotenv import load_dotenv

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
DOC_URL = os.getenv("DOC_URL")
COMPANY_NAME = os.getenv("COMPANY_NAME", "MyCompany")

app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)

client = genai.Client(api_key=GOOGLE_API_KEY)

print("✅ Slack bot starting with Socket Mode") 
print(f"✅ Using SLACK_APP_TOKEN: {os.getenv('SLACK_APP_TOKEN')[:10]}...") # show first 10 chars 
print(f"✅ Using model: models/gemini-2.5-flash")

def fetch_doc_text():
    """Fetch Google Doc content as plain text"""
    try:
        resp = requests.get(DOC_URL)
        if resp.status_code == 200:
            return resp.text
        else:
            return ""
    except Exception as e:
        return ""

@app.event("message")
def handle_message_events(body, say, logger):
    try:
        user_question = body.get("event", {}).get("text", "")
        if not user_question:
            return

        # Fetch doc content
        doc_text = fetch_doc_text()

        if not doc_text:
            say("Could not fetch document content.")
            return

        # Build prompt for Gemini
        prompt = (
            f"You are an assistant for {COMPANY_NAME}. "
            f"Only answer using the following document content:\n\n{doc_text}\n\n"
            f"User question: {user_question}\n\n"
            f"If the question cannot be answered from the document, reply exactly with: Please contact HR"
        )

        response = client.models.generate_content(
            model="models/gemini-2.5-flash", 
            contents=prompt 
        ) 

        ai_response = response.text.strip()

        say(ai_response)
        print(f"📩 Received message: {user_question}")
        print(f"🤖 Gemini reply: {ai_response}")


    except Exception as e:
        logger.error(f"Error handling message: {e}")
        say("An error occurred while processing your request.")

if __name__ == "__main__":
    print("✅ Slack bot is starting up...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
