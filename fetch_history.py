import os
import time
import json
from datetime import datetime
from slack_sdk import WebClient
from google.oauth2 import service_account
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
LOG_SHEET_ID = os.getenv("LOG_SHEET_ID")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds_info = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
sheets_service = build("sheets", "v4", credentials=creds)

OLDEST_TS = "1740787200"  # Mar 1 2026

def get_bot_user_id():
    response = client.auth_test()
    bot_id = response['user_id']
    print(f"Bot User ID: {bot_id}")
    return bot_id

def get_all_users():
    users = []
    cursor = None
    while True:
        response = client.users_list(cursor=cursor, limit=200)
        for user in response['members']:
            if not user.get('is_bot') and not user.get('deleted') and user['id'] != 'USLACKBOT':
                users.append({
                    'id': user['id'],
                    'name': user.get('real_name', 'Unknown')
                })
        cursor = response.get('response_metadata', {}).get('next_cursor')
        if not cursor:
            break
    print(f"Found {len(users)} users")
    return users

def get_dm_channel(user_id):
    try:
        response = client.conversations_open(users=user_id)
        return response['channel']['id']
    except Exception as e:
        print(f"Could not open DM with {user_id}: {e}")
        return None

def get_messages(channel_id, bot_user_id):
    messages = []
    cursor = None
    while True:
        try:
            response = client.conversations_history(
                channel=channel_id,
                oldest=OLDEST_TS,
                limit=200,
                cursor=cursor
            )
            for msg in response.get('messages', []):
                if (msg.get('user')
                        and msg['user'] != bot_user_id
                        and not msg.get('subtype')
                        and msg.get('text', '').strip()):
                    messages.append(msg)
            cursor = response.get('response_metadata', {}).get('next_cursor')
            if not cursor:
                break
        except Exception as e:
            print(f"Error reading channel {channel_id}: {e}")
            break
    return messages

def fetch_all_history():
    bot_user_id = get_bot_user_id()
    users = get_all_users()

    sheets_service.spreadsheets().values().update(
        spreadsheetId=LOG_SHEET_ID,
        range="Sheet1!A1:E1",
        valueInputOption="RAW",
        body={"values": [["Timestamp", "SlackID", "Name", "EmploymentType", "Question"]]}
    ).execute()

    total = 0
    for user in users:
        print(f"Checking {user['name']}...")
        dm_channel = get_dm_channel(user['id'])
        if not dm_channel:
            continue

        messages = get_messages(dm_channel, bot_user_id)
        if not messages:
            continue

        rows = []
        for msg in messages:
            ts = float(msg['ts'])
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            text = msg.get('text', '').strip()
            rows.append([dt, user['id'], user['name'], "Unknown", text])

        if rows:
            sheets_service.spreadsheets().values().append(
                spreadsheetId=LOG_SHEET_ID,
                range="Sheet1!A:E",
                valueInputOption="RAW",
                body={"values": rows}
            ).execute()
            total += len(rows)
            print(f"  {len(rows)} messages from {user['name']}")

        time.sleep(1)

    print(f"\nDone! {total} total messages written to Google Sheet.")

if __name__ == "__main__":
    fetch_all_history()
