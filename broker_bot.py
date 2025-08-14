
import asyncio
import aiosqlite
import aiohttp
import math
import os
import re
from datetime import datetime
from typing import List, Optional, Tuple

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
GEOCODE_UA = os.getenv("GEOCODE_UA", "tg-broker-bot-mvp/1.0 (contact: set-your-email@example.com)")

# ===== Utils =====
CATEGORY_CHOICES = [
    "Экскаватор", "Погрузчик", "Манипулятор", "Автокран",
    "Самосвал", "Бетономешалка", "Демонтажная бригада", "Отделочная бригада",
    "Арматурщики", "Сварщики", "Электрики", "Кровельщики",
]

PHONE_OR_LINK = re.compile(r"(\+?\d[\d\-\s]{6,}|@[\w_]{3,}|https?://\S+|t\.me/\S+)", re.I)

def mask_contacts(text: str) -> str:
    return PHONE_OR_LINK.sub("[[скрыто до согласования]]", text or "")

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    import math as _m
    phi1 = _m.radians(lat1); phi2 = _m.radians(lat2)
    dphi = _m.radians(lat2 - lat1)
    dlambda = _m.radians(lon2 - lon1)
    a = _m.sin(dphi/2)**2 + _m.cos(phi1)*_m.cos(phi2)*_m.sin(dlambda/2)**2
    return 2*R*_m.asin(_m.sqrt(a))

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def geocode_address(q: str) -> List[dict]:
    url = "https://nominatim.openstreetmap.org/search"
    params = {"format": "json", "q": q, "limit": "5", "addressdetails": "0"}
    headers = {"User-Agent": GEOCODE_UA}
    async with aiohttp.ClientSession(headers=headers) as sess:
        async with sess.get(url, params=params, timeout=15) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            out = []
            for it in data:
                try:
                    out.append({
                        "display_name": it.get("display_name", ""),
                        "lat": float(it["lat"]),
                        "lon": float(it["lon"]),
                    })
                except Exception:
                    continue
            return out

# ===== DB Layer (SQLite async) =====
DB_PATH = "broker.db"

CREATE_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_id INTEGER UNIQUE,
  username TEXT,
  first_name TEXT,
  last_name TEXT,
  role TEXT CHECK(role IN ('client','executor','admin')) DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS settings(
  id INTEGER PRIMARY KEY CHECK (id=1),
  prefer_owner_first INTEGER DEFAULT 1
);
INSERT OR IGNORE INTO settings(id, prefer_owner_first) VALUES(1,1);

CREATE TABLE IF NOT EXISTS executors(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,                    -- FK to users.id (nullable)
  pending_username TEXT,
  direct_tg_id INTEGER,               -- можно задать сразу числовой TG ID
  categories TEXT,
  city TEXT,
  lat REAL,
  lon REAL,
  radius_km REAL DEFAULT 50,
  is_owner INTEGER DEFAULT 0,
  is_active INTEGER DEFAULT 1,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_user_id INTEGER,
  category TEXT,
  description TEXT,
  address_text TEXT,
  city TEXT,
  lat REAL,
  lon REAL,
  client_radius_km REAL,
  mode TEXT,                          -- 'auction' | 'catalog'
  status TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS offers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER,
  executor_id INTEGER,
  rate_type TEXT,          -- 'час' | 'смена' | 'объект'
  rate_value REAL,
  comment TEXT,
  status TEXT,             -- 'active' | 'accepted' | 'rejected'
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS deals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER,
  offer_id INTEGER,
  contacts_released INTEGER DEFAULT 0,
  created_at TEXT
);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        # миграции "ленивые"
        # add direct_tg_id if missing
        cur = await db.execute("PRAGMA table_info(executors)")
        cols = [r[1] for r in await cur.fetchall()]
        if "direct_tg_id" not in cols:
            await db.execute("ALTER TABLE executors ADD COLUMN direct_tg_id INTEGER")
        # add address_text/mode if missing
        cur = await db.execute("PRAGMA table_info(requests)")
        cols = [r[1] for r in await cur.fetchall()]
        if "address_text" not in cols:
            await db.execute("ALTER TABLE requests ADD COLUMN address_text TEXT")
        if "mode" not in cols:
            await db.execute("ALTER TABLE requests ADD COLUMN mode TEXT")
        await db.commit()

async def get_or_create_user(tg, role: Optional[str]=None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, role FROM users WHERE tg_id=?", (tg.id,))
        row = await cur.fetchone()
        if row:
            uid, old_role = row
            if role and old_role != role and not is_admin(tg.id):
                await db.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
                await db.commit()
            return uid
        await db.execute(
            "INSERT INTO users(tg_id, username, first_name, last_name, role) VALUES(?,?,?,?,?)",
            (tg.id, tg.username, getattr(tg, "first_name", None), getattr(tg, "last_name", None), 'admin' if is_admin(tg.id) else role)
        )
        await db.commit()
        # auto-link executor by pending_username
        if tg.username:
            await db.execute(
                "UPDATE executors SET user_id=(SELECT id FROM users WHERE tg_id=?), pending_username=NULL "
                "WHERE pending_username=?", (tg.id, tg.username)
            )
            await db.commit()
        # auto-link by direct_tg_id
        await db.execute(
            "UPDATE executors SET user_id=(SELECT id FROM users WHERE tg_id=? ) "
            "WHERE direct_tg_id=?", (tg.id, tg.id)
        )
        await db.commit()
        cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg.id,))
        uid = (await cur.fetchone())[0]
        return uid

async def set_role(tg_id: int, role: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET role=? WHERE tg_id=?", (role, tg_id))
        await db.commit()

async def settings_get():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT prefer_owner_first FROM settings WHERE id=1")
        r = await cur.fetchone()
        return bool(r[0]) if r else True

async def settings_set_prefer_owner(v: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE settings SET prefer_owner_first=? WHERE id=1", (1 if v else 0,))
        await db.commit()

async def admin_add_executor(pending_username: Optional[str], city: str, radius_km: float,
                             categories: List[str], is_owner: bool, direct_tg_id: Optional[int]=None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO executors(user_id, pending_username, direct_tg_id, categories, city, lat, lon, radius_km, is_owner, is_active, created_at) "
            "VALUES(NULL,?,?,?,?,NULL,NULL,?, ?, 1, ?)",
            (pending_username, direct_tg_id, ",".join(categories), city, radius_km, 1 if is_owner else 0, datetime.utcnow().isoformat())
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        return (await cur.fetchone())[0]

async def admin_list_executors() -> List[Tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, user_id, pending_username, direct_tg_id, city, radius_km, categories, is_owner, is_active FROM executors ORDER BY id DESC"
        )
        return await cur.fetchall()

async def set_executor_location(exec_id: int, lat: float, lon: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE executors SET lat=?, lon=? WHERE id=?", (lat, lon, exec_id))
        await db.commit()

async def set_executor_active(exec_id: int, active: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE executors SET is_active=? WHERE id=?", (1 if active else 0, exec_id))
        await db.commit()

async def new_request(client_user_id: int, category: str, description: str,
                      address_text: str, city: str, lat: float, lon: float, radius_km: float, mode: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO requests(client_user_id, category, description, address_text, city, lat, lon, client_radius_km, mode, status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'published',?)",
            (client_user_id, category, description, address_text, city, lat, lon, radius_km, mode, datetime.utcnow().isoformat())
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        return (await cur.fetchone())[0]

async def get_request(request_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, client_user_id, category, description, address_text, city, lat, lon, client_radius_km, mode, status FROM requests WHERE id=?", (request_id,))
        return await cur.fetchone()

async def get_executor(exec_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, user_id, pending_username, direct_tg_id, categories, city, lat, lon, radius_km, is_owner, is_active FROM executors WHERE id=?", (exec_id,))
        return await cur.fetchone()

async def find_candidates(req_id: int) -> List[Tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT category, lat, lon, client_radius_km FROM requests WHERE id=?", (req_id,))
        r = await cur.fetchone()
        if not r: return []
        cat, rlat, rlon, rr = r
        cur = await db.execute(
            "SELECT id, user_id, pending_username, direct_tg_id, categories, city, lat, lon, radius_km, is_owner "
            "FROM executors WHERE is_active=1"
        )
        rows = await cur.fetchall()
    matches = []
    for row in rows:
        exec_id, user_id, pending_username, direct_tg_id, cats, city, elat, elon, eradius, is_owner = row
        if not cats: continue
        if cat not in [c.strip() for c in cats.split(",")]:
            continue
        if elat is None or elon is None:
            continue
        dist = haversine_km(rlat, rlon, elat, elon)
        if dist <= eradius and dist <= rr:
            matches.append((exec_id, user_id, pending_username, direct_tg_id, dist, is_owner, city))
    prefer_owner = await settings_get()
    matches.sort(key=lambda x: (0 if (prefer_owner and x[5]) else 1, x[4]))
    return matches

async def create_offer(request_id: int, executor_id: int, rate_type: str, rate_value: float, comment: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO offers(request_id, executor_id, rate_type, rate_value, comment, status, created_at) "
            "VALUES(?,?,?,?,?,'active',?)",
            (request_id, executor_id, rate_type, rate_value, comment, datetime.utcnow().isoformat())
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        return (await cur.fetchone())[0]

async def set_offer_status(offer_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE offers SET status=? WHERE id=?", (status, offer_id))
        await db.commit()

async def create_deal(request_id: int, offer_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO deals(request_id, offer_id, contacts_released, created_at) VALUES(?,?,0,?)",
            (request_id, offer_id, datetime.utcnow().isoformat())
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        return (await cur.fetchone())[0]

async def release_contacts(deal_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE deals SET contacts_released=1 WHERE id=?", (deal_id,))
        await db.commit()

async def tg_id_by_user_id(user_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None

async def username_by_user_id(user_id: Optional[int]) -> str:
    if not user_id: return ""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT username FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return (row[0] or "") if row else ""

async def send_to_executor(context: ContextTypes.DEFAULT_TYPE, ex_row, text: str, reply_markup=None) -> bool:
    ex_id, user_id, pending_username, direct_tg_id, *_ = ex_row
    chat_id = None
    if user_id:
        chat_id = await tg_id_by_user_id(user_id)
    if not chat_id and direct_tg_id:
        chat_id = direct_tg_id
    if not chat_id:
        return False
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return True
    except Exception:
        return False

# ===== Conversations =====
ROLE_SEL, MODE_SEL, CAT_SEL, DESC_IN, ADDR_IN, GEO_PICK, RAD_IN = range(7)
OFFER_RATE_TYPE, OFFER_RATE_VALUE, OFFER_COMMENT = 7, 8, 9

# --- Start / Roles
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await get_or_create_user(update.effective_user)
    kb = [["Я заказчик", "Я исполнитель"]]
    if is_admin(update.effective_user.id):
        kb[0].append("Админ")
    await update.message.reply_text(
        "Привет! Я — посредник по стройуслугам. Кем вы будете пользоваться?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True)
    )
    return ROLE_SEL

async def role_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Я заказчик":
        await set_role(update.effective_user.id, "client")
        await update.message.reply_text("Окей. Команда: /new_request — создать заявку.\nТакже доступен поиск: /catalog",
                                        reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    elif text == "Я исполнитель":
        await set_role(update.effective_user.id, "executor")
        await update.message.reply_text(
            "Вы — исполнитель. Админ добавит вас по @username или по числовому ID.\n"
            "Важно: Telegram разрешает получать сообщения от бота только после вашего первого /start.\n"
            "Проверьте статус: /me", reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    elif text == "Админ" and is_admin(update.effective_user.id):
        await set_role(update.effective_user.id, "admin")
        await update.message.reply_text(
            "Режим админа. Команды:\n"
            "/admin prefer_owner on|off\n"
            "/admin add_executor @username \"Город\" 50 \"кат1,кат2\" [--owner]\n"
            "/admin add_exec_id 123456789 \"Город\" 50 \"кат1,кат2\" [--owner]\n"
            "/admin list_exec\n"
            "/admin set_loc <exec_id> (ответьте геолокацией)\n"
            "/admin assign <request_id> <executor_id>",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    else:
        await update.message.reply_text("Выберите кнопку.")
        return ROLE_SEL

# --- Client: new request (mode -> cat -> desc -> address -> geocode -> radius)
async def cmd_new_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["Аукцион", "Каталог"]]
    await update.message.reply_text("Выберите режим:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return MODE_SEL

async def mode_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    if val not in ("Аукцион", "Каталог"):
        await update.message.reply_text("Выберите кнопку «Аукцион» или «Каталог».")
        return MODE_SEL
    context.user_data["req_mode"] = "auction" if val == "Аукцион" else "catalog"
    kb = [CATEGORY_CHOICES[i:i+3] for i in range(0, len(CATEGORY_CHOICES), 3)]
    await update.message.reply_text("Выберите категорию:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return CAT_SEL

async def cat_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text
    if cat not in CATEGORY_CHOICES:
        await update.message.reply_text("Выберите категорию из списка.")
        return CAT_SEL
    context.user_data["req_cat"] = cat
    await update.message.reply_text("Опишите ТЗ (без контактов):", reply_markup=ReplyKeyboardRemove())
    return DESC_IN

async def desc_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["req_desc"] = mask_contacts(update.message.text)
    await update.message.reply_text("Укажите адрес (город, улица, номер; можно ориентиры). Я найду координаты.")
    return ADDR_IN

async def addr_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = update.message.text.strip()
    context.user_data["req_addr"] = addr
    await update.message.reply_text("Ищу адрес…")
    results = await geocode_address(addr)
    if not results:
        await update.message.reply_text("Не нашёл адрес. Попробуйте сформулировать по-другому.")
        return ADDR_IN
    context.user_data["geocode_results"] = results
    buttons = [[InlineKeyboardButton(r["display_name"], callback_data=f"geo_pick:{i}")] for i, r in enumerate(results)]
    await update.message.reply_text("Выберите подходящий вариант:", reply_markup=InlineKeyboardMarkup(buttons))
    return GEO_PICK

async def on_geo_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, idx = q.data.split(":")
    idx = int(idx)
    results = context.user_data.get("geocode_results", [])
    if not results or idx < 0 or idx >= len(results):
        await q.message.reply_text("Выбор недействителен. Введите адрес заново:")
        return ADDR_IN
    sel = results[idx]
    context.user_data["req_lat"] = sel["lat"]
    context.user_data["req_lon"] = sel["lon"]
    context.user_data["req_addr_resolved"] = sel["display_name"]
    await q.message.reply_text("Укажите радиус поиска исполнителей, км (например 50):")
    return RAD_IN

async def radius_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        r = float(update.message.text.replace(",", "."))
        if r <= 0 or r > 1000: raise ValueError
    except:
        await update.message.reply_text("Введите число км (1..1000).")
        return RAD_IN
    context.user_data["req_radius"] = r
    client_uid = await get_or_create_user(update.effective_user, role="client")
    req_id = await new_request(
        client_user_id=client_uid,
        category=context.user_data["req_cat"],
        description=context.user_data["req_desc"],
        address_text=context.user_data.get("req_addr_resolved") or context.user_data.get("req_addr") or "",
        city="",
        lat=context.user_data["req_lat"], lon=context.user_data["req_lon"],
        radius_km=context.user_data["req_radius"],
        mode=context.user_data.get("req_mode","auction")
    )
    mode = context.user_data.get("req_mode","auction")
    if mode == "auction":
        await update.message.reply_text(f"Заявка #{req_id} создана. Ищу исполнителей…")
        candidates = await find_candidates(req_id)
        if not candidates:
            await update.message.reply_text("Подходящих исполнителей не найдено. Админ будет уведомлён.")
        else:
            for row in candidates:
                exid, user_id, pun, direct_tg_id, dist, is_owner, city = row
                text = (
                    f"Новая заявка #{req_id}\n"
                    f"Категория: {context.user_data['req_cat']}\n"
                    f"Адрес: {context.user_data.get('req_addr_resolved','')}\n"
                    f"Описание: {context.user_data['req_desc']}\n"
                    f"Дистанция до объекта: ~{dist:.1f} км\n\n"
                    "Отправьте предложение:"
                )
                kb = InlineKeyboardMarkup.from_button(
                    InlineKeyboardButton(f"Откликнуться на #{req_id}", callback_data=f"offer:{req_id}:{exid}")
                )
                try:
                    await send_to_executor(context, (exid, user_id, pun, direct_tg_id), text, kb)
                except Exception:
                    pass
            await update.message.reply_text("Заявка разослана. Как только поступят офферы — я пришлю.")
        return ConversationHandler.END
    else:
        # catalog: показать список подходящих исполнителей клиенту
        candidates = await find_candidates(req_id)
        if not candidates:
            await update.message.reply_text("Исполнителей в радиусе не найдено.")
            return ConversationHandler.END
        lines = ["Нашёл исполнителей (сначала свои, затем по расстоянию):"]
        buttons = []
        for exid, user_id, pun, direct_tg_id, dist, is_owner, city in candidates[:20]:
            lines.append(f"E-{exid:05d} | {city or '—'} | ~{dist:.1f} км | {'СВОЙ' if is_owner else 'подряд'}")
            buttons.append([InlineKeyboardButton(f"Запросить оффер у E-{exid:05d}", callback_data=f"req_offer:{req_id}:{exid}")])
        await update.message.reply_text("\n".join(lines))
        await update.message.reply_text("Выберите исполнителя, чтобы запросить оффер:", reply_markup=InlineKeyboardMarkup(buttons))
        return ConversationHandler.END

# --- Catalog callbacks: request offer from a specific executor
async def on_request_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, sreq, sexec = q.data.split(":")
    req_id = int(sreq); exid = int(sexec)
    req = await get_request(req_id)
    ex = await get_executor(exid)
    if not req or not ex:
        await q.message.reply_text("Не удалось отправить запрос — проверьте наличие заявки/исполнителя.")
        return
    _, _, category, desc, addr, city, lat, lon, crad, mode, status = req
    text = (
        f"Запрос оффера по заявке #{req_id}\n"
        f"Категория: {category}\n"
        f"Адрес: {addr or '—'}\n"
        f"Описание: {desc}\n\n"
        "Отправьте предложение:"
    )
    kb = InlineKeyboardMarkup.from_button(
        InlineKeyboardButton(f"Откликнуться на #{req_id}", callback_data=f"offer:{req_id}:{exid}")
    )
    ok = await send_to_executor(context, ex, text, kb)
    if ok:
        await q.message.reply_text(f"Запрос оффера отправлен исполнителю E-{exid:05d}.")
    else:
        await q.message.reply_text("Не удалось доставить запрос. Исполнитель мог не запускать бота.")

# --- Executor: offer flow
async def on_offer_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    if len(parts) != 3: return
    _, req_id, exec_id = parts
    context.user_data["offer_req_id"] = int(req_id)
    context.user_data["offer_exec_id"] = int(exec_id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Ставка за час", callback_data="rt:час")],
        [InlineKeyboardButton("Ставка за смену", callback_data="rt:смена")],
        [InlineKeyboardButton("Фикс за объект", callback_data="rt:объект")]
    ])
    await q.message.reply_text(f"Оффер для заявки #{req_id}. Выберите тип ставки:", reply_markup=kb)
    return OFFER_RATE_TYPE

async def on_rate_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    rt = q.data.split(":",1)[1]
    context.user_data["rate_type"] = rt
    await q.message.reply_text("Введите числовое значение ставки (пример: 50.0):")
    return OFFER_RATE_VALUE

async def on_rate_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(update.message.text.replace(",", "."))
    except:
        await update.message.reply_text("Нужно число. Попробуйте ещё раз:")
        return OFFER_RATE_VALUE
    context.user_data["rate_value"] = val
    await update.message.reply_text("Комментарий к офферу (опционально, без контактов):")
    return OFFER_COMMENT

async def on_offer_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = mask_contacts(update.message.text or "")
    rid = context.user_data["offer_req_id"]
    exid = context.user_data["offer_exec_id"]
    rt = context.user_data["rate_type"]
    rv = context.user_data["rate_value"]
    offer_id = await create_offer(rid, exid, rt, rv, comment)
    req = await get_request(rid)
    if req:
        _, client_user_id, category, desc, addr, city, lat, lon, crad, mode, status = req
        client_tg = await tg_id_by_user_id(client_user_id)
        if client_tg:
            kb = InlineKeyboardMarkup.from_button(
                InlineKeyboardButton("Принять оффер", callback_data=f"accept_offer:{offer_id}")
            )
            await context.bot.send_message(
                chat_id=client_tg,
                text=(
                    f"Новый оффер по заявке #{rid}\n"
                    f"Тип ставки: {rt}\nСтавка: {rv}\nКомментарий: {comment or '—'}\n"
                    f"Исполнитель: E-{exid:05d} (скрыто)\n\n"
                    "Если вас устраивает — нажмите «Принять оффер». Контакты откроются."
                ),
                reply_markup=kb
            )
    await update.message.reply_text("Оффер отправлен заказчику.")
    return ConversationHandler.END

# --- Accept offer -> reveal contacts
async def on_accept_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, sid = q.data.split(":")
    offer_id = int(sid)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT o.request_id, o.executor_id, e.user_id, e.direct_tg_id FROM offers o "
            "LEFT JOIN executors e ON e.id=o.executor_id WHERE o.id=?", (offer_id,)
        )
        row = await cur.fetchone()
    if not row:
        await q.message.reply_text("Оффер не найден.")
        return
    request_id, exec_id, exec_user_id, direct_tg_id = row
    await set_offer_status(offer_id, "accepted")
    deal_id = await create_deal(request_id, offer_id)
    await release_contacts(deal_id)
    contact = ""
    if exec_user_id:
        uname = await username_by_user_id(exec_user_id)
        if uname: contact = f"@{uname}"
    elif direct_tg_id:
        contact = f"tg://user?id={direct_tg_id}"
    await q.message.reply_text(f"Оффер принят. Сделка #{deal_id}.\nКонтакты исполнителя: {contact or 'появятся после /start'}")
    req = await get_request(request_id)
    if req:
        _, client_user_id, *_ = req
        client_tg = await tg_id_by_user_id(client_user_id)
        if client_tg:
            try:
                if exec_user_id:
                    ex_tg = await tg_id_by_user_id(exec_user_id)
                else:
                    ex_tg = direct_tg_id
                if ex_tg:
                    uname_client = await username_by_user_id(client_user_id)
                    await context.bot.send_message(ex_tg, f"Ваш оффер принят по заявке #{request_id}. Контакты клиента: @{uname_client or ''}")
            except Exception:
                pass

# --- /me (исполнитель)
async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = await get_or_create_user(update.effective_user)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, categories, city, radius_km, is_owner, is_active FROM executors WHERE user_id=?", (uid,))
        row = await cur.fetchone()
    if not row:
        await update.message.reply_text("Вы ещё не добавлены админом как исполнитель. Попросите админа: /admin add_exec_id … или add_executor …")
        return
    ex_id, cats, city, rad, is_owner, is_active = row
    await update.message.reply_text(
        f"Профиль исполнителя E-{ex_id:05d}\n"
        f"Город: {city or '—'} | Радиус: {rad} км | Категории: {cats}\n"
        f"{'СВОЙ ПАРК' if is_owner else 'подрядчик'} | {'Активен' if is_active else 'Неактивен'}"
    )

# --- Admin commands
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Использование:\n"
            "/admin prefer_owner on|off\n"
            "/admin add_executor @username \"Город\" 50 \"кат1,кат2\" [--owner]\n"
            "/admin add_exec_id 123456789 \"Город\" 50 \"кат1,кат2\" [--owner]\n"
            "/admin list_exec\n"
            "/admin set_loc <exec_id> (ответьте геолокацией)\n"
            "/admin assign <request_id> <executor_id>"
        )
        return
    sub = args[0]
    if sub == "prefer_owner" and len(args)>=2:
        v = args[1].lower() in ("on","1","true","yes")
        await settings_set_prefer_owner(v)
        await update.message.reply_text(f"prefer_owner_first = {v}")
    elif sub == "add_executor":
        try:
            text = update.message.text
            m = re.search(r'add_executor\s+(@\w+)\s+"([^"]+)"\s+([\d\.]+)\s+"([^"]+)"(\s+--owner)?', text)
            if not m:
                raise ValueError
            uname, city, radius, cats, owner_flag = m.groups()
            exec_id = await admin_add_executor(
                pending_username=uname.lstrip("@"),
                city=city, radius_km=float(radius),
                categories=[c.strip() for c in cats.split(",") if c.strip()],
                is_owner=bool(owner_flag),
                direct_tg_id=None
            )
            await update.message.reply_text(f"Исполнитель добавлен E-{exec_id:05d}. До первого /start будет висеть по @{uname}.")
        except Exception:
            await update.message.reply_text('Формат: /admin add_executor @username "Город" 50 "кат1,кат2" [--owner]')
    elif sub == "add_exec_id":
        try:
            text = update.message.text
            m = re.search(r'add_exec_id\s+(\d+)\s+"([^"]+)"\s+([\d\.]+)\s+"([^"]+)"(\s+--owner)?', text)
            if not m:
                raise ValueError
            tgid, city, radius, cats, owner_flag = m.groups()
            exec_id = await admin_add_executor(
                pending_username=None,
                city=city, radius_km=float(radius),
                categories=[c.strip() for c in cats.split(",") if c.strip()],
                is_owner=bool(owner_flag),
                direct_tg_id=int(tgid)
            )
            await update.message.reply_text(f"Исполнитель добавлен E-{exec_id:05d} (tg_id={tgid}). Напомните ему запустить бота.")
        except Exception:
            await update.message.reply_text('Формат: /admin add_exec_id 123456789 "Город" 50 "кат1,кат2" [--owner]')
    elif sub == "list_exec":
        rows = await admin_list_executors()
        if not rows:
            await update.message.reply_text("Исполнителей нет.")
            return
        lines = []
        for (eid, uid, pun, tgid, city, rad, cats, owner, active) in rows:
            lines.append(f"E-{eid:05d} | @{pun or '-'} | tg_id={tgid or '-'} | user_id={uid or '-'} | {city or '-'} | {rad}км | [{cats}] | "
                         f"{'СВОЙ' if owner else 'подряд'} | {'ON' if active else 'OFF'}")
        await update.message.reply_text("\n".join(lines)[:4000])
    elif sub == "set_loc" and len(args)>=2:
        context.user_data["await_loc_for_exec"] = int(args[1])
        await update.message.reply_text("Окей. Отправьте геолокацию сообщением-ответом.")
    elif sub == "assign" and len(args)>=3:
        rid = int(args[1]); exid = int(args[2])
        req = await get_request(rid)
        ex = await get_executor(exid)
        if not req or not ex:
            await update.message.reply_text("Проверьте request_id и executor_id.")
            return
        _, _, category, desc, addr, city, lat, lon, crad, mode, status = req
        text = (
            f"[Админ-назначение] Заявка #{rid}\n"
            f"Категория: {category}\nАдрес: {addr or '—'}\nОписание: {desc}\n"
            "Отправьте предложение:"
        )
        kb = InlineKeyboardMarkup.from_button(
            InlineKeyboardButton(f"Откликнуться на #{rid}", callback_data=f"offer:{rid}:{exid}")
        )
        ok = await send_to_executor(context, ex, text, kb)
        await update.message.reply_text("Назначено." if ok else "Не удалось отправить (возможно, исполнитель не запускал бота).")
    else:
        await update.message.reply_text("Не понял подкоманду. Напишите /admin без аргументов для помощи.")

async def on_location_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    exid = context.user_data.get("await_loc_for_exec")
    if not exid:
        return
    if not update.message.location:
        await update.message.reply_text("Нужна геолокация.")
        return
    await set_executor_location(exid, update.message.location.latitude, update.message.location.longitude)
    context.user_data.pop("await_loc_for_exec", None)
    await update.message.reply_text(f"Локация исполнителя E-{exid:05d} обновлена.")

def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    start_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ROLE_SEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, role_selected)]
        },
        fallbacks=[]
    )
    req_conv = ConversationHandler(
        entry_points=[CommandHandler("new_request", cmd_new_request)],
        states={
            MODE_SEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, mode_selected)],
            CAT_SEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, cat_selected)],
            DESC_IN: [MessageHandler(filters.TEXT & ~filters.COMMAND, desc_input)],
            ADDR_IN: [MessageHandler(filters.TEXT & ~filters.COMMAND, addr_input)],
            GEO_PICK: [CallbackQueryHandler(on_geo_pick, pattern=r"^geo_pick:\d+$")],
            RAD_IN: [MessageHandler(filters.TEXT & ~filters.COMMAND, radius_input)]
        },
        fallbacks=[]
    )

    offer_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_offer_click, pattern=r"^offer:\d+:\d+$")],
        states={
            OFFER_RATE_TYPE: [CallbackQueryHandler(on_rate_type, pattern=r"^rt:(час|смена|объект)$")],
            OFFER_RATE_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_rate_value)],
            OFFER_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_offer_comment)],
        },
        fallbacks=[]
    )

    app.add_handler(start_conv)
    app.add_handler(req_conv)
    app.add_handler(offer_conv)
    app.add_handler(CallbackQueryHandler(on_request_offer, pattern=r"^req_offer:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_accept_offer, pattern=r"^accept_offer:\d+$"))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(MessageHandler(filters.LOCATION & filters.REPLY, on_location_reply))
    return app

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in environment (BOT_TOKEN)")
    # Ensure event loop exists (Python 3.13 on Render)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(db_init())
    application = build_app()
    print("Bot is running (polling). Press Ctrl+C to stop.")
    application.run_polling()
