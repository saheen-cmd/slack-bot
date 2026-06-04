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

def get_all_dm_channels():
    """Get ALL DM channels the bot is part of using pagination"""
    channels = []
    cursor = None
    while True:
        try:
            response = client.conversations_list(
                types="im",
                limit=200,
                cursor=cursor
            )
            for channel in response['channels']:
                if channel.get('user') and channel['user'] != 'USLACKBOT':
                    channels.append({
                        'user_id': channel['user'],
                        'channel_id': channel['id']
                    })
            cursor = response.get('response_metadata', {}).get('next_cursor')
            if not cursor:
                break
            time.sleep(1)
        except Exception as e:
            if 'ratelimited' in str(e):
                print("Rate limited, waiting 15 seconds...")
                time.sleep(15)
                continue
            print(f"Error listing channels: {e}")
            break
    print(f"Found {len(channels)} DM channels")
    return channels

def get_user_name(user_id):
    try:
        time.sleep(0.5)
        response = client.users_info(user=user_id)
        return response['user'].get('real_name', 'Unknown')
    except Exception as e:
        if 'ratelimited' in str(e):
            time.sleep(10)
            try:
                response = client.users_info(user=user_id)
                return response['user'].get('real_name', 'Unknown')
            except:
                return 'Unknown'
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
                print(f"  Rate limited, waiting 20 seconds...")
                time.sleep(20)
                continue
            print(f"  Error reading channel {channel_id}: {e}")
            break
    return messages

def clear_sheet():
    """Clear existing data before writing fresh"""
    try:
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=LOG_SHEET_ID,
            range="Sheet1"
        ).execute()
        print("Sheet cleared")
    except Exception as e:
        print(f"Could not clear sheet: {e}")

def fetch_all_history():
    bot_user_id = get_bot_user_id()
    channels = get_all_dm_channels()

    # Clear sheet and set headers
    clear_sheet()
    sheets_service.spreadsheets().values().update(
        spreadsheetId=LOG_SHEET_ID,
        range="Sheet1!A1:E1",
        valueInputOption="RAW",
        body={"values": [["Timestamp", "SlackID", "Name", "EmploymentType", "Question"]]}
    ).execute()

    total = 0
    for i, ch in enumerate(channels, 1):
        user_id = ch['user_id']
        channel_id = ch['channel_id']

        name = get_user_name(user_id)
        print(f"[{i}/{len(channels)}] Checking {name}...")

        messages = get_messages(channel_id, bot_user_id)
        if not messages:
            print(f"  No messages")
            time.sleep(1)
            continue

        rows = []
        for msg in messages:
            ts = float(msg['ts'])
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            text = msg.get('text', '').strip()
            rows.append([dt, user_id, name, "Unknown", text])

        if rows:
            try:
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=LOG_SHEET_ID,
                    range="Sheet1!A:E",
                    valueInputOption="RAW",
                    body={"values": rows}
                ).execute()
                total += len(rows)
                print(f"  ✅ {len(rows)} messages saved")
            except Exception as e:
                print(f"  ❌ Failed to write to sheet: {e}")

        time.sleep(1)

    print(f"\n🎉 Done! {total} total messages written to Google Sheet.")

if __name__ == "__main__":
    fetch_all_history()
