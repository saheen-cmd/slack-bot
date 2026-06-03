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

OLDEST_TS = "0"  # all time

def get_bot_user_id():
    response = client.auth_test()
    bot_id = response['user_id']
    print(f"Bot User ID: {bot_id}")
    return bot_id

def get_dm_users():
    """Get only users who have actually DM'd the bot - much faster than checking all 662 users"""
    users = []
    cursor = None
    while True:
        try:
            response = client.conversations_list(
                types="im",
                limit=200,
                cursor=cursor
            )
            for channel in response['channels']:
                if not channel.get('is_user_deleted') and channel.get('user') and channel['user'] != 'USLACKBOT':
                    users.append({
                        'id': channel['user'],
                        'channel_id': channel['id']  # we already have the DM channel ID!
                    })
            cursor = response.get('response_metadata', {}).get('next_cursor')
            if not cursor:
                break
        except Exception as e:
            print(f"Error listing DMs: {e}")
            break
    print(f"Found {len(users)} users who have DM'd the bot")
    return users

def get_user_name(user_id):
    try:
        response = client.users_info(user=user_id)
        return response['user'].get('real_name', 'Unknown')
    except:
        return 'Unknown'

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
            time.sleep(1)
        except Exception as e:
            if 'ratelimited' in str(e):
                print(f"  Rate limited, waiting 15 seconds...")
                time.sleep(15)
                continue
            print(f"Error reading channel {channel_id}: {e}")
            break
    return messages

def fetch_all_history():
    bot_user_id = get_bot_user_id()
    users = get_dm_users()

    # Set headers in sheet
    sheets_service.spreadsheets().values().update(
        spreadsheetId=LOG_SHEET_ID,
        range="Sheet1!A1:E1",
        valueInputOption="RAW",
        body={"values": [["Timestamp", "SlackID", "Name", "EmploymentType", "Question"]]}
    ).execute()

    total = 0
    for i, user in enumerate(users, 1):
        user_id = user['id']
        channel_id = user['channel_id']  # no need to call conversations.open!

        name = get_user_name(user_id)
        print(f"[{i}/{len(users)}] Checking {name}...")

        messages = get_messages(channel_id, bot_user_id)
        if not messages:
            print(f"  No messages")
            continue

        rows = []
        for msg in messages:
            ts = float(msg['ts'])
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            text = msg.get('text', '').strip()
            rows.append([dt, user_id, name, "Unknown", text])

        if rows:
            sheets_service.spreadsheets().values().append(
                spreadsheetId=LOG_SHEET_ID,
                range="Sheet1!A:E",
                valueInputOption="RAW",
                body={"values": rows}
            ).execute()
            total += len(rows)
            print(f"  ✅ {len(rows)} messages saved")

        time.sleep(1)

    print(f"\nDone! {total} total messages written to Google Sheet.")

if __name__ == "__main__":
    fetch_all_history()
