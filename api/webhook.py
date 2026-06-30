import base64
import hashlib
import hmac as hmac_mod
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

import dateparser
import pytz
from dateparser.search import search_dates
from flask import Flask, abort, request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)
TW_TZ = pytz.timezone('Asia/Taipei')

line_configuration = Configuration(access_token=os.environ['LINE_CHANNEL_ACCESS_TOKEN'].strip())
webhook_handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'].strip())

CALENDAR_IDS = {
    'xiang': os.environ['CALENDAR_ID_XIANG'].strip(),
    'hannah': os.environ['CALENDAR_ID_HANNAH'].strip(),
    'we': os.environ['CALENDAR_ID_WE'].strip(),
}

PERSON_DISPLAY = {'xiang': 'Xiang', 'hannah': 'Hannah', 'we': 'We'}

CMD_HELP = {
    '/add': """➕ 新增格式：
/add [對象] [日期] [時間（選填）] [事件] [長度（選填）] [重複（選填）] [結束日期（重複時必填）]

例：/add xiang 明天 1500 跟客戶開會
例：/add xiang 明天 跟客戶開會（全天）
例：/add xiang 每週一 1500 開會 1小時 每週 2026/12/31""",

    '/del': """🗑️ 刪除格式：
/del [對象] [日期] [時間（選填）] [事件] [only|all（選填）]

例：/del xiang 明天 跟客戶開會
例：/del xiang 明天 1500 跟客戶開會 only""",

    '/edit': """✏️ 修改格式：
/edit [對象] [日期] [時間（選填）] [事件] > [key=value ...] [only|all（選填）]

例：/edit xiang 明天 1500 跟客戶開會 > date=後天
例：/edit hannah 下週五 2000 吃飯 > date=下週六 time=1900 name=慶生晚餐 dur=2小時 to=we only

key：date / time / name / to / dur""",

    '/q': """🔍 查找格式：
/q [事件名稱] [範圍（選填）]

例：/q 頭痛
例：/q 開會 一個月
例：/q 頭痛 今年
例：/q 頭痛+吃藥 今年

多關鍵字：用 + 連接，OR 搜尋（如 頭痛+吃藥）
範圍：今年 / 去年 / 最近=±2週 / 一個月=±1個月 / 半年=±3個月 / 一年=±6個月
（不填預設 ±3 個月）""",
}

# 每兩週 must come before 每週 to avoid partial match
RRULE_MAP = {
    '每兩週': 'RRULE:FREQ=WEEKLY;INTERVAL=2',
    '每週': 'RRULE:FREQ=WEEKLY',
    '每天': 'RRULE:FREQ=DAILY',
    '每月': 'RRULE:FREQ=MONTHLY',
}

HELP_TEXT = """📅 LINE Calendar Bot 指令說明

➕ 新增
/add [對象] [日期] [時間（選填）] [事件] [長度（選填）] [重複（選填）] [結束日期（重複時必填）]
例：/add xiang 明天 1500 跟客戶開會 1小時 每週 2026/12/31
例（全天）：/add xiang 明天 跟客戶開會

🗑️ 刪除
/del [對象] [日期] [時間（選填）] [事件] [only|all（選填）]
例：/del xiang 明天 跟客戶開會 only

✏️ 修改
/edit [對象] [日期] [時間（選填）] [事件] > [key=value ...] [only|all（選填）]
例：/edit hannah 下週五 2000 吃飯 > date=下週六 time=1900 name=慶生晚餐 dur=2小時 to=we only

🔍 查找
/q [事件名稱] [範圍（選填）]
例：/q 頭痛
例：/q 頭痛 今年
例：/q 頭痛+吃藥 今年（OR 搜尋）

👤 對象：xiang / hannah / we
⏰ 時間：24小時制，如 1800 或 18:00（不填則為全天事件）
📏 長度：5分鐘 / 半小時 / 1小時 / 1小時半 / 2小時 / 2小時半
🔁 重複：每天 / 每週 / 每兩週 / 每月
🔍 查找範圍：今年 / 去年 / 最近=±2週 / 一個月=±1個月 / 半年=±3個月 / 一年=±6個月（不填=±3個月）
✏️ edit key（至少填一個）：date（選填）/ time（選填）/ name（選填）/ to（選填）/ dur（選填）"""


