import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, PreCheckoutQuery
from aiogram.fsm.storage.memory import MemoryStorage
import sqlite3
import json

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = "YOUR_BOT_TOKEN"  # Получить у @BotFather
ADMIN_IDS = [123456789]  # Ваш Telegram ID

# AI API (бесплатные)
AI_PROVIDERS = {
    "gpt4": {"url": "https://text.pollinations.ai/", "model": "openai"},
    "deepseek": {"url": "https://text.pollinations.ai/", "model": "deepseek"},
    "claude": {"url": "https://text.pollinations.ai/", "model": "claude-hybridspace"},
    "llama": {"url": "https://text.pollinations.ai/", "model": "llama"},
    "mistral": {"url": "https://text.pollinations.ai/", "model": "mistral"},
}

# Тарифы подписок
SUBSCRIPTION_PLANS = {
    "basic": {"name": "Базовый", "price": 99, "days": 7, "requests": 100, "stars": 50},
    "pro": {"name": "Про", "price": 299, "days": 30, "requests": 1000, "stars": 150},
    "unlimited": {"name": "Безлимит", "price": 699, "days": 30, "requests": -1, "stars": 350},
}

FREE_REQUESTS_PER_DAY = 5  # Бесплатных запросов в день

# Платёжные системы
YOOKASSA_SHOP_ID = "YOUR_SHOP_ID"
YOOKASSA_SECRET = "YOUR_SECRET_KEY"
TELEGRAM_PAYMENT_TOKEN = "YOUR_PAYMENT_TOKEN"  # Для Telegram Stars

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        registered_at TEXT,
        subscription_type TEXT DEFAULT 'free',
        subscription_end TEXT,
        requests_left INTEGER DEFAULT 5,
        requests_today INTEGER DEFAULT 0,
        last_request_date TEXT,
        total_requests INTEGER DEFAULT 0,
        ai_model TEXT DEFAULT 'gpt4',
        balance INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        currency TEXT,
        plan TEXT,
        status TEXT,
        payment_id TEXT,
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        role TEXT,
        content TEXT,
        created_at TEXT
    )''')
    conn.commit()
    conn.close()

class Database:
    def __init__(self):
        self.conn = sqlite3.connect('bot_database.db', check_same_thread=False)
        
    def get_user(self, user_id: int) -> Optional[dict]:
        c = self.conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if row:
            return {
                "user_id": row[0], "username": row[1], "first_name": row[2],
                "registered_at": row[3], "subscription_type": row[4],
                "subscription_end": row[5], "requests_left": row[6],
                "requests_today": row[7], "last_request_date": row[8],
                "total_requests": row[9], "ai_model": row[10], "balance": row[11]
            }
        return None
    
    def create_user(self, user_id: int, username: str, first_name: str):
        c = self.conn.cursor()
        c.execute("""INSERT OR IGNORE INTO users 
            (user_id, username, first_name, registered_at, last_request_date)
            VALUES (?, ?, ?, ?, ?)""",
            (user_id, username, first_name, datetime.now().isoformat(), datetime.now().date().isoformat()))
        self.conn.commit()
        
    def update_subscription(self, user_id: int, plan: str):
        plan_info = SUBSCRIPTION_PLANS[plan]
        end_date = datetime.now() + timedelta(days=plan_info["days"])
        requests = plan_info["requests"]
        c = self.conn.cursor()
        c.execute("""UPDATE users SET 
            subscription_type = ?, subscription_end = ?, requests_left = ?
            WHERE user_id = ?""",
            (plan, end_date.isoformat(), requests, user_id))
        self.conn.commit()
        
    def check_and_reset_daily(self, user_id: int):
        user = self.get_user(user_id)
        if not user:
            return
        today = datetime.now().date().isoformat()
        if user["last_request_date"] != today:
            c = self.conn.cursor()
            c.execute("""UPDATE users SET 
                requests_today = 0, last_request_date = ?
                WHERE user_id = ?""", (today, user_id))
            self.conn.commit()
            
    def use_request(self, user_id: int) -> bool:
        self.check_and_reset_daily(user_id)
        user = self.get_user(user_id)
        if not user:
            return False
            
        # Проверка подписки
        if user["subscription_type"] != "free":
            if user["subscription_end"]:
                end = datetime.fromisoformat(user["subscription_end"])
                if end < datetime.now():
                    # Подписка истекла
                    c = self.conn.cursor()
                    c.execute("UPDATE users SET subscription_type = 'free' WHERE user_id = ?", (user_id,))
                    self.conn.commit()
                    user["subscription_type"] = "free"
                    
        # Безлимитный тариф
        if user["requests_left"] == -1:
            c = self.conn.cursor()
            c.execute("UPDATE users SET total_requests = total_requests + 1 WHERE user_id = ?", (user_id,))
            self.conn.commit()
            return True
            
        # Платная подписка с лимитом
        if user["subscription_type"] != "free" and user["requests_left"] > 0:
            c = self.conn.cursor()
            c.execute("""UPDATE users SET 
                requests_left = requests_left - 1, total_requests = total_requests + 1
                WHERE user_id = ?""", (user_id,))
            self.conn.commit()
            return True
            
        # Бесплатные запросы
        if user["requests_today"] < FREE_REQUESTS_PER_DAY:
            c = self.conn.cursor()
            c.execute("""UPDATE users SET 
                requests_today = requests_today + 1, total_requests = total_requests + 1
                WHERE user_id = ?""", (user_id,))
            self.conn.commit()
            return True
            
        return False
    
    def get_chat_history(self, user_id: int, limit: int = 10) -> list:
        c = self.conn.cursor()
        c.execute("""SELECT role, content FROM messages 
            WHERE user_id = ? ORDER BY id DESC LIMIT ?""", (user_id, limit))
        rows = c.fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    
    def add_message(self, user_id: int, role: str, content: str):
        c = self.conn.cursor()
        c.execute("""INSERT INTO messages (user_id, role, content, created_at)
            VALUES (?, ?, ?, ?)""", (user_id, role, content, datetime.now().isoformat()))
        self.conn.commit()
        
    def set_ai_model(self, user_id: int, model: str):
        c = self.conn.cursor()
        c.execute("UPDATE users SET ai_model = ? WHERE user_id = ?", (model, user_id))
        self.conn.commit()
        
    def add_payment(self, user_id: int, amount: float, currency: str, plan: str, payment_id: str):
        c = self.conn.cursor()
        c.execute("""INSERT INTO payments (user_id, amount, currency, plan, status, payment_id, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            (user_id, amount, currency, plan, payment_id, datetime.now().isoformat()))
        self.conn.commit()
        
    def get_stats(self) -> dict:
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        total_users = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users WHERE subscription_type != 'free'")
        paid_users = c.fetchone()[0]
        c.execute("SELECT SUM(total_requests) FROM users")
        total_requests = c.fetchone()[0] or 0
        c.execute("SELECT SUM(amount) FROM payments WHERE status = 'completed'")
        total_revenue = c.fetchone()[0] or 0
        return {
            "total_users": total_users,
            "paid_users": paid_users,
            "total_requests": total_requests,
            "total_revenue": total_revenue
        }

# ==================== AI ФУНКЦИИ ====================
async def get_ai_response(user_id: int, message: str, db: Database) -> str:
    user = db.get_user(user_id)
    model = user["ai_model"] if user else "gpt4"
    provider = AI_PROVIDERS.get(model, AI_PROVIDERS["gpt4"])
    
    # Получаем историю чата
    history = db.get_chat_history(user_id)
    history.append({"role": "user", "content": message})
    
    messages = [
        {"role": "system", "content": "Ты полезный AI-ассистент. Отвечай на русском языке, будь дружелюбным и информативным."},
        *history
    ]
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                provider["url"],
                json={"messages": messages, "model": provider["model"]},
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status == 200:
                    response_text = await resp.text()
                    db.add_message(user_id, "user", message)
                    db.add_message(user_id, "assistant", response_text)
                    return response_text
                else:
                    return f"❌ Ошибка API: {resp.status}"
    except Exception as e:
        return f"❌ Ошибка: {str(e)}"

# ==================== БОТ ====================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
db = Database()

# Клавиатуры
def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Выбрать AI", callback_data="select_ai"),
         InlineKeyboardButton(text="💎 Подписка", callback_data="subscription")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
         InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ])

def get_ai_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧠 GPT-4o", callback_data="ai_gpt4"),
         InlineKeyboardButton(text="🌊 DeepSeek", callback_data="ai_deepseek")],
        [InlineKeyboardButton(text="🎭 Claude", callback_data="ai_claude"),
         InlineKeyboardButton(text="🦙 Llama", callback_data="ai_llama")],
        [InlineKeyboardButton(text="🌀 Mistral", callback_data="ai_mistral")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")]
    ])

def get_subscription_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Базовый — {SUBSCRIPTION_PLANS['basic']['price']}₽", callback_data="buy_basic")],
        [InlineKeyboardButton(text=f"🚀 Про — {SUBSCRIPTION_PLANS['pro']['price']}₽", callback_data="buy_pro")],
        [InlineKeyboardButton(text=f"💎 Безлимит — {SUBSCRIPTION_PLANS['unlimited']['price']}₽", callback_data="buy_unlimited")],
        [InlineKeyboardButton(text="⭐ Оплата Stars", callback_data="pay_stars")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")]
    ])

def get_payment_keyboard(plan: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 ЮKassa", callback_data=f"pay_yookassa_{plan}")],
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data=f"pay_stars_{plan}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="subscription")]
    ])

# Хендлеры
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user
    db.create_user(user.id, user.username, user.first_name)
    
    await message.answer(
        f"👋 Привет, {user.first_name}!\n\n"
        f"Я — AI-ассистент с доступом к лучшим нейросетям:\n"
        f"• 🧠 GPT-4o\n• 🌊 DeepSeek\n• 🎭 Claude\n• 🦙 Llama\n• 🌀 Mistral\n\n"
        f"📨 Просто напишите сообщение и я отвечу!\n\n"
        f"🆓 Бесплатно: {FREE_REQUESTS_PER_DAY} запросов в день\n"
        f"💎 Подписка: безлимитный доступ",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

@dp.callback_query(F.data == "select_ai")
async def select_ai(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    current = user["ai_model"] if user else "gpt4"
    await callback.message.edit_text(
        f"🤖 Выберите AI модель\n\nТекущая: {current}",
        parse_mode="HTML",
        reply_markup=get_ai_keyboard()
    )

@dp.callback_query(F.data.startswith("ai_"))
async def set_ai(callback: types.CallbackQuery):
    model = callback.data.replace("ai_", "")
    db.set_ai_model(callback.from_user.id, model)
    await callback.answer(f"✅ Модель изменена на {model}!")
    await callback.message.edit_text(
        f"✅ AI модель изменена на {model}!",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

@dp.callback_query(F.data == "subscription")
async def show_subscription(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "💎 Тарифные планы\n\n"
        f"⭐ Базовый — {SUBSCRIPTION_PLANS['basic']['price']}₽/неделя\n"
        f"   └ {SUBSCRIPTION_PLANS['basic']['requests']} запросов\n\n"
        f"🚀 Про — {SUBSCRIPTION_PLANS['pro']['price']}₽/месяц\n"
        f"   └ {SUBSCRIPTION_PLANS['pro']['requests']} запросов\n\n"
        f"💎 Безлимит — {SUBSCRIPTION_PLANS['unlimited']['price']}₽/месяц\n"
        f"   └ Неограниченно запросов\n\n"
        "Выберите тариф для оплаты:",
        parse_mode="HTML",
        reply_markup=get_subscription_keyboard()
    )

@dp.callback_query(F.data.startswith("buy_"))
async def buy_plan(callback: types.CallbackQuery):
    plan = callback.data.replace("buy_", "")
    await callback.message.edit_text(
        f"💳 Оплата тарифа «{SUBSCRIPTION_PLANS[plan]['name']}»\n\n"
        f"Сумма: {SUBSCRIPTION_PLANS[plan]['price']}₽\n\n"
        "Выберите способ оплаты:",
        parse_mode="HTML",
        reply_markup=get_payment_keyboard(plan)
    )

@dp.callback_query(F.data.startswith("pay_stars_"))
async def pay_with_stars(callback: types.CallbackQuery):
    plan = callback.data.replace("pay_stars_", "")
    if plan not in SUBSCRIPTION_PLANS:
        await callback.answer("❌ Тариф не найден")
        return
        
    plan_info = SUBSCRIPTION_PLANS[plan]
    prices = [LabeledPrice(label=f"Подписка «{plan_info['name']}»", amount=plan_info["stars"])]
    
    await callback.message.answer_invoice(
        title=f"Подписка «{plan_info['name']}»",
        description=f"Доступ к AI на {plan_info['days']} дней. {plan_info['requests']} запросов." if plan_info['requests'] > 0 else f"Безлимитный доступ на {plan_info['days']} дней.",
        payload=f"sub_{plan}_{callback.from_user.id}",
        currency="XTR",  # Telegram Stars
        prices=prices
    )
    await callback.answer()

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payment = message.successful_payment
    payload = payment.invoice_payload
    
    if payload.startswith("sub_"):
        parts = payload.split("_")
        plan = parts[1]
        user_id = int(parts[2])
        
        db.update_subscription(user_id, plan)
        db.add_payment(user_id, payment.total_amount, "XTR", plan, payment.telegram_payment_charge_id)
        
        await message.answer(
            f"🎉 Оплата прошла успешно!\n\n"
            f"Подписка «{SUBSCRIPTION_PLANS[plan]['name']}» активирована!\n"
            f"Приятного использования! 🚀",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )

@dp.callback_query(F.data == "profile")
async def show_profile(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Профиль не найден")
        return
        
    sub_text = "Бесплатный" if user["subscription_type"] == "free" else f"{SUBSCRIPTION_PLANS.get(user['subscription_type'], {}).get('name', 'Неизвестный')}"
    requests_text = f"{FREE_REQUESTS_PER_DAY - user['requests_today']}/{FREE_REQUESTS_PER_DAY}" if user["subscription_type"] == "free" else ("∞" if user["requests_left"] == -1 else str(user["requests_left"]))
    
    await callback.message.edit_text(
        f"👤 Ваш профиль\n\n"
        f"🆔 ID: {user['user_id']}\n"
        f"📅 Регистрация: {user['registered_at'][:10]}\n\n"
        f"💎 Подписка: {sub_text}\n"
        f"🤖 AI модель: {user['ai_model']}\n"
        f"📊 Запросов осталось: {requests_text}\n"
        f"📈 Всего запросов: {user['total_requests']}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")]
        ])
    )

@dp.callback_query(F.data == "help")
async def show_help(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "❓ Помощь\n\n"
        "🔹 Просто напишите сообщение — AI ответит\n"
        "🔹 /clear — очистить историю чата\n"
        "🔹 /model — выбрать AI модель\n"
        "🔹 /sub — информация о подписке\n\n"
        "💡 Совет: AI запоминает контекст беседы!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")]
        ])
    )

@dp.callback_query(F.data == "back_main")
async def back_to_main(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🏠 Главное меню\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    c = db.conn.cursor()
    c.execute("DELETE FROM messages WHERE user_id = ?", (message.from_user.id,))
    db.conn.commit()
    await message.answer("🗑 История чата очищена!")

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
        
    stats = db.get_stats()
    await message.answer(
        f"📊 Админ-панель\n\n"
        f"👥 Пользователей: {stats['total_users']}\n"
        f"💎 Платных: {stats['paid_users']}\n"
        f"📨 Запросов: {stats['total_requests']}\n"
        f"💰 Доход: {stats['total_revenue']}₽",
        parse_mode="HTML"
    )

# Обработка сообщений
@dp.message(F.text)
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    db.create_user(user_id, message.from_user.username, message.from_user.first_name)
    
    # Проверяем лимиты
    if not db.use_request(user_id):
        user = db.get_user(user_id)
        await message.answer(
            "⚠️ Лимит запросов исчерпан!\n\n"
            f"Бесплатных запросов: {FREE_REQUESTS_PER_DAY} в день\n\n"
            "💎 Оформите подписку для безлимитного доступа:",
            parse_mode="HTML",
            reply_markup=get_subscription_keyboard()
        )
        return
    
    # Отправляем "печатает..."
    await bot.send_chat_action(message.chat.id, "typing")
    
    # Получаем ответ от AI
    response = await get_ai_response(user_id, message.text, db)
    
    # Отправляем ответ (разбиваем на части если длинный)
    if len(response) > 4096:
        for i in range(0, len(response), 4096):
            await message.answer(response[i:i+4096])
    else:
        await message.answer(response)

# Запуск
async def main():
    init_db()
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())