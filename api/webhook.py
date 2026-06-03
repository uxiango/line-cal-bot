import json
import os
import re
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler

import dateparser
import pytz
from dateparser.search import search_dates
from google.oauth2 import service_account
from googleapiclient.discovery import build
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

TW_TZ = pytz.timezone('Asia/Taipei')

line_bot_api = LineBotApi(os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
webhook_handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])

CALENDAR_IDS = {
    'xiang': os.environ['CALENDAR_ID_XIANG'],
    'hannah': os.environ['CALENDAR_ID_HANNAH'],
    'we': os.environ['CALENDAR_ID_WE'],
}

PERSON_DISPLAY = {'xiang': 'Xiang', 'hannah': 'Hannah', 'we': 'We'}

RRULE_MAP = {
    '每兩週': 'RRULE:FREQ=WEEKLY;INTERVAL=2',
    '每週': 'RRULE:FREQ=WEEKLY',
    '每天': 'RRULE:FREQ=DAILY',
    '每月': 'RRULE:FREQ=MONTHLY',
}


def parse_duration(text):
    patterns = [
        (r'(\d+)小時半', lambda m: int(m.group(1)) * 60 + 30),
        (r'(\d+)小時', lambda m: int(m.group(1)) * 60),
        (r'半小時', lambda m: 30),
    ]
    for pattern, calc in patterns:
        m = re.search(pattern, text)
        if m:
            return calc(m), (text[:m.start()] + text[m.end():]).strip()
    return 60, text


def parse_recurrence(text):
    for keyword, rrule in RRULE_MAP.items():
        if keyword in text:
            return rrule, text.replace(keyword, '').strip()
    return None, text


def parse_end_date(text):
    m = re.search(r'(\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2})', text)
    if m:
        date_str = m.group(1)
        if date_str.count('/') == 1:
            year = datetime.now(TW_TZ).year
            date_str = f'{year}/{date_str}'
        end_date = datetime.strptime(date_str, '%Y/%m/%d')
        text = (text[:m.start()] + text[m.end():]).strip()
        return end_date.strftime('%Y%m%d'), text
    return None, text


def parse_datetime_and_event(text):
    results = search_dates(
        text,
        languages=['zh'],
        settings={
            'TIMEZONE': 'Asia/Taipei',
            'RETURN_AS_TIMEZONE_AWARE': True,
            'PREFER_DATES_FROM': 'future',
            'DATE_ORDER': 'YMD',
        }
    )
    if not results:
        return None, text
    date_str, parsed_dt = results[0]
    event_name = text.replace(date_str, '').strip()
    return parsed_dt, event_name


def parse_command(text):
    text = text[4:].strip()  # remove /cal

    parts = text.split(None, 1)
    if len(parts) < 2:
        return None
    person = parts[0].lower()
    if person not in CALENDAR_IDS:
        return None
    remaining = parts[1]

    rrule, remaining = parse_recurrence(remaining)
    end_date = None
    if rrule:
        end_date, remaining = parse_end_date(remaining)
        if not end_date:
            return None
    duration_minutes, remaining = parse_duration(remaining)
    parsed_dt, event_name = parse_datetime_and_event(remaining)

    if not parsed_dt or not event_name:
        return None

    return {
        'person': person,
        'datetime': parsed_dt,
        'event_name': event_name,
        'duration_minutes': duration_minutes,
        'rrule': rrule,
        'end_date': end_date,
    }


def create_calendar_event(parsed):
    info = json.loads(os.environ['GOOGLE_CREDENTIALS_JSON'])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/calendar']
    )
    service = build('calendar', 'v3', credentials=creds)

    start_dt = parsed['datetime']
    end_dt = start_dt + timedelta(minutes=parsed['duration_minutes'])

    event_body = {
        'summary': parsed['event_name'],
        'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Taipei'},
        'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Taipei'},
    }

    if parsed['rrule']:
        rrule = parsed['rrule']
        if parsed['end_date']:
            rrule += f';UNTIL={parsed["end_date"]}'
        event_body['recurrence'] = [rrule]

    return service.events().insert(
        calendarId=CALENDAR_IDS[parsed['person']], body=event_body
    ).execute()


@webhook_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    if not text.lower().startswith('/cal '):
        return

    parsed = parse_command(text)
    if not parsed:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text='❌ 格式錯誤\n請用：/cal [xiang/hannah/we] [時間] [事件] [長度] [重複] [結束日期]'
            ),
        )
        return

    try:
        create_calendar_event(parsed)
        label = PERSON_DISPLAY[parsed['person']]
        start_str = parsed['datetime'].strftime('%Y-%m-%d %H:%M')
        reply = f'✅ 已新增到 {label} 行事曆\n📅 {start_str} {parsed["event_name"]}'
        if parsed['rrule'] and parsed['end_date']:
            ed = parsed['end_date']
            reply += f'\n🔁 重複至 {ed[:4]}/{ed[4:6]}/{ed[6:]}'
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    except Exception:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text='❌ 新增失敗，請稍後再試'),
        )


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8')
        signature = self.headers.get('X-Line-Signature', '')
        try:
            webhook_handler.handle(body, signature)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
        except InvalidSignatureError:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):
        pass