# ---------------------------------------------------------------------------
# Calendar service
# ---------------------------------------------------------------------------

def get_calendar_service():
    info = json.loads(os.environ['GOOGLE_CREDENTIALS_JSON'].lstrip('﻿'))
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/calendar']
    )
    return build('calendar', 'v3', credentials=creds)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_time_24h(text):
    """Extract 24h time from text. Returns (hour, minute, remaining)."""
    # HH:MM
    m = re.search(r'\b(\d{1,2}):(\d{2})\b', text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi, (text[:m.start()] + text[m.end():]).strip()
    # HHMM or HMM (3–4 digits)
    m = re.search(r'\b(\d{3,4})\b', text)
    if m:
        val = m.group(1).zfill(4)
        h, mi = int(val[:2]), int(val[2:])
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi, (text[:m.start()] + text[m.end():]).strip()
    return None, None, text


WEEKDAY_CN = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}


def _next_weekday(wd, from_date, min_days=1):
    days_ahead = wd - from_date.weekday()
    if days_ahead < min_days:
        days_ahead += 7
    return from_date + timedelta(days=days_ahead)


def _to_aware(d):
    return TW_TZ.localize(datetime(d.year, d.month, d.day))


def parse_date_natural(text):
    """Extract natural language date. Returns (aware datetime, remaining)."""
    now = datetime.now(TW_TZ)
    today = now.date()

    # MM/DD explicit — avoid dateparser year bug
    m = re.search(r'\b(\d{1,2})/(\d{1,2})\b', text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year
        try:
            if (month, day) < (today.month, today.day):
                year += 1
            d = date(year, month, day)
        except ValueError:
            return None, text
        return _to_aware(d), (text[:m.start()] + text[m.end():]).strip()

    # Chinese relative date patterns (ordered: longer patterns first)
    cn_patterns = [
        (r'大後天', lambda _: today + timedelta(days=3)),
        (r'後天',   lambda _: today + timedelta(days=2)),
        (r'明天',   lambda _: today + timedelta(days=1)),
        (r'今天',   lambda _: today),
        # 下週X / 下星期X → next occurrence of weekday, at least 1 day away
        (r'(?:下週|下星期)([一二三四五六日天])',
         lambda m: _next_weekday(WEEKDAY_CN[m.group(1)], today, min_days=1)),
        # 隔週X / 隔星期X → weekday at least 7 days away
        (r'(?:隔週|隔星期)([一二三四五六日天])',
         lambda m: _next_weekday(WEEKDAY_CN[m.group(1)], today + timedelta(days=7), min_days=1)),
        # 每週X / 每星期X → nearest upcoming weekday (start of recurrence)
        (r'(?:每週|每星期)([一二三四五六日天])',
         lambda m: _next_weekday(WEEKDAY_CN[m.group(1)], today, min_days=0)),
        # 這週X / 本週X → weekday this week
        (r'(?:這週|本週|這星期|本星期)([一二三四五六日天])',
         lambda m: _next_weekday(WEEKDAY_CN[m.group(1)], today - timedelta(days=7), min_days=1)),
    ]

    for pattern, date_fn in cn_patterns:
        m = re.search(pattern, text)
        if m:
            result_date = date_fn(m)
            remaining = (text[:m.start()] + text[m.end():]).strip()
            return _to_aware(result_date), remaining

    # Fallback: dateparser on first token
    parts = text.split(None, 1)
    parsed = dateparser.parse(
        parts[0],
        languages=['zh'],
        settings={
            'TIMEZONE': 'Asia/Taipei',
            'RETURN_AS_TIMEZONE_AWARE': True,
            'PREFER_DATES_FROM': 'future',
            'DATE_ORDER': 'YMD',
        },
    )
    if parsed:
        if abs(parsed.year - now.year) > 5:
            try:
                parsed = parsed.replace(year=now.year)
                if parsed.date() < today:
                    parsed = parsed.replace(year=now.year + 1)
            except ValueError:
                pass
        return parsed, (parts[1] if len(parts) > 1 else '')

    return None, text


def parse_duration(text):
    patterns = [
        (r'(\d+)小時半', lambda m: int(m.group(1)) * 60 + 30),
        (r'(\d+)小時', lambda m: int(m.group(1)) * 60),
        (r'半小時',     lambda _: 30),
        (r'(\d+)分鐘',  lambda m: int(m.group(1))),
    ]
    for pattern, calc in patterns:
        m = re.search(pattern, text)
        if m:
            return calc(m), (text[:m.start()] + text[m.end():]).strip()
    return 60, text


def parse_recurrence(text):
    patterns = [
        ('每兩週', r'每兩週', 'RRULE:FREQ=WEEKLY;INTERVAL=2'),
        # 每週 must NOT match 每週一/二/三/四/五/六/日/天 (those are weekday date expressions)
        ('每週', r'每週(?![一二三四五六日天])', 'RRULE:FREQ=WEEKLY'),
        ('每天', r'每天', 'RRULE:FREQ=DAILY'),
        ('每月', r'每月', 'RRULE:FREQ=MONTHLY'),
    ]
    for _, pattern, rrule in patterns:
        m = re.search(pattern, text)
        if m:
            return rrule, (text[:m.start()] + text[m.end():]).strip()
    return None, text


def parse_end_date(text):
    # Only match at end of string to avoid confusing with event date
    m = re.search(r'(\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2})\s*$', text)
    if m:
        date_str = m.group(1)
        if date_str.count('/') == 1:
            year = datetime.now(TW_TZ).year
            date_str = f'{year}/{date_str}'
        end_date = datetime.strptime(date_str, '%Y/%m/%d')
        return end_date.strftime('%Y%m%d'), (text[:m.start()]).strip()
    return None, text


def parse_scope(text):
    """Extract only/all from end of text. Returns (scope, remaining)."""
    if re.search(r'\bonly\b\s*$', text):
        return 'only', re.sub(r'\s*\bonly\b\s*$', '', text).strip()
    if re.search(r'\ball\b\s*$', text):
        return 'all', re.sub(r'\s*\ball\b\s*$', '', text).strip()
    return 'all', text


def combine_date_time(date_dt, hour, minute):
    return TW_TZ.localize(datetime(date_dt.year, date_dt.month, date_dt.day, hour, minute))


# ---------------------------------------------------------------------------
# Command parsers
# ---------------------------------------------------------------------------

def parse_add_command(text):
    text = text[4:].strip()
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
    hour, minute, remaining = parse_time_24h(remaining)

    date_dt, event_name = parse_date_natural(remaining)
    if not date_dt or not event_name:
        return None

    return {
        'person': person,
        'datetime': combine_date_time(date_dt, hour, minute) if hour is not None else None,
        'date': date_dt,
        'all_day': hour is None,
        'event_name': event_name.strip(),
        'duration_minutes': duration_minutes,
        'rrule': rrule,
        'end_date': end_date,
    }


def parse_del_command(text):
    text = text[4:].strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        return None
    person = parts[0].lower()
    if person not in CALENDAR_IDS:
        return None
    remaining = parts[1]

    scope, remaining = parse_scope(remaining)

    # Time is optional for /del
    hour, minute, remaining = parse_time_24h(remaining)

    date_dt, event_name = parse_date_natural(remaining)
    if not date_dt or not event_name:
        return None

    target_dt = combine_date_time(date_dt, hour, minute) if hour is not None else None

    return {
        'person': person,
        'date': date_dt,
        'datetime': target_dt,
        'event_name': event_name.strip(),
        'scope': scope,
    }


def parse_edit_command(text):
    text = text[5:].strip()
    if '>' not in text:
        return None

    before, after = text.split('>', 1)
    before, after = before.strip(), after.strip()

    parts = before.split(None, 1)
    if len(parts) < 2:
        return None
    person = parts[0].lower()
    if person not in CALENDAR_IDS:
        return None
    remaining = parts[1]

    scope, after = parse_scope(after)
    hour, minute, remaining = parse_time_24h(remaining)

    date_dt, event_name = parse_date_natural(remaining)
    if not date_dt or not event_name:
        return None

    # Parse key=value pairs; quoted values allow spaces
    changes = {}
    for m in re.finditer(r'(\w+)=(?:"([^"]*)"|((?:[^\s"]+)))', after):
        key = m.group(1)
        changes[key] = m.group(2) if m.group(2) is not None else m.group(3)

    if not changes:
        return None

    return {
        'person': person,
        'datetime': combine_date_time(date_dt, hour, minute) if hour is not None else None,
        'date': date_dt,
        'all_day': hour is None,
        'event_name': event_name.strip(),
        'scope': scope,
        'changes': changes,
    }


Q_RANGE_DAYS = {'一個月': 30, '半年': 90, '一年': 180}


def parse_q_range(text):
    """Parse range suffix from text. Returns (time_min, time_max, event_name).
    All ranges are centered on today. Default ±90 days."""
    now = datetime.now(TW_TZ)
    year = now.year

    if text.endswith('今年'):
        candidate = text[:-2].strip()
        if candidate:
            time_min = TW_TZ.localize(datetime(year, 1, 1))
            time_max = TW_TZ.localize(datetime(year + 1, 1, 1))
            return time_min, time_max, candidate

    if text.endswith('去年'):
        candidate = text[:-2].strip()
        if candidate:
            time_min = TW_TZ.localize(datetime(year - 1, 1, 1))
            time_max = TW_TZ.localize(datetime(year, 1, 1))
            return time_min, time_max, candidate

    if text.endswith('最近'):
        return now - timedelta(days=14), now + timedelta(days=14), text[:-2].strip()

    for r, d in Q_RANGE_DAYS.items():
        if text.endswith(r):
            candidate = text[:-len(r)].strip()
            if candidate:
                return now - timedelta(days=d), now + timedelta(days=d), candidate

    return now - timedelta(days=90), now + timedelta(days=90), text


def parse_q_command(text):
    text = text[2:].strip()
    if not text:
        return None
    time_min, time_max, event_name = parse_q_range(text)
    if not event_name:
        return None
    keywords = [k.strip() for k in event_name.split('+') if k.strip()]
    return {'keywords': keywords, 'event_name': event_name, 'time_min': time_min, 'time_max': time_max}


# ---------------------------------------------------------------------------
# Calendar operations
# ---------------------------------------------------------------------------

def find_events(service, calendar_id, event_name, target_dt=None, date_dt=None):
    """Search by exact datetime (±2 min) or full day if only date given."""
    if target_dt:
        time_min = (target_dt - timedelta(minutes=2)).isoformat()
        time_max = (target_dt + timedelta(minutes=2)).isoformat()
    else:
        # Full day search
        day_start = TW_TZ.localize(datetime(date_dt.year, date_dt.month, date_dt.day, 0, 0))
        day_end = day_start + timedelta(days=1)
        time_min = day_start.isoformat()
        time_max = day_end.isoformat()

    result = service.events().list(
        calendarId=calendar_id,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy='startTime',
    ).execute()
    return [e for e in result.get('items', []) if event_name in e.get('summary', '')]


def _set_rrule_until(recurrence, until_str):
    new = []
    for rule in recurrence:
        if rule.startswith('RRULE:'):
            rule = re.sub(r';UNTIL=[^;]*', '', rule)
            rule += f';UNTIL={until_str}'
        new.append(rule)
    return new


def do_delete_event(service, calendar_id, event, scope):
    event_id = event['id']
    recurring_id = event.get('recurringEventId')

    if scope == 'only' or not recurring_id:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return

    # scope == 'all': end the base series just before this instance
    base = service.events().get(calendarId=calendar_id, eventId=recurring_id).execute()
    start_str = event['start'].get('dateTime', event['start'].get('date'))
    start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
    until_str = (start_dt - timedelta(days=1)).strftime('%Y%m%d')
    base['recurrence'] = _set_rrule_until(base.get('recurrence', []), until_str)
    service.events().update(calendarId=calendar_id, eventId=recurring_id, body=base).execute()


def _apply_changes(event_body, changes, current_start_str):
    is_all_day = 'T' not in current_start_str

    if is_all_day:
        start_date = date.fromisoformat(current_start_str)
        start_dt = TW_TZ.localize(datetime(start_date.year, start_date.month, start_date.day))
        duration_minutes = 0
    else:
        start_dt = datetime.fromisoformat(current_start_str.replace('Z', '+00:00')).astimezone(TW_TZ)
        end_str = event_body['end'].get('dateTime', event_body['end'].get('date'))
        end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00')).astimezone(TW_TZ)
        duration_minutes = int((end_dt - start_dt).total_seconds() / 60)

    new_date = start_dt.date()
    new_hour, new_minute = start_dt.hour, start_dt.minute
    new_is_all_day = is_all_day

    if 'date' in changes:
        parsed, _ = parse_date_natural(changes['date'])
        if parsed:
            new_date = parsed.date()

    if 'time' in changes:
        h, mi, _ = parse_time_24h(changes['time'])
        if h is not None:
            new_hour, new_minute = h, mi
            new_is_all_day = False

    if 'name' in changes:
        event_body['summary'] = changes['name']

    if 'dur' in changes:
        dur_min, _ = parse_duration(changes['dur'])
        duration_minutes = dur_min
        new_is_all_day = False

    if new_is_all_day:
        event_body['start'] = {'date': new_date.isoformat()}
        event_body['end'] = {'date': (new_date + timedelta(days=1)).isoformat()}
    else:
        if duration_minutes == 0:
            duration_minutes = 60
        new_start = TW_TZ.localize(datetime(new_date.year, new_date.month, new_date.day, new_hour, new_minute))
        new_end = new_start + timedelta(minutes=duration_minutes)
        event_body['start'] = {'dateTime': new_start.isoformat(), 'timeZone': 'Asia/Taipei'}
        event_body['end'] = {'dateTime': new_end.isoformat(), 'timeZone': 'Asia/Taipei'}


def do_edit_event(service, calendar_id, event, changes, scope):
    event_id = event['id']
    recurring_id = event.get('recurringEventId')
    target_cal = CALENDAR_IDS.get(changes.get('to', '').lower(), calendar_id)

    if scope == 'all' and recurring_id:
        base = service.events().get(calendarId=calendar_id, eventId=recurring_id).execute()
        start_str = event['start'].get('dateTime', event['start'].get('date'))
        start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
        until_str = (start_dt - timedelta(days=1)).strftime('%Y%m%d')

        # End original series before this instance
        base['recurrence'] = _set_rrule_until(base.get('recurrence', []), until_str)
        service.events().update(calendarId=calendar_id, eventId=recurring_id, body=base).execute()

        # Build new series from this instance, preserving original RRULE (without our added UNTIL)
        orig_rrule = [r for r in (base.get('recurrence') or []) if r.startswith('RRULE:')]
        clean_rrule = [re.sub(r';UNTIL=[^;]*', '', r) for r in orig_rrule]

        new_event = {
            'summary': base.get('summary'),
            'start': event['start'].copy(),
            'end': event['end'].copy(),
            'recurrence': clean_rrule,
        }
        _apply_changes(new_event, changes, start_str)
        return service.events().insert(calendarId=target_cal, body=new_event).execute()

    else:
        target_event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        start_str = target_event['start'].get('dateTime', target_event['start'].get('date'))
        _apply_changes(target_event, changes, start_str)

        if target_cal != calendar_id:
            service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
            return service.events().insert(calendarId=target_cal, body=target_event).execute()
        else:
            return service.events().update(
                calendarId=calendar_id, eventId=event_id, body=target_event
            ).execute()


# ---------------------------------------------------------------------------
# Reply helper
# ---------------------------------------------------------------------------

def reply_message(reply_token, text):
    with ApiClient(line_configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def format_event_start(event):
    start = event['start'].get('dateTime', event['start'].get('date'))
    if 'T' in start:
        dt = datetime.fromisoformat(start.replace('Z', '+00:00')).astimezone(TW_TZ)
        return dt.strftime('%Y-%m-%d %H:%M')
    return f'{start}（全天）'


def format_multiple_events(events):
    lines = ['找到多筆符合的事件，請提供更具體的資訊：']
    for i, e in enumerate(events, 1):
        lines.append(f'{i}. {format_event_start(e)} {e.get("summary", "")}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

@webhook_handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()
    lower = text.lower()

    if lower == '/help':
        reply_message(event.reply_token, HELP_TEXT)
    elif lower in CMD_HELP:
        reply_message(event.reply_token, CMD_HELP[lower])
    elif lower.startswith('/add '):
        handle_add(event, text)
    elif lower.startswith('/del '):
        handle_del(event, text)
    elif lower.startswith('/edit '):
        handle_edit(event, text)
    elif lower.startswith('/q '):
        handle_q(event, text)


def handle_add(event, text):
    parsed = parse_add_command(text)
    if not parsed:
        reply_message(event.reply_token,
            '❌ 格式錯誤，請用：/add [xiang/hannah/we] [日期] [時間（選填）] [事件] [長度] [重複] [結束日期]')
        return
    try:
        service = get_calendar_service()

        if parsed['all_day']:
            date_str = parsed['date'].strftime('%Y-%m-%d')
            next_day = (parsed['date'] + timedelta(days=1)).strftime('%Y-%m-%d')
            event_body = {
                'summary': parsed['event_name'],
                'start': {'date': date_str},
                'end': {'date': next_day},
            }
        else:
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

        service.events().insert(calendarId=CALENDAR_IDS[parsed['person']], body=event_body).execute()

        label = PERSON_DISPLAY[parsed['person']]
        if parsed['all_day']:
            start_str = parsed['date'].strftime('%Y-%m-%d')
            reply = f'✅ 已新增到 {label} 行事曆\n📅 {start_str} {parsed["event_name"]}（全天）'
        else:
            start_str = parsed['datetime'].strftime('%Y-%m-%d %H:%M')
            reply = f'✅ 已新增到 {label} 行事曆\n📅 {start_str} {parsed["event_name"]}'
        if parsed['rrule'] and parsed['end_date']:
            ed = parsed['end_date']
            reply += f'\n🔁 重複至 {ed[:4]}/{ed[4:6]}/{ed[6:]}'
        reply_message(event.reply_token, reply)
    except Exception as e:
        print(f'[ERROR] {e}', file=sys.stderr)
        reply_message(event.reply_token, '❌ 新增失敗，請稍後再試')


def handle_del(event, text):
    parsed = parse_del_command(text)
    if not parsed:
        reply_message(event.reply_token,
            '❌ 格式錯誤，請用：/del [xiang/hannah/we] [日期] [時間] [事件] [only|all]')
        return
    try:
        service = get_calendar_service()
        cal_id = CALENDAR_IDS[parsed['person']]
        events = find_events(
            service, cal_id, parsed['event_name'],
            target_dt=parsed['datetime'], date_dt=parsed['date']
        )

        if not events:
            reply_message(event.reply_token, '❌ 找不到符合的事件')
            return
        if len(events) > 1:
            reply_message(event.reply_token, format_multiple_events(events))
            return

        do_delete_event(service, cal_id, events[0], parsed['scope'])
        reply_message(event.reply_token, f'✅ 已刪除\n📅 {format_event_start(events[0])} {parsed["event_name"]}')
    except Exception as e:
        print(f'[ERROR] {e}', file=sys.stderr)
        reply_message(event.reply_token, '❌ 刪除失敗，請稍後再試')


def handle_q(event, text):
    parsed = parse_q_command(text)
    if not parsed:
        reply_message(event.reply_token, '❌ 格式錯誤，請用：/q [事件名稱] [範圍（選填）]')
        return
    try:
        service = get_calendar_service()
        time_min = parsed['time_min'].isoformat()
        time_max = parsed['time_max'].isoformat()
        keywords = parsed['keywords']
        is_multi = len(keywords) > 1

        seen_ids = set()
        all_events = []
        for keyword in keywords:
            for person, cal_id in CALENDAR_IDS.items():
                page_token = None
                while True:
                    result = service.events().list(
                        calendarId=cal_id,
                        timeMin=time_min,
                        timeMax=time_max,
                        q=keyword,
                        singleEvents=True,
                        orderBy='startTime',
                        pageToken=page_token,
                    ).execute()
                    for e in result.get('items', []):
                        uid = (person, e['id'])
                        if uid not in seen_ids:
                            seen_ids.add(uid)
                            all_events.append((person, e))
                    page_token = result.get('nextPageToken')
                    if not page_token:
                        break

        all_events.sort(key=lambda x: x[1]['start'].get('dateTime', x[1]['start'].get('date')))

        display_name = parsed['event_name']
        if not all_events:
            reply_message(event.reply_token, f'🔍 找不到「{display_name}」相關事件')
            return

        total = len(all_events)
        lines = [f'🔍 {display_name}（{total} 筆）']
        for person, e in all_events:
            summary = e.get('summary', '')
            if is_multi:
                lines.append(f'📅 {format_event_start(e)} {PERSON_DISPLAY[person]} {summary}')
            else:
                lines.append(f'📅 {format_event_start(e)} {PERSON_DISPLAY[person]}')
        reply_message(event.reply_token, '\n'.join(lines))
    except Exception as e:
        print(f'[ERROR] {e}', file=sys.stderr)
        reply_message(event.reply_token, '❌ 查詢失敗，請稍後再試')


def handle_edit(event, text):
    parsed = parse_edit_command(text)
    if not parsed:
        reply_message(event.reply_token,
            '❌ 格式錯誤，請用：/edit [xiang/hannah/we] [日期] [時間（選填）] [事件] > [key=value ...] [only|all]')
        return
    try:
        service = get_calendar_service()
        cal_id = CALENDAR_IDS[parsed['person']]

        if parsed['all_day']:
            events = find_events(service, cal_id, parsed['event_name'], date_dt=parsed['date'])
        else:
            events = find_events(service, cal_id, parsed['event_name'], target_dt=parsed['datetime'])

        if not events:
            reply_message(event.reply_token, '❌ 找不到符合的事件')
            return
        if len(events) > 1:
            reply_message(event.reply_token, format_multiple_events(events))
            return

        result = do_edit_event(service, cal_id, events[0], parsed['changes'], parsed['scope'])
        new_name = result.get('summary', parsed['event_name'])
        reply_message(event.reply_token, f'✅ 已修改\n📅 {format_event_start(result)} {new_name}')
    except Exception as e:
        print(f'[ERROR] {e}', file=sys.stderr)
        reply_message(event.reply_token, '❌ 修改失敗，請稍後再試')


# ---------------------------------------------------------------------------
# Vercel entrypoint
# ---------------------------------------------------------------------------

@app.route('/api/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    secret = os.environ.get('LINE_CHANNEL_SECRET', '').strip()

    computed = base64.b64encode(
        hmac_mod.new(secret.encode('utf-8'), body.encode('utf-8'), hashlib.sha256).digest()
    ).decode('utf-8')

    print(f'[DEBUG] secret_len={len(secret)} body_len={len(body)} '
          f'received={signature[:20]} computed={computed[:20]}', file=sys.stderr)

    try:
        webhook_handler.handle(body, signature)
        return 'OK', 200
    except InvalidSignatureError:
        abort(400)
