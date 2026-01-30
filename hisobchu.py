import logging
import asyncio
import io
import os
import csv
import re
import json
import threading
import tempfile
import uuid
import time
from contextlib import contextmanager  # <--- YANGI: Barqarorlik uchun
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from typing import Dict, List, Tuple, Optional

# Tashqi kutubxonalar
import speech_recognition as sr
import soundfile as sf
import matplotlib
import matplotlib.pyplot as plt
import psycopg2
from psycopg2 import pool
import google.generativeai as genai
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.request import HTTPXRequest

# Serverda (ekransiz) ishlashi uchun
matplotlib.use('Agg')

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== SOZLAMALAR ====================
# Xavfsizlik uchun: Avval Environmentdan qidiradi, topmasa siz berganini ishlatadi
NEON_DB_URL = os.getenv("NEON_DB_URL", "postgresql://neondb_owner:npg_DE94nSeTHjLa@ep-dark-forest-ahrj0z9l-pooler.c-3.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyA6zYJe7lRnrdpDQFpmLoz0vS-mdeaat0M")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8335598547:AAGFXjcgg7Edh_tCGOeIdeGW4TCWhiip4lU")

# AI Sozlamalari
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')

CATEGORIES = [
    "Oziq-ovqat",
    "Transport",
    "Uy-ro'zg'or",
    "Aloqa",
    "Sog'liq",
    "Kiyim",
    "O'yin-kulgi",
    "Ta'lim",
    "Boshqa",
]

# ==================== RENDER HEALTH CHECK ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running stable!")

def start_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"✅ Health check server {port}-portda ishga tushdi.")
    server.serve_forever()

# ==================== MANTIQ: AI & REGEX ====================
def clean_json_string(text: str) -> str:
    """AI javobidan toza JSON ni ajratib oladi (Xatolikni kamaytirish uchun)"""
    # Matn ichidan { ... } qismini qidiradi
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return match.group(0)
    return text

def analyze_text_with_gemini(text: str) -> Tuple[float, str, str]:
    """Gemini AI orqali matnni tahlil qilish"""
    if not GEMINI_API_KEY:
        return 0.0, text, "Boshqa"
    
    try:
        prompt = f"""
        Analyze this Uzbek expense text: "{text}"
        Return ONLY a JSON object with keys: "amount" (number in UZS), "title" (short string), "category" (string).
        Categories: "Oziq-ovqat", "Transport", "Uy-ro'zg'or", "Aloqa", "Sog'liq", "Kiyim", "O'yin-kulgi", "Ta'lim", "Boshqa".
        If currency is USD, convert to UZS (rate: 12800). If no amount, set "amount": 0.
        Example output: {{"amount": 15000, "title": "Taksi", "category": "Transport"}}
        """
        response = model.generate_content(prompt)
        # Javobni tozalash
        json_str = clean_json_string(response.text)
        data = json.loads(json_str)
        
        return float(data.get("amount", 0)), data.get("title", "Nomsiz"), data.get("category", "Boshqa")
    except Exception as e:
        logger.error(f"AI Error: {e}")
        # AI ishlamasa, oddiy regex usuliga o'tish (fallback)
        return parse_expense_text_regex(text)

def parse_expense_text_regex(text: str) -> Tuple[float, str, str]:
    """Zahira varianti: Regex orqali tahlil"""
    text = text.lower()
    match = re.search(r'(\d+(?:[.,\s]\d+)*)', text)
    amount = 0.0
    title = text
    if match:
        num_str = match.group(1).replace(" ", "").replace(",", ".")
        try:
            amount = float(num_str)
            if 0 < amount < 1000: amount *= 1000
            title = text.replace(match.group(1), "").replace("so'm", "").replace("som", "").strip()
        except: pass
    if not title or len(title) < 2: title = "Nomsiz harajat"
    return amount, title.capitalize(), "Boshqa"

# ==================== DATABASE CLASS (OPTIMIZED) ====================
class TelegramExpenseBot:
    def __init__(self):
        self.init_pool()
        self.init_database()
        self._user_cache: Dict[int, Dict] = {}
    
    def init_pool(self):
        try:
            self.pool = psycopg2.pool.SimpleConnectionPool(1, 20, NEON_DB_URL)
            logger.info("✅ Neon PostgreSQL bazasiga ulandi!")
        except Exception as e:
            logger.critical(f"❌ Baza ulanishida kritik xato: {e}")
            raise e

    @contextmanager
    def get_cursor(self):
        """Context Manager: Ulanishni oladi va avtomatik qaytaradi"""
        conn = self.pool.getconn()
        try:
            yield conn.cursor()
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"DB Query Error: {e}")
            raise e
        finally:
            self.pool.putconn(conn)

    def init_database(self):
        with self.get_cursor() as cursor:
            cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id SERIAL PRIMARY KEY, telegram_id BIGINT UNIQUE NOT NULL, custom_username TEXT UNIQUE NOT NULL, full_name TEXT, budget_limit REAL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS expenses (expense_id SERIAL PRIMARY KEY, creator_id INTEGER NOT NULL REFERENCES users(user_id), title TEXT NOT NULL, amount REAL NOT NULL, category TEXT NOT NULL, expense_date DATE NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS user_links (link_id SERIAL PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(user_id), viewer_id INTEGER NOT NULL REFERENCES users(user_id), UNIQUE(owner_id, viewer_id))''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS expense_permissions (permission_id SERIAL PRIMARY KEY, expense_id INTEGER NOT NULL REFERENCES expenses(expense_id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(user_id), UNIQUE(expense_id, user_id))''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS notifications (notification_id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(user_id), message TEXT NOT NULL, is_read BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

            cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_links_owner_id ON user_links(owner_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_expenses_creator_date ON expenses(creator_id, expense_date)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_expense_permissions_user_id ON expense_permissions(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_expense_permissions_expense_id ON expense_permissions(expense_id)')

    def get_user(self, telegram_id: int) -> Optional[Dict]:
        with self.get_cursor() as cursor:
            cursor.execute('SELECT user_id, custom_username, full_name, budget_limit FROM users WHERE telegram_id = %s', (telegram_id,))
            user = cursor.fetchone()
            return {'user_id': user[0], 'username': user[1], 'full_name': user[2], 'budget_limit': user[3]} if user else None

    def get_user_cached(self, telegram_id: int) -> Optional[Dict]:
        cached = self._user_cache.get(telegram_id)
        if cached is not None:
            return cached
        user = self.get_user(telegram_id)
        if user:
            self._user_cache[telegram_id] = user
        return user

    def check_username_exists(self, username: str) -> bool:
        with self.get_cursor() as cursor:
            cursor.execute('SELECT 1 FROM users WHERE custom_username = %s', (username.lower(),))
            return cursor.fetchone() is not None

    def register_user(self, telegram_id: int, username: str, full_name: str) -> Dict:
        try:
            with self.get_cursor() as cursor:
                cursor.execute('INSERT INTO users (telegram_id, custom_username, full_name) VALUES (%s, %s, %s)', (telegram_id, username.lower(), full_name))
            user = self.get_user(telegram_id)
            if user:
                self._user_cache[telegram_id] = user
            return {'success': True}
        except psycopg2.IntegrityError:
            return {'success': False, 'message': "Bu username band!"}

    def add_partner_by_id(self, owner_id: int, partner_user_id: int) -> Dict:
        if owner_id == partner_user_id: return {'success': False, 'message': "O'zingizni qo'sha olmaysiz!"}
        try:
            with self.get_cursor() as cursor:
                cursor.execute('SELECT full_name FROM users WHERE user_id = %s', (partner_user_id,))
                partner = cursor.fetchone()
                if not partner: return {'success': False, 'message': "ID topilmadi!"}
                
                cursor.execute('INSERT INTO user_links (owner_id, viewer_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', (owner_id, partner_user_id))
                cursor.execute('INSERT INTO user_links (owner_id, viewer_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', (partner_user_id, owner_id))
                
                cursor.execute('INSERT INTO expense_permissions (expense_id, user_id) SELECT expense_id, %s FROM expenses WHERE creator_id = %s ON CONFLICT DO NOTHING', (partner_user_id, owner_id))
                cursor.execute('INSERT INTO expense_permissions (expense_id, user_id) SELECT expense_id, %s FROM expenses WHERE creator_id = %s ON CONFLICT DO NOTHING', (owner_id, partner_user_id))
                
                return {'success': True, 'partner_name': partner[0]}
        except Exception as e:
            return {'success': False, 'message': str(e)}

    def create_expense(self, creator_id: int, title: str, amount: float, category: str) -> Dict:
        try:
            with self.get_cursor() as cursor:
                cursor.execute(
                    '''
                    SELECT
                        COALESCE(SUM(amount), 0),
                        (SELECT budget_limit FROM users WHERE user_id = %s)
                    FROM expenses
                    WHERE creator_id = %s
                      AND expense_date >= date_trunc('month', CURRENT_DATE)::date
                      AND expense_date < (date_trunc('month', CURRENT_DATE) + interval '1 month')::date
                    ''',
                    (creator_id, creator_id),
                )
                spent_before, _limit_before = cursor.fetchone()

                date = datetime.now().strftime('%Y-%m-%d')
                cursor.execute('INSERT INTO expenses (creator_id, title, amount, category, expense_date) VALUES (%s, %s, %s, %s, %s) RETURNING expense_id', (creator_id, title, amount, category, date))
                exp_id = cursor.fetchone()[0]
                
                cursor.execute('INSERT INTO expense_permissions (expense_id, user_id) VALUES (%s, %s)', (exp_id, creator_id))
                
                cursor.execute('SELECT u.user_id, u.telegram_id FROM user_links ul JOIN users u ON ul.viewer_id = u.user_id WHERE ul.owner_id = %s', (creator_id,))
                partners = cursor.fetchall()

                cursor.execute('SELECT full_name FROM users WHERE user_id = %s', (creator_id,))
                creator_name_row = cursor.fetchone()
                creator_name = creator_name_row[0] if creator_name_row else ""

                partner_tg_ids: List[int] = []
                if partners:
                    permissions_rows = [(exp_id, p_id) for p_id, _ in partners]
                    cursor.executemany(
                        'INSERT INTO expense_permissions (expense_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING',
                        permissions_rows,
                    )

                    notif_msg = f"🆕 {title}: {amount:,.0f} ({creator_name})"
                    notif_rows = [(p_id, notif_msg) for p_id, _ in partners]
                    cursor.executemany('INSERT INTO notifications (user_id, message) VALUES (%s, %s)', notif_rows)

                    partner_tg_ids = [p_tg for _, p_tg in partners]
                
                cursor.execute(
                    '''
                    SELECT
                        COALESCE(SUM(amount), 0),
                        (SELECT budget_limit FROM users WHERE user_id = %s)
                    FROM expenses
                    WHERE creator_id = %s
                      AND expense_date >= date_trunc('month', CURRENT_DATE)::date
                      AND expense_date < (date_trunc('month', CURRENT_DATE) + interval '1 month')::date
                    ''',
                    (creator_id, creator_id),
                )
                spent, limit = cursor.fetchone()

                spent_before = float(spent_before or 0)
                spent_after = float(spent or 0)
                limit_val = float(limit or 0)

                crossed_80 = False
                crossed_90 = False
                crossed_100 = False
                if limit_val > 0:
                    crossed_80 = (spent_before < 0.8 * limit_val) and (spent_after >= 0.8 * limit_val)
                    crossed_90 = (spent_before < 0.9 * limit_val) and (spent_after >= 0.9 * limit_val)
                    crossed_100 = (spent_before < limit_val) and (spent_after >= limit_val)

                return {
                    'success': True,
                    'total': spent_after,
                    'limit': limit_val,
                    'is_limit_reached': (limit_val > 0 and spent_after >= limit_val),
                    'partner_tg_ids': partner_tg_ids,
                    'creator_name': creator_name,
                    'final_amount': amount,
                    'category': category,
                    'spent_before': spent_before,
                    'spent_after': spent_after,
                    'crossed_80': crossed_80,
                    'crossed_90': crossed_90,
                    'crossed_100': crossed_100,
                }
        except Exception as e:
            return {'success': False, 'message': str(e)}

    def delete_expense(self, expense_id: int, user_id: int):
        with self.get_cursor() as cursor:
            cursor.execute('DELETE FROM expenses WHERE expense_id = %s AND creator_id = %s', (expense_id, user_id))
            return cursor.rowcount > 0

    def get_expenses(self, user_id: int):
        with self.get_cursor() as cursor:
            cursor.execute('SELECT e.expense_id, e.title, e.amount, u.full_name, e.category, e.creator_id FROM expenses e JOIN expense_permissions ep ON e.expense_id = ep.expense_id JOIN users u ON e.creator_id = u.user_id WHERE ep.user_id = %s ORDER BY e.created_at DESC LIMIT 10', (user_id,))
            res = cursor.fetchall()
            cursor.execute('SELECT SUM(e.amount) FROM expenses e JOIN expense_permissions ep ON e.expense_id = ep.expense_id WHERE ep.user_id = %s', (user_id,))
            total = cursor.fetchone()[0] or 0
            return res, total

    def get_statistics(self, user_id: int):
        with self.get_cursor() as cursor:
            cursor.execute(
                """
                SELECT category, SUM(amount)
                FROM expenses e
                JOIN expense_permissions ep ON e.expense_id = ep.expense_id
                WHERE ep.user_id = %s
                  AND e.expense_date >= date_trunc('month', CURRENT_DATE)::date
                  AND e.expense_date < (date_trunc('month', CURRENT_DATE) + interval '1 month')::date
                GROUP BY category
                ORDER BY SUM(amount) DESC
                """,
                (user_id,),
            )
            cat_stats = cursor.fetchall()
            cursor.execute(
                """
                SELECT expense_date, SUM(amount)
                FROM expenses e
                JOIN expense_permissions ep ON e.expense_id = ep.expense_id
                WHERE ep.user_id = %s
                  AND e.expense_date >= date_trunc('month', CURRENT_DATE)::date
                  AND e.expense_date < (date_trunc('month', CURRENT_DATE) + interval '1 month')::date
                GROUP BY expense_date
                ORDER BY expense_date ASC
                """,
                (user_id,),
            )
            daily_stats = cursor.fetchall()
            cursor.execute('SELECT budget_limit FROM users WHERE user_id = %s', (user_id,))
            limit = cursor.fetchone()[0] or 0
            return cat_stats, daily_stats, limit

    def get_export_data(self, user_id: int):
        with self.get_cursor() as cursor:
            cursor.execute("""SELECT expense_date, title, category, amount, u.full_name FROM expenses e JOIN expense_permissions ep ON e.expense_id = ep.expense_id JOIN users u ON e.creator_id = u.user_id WHERE ep.user_id = %s ORDER BY expense_date DESC""", (user_id,))
            return cursor.fetchall()

    def set_limit(self, user_id: int, amount: float):
        with self.get_cursor() as cursor:
            cursor.execute('UPDATE users SET budget_limit = %s WHERE user_id = %s', (amount, user_id))

    def get_notifications(self, user_id: int, limit: int = 10):
        with self.get_cursor() as cursor:
            cursor.execute(
                'SELECT notification_id, message, is_read, created_at FROM notifications WHERE user_id = %s ORDER BY created_at DESC LIMIT %s',
                (user_id, limit),
            )
            return cursor.fetchall()

    def mark_notifications_read(self, user_id: int, notif_ids: List[int]):
        if not notif_ids:
            return
        with self.get_cursor() as cursor:
            cursor.execute(
                'UPDATE notifications SET is_read = TRUE WHERE user_id = %s AND notification_id = ANY(%s)',
                (user_id, notif_ids),
            )

# Dasturni ishga tushirish
bot_db = TelegramExpenseBot()

# ==================== CHART GENERATION ====================
def create_pie_chart(stats):
    if not stats: return None
    labels, sizes = [r[0] for r in stats], [r[1] for r in stats]
    fig, ax = plt.subplots(figsize=(8, 8))
    colors = ['#ff9999','#66b3ff','#99ff99','#ffcc99', '#c2c2f0', '#ffb3e6']
    ax.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=90, pctdistance=0.85, colors=colors[:len(labels)], textprops={'fontweight':'bold'})
    ax.add_artist(plt.Circle((0,0),0.60,fc='white'))
    plt.title("Xarajatlar Kategoriyasi", fontsize=14)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    plt.close()
    buf.seek(0)
    return buf

def create_bar_chart(stats):
    if not stats: return None
    dates, amounts = [r[0].strftime('%d') for r in stats], [r[1] for r in stats]
    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.bar(dates, amounts, color='#66b3ff')
    ax.bar_label(bars, fmt='%d')
    plt.title("Kunlik Dinamika", fontsize=14)
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    plt.close()
    buf.seek(0)
    return buf

# ==================== HANDLERS ====================
async def get_current_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[Dict]:
    tg_id = update.effective_user.id
    cached = context.user_data.get('user')
    if cached and cached.get('telegram_id') == tg_id:
        return cached
    user = await asyncio.to_thread(bot_db.get_user_cached, tg_id)
    if user:
        user = {**user, 'telegram_id': tg_id}
        context.user_data['user'] = user
    return user

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_current_user(update, context)
    if user:
        await show_main_menu(update, f"Xush kelibsiz, <b>{user['full_name']}</b>!")
        context.user_data.clear()
    else:
        await update.message.reply_text("🤖 <b>Aqlli Hamyon</b>ga xush kelibsiz!\nIltimos, ro'yxatdan o'tish uchun <b>username</b> tanlang:", parse_mode='HTML')
        context.user_data['state'] = 'choosing_username'

async def show_main_menu(update: Update, text: str):
    kb = ReplyKeyboardMarkup([
        [KeyboardButton("➕ Yangi harajat"), KeyboardButton("📋 Harajatlar")], 
        [KeyboardButton("📊 Statistika"), KeyboardButton("📥 Excel yuklash")],
        [KeyboardButton("⚙️ Limit o'rnatish"), KeyboardButton("👥 Sherik qo'shish")],
        [KeyboardButton("🔔 Bildirishnomalar"), KeyboardButton("🆔 ID raqamim")]
    ], resize_keyboard=True)
    try:
        if update.callback_query: await update.callback_query.message.reply_text(text, reply_markup=kb, parse_mode='HTML')
        else: await update.message.reply_text(text, reply_markup=kb, parse_mode='HTML')
    except: pass

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Yordam</b>\n"
        "- Harajat qo'shish: ➕ Yangi harajat yoki matnni bevosita yozing\n"
        "- Ovoz: voice yuboring (60s gacha)\n"
        "- Bildirishnomalar: 🔔 Bildirishnomalar\n"
        "- Bekor qilish: /cancel"
    )
    try:
        await update.message.reply_text(text, parse_mode='HTML')
    except:
        pass

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = await get_current_user(update, context)
    if user:
        await show_main_menu(update, "✅ Bekor qilindi.")
    else:
        try:
            await update.message.reply_text("✅ Bekor qilindi. /start bosing.")
        except:
            pass

async def process_expense(update, context, user, amount, title, category):
    res = await asyncio.to_thread(bot_db.create_expense, user['user_id'], title, amount, category)
    if not res['success']: return await update.message.reply_text(f"❌ Xatolik: {res['message']}")

    text = f"✅ <b>Saqlandi!</b>\n📝 {title}\n📂 {category}\n💰 <b>{amount:,.0f} so'm</b>"
    await show_main_menu(update, text)

    warn_pids = res.get('partner_tg_ids', []) + [user['telegram_id']]

    if res.get('crossed_80'):
        msg = f"⚠️ <b>Limit 80% ga yetdi</b>\nSarflandi: {res['spent_after']:,.0f} / {res['limit']:,.0f}"
        for pid in warn_pids:
            try: await context.bot.send_message(pid, msg, parse_mode='HTML')
            except: pass

    if res.get('crossed_90'):
        msg = f"⚠️ <b>Limit 90% ga yetdi</b>\nSarflandi: {res['spent_after']:,.0f} / {res['limit']:,.0f}"
        for pid in warn_pids:
            try: await context.bot.send_message(pid, msg, parse_mode='HTML')
            except: pass

    if res.get('crossed_100') or res.get('is_limit_reached'):
        msg = f"🚨 <b>DIQQAT! LIMIT OSHDI!</b>\nSarflandi: {res['total']:,.0f} / {res['limit']:,.0f}"
        for pid in warn_pids:
            try: await context.bot.send_message(pid, msg, parse_mode='HTML')
            except: pass
    else:
        msg = f"🆕 <b>{title}</b>: {amount:,.0f} so'm ({user['full_name']})"
        for pid in res.get('partner_tg_ids', []):
            try: await context.bot.send_message(pid, msg, parse_mode='HTML')
            except: pass

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_current_user(update, context)
    if not user: return await update.message.reply_text("Avval /start bosing.")

    wait_msg = await update.message.reply_text("🎤 Eshitayapman (AI)...")
    try:
        tg_voice = update.message.voice
        if tg_voice and getattr(tg_voice, "duration", 0) and tg_voice.duration > 60:
            try:
                await wait_msg.edit_text("❌ Ovoz juda uzun. Iltimos 60 soniyadan qisqa yuboring.")
            except:
                pass
            return

        file = await context.bot.get_file(update.message.voice.file_id)
        uniq = uuid.uuid4().hex
        f_ogg = os.path.join(tempfile.gettempdir(), f"voice_{user['user_id']}_{uniq}.ogg")
        f_wav = os.path.join(tempfile.gettempdir(), f"voice_{user['user_id']}_{uniq}.wav")
        await file.download_to_drive(f_ogg)

        def recognize_from_file() -> str:
            try:
                data, rate = sf.read(f_ogg)
                if hasattr(data, "ndim") and data.ndim > 1:
                    data = data.mean(axis=1)
                if rate and rate != 16000:
                    pass
                sf.write(f_wav, data, rate, subtype='PCM_16')
            except Exception:
                raise

            r = sr.Recognizer()
            r.dynamic_energy_threshold = True
            r.pause_threshold = 0.8
            r.non_speaking_duration = 0.4

            with sr.AudioFile(f_wav) as source:
                try:
                    r.adjust_for_ambient_noise(source, duration=0.4)
                except Exception:
                    pass
                audio = r.record(source)

            last_err = None
            for _ in range(2):
                try:
                    return r.recognize_google(audio, language="uz-UZ")
                except sr.RequestError as e:
                    last_err = e
                    time.sleep(0.4)
                except sr.UnknownValueError as e:
                    last_err = e
                    break
            if last_err:
                raise last_err
            raise sr.UnknownValueError()

        text = await asyncio.to_thread(recognize_from_file)

        try:
            await wait_msg.delete()
        except:
            pass

        amount, title, category = await asyncio.to_thread(analyze_text_with_gemini, text)
        if amount > 0: await process_expense(update, context, user, amount, title, category)
        else:
            context.user_data.update({'title': text, 'state': 'exp_amount'})
            await update.message.reply_text(f"🗣 <b>Eshitdim:</b> \"{text}\"\nSummani yozing:", parse_mode='HTML')

    except sr.UnknownValueError:
        try:
            await wait_msg.edit_text("❌ Ovoz aniq tushunilmadi. Iltimos sekinroq va yaqinroq gapirib qayta yuboring.")
        except:
            pass
    except sr.RequestError:
        try:
            await wait_msg.edit_text("❌ Ovoz tanish xizmati vaqtincha ishlamayapti. Keyinroq urinib ko'ring.")
        except:
            pass
    except Exception as e:
        logger.error(f"Voice Error: {e}")
        try:
            await wait_msg.edit_text("❌ Tushunmadim yoki xatolik.")
        except:
            pass
    finally:
        for f in [locals().get('f_ogg'), locals().get('f_wav')]:
            if f and isinstance(f, str) and os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    pass

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    user = await get_current_user(update, context)
    state = context.user_data.get('state')

    if state == 'choosing_username':
        if len(msg) < 3: return await update.message.reply_text("Username juda qisqa.")
        res = await asyncio.to_thread(bot_db.register_user, update.effective_user.id, msg, update.effective_user.first_name)
        if res['success']:
            context.user_data.clear()
            await show_main_menu(update, "✅ Muvaffaqiyatli ro'yxatdan o'tdingiz!")
        else: await update.message.reply_text(res['message'])
        return

    if not user: return await start(update, context)

    # AI Analiz (Direct Input)
    if not state and msg not in ["➕ Yangi harajat", "📋 Harajatlar", "📊 Statistika", "⚙️ Limit o'rnatish", "👥 Sherik qo'shish", "🔔 Bildirishnomalar", "🆔 ID raqamim", "📥 Excel yuklash"]:
        amount, title, category = await asyncio.to_thread(analyze_text_with_gemini, msg)
        if amount > 0: return await process_expense(update, context, user, amount, title, category)

    if msg == "➕ Yangi harajat":
        await update.message.reply_text("Nomi:", reply_markup=ReplyKeyboardRemove())
        context.user_data['state'] = 'exp_title'

    elif msg == "📋 Harajatlar":
        exps, total = await asyncio.to_thread(bot_db.get_expenses, user['user_id'])
        if not exps: await update.message.reply_text("📭 Bo'sh")
        else:
            await update.message.reply_text(f"💰 <b>JAMI: {total:,.0f} so'm</b>", parse_mode='HTML')
            for e in exps:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 O'chirish", callback_data=f"del_{e[0]}")]]) if e[5] == user['user_id'] else None
                await update.message.reply_text(f"🗓 {e[1]}\n💸 {e[2]:,.0f}\n📂 {e[4]}\n👤 {e[3]}", reply_markup=kb)

    elif msg == "📥 Excel yuklash":

        await update.message.reply_text("⏳ Tayyorlanmoqda...")
        data = await asyncio.to_thread(bot_db.get_export_data, user['user_id'])
        if data:
            s = io.StringIO()
            csv.writer(s).writerow(["Sana", "Nomi", "Kategoriya", "Summa", "Kim"])
            for row in data: csv.writer(s).writerow(row)
            s.seek(0)
            doc = io.BytesIO(s.getvalue().encode('utf-8-sig'))
            doc.name = "Hisobot.csv"
            await update.message.reply_document(doc, caption="📊 To'liq hisobot")
        else: await update.message.reply_text("Ma'lumot yo'q")

    elif msg == "📊 Statistika":

        cat_s, day_s, lim = await asyncio.to_thread(bot_db.get_statistics, user['user_id'])
        if cat_s:
            await update.message.reply_photo(await asyncio.to_thread(create_pie_chart, cat_s), caption=f"Jami: {sum([x[1] for x in cat_s]):,.0f} so'm")
            if day_s: await update.message.reply_photo(await asyncio.to_thread(create_bar_chart, day_s))
        else: await update.message.reply_text("Ma'lumot yo'q")

    elif msg == "🔔 Bildirishnomalar":
        rows = await asyncio.to_thread(bot_db.get_notifications, user['user_id'], 10)
        if not rows:
            return await update.message.reply_text("🔔 Bildirishnoma yo'q")

        unread_ids = []
        out_lines = []
        for nid, message, is_read, created_at in rows:
            if not is_read:
                unread_ids.append(nid)
            ts = created_at.strftime('%d.%m %H:%M') if hasattr(created_at, 'strftime') else str(created_at)
            prefix = "✅" if is_read else "🆕"
            out_lines.append(f"{prefix} <i>{ts}</i>\n{message}")
        await update.message.reply_text("\n\n".join(out_lines), parse_mode='HTML')
        if unread_ids:
            await asyncio.to_thread(bot_db.mark_notifications_read, user['user_id'], unread_ids)

    elif msg == "🆔 ID raqamim":
        await update.message.reply_text(f"🆔 ID: <code>{user['user_id']}</code>", parse_mode='HTML')

    elif msg == "⚙️ Limit o'rnatish":
        await update.message.reply_text("Summa:", reply_markup=ReplyKeyboardRemove())
        context.user_data['state'] = 'setting_limit'

    elif msg == "👥 Sherik qo'shish":
        await update.message.reply_text("ID raqam:", reply_markup=ReplyKeyboardRemove())
        context.user_data['state'] = 'adding_partner'

    elif state == 'exp_title':
        context.user_data['title'] = msg
        await update.message.reply_text("Summa:")
        context.user_data['state'] = 'exp_amount'

    elif state == 'exp_amount':
        try:
            val = float(msg.replace(" ", "").replace(",", ""))
            context.user_data['amount'] = val
            context.user_data['state'] = 'exp_category'
            await update.message.reply_text("Kategoriya tanlang:", reply_markup=build_category_kb())
        except: await update.message.reply_text("Raqam yozing!")

    elif state == 'exp_category':
        await update.message.reply_text("Kategoriya tanlang:", reply_markup=build_category_kb())

    elif state == 'setting_limit':
        try:
            await asyncio.to_thread(bot_db.set_limit, user['user_id'], float(msg))
            await show_main_menu(update, "✅ Limit o'rnatildi!")
            context.user_data.clear()
        except: pass

    elif state == 'adding_partner':
        try:
            res = await asyncio.to_thread(bot_db.add_partner_by_id, user['user_id'], int(msg))
            await show_main_menu(update, f"✅ {res['partner_name']} ulandi!" if res['success'] else f"❌ {res['message']}")
            context.user_data.clear()
        except: pass

async def delete_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data.startswith("del_"):
        uid = await asyncio.to_thread(bot_db.get_user_cached, q.from_user.id)
        if uid and await asyncio.to_thread(bot_db.delete_expense, int(q.data.split("_")[1]), uid['user_id']):
            await q.edit_message_text("🗑 O'chirildi!")
        else:
            await q.edit_message_text("❌ Xatolik")
        return

    if q.data.startswith("cat_"):
        cat = q.data[len("cat_"):]
        user = await get_current_user(update, context)
        amount = context.user_data.get('amount')
        title = context.user_data.get('title')
        if not user or amount is None or not title:
            try:
                await q.edit_message_text("❌ Jarayon eskirib qolgan. Qaytadan urinib ko'ring.")
            except:
                pass
            context.user_data.clear()
            return

        try:
            await q.edit_message_text(f"✅ Kategoriya: {cat}")
        except:
            pass

        await process_expense(update, context, user, float(amount), title, cat)
        context.user_data.clear()
        return

def build_category_kb() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for i, cat in enumerate(CATEGORIES, start=1):
        row.append(InlineKeyboardButton(cat, callback_data=f"cat_{cat}"))
        if i % 2 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def main():
    if not BOT_TOKEN: return print("❌ BOT_TOKEN topilmadi!")

    Thread(target=start_health_check_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).request(HTTPXRequest(connection_pool_size=1000, connect_timeout=30)).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    app.add_handler(CallbackQueryHandler(delete_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("Bot ishga tushdi...")
    app.run_polling()

if __name__ == '__main__': main()