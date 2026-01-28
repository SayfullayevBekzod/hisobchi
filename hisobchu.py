import logging
import asyncio
import io
import os
import re
import threading
import speech_recognition as sr
import soundfile as sf
import matplotlib
import matplotlib.pyplot as plt
import psycopg2
from psycopg2 import pool
from datetime import datetime
from typing import Dict, List, Tuple
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.request import HTTPXRequest

# Serverda ishlashi uchun
matplotlib.use('Agg')

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 👇👇👇 SHU YERGA NEON HAVOLANGIZNI QO'YING 👇👇👇
NEON_DB_URL = "postgresql://neondb_owner:npg_DE94nSeTHjLa@ep-dark-forest-ahrj0z9l-pooler.c-3.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
# 👆👆👆👆👆👆👆👆👆👆👆👆👆👆👆👆👆👆👆👆👆👆

# ==================== KATEGORIYA MANTIQI ====================
def detect_category(text: str) -> str:
    text = text.lower()
    categories = {
        "🍔 Oziq-ovqat": ["osh", "non", "suv", "go'sht", "tushlik", "kechki", "somsa", "shashlik", "bozor", "supermarket", "qatiq", "sut", "ovqat", "restoran", "lavash", "burger"],
        "🚕 Transport": ["taksi", "taxi", "avtobus", "metro", "benzin", "gaz", "moy", "zapravka", "propan", "yo'l", "yol", "proezd"],
        "🏠 Uy-ro'zg'or": ["svet", "gaz", "suv", "ijara", "remont", "mebel", "ximiya", "poroshok", "kommunal", "elektr"],
        "🌐 Aloqa": ["internet", "telefon", "paynet", "wi-fi", "mb", "trafik", "tarif"],
        "💊 Sog'liq": ["dorixona", "dori", "vrach", "bolnitsa", "tish", "analiz", "davolanish", "doktor"],
        "👕 Kiyim": ["shim", "ko'ylak", "oyoq", "krossovka", "paypoq", "kiyim", "etik"],
        "🎉 O'yin-kulgi": ["kafe", "kino", "park", "sovg'a", "choyxona", "dam", "gap"],
        "📚 Ta'lim": ["kurs", "kitob", "daftar", "qalam", "o'qish", "universitet", "kontrakt"]
    }
    for cat, keywords in categories.items():
        for keyword in keywords:
            if keyword in text: return cat
    return "📦 Boshqa"

# ==================== DATABASE CLASS (NEON / POSTGRESQL) ====================
class TelegramExpenseBot:
    def __init__(self):
        self.init_pool()
        self.init_database()
    
    def init_pool(self):
        """Neon bazasiga ulanish hovuzini yaratish"""
        try:
            self.pool = psycopg2.pool.SimpleConnectionPool(1, 10, NEON_DB_URL)
            logger.info("✅ Neon PostgreSQL bazasiga ulandi!")
        except Exception as e:
            logger.error(f"❌ Baza ulanishida xato: {e}")
            raise e

    def get_conn(self):
        return self.pool.getconn()

    def put_conn(self, conn):
        if conn:
            self.pool.putconn(conn)

    def init_database(self):
        conn = self.get_conn()
        try:
            cursor = conn.cursor()
            # Usersalizor
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    custom_username TEXT UNIQUE NOT NULL,
                    full_name TEXT, 
                    budget_limit REAL DEFAULT 0, 
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Expenses
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS expenses (
                    expense_id SERIAL PRIMARY KEY, 
                    creator_id INTEGER NOT NULL REFERENCES users(user_id), 
                    title TEXT NOT NULL, 
                    amount REAL NOT NULL, 
                    category TEXT NOT NULL, 
                    expense_date DATE NOT NULL, 
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Links
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_links (
                    link_id SERIAL PRIMARY KEY, 
                    owner_id INTEGER NOT NULL REFERENCES users(user_id), 
                    viewer_id INTEGER NOT NULL REFERENCES users(user_id), 
                    UNIQUE(owner_id, viewer_id)
                )
            ''')
            # Permissions
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS expense_permissions (
                    permission_id SERIAL PRIMARY KEY, 
                    expense_id INTEGER NOT NULL REFERENCES expenses(expense_id) ON DELETE CASCADE, 
                    user_id INTEGER NOT NULL REFERENCES users(user_id), 
                    UNIQUE(expense_id, user_id)
                )
            ''')
            # Notifications
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    notification_id SERIAL PRIMARY KEY, 
                    user_id INTEGER NOT NULL REFERENCES users(user_id), 
                    message TEXT NOT NULL, 
                    is_read BOOLEAN DEFAULT FALSE, 
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"DB Init Error: {e}")
        finally:
            self.put_conn(conn)
    
    def get_user(self, telegram_id: int) -> Dict:
        conn = self.get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, custom_username, full_name, budget_limit FROM users WHERE telegram_id = %s', (telegram_id,))
            user = cursor.fetchone()
            return {'user_id': user[0], 'username': user[1], 'full_name': user[2], 'budget_limit': user[3]} if user else None
        finally:
            self.put_conn(conn)

    def check_username_exists(self, username: str) -> bool:
        conn = self.get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM users WHERE custom_username = %s', (username.lower(),))
            return cursor.fetchone() is not None
        finally:
            self.put_conn(conn)

    def register_user(self, telegram_id: int, username: str, full_name: str) -> Dict:
        conn = self.get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO users (telegram_id, custom_username, full_name) VALUES (%s, %s, %s)', 
                         (telegram_id, username.lower(), full_name))
            conn.commit()
            return {'success': True}
        except psycopg2.IntegrityError:
            conn.rollback()
            return {'success': False, 'message': "Bu username band!"}
        finally:
            self.put_conn(conn)

    def add_partner_by_id(self, owner_id: int, partner_user_id: int) -> Dict:
        if owner_id == partner_user_id: return {'success': False, 'message': "O'zingizni qo'sha olmaysiz!"}
        conn = self.get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT full_name FROM users WHERE user_id = %s', (partner_user_id,))
            partner = cursor.fetchone()
            if not partner: return {'success': False, 'message': "ID topilmadi!"}
            
            # Postgres syntax (ON CONFLICT DO NOTHING)
            cursor.execute('INSERT INTO user_links (owner_id, viewer_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', (owner_id, partner_user_id))
            cursor.execute('INSERT INTO user_links (owner_id, viewer_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', (partner_user_id, owner_id))
            
            cursor.execute('INSERT INTO expense_permissions (expense_id, user_id) SELECT expense_id, %s FROM expenses WHERE creator_id = %s ON CONFLICT DO NOTHING', (partner_user_id, owner_id))
            cursor.execute('INSERT INTO expense_permissions (expense_id, user_id) SELECT expense_id, %s FROM expenses WHERE creator_id = %s ON CONFLICT DO NOTHING', (owner_id, partner_user_id))
            
            conn.commit()
            return {'success': True, 'partner_name': partner[0]}
        except Exception as e:
            conn.rollback()
            return {'success': False, 'message': str(e)}
        finally:
            self.put_conn(conn)

    def create_expense(self, creator_id: int, title: str, amount: float) -> Dict:
        conn = self.get_conn()
        try:
            if 0 < amount < 1000: amount *= 1000
            normalized_title = title.strip().capitalize()
            category = detect_category(normalized_title)
            
            cursor = conn.cursor()
            date = datetime.now().strftime('%Y-%m-%d')
            
            # Insert and get ID
            cursor.execute('INSERT INTO expenses (creator_id, title, amount, category, expense_date) VALUES (%s, %s, %s, %s, %s) RETURNING expense_id', 
                         (creator_id, normalized_title, amount, category, date))
            exp_id = cursor.fetchone()[0]
            
            cursor.execute('INSERT INTO expense_permissions (expense_id, user_id) VALUES (%s, %s)', (exp_id, creator_id))
            
            cursor.execute('SELECT u.user_id, u.telegram_id FROM user_links ul JOIN users u ON ul.viewer_id = u.user_id WHERE ul.owner_id = %s', (creator_id,))
            partners = cursor.fetchall()
            
            cursor.execute('SELECT full_name FROM users WHERE user_id = %s', (creator_id,))
            creator_name = cursor.fetchone()[0]
            partner_tg_ids = []
            
            for p_id, p_tg in partners:
                cursor.execute('INSERT INTO expense_permissions (expense_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', (exp_id, p_id))
                cursor.execute('INSERT INTO notifications (user_id, message) VALUES (%s, %s)', (p_id, f"🆕 {normalized_title}: {amount:,.0f} ({creator_name})"))
                partner_tg_ids.append(p_tg)
            
            # Limit check (Postgres Date functions)
            cursor.execute('''
                SELECT COALESCE(SUM(amount), 0), (SELECT budget_limit FROM users WHERE user_id = %s) 
                FROM expenses 
                WHERE creator_id = %s AND TO_CHAR(expense_date, 'YYYY-MM') = TO_CHAR(NOW(), 'YYYY-MM')
            ''', (creator_id, creator_id))
            spent, limit = cursor.fetchone()
            
            conn.commit()
            return {'success': True, 'total': spent, 'limit': limit, 'is_limit_reached': (limit > 0 and spent >= limit), 'partner_tg_ids': partner_tg_ids, 'creator_name': creator_name, 'final_amount': amount, 'category': category}
        except Exception as e:
            conn.rollback()
            return {'success': False, 'message': str(e)}
        finally:
            self.put_conn(conn)

    def delete_expense(self, expense_id: int, user_id: int):
        conn = self.get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM expenses WHERE expense_id = %s AND creator_id = %s', (expense_id, user_id))
            deleted = cursor.rowcount > 0
            conn.commit()
            return deleted
        finally:
            self.put_conn(conn)

    def get_expenses(self, user_id: int):
        conn = self.get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT e.expense_id, e.title, e.amount, u.full_name, e.category, e.creator_id FROM expenses e JOIN expense_permissions ep ON e.expense_id = ep.expense_id JOIN users u ON e.creator_id = u.user_id WHERE ep.user_id = %s ORDER BY e.created_at DESC LIMIT 10', (user_id,))
            res = cursor.fetchall()
            cursor.execute('SELECT SUM(e.amount) FROM expenses e JOIN expense_permissions ep ON e.expense_id = ep.expense_id WHERE ep.user_id = %s', (user_id,))
            total_res = cursor.fetchone()
            total = total_res[0] if total_res and total_res[0] else 0
            return res, total
        finally:
            self.put_conn(conn)

    def get_statistics(self, user_id: int):
        conn = self.get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT category, SUM(amount) FROM expenses e JOIN expense_permissions ep ON e.expense_id = ep.expense_id WHERE ep.user_id = %s AND TO_CHAR(expense_date, 'YYYY-MM') = TO_CHAR(NOW(), 'YYYY-MM') GROUP BY category ORDER BY SUM(amount) DESC", (user_id,))
            stats = cursor.fetchall()
            cursor.execute('SELECT budget_limit FROM users WHERE user_id = %s', (user_id,))
            limit = cursor.fetchone()[0] or 0
            return stats, limit
        finally:
            self.put_conn(conn)

    def set_limit(self, user_id: int, amount: float):
        conn = self.get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET budget_limit = %s WHERE user_id = %s', (amount, user_id))
            conn.commit()
        finally:
            self.put_conn(conn)

bot_db = TelegramExpenseBot()

# ==================== TEXT TO NUMBER ====================
def uzbek_text_to_number(text: str) -> Tuple[float, str]:
    text = text.lower().replace("so'm", "").replace("som", "").replace("sum", "").strip()
    ONES = {"bir": 1, "ikki": 2, "uch": 3, "to'rt": 4, "tort": 4, "besh": 5, "olti": 6, "yetti": 7, "sakkiz": 8, "to'qqiz": 9, "toqqiz": 9, "yarim": 0.5}
    TENS = {"o'n": 10, "on": 10, "yigirma": 20, "o'ttiz": 30, "ottiz": 30, "qirq": 40, "ellik": 50, "oltmish": 60, "yetmish": 70, "sakson": 80, "to'qson": 90, "toqson": 90}
    MULTIPLIERS = {"yuz": 100, "ming": 1000, "min": 1000, "million": 1000000, "milyon": 1000000, "mln": 1000000}

    words = text.split()
    total_value = 0
    current_chunk = 0
    title_words = []
    has_number = False

    for word in words:
        clean = word.replace(",", "").replace(".", "")
        if clean.isdigit():
            val = float(clean)
            current_chunk += val
            has_number = True
        elif clean in ONES:
            current_chunk += ONES[clean]
            has_number = True
        elif clean in TENS:
            current_chunk += TENS[clean]
            has_number = True
        elif clean in MULTIPLIERS:
            mult = MULTIPLIERS[clean]
            has_number = True
            if mult == 100:
                if current_chunk == 0: current_chunk = 1
                current_chunk *= 100
            else:
                if current_chunk == 0: current_chunk = 1
                total_value += current_chunk * mult
                current_chunk = 0
        else:
            title_words.append(word)

    total_value += current_chunk
    title = " ".join(title_words).strip().capitalize()
    if 0 < total_value < 1000 and has_number: total_value *= 1000
    if not has_number: return 0.0, text.capitalize()
    return float(total_value), title

# ==================== CHART GENERATION ====================
def create_chart(stats):
    if not stats: return None
    labels = [row[0] for row in stats]
    sizes = [row[1] for row in stats]
    total = sum(sizes)

    fig, ax = plt.subplots(figsize=(8, 8))
    colors = ['#ff9999','#66b3ff','#99ff99','#ffcc99', '#c2c2f0', '#ffb3e6', '#c4e17f']
    wedges, texts, autotexts = ax.pie(
        sizes, labels=labels, autopct='%1.1f%%',
        startangle=90, pctdistance=0.85, colors=colors[:len(labels)],
        textprops=dict(color="black", fontsize=10, fontweight='bold'),
        wedgeprops=dict(width=0.4, edgecolor='w')
    )
    ax.text(0, 0, f"JAMI:\n{total:,.0f}", ha='center', va='center', fontsize=14, fontweight='bold')
    plt.title("Xarajatlar Kategoriyasi", fontsize=16, pad=20)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=100)
    buf.seek(0)
    plt.close()
    return buf

# ==================== HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await asyncio.to_thread(bot_db.get_user, update.effective_user.id)
    intro_text = (
        "🤖 <b>Aqlli Hamyon Botiga Xush Kelibsiz!</b>\n\n"
        "Men sizga moliyaviy erkinlikka erishishda yordam beraman.\n\n"
        "🌟 <b>Mening imkoniyatlarim:</b>\n\n"
        "🗣 <b>Ovozli va Matnli kiritish</b>\n"
        "<i>\"Tushlik 50 ming\"</i> deb yozing yoki gapiring. Men o'zim tushunib, hisobga qo'shib qo'yaman.\n\n"
        "🧠 <b>Aqlli Kategoriya</b>\n"
        "Xarajat nomiga qarab avtomatik kategoriya aniqlayman.\n\n"
        "👥 <b>Sheriklik Rejimi</b>\n"
        "Hisob-kitobni sheriklar bilan birga yuriting.\n\n"
        "🚀 <b>Boshlash uchun pastdagi tugmani bosing!</b>"
    )
    if user:
        await update.message.reply_text(intro_text, parse_mode='HTML')
        await show_main_menu(update, f"Xush kelibsiz, <b>{user['full_name']}</b>!")
        context.user_data.clear()
    else:
        await update.message.reply_text(intro_text, parse_mode='HTML')
        await update.message.reply_text("🆔 <b>Ro'yxatdan o'tish uchun o'zingizga unikal username tanlang:</b>", parse_mode='HTML')
        context.user_data['state'] = 'choosing_username'

async def show_main_menu(update: Update, text: str):
    kb = ReplyKeyboardMarkup([
        [KeyboardButton("➕ Yangi harajat"), KeyboardButton("📋 Harajatlar")],
        [KeyboardButton("📊 Statistika"), KeyboardButton("⚙️ Limit o'rnatish")],
        [KeyboardButton("👥 Sherik qo'shish"), KeyboardButton("🆔 ID raqamim")]
    ], resize_keyboard=True)
    if update.callback_query: 
        try: await update.callback_query.message.reply_text(text, reply_markup=kb, parse_mode='HTML')
        except: pass
    else: 
        await update.message.reply_text(text, reply_markup=kb, parse_mode='HTML')

async def process_expense(update, context, user, amount, title, source_type="text"):
    if len(title) < 2: title = "Nomsiz harajat"
    res = await asyncio.to_thread(bot_db.create_expense, user['user_id'], title, amount)
    
    if not res.get('success'):
        await update.message.reply_text(f"❌ Baza xatosi: {res.get('message', 'Noma\'lum xato')}")
        return

    icon = "🗣" if source_type == "voice" else "✍️"
    response_text = (
        f"{icon} <b>Qabul qilindi!</b>\n\n"
        f"📝 <b>{title}</b>\n"
        f"📂 <i>{res['category']}</i>\n"
        f"💰 <b>{res['final_amount']:,.0f} so'm</b>"
    )
    
    if source_type == "voice":
        await update.message.reply_text(response_text, parse_mode='HTML')
    else:
        await show_main_menu(update, response_text)

    partners = res['partner_tg_ids']
    if res['is_limit_reached']:
        alert = f"🚨 <b>DIQQAT! LIMIT OSHDI!</b>\n({res['total']:,.0f} / {res['limit']:,.0f})"
        for pid in partners + [user['telegram_id']]:
            try: await context.bot.send_message(pid, alert, parse_mode='HTML')
            except: pass
    else:
        msg = f"🆕 <b>{title}</b>: {res['final_amount']:,.0f} so'm\n📂 {res['category']}\n👤 {res['creator_name']}"
        for pid in partners:
            try: await context.bot.send_message(pid, msg, parse_mode='HTML')
            except: pass

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await asyncio.to_thread(bot_db.get_user, update.effective_user.id)
    if not user: return await update.message.reply_text("Avval /start bosing.")

    waiting_msg = await update.message.reply_text("🎤 Eshitayapman...")

    try:
        new_file = await context.bot.get_file(update.message.voice.file_id)
        file_path = f"voice_{update.effective_user.id}.ogg"
        wav_path = f"voice_{update.effective_user.id}.wav"
        
        await new_file.download_to_drive(file_path)

        def convert_and_recognize():
            data, samplerate = sf.read(file_path)
            sf.write(wav_path, data, samplerate, subtype='PCM_16')
            r = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                audio = r.record(source)
                try: return r.recognize_google(audio, language="uz-UZ")
                except: return ""
        
        text = await asyncio.to_thread(convert_and_recognize)
        
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.exists(wav_path): os.remove(wav_path)
        
        await waiting_msg.delete()

        if not text: 
            return await update.message.reply_text("🤷‍♂️ Tushunmadim.")

        amount, title = uzbek_text_to_number(text)
        
        if amount > 0:
            await process_expense(update, context, user, amount, title, source_type="voice")
        else:
            context.user_data['title'] = text
            context.user_data['state'] = 'exp_amount'
            await update.message.reply_text(f"🗣 <b>Eshitdim:</b> \"{text}\"\n📝 Nomi tushunarli. Endi summani yozing:", parse_mode='HTML')

    except Exception as e:
        logger.error(f"Voice Error: {e}")
        await update.message.reply_text(f"❌ Xatolik: {e}")
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.exists(wav_path): os.remove(wav_path)

async def delete_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    if data[0] == "del":
        exp_id = int(data[1])
        user = await asyncio.to_thread(bot_db.get_user, query.from_user.id)
        success = await asyncio.to_thread(bot_db.delete_expense, exp_id, user['user_id'])
        if success: await query.edit_message_text(f"🗑 Harajat o'chirildi!")
        else: await query.edit_message_text(f"❌ O'chira olmaysiz.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    user = await asyncio.to_thread(bot_db.get_user, update.effective_user.id)
    state = context.user_data.get('state')

    if state == 'choosing_username':
        if len(msg) < 3: return await update.message.reply_text("Username qisqa (min 3 harf).")
        exists = await asyncio.to_thread(bot_db.check_username_exists, msg)
        if exists: await update.message.reply_text("❌ Bu username band.")
        else:
            res = await asyncio.to_thread(bot_db.register_user, update.effective_user.id, msg, update.effective_user.first_name)
            if res['success']:
                context.user_data.clear()
                await show_main_menu(update, "✅ <b>Muvaffaqiyatli ro'yxatdan o'tdingiz!</b>")
            else: await update.message.reply_text(res['message'])
        return

    if not user: 
        if msg == "/start": await start(update, context)
        else: await update.message.reply_text("Iltimos /start bosing")
        return

    if state is None and msg not in ["➕ Yangi harajat", "📋 Harajatlar", "📊 Statistika", "⚙️ Limit o'rnatish", "👥 Sherik qo'shish", "🆔 ID raqamim"]:
        amount, title = uzbek_text_to_number(msg)
        if amount > 0:
            await process_expense(update, context, user, amount, title, source_type="text")
            return

    if msg == "➕ Yangi harajat":
        await update.message.reply_text("📝 <b>Harajat nomini yozing:</b>", parse_mode='HTML', reply_markup=ReplyKeyboardRemove())
        context.user_data['state'] = 'exp_title'
    elif msg == "📋 Harajatlar":
        exps, total = await asyncio.to_thread(bot_db.get_expenses, user['user_id'])
        if not exps: await update.message.reply_text("📭 Harajatlar yo'q")
        else:
            await update.message.reply_text(f"💰 <b>JAMI: {total:,.0f} so'm</b>\nSo'nggi 10 ta:", parse_mode='HTML')
            for e in exps:
                txt = f"🗓 <b>{e[1]}</b>\n💸 {e[2]:,.0f} so'm\n📂 {e[4]}\n👤 {e[3]}"
                if e[5] == user['user_id']:
                    await update.message.reply_text(txt, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 O'chirish", callback_data=f"del_{e[0]}")]]))
                else: await update.message.reply_text(txt, parse_mode='HTML')

    elif msg == "⚙️ Limit o'rnatish":
        await update.message.reply_text("💰 <b>Oylik limit summasini yozing:</b>", parse_mode='HTML', reply_markup=ReplyKeyboardRemove())
        context.user_data['state'] = 'setting_limit'
    elif msg == "👥 Sherik qo'shish":
        await update.message.reply_text("🆔 <b>Sherigingizning ID raqamini yozing:</b>", parse_mode='HTML', reply_markup=ReplyKeyboardRemove())
        context.user_data['state'] = 'adding_partner'
    elif msg == "🆔 ID raqamim":
        await update.message.reply_text(f"🆔 ID: <code>{user['user_id']}</code>", parse_mode='HTML')
    elif msg == "📊 Statistika":
        stats, limit = await asyncio.to_thread(bot_db.get_statistics, user['user_id'])
        if stats:
            chart = await asyncio.to_thread(create_chart, stats)
            total = sum([s[1] for s in stats])
            report = "📊 <b>KATEGORIYALAR BO'YICHA:</b>\n\n"
            for s in stats: report += f"🔹 {s[0]}: <b>{s[1]:,.0f}</b>\n"
            report += f"\n💰 <b>JAMI: {total:,.0f} so'm</b>"
            await update.message.reply_photo(chart, caption=report, parse_mode='HTML')
        else: await update.message.reply_text("Ma'lumot yo'q")

    elif state == 'exp_title':
        context.user_data['title'] = msg
        await update.message.reply_text("💰 <b>Summani yozing:</b>", parse_mode='HTML')
        context.user_data['state'] = 'exp_amount'
    elif state == 'exp_amount':
        try:
            val = float(msg.replace(" ", "").replace(",", ""))
            await process_expense(update, context, user, val, context.user_data['title'], source_type="text")
            context.user_data.clear()
        except: await update.message.reply_text("❌ Faqat raqam yozing!")
    elif state == 'adding_partner':
        try:
            res = await asyncio.to_thread(bot_db.add_partner_by_id, user['user_id'], int(msg))
            await show_main_menu(update, f"✅ <b>{res['partner_name']}</b> ulandi!" if res['success'] else f"❌ {res['message']}")
            context.user_data.clear()
        except: pass
    elif state == 'setting_limit':
        try:
            await asyncio.to_thread(bot_db.set_limit, user['user_id'], float(msg))
            await show_main_menu(update, "✅ <b>Limit o'rnatildi!</b>")
            context.user_data.clear()
        except: pass

def main():
    TOKEN = "8531060867:AAE7rrbe7NglVju8fb0yRY7LslOa40Z0H2E"
    req = HTTPXRequest(connection_pool_size=1000, connect_timeout=30)
    app = Application.builder().token(TOKEN).request(req).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    app.add_handler(CallbackQueryHandler(delete_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("Bot ishga tushdi...")
    app.run_polling()

if __name__ == '__main__': main()