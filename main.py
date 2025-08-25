import os
import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# ===================== SIMPLE, INLINE-FIRST MVP =====================
# Особенности для аудитории «50+»:
# • Все действия через понятные ИНЛАЙН-кнопки.
# • Дата — выбор из ближайших дней + «Сегодня/Завтра», время — 3 слота.
# • Адрес — текстом (улица, дом). Геометку можно прислать «скрепкой», но это необязательно.
# • «Позвонить» — карточка контакта; «Оставить номер» — написать цифрами.
# • Короткие сообщения.

# --------------------- Config & Globals ---------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher(storage=MemoryStorage())

SUPPORT_PHONE = os.getenv("SUPPORT_PHONE", "+375290000000")
SUPPORT_NAME = os.getenv("SUPPORT_NAME", "Диспетчер")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
PHONE_SHARE_RATE_LIMIT = int(os.getenv("PHONE_SHARE_RATE_LIMIT", "300"))  # сек
COMMISSION_PCT = float(os.getenv("COMMISSION_PCT", "10")) / 100.0

# --------------------- Data Models ---------------------

def mention(user_id: int, username: Optional[str], full_name: str) -> str:
    return f"@{username}" if username else f"[{full_name}](tg://user?id={user_id})"

@dataclass
class User:
    user_id: int
    role: Optional[str] = None  # customer|executor|dispatcher
    username: Optional[str] = None
    full_name: str = ""
    availability_text: Optional[str] = None

@dataclass
class Order:
    id: int
    customer_id: int
    description: str
    when_dt: Optional[datetime] = None
    address_text: Optional[str] = None
    latlon: Optional[Tuple[float, float]] = None
    attachments_count: int = 0
    status: str = "open"  # open|matched|closed
    bids: Dict[int, float] = field(default_factory=dict)  # executor_id -> price (net)
    chosen_executor_id: Optional[int] = None

@dataclass
class Match:
    order_id: int
    customer_id: int
    executor_id: int
    active: bool = True
    reveal_requested: Dict[int, bool] = field(default_factory=dict)
    reveal_approved_by_dispatcher: bool = False

USERS: Dict[int, User] = {}
ORDERS: Dict[int, Order] = {}
MATCHES: Dict[int, Match] = {}
ACTIVE_CHATS: Dict[int, Tuple[int, int]] = {}  # user_id -> (peer_id, order_id)

LAST_PHONE_SHARE: Dict[int, datetime] = {}

_order_seq = 1

def next_order_id() -> int:
    global _order_seq
    i = _order_seq
    _order_seq += 1
    return i

# --------------------- Helpers ---------------------

def is_dispatcher(uid: int) -> bool:
    return uid in ADMIN_IDS

def only_digits_phone(p: str) -> str:
    return ''.join(ch for ch in (p or '') if ch in '+0123456789')

async def ensure_user(m: Message) -> User:
    u = USERS.get(m.from_user.id)
    if not u:
        u = User(user_id=m.from_user.id,
                 username=m.from_user.username,
                 full_name=m.from_user.full_name or m.from_user.first_name or "Пользователь")
        USERS[m.from_user.id] = u
    else:
        u.username = m.from_user.username
        u.full_name = m.from_user.full_name or u.full_name
    return u

async def send_support_contacts(chat_id: int):
    # Telegram не принимает inline-URL вида tel:+...
    # Отправляем текст + карточку контакта (в клиентах есть кнопка «Позвонить»).
    text = "📞 Наш номер: {}\nЕсли хотите, просто напишите ваш номер ответным сообщением — мы перезвоним.".format(SUPPORT_PHONE)
    await bot.send_message(chat_id, text)
    try:
        digits = only_digits_phone(SUPPORT_PHONE)
        await bot.send_contact(chat_id, phone_number=digits, first_name=SUPPORT_NAME)
    except Exception:
        pass

async def show_menu(uid: int):
    u = USERS.get(uid)
    if not u or not u.role:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Я заказчик", callback_data="role:c")],
            [InlineKeyboardButton(text="Я исполнитель", callback_data="role:e")],
            [InlineKeyboardButton(text="Диспетчер", callback_data="role:d")]
        ])
        await bot.send_message(uid, "Выберите роль:", reply_markup=kb)
        await send_support_contacts(uid)
        return
    if u.role == "customer":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Новый заказ", callback_data="c:new")],
            [InlineKeyboardButton(text="📬 Мои заказы/предложения", callback_data="c:offers")],
            [InlineKeyboardButton(text="📞 Связаться", callback_data="call:0"),
             InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")]
        ])
        await bot.send_message(uid, "Главное меню (заказчик):", reply_markup=kb)
    elif u.role == "executor":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚦 Заказы рядом", callback_data="e:feed")],
            [InlineKeyboardButton(text="🗓 Моя доступность", callback_data="e:avail")],
            [InlineKeyboardButton(text="📞 Связаться", callback_data="call:0"),
             InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")]
        ])
        await bot.send_message(uid, "Главное меню (исполнитель):", reply_markup=kb)
    else:
        if not is_dispatcher(uid):
            await bot.send_message(uid, "Роль диспетчера доступна только утверждённым аккаунтам. Напишите нам: /contacts")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👁 Открытые заказы", callback_data="d:open")],
            [InlineKeyboardButton(text="🔗 Активные чаты", callback_data="d:chats")],
            [InlineKeyboardButton(text="📞 Логи звонков", callback_data="d:logs")],
            [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="d:help")],
        ])
        await bot.send_message(uid, "Панель диспетчера:", reply_markup=kb)

async def broadcast_to_dispatchers(text: str, kb: Optional[InlineKeyboardMarkup] = None):
    for u in USERS.values():
        if u.role == "dispatcher" and is_dispatcher(u.user_id):
            try:
                await bot.send_message(u.user_id, text, reply_markup=kb)
            except Exception:
                pass

# --------------------- States ---------------------

class CreateOrder(StatesGroup):
    waiting_desc = State()
    waiting_day = State()
    waiting_time = State()
    waiting_address = State()
    collecting_docs = State()

class ExecBid(StatesGroup):
    waiting_price = State()

class SharePhone(StatesGroup):
    waiting_phone_text = State()

class Availability(StatesGroup):
    waiting_text = State()

# --------------------- Start & Role ---------------------

@dp.message(CommandStart())
async def start(m: Message):
    u = await ensure_user(m)
    if u.role == "dispatcher" and not is_dispatcher(u.user_id):
        u.role = None
    await bot.send_message(m.chat.id, "Привет! Я помогу быстро найти исполнителя для стройработ. Всё просто, по шагам.")
    await show_menu(m.from_user.id)

@dp.message(Command("menu"))
async def menu_cmd(m: Message):
    await show_menu(m.from_user.id)

@dp.message(Command("contacts"))
async def contacts_cmd(m: Message):
    await send_support_contacts(m.chat.id)

@dp.callback_query(F.data == "home")
async def home_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_menu(c.from_user.id)
    await c.answer()

@dp.callback_query(F.data.startswith("role:"))
async def pick_role(c: CallbackQuery):
    code = c.data.split(":")[1]
    u = await ensure_user(c.message)
    if code == "c":
        u.role = "customer"
    elif code == "e":
        u.role = "executor"
    else:
        if not is_dispatcher(c.from_user.id):
            await c.answer("Только для утверждённых аккаунтов", show_alert=True)
            return
        u.role = "dispatcher"
    await c.answer("Роль сохранена")
    await show_menu(c.from_user.id)

# --------------------- Customer: Create Order (INLINE) ---------------------

@dp.callback_query(F.data == "c:new")
async def c_new(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(CreateOrder.waiting_desc)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="home")]])
    await c.message.answer("✍️ Опишите задачу простыми словами.\nПример: «Снять старые обои и поклеить новые, комната 18м²».", reply_markup=kb)
    await c.answer()

@dp.message(CreateOrder.waiting_desc)
async def c_desc(m: Message, state: FSMContext):
    await state.update_data(description=m.text.strip())
    await state.set_state(CreateOrder.waiting_day)
    today = datetime.now()
    days = [(today + timedelta(days=i)) for i in range(0, 7)]
    rows = []
    rows.append([InlineKeyboardButton(text="Сегодня", callback_data=f"cday:{today.strftime('%Y-%m-%d')}")])
    rows.append([InlineKeyboardButton(text="Завтра", callback_data=f"cday:{(today+timedelta(days=1)).strftime('%Y-%m-%d')}")])
    for d in days:
        label = d.strftime("%a %d.%m")
        rows.append([InlineKeyboardButton(text=label, callback_data=f"cday:{d.strftime('%Y-%m-%d')}")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="home")])
    await m.answer("📅 Когда начать работы? Выберите день:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data.startswith("cday:"))
async def c_day(c: CallbackQuery, state: FSMContext):
    day = c.data.split(":")[1]
    await state.update_data(day=day)
    await state.set_state(CreateOrder.waiting_time)
    rows = [
        [InlineKeyboardButton(text="Утро (09:00)", callback_data="ctime:09:00")],
        [InlineKeyboardButton(text="День (13:00)", callback_data="ctime:13:00")],
        [InlineKeyboardButton(text="Вечер (18:00)", callback_data="ctime:18:00")],
        [InlineKeyboardButton(text="Другое время", callback_data="ctime:custom")],
        [InlineKeyboardButton(text="Отмена", callback_data="home")]
    ]
    await c.message.answer("⏰ Во сколько удобно?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()

@dp.callback_query(F.data.startswith("ctime:"))
async def c_time(c: CallbackQuery, state: FSMContext):
    val = c.data.split(":")[1]
    if val == "custom":
        await state.set_state(CreateOrder.waiting_time)
        await c.message.answer("Введите время в формате ЧЧ:ММ, например 10:30.")
        await c.answer()
        return
    await state.update_data(time=val)
    await ask_address(c.message, state)
    await c.answer()

@dp.message(CreateOrder.waiting_time)
async def c_time_text(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    try:
        datetime.strptime(txt, "%H:%M")
    except Exception:
        await m.answer("Не понял время. Пример: 10:30")
        return
    await state.update_data(time=txt)
    await ask_address(m, state)

async def ask_address(target_message_holder, state: FSMContext):
    await state.set_state(CreateOrder.waiting_address)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="home")]])
    if isinstance(target_message_holder, Message):
        await target_message_holder.answer("📍 Укажите адрес словами (улица, дом). Можно прислать геометку через скрепку (необязательно).", reply_markup=kb)
    else:
        await bot.send_message(target_message_holder.chat.id, "📍 Укажите адрес словами (улица, дом).", reply_markup=kb)

@dp.message(CreateOrder.waiting_address, F.content_type.in_({"text", "location"}))
async def c_address(m: Message, state: FSMContext):
    data = await state.get_data()
    day = data.get("day")
    time = data.get("time")
    desc = data.get("description")
    when = datetime.strptime(f"{day} {time}", "%Y-%m-%d %H:%M")

    address_text = None
    latlon = None
    if m.location:
        latlon = (m.location.latitude, m.location.longitude)
    else:
        address_text = m.text.strip()

    oid = next_order_id()
    ORDERS[oid] = Order(
        id=oid, customer_id=m.from_user.id, description=desc, when_dt=when,
        address_text=address_text, latlon=latlon, attachments_count=0, status="open"
    )

    await state.set_state(CreateOrder.collecting_docs)
    rows = [[InlineKeyboardButton(text="📎 Готово (без документов)", callback_data=f"cfinish:{oid}")]]
    addr_show = address_text or "геометка"
    await m.answer(
        f"✅ Заказ #{oid} создан.\nДата и время: *{when.strftime('%d.%m %H:%M')}*\nАдрес: *{addr_show}*\n\n"
        f"Если хотите — пришлите фото/файлы. Потом нажмите кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )

@dp.message(CreateOrder.collecting_docs, F.content_type.in_({"photo", "document"}))
async def c_docs(m: Message, state: FSMContext):
    for o in ORDERS.values():
        if o.customer_id == m.from_user.id and o.status == "open":
            o.attachments_count += 1
            break
    await m.answer("📎 Принял. Можно добавить ещё или нажать ‘Готово’.")

@dp.callback_query(F.data.startswith("cfinish:"))
async def c_finish(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    o = ORDERS.get(oid)
    if not o:
        await c.answer("Не нашёл заказ", show_alert=True)
        return
    await c.answer()
    await c.message.answer("Заказ опубликован. Исполнители рядом увидят и пришлют цены.")
    await show_menu(c.from_user.id)

# --------------------- Executor: Feed & Bids (INLINE) ---------------------

@dp.callback_query(F.data == "e:feed")
async def e_feed(c: CallbackQuery):
    opens = [o for o in ORDERS.values() if o.status == "open"]
    if not opens:
        await c.message.answer("Пока нет открытых заказов. Зайдите позже.")
        await c.answer()
        return
    for o in sorted(opens, key=lambda x: (x.when_dt or datetime.max)):
        addr = o.address_text or "геометка"
        text = (
            f"📌 Заказ #{o.id}\n"
            f"Дата: {o.when_dt.strftime('%d.%m %H:%M') if o.when_dt else '—'}\n"
            f"Адрес: {addr}\n\n"
            f"{o.description}\n\n📎 Вложений: {o.attachments_count}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Предложить цену", callback_data=f"ebid:{o.id}")]
        ])
        await c.message.answer(text, reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data.startswith("ebid:"))
async def e_bid(c: CallbackQuery, state: FSMContext):
    oid = int(c.data.split(":")[1])
    o = ORDERS.get(oid)
    if not o or o.status != "open":
        await c.answer("Заказ недоступен", show_alert=True)
        return
    await state.set_state(ExecBid.waiting_price)
    await state.update_data(order_id=oid)
    await c.message.answer("Введите вашу цену (только число). Комиссия для клиента добавится автоматически.")
    await c.answer()

@dp.message(ExecBid.waiting_price)
async def e_price(m: Message, state: FSMContext):
    data = await state.get_data()
    oid = data.get("order_id")
    o = ORDERS.get(oid)
    if not o or o.status != "open":
        await state.clear()
        await m.answer("Заказ недоступен")
        return
    try:
        price = float((m.text or "").replace(",", "."))
        if price <= 0:
            raise ValueError
    except Exception:
        await m.answer("Пожалуйста, введите число, например 350")
        return
    o.bids[m.from_user.id] = price
    await state.clear()
    commission = round(price * COMMISSION_PCT, 2)
    total = round(price + commission, 2)
    await m.answer(f"Ваше предложение отправлено. Клиент увидит: цена {price:.2f} + комиссия {commission:.2f} = *{total:.2f}*.")
    try:
        await bot.send_message(
            o.customer_id,
            f"📨 Новое предложение по заказу #{o.id}: *{total:.2f}* (включая комиссию). Зайдите в Мои заказы, чтобы выбрать."
        )
    except Exception:
        pass

# --------------------- Customer: Offers & Choose (INLINE) ---------------------

@dp.callback_query(F.data == "c:offers")
async def c_offers(c: CallbackQuery):
    my = [o for o in ORDERS.values() if o.customer_id == c.from_user.id and o.status == "open"]
    if not my:
        await c.message.answer("Открытых заказов нет.")
        await c.answer()
        return
    for o in my:
        if not o.bids:
            await c.message.answer(f"Заказ #{o.id}: предложений пока нет.")
            continue
        lines = [f"Заказ #{o.id} — {o.when_dt.strftime('%d.%m %H:%M') if o.when_dt else '—'}"]
        rows = []
        for exec_id, price in o.bids.items():
            commission = round(price * COMMISSION_PCT, 2)
            total = round(price + commission, 2)
            lines.append(f"• Исполнитель {exec_id}: *{total:.2f}* (в т.ч. комиссия {commission:.2f})")
            rows.append([InlineKeyboardButton(text=f"Выбрать {exec_id}", callback_data=f"cchoose:{o.id}:{exec_id}")])
        await c.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()

@dp.callback_query(F.data.startswith("cchoose:"))
async def c_choose(c: CallbackQuery):
    _, oid_s, eid_s = c.data.split(":")
    oid, eid = int(oid_s), int(eid_s)
    o = ORDERS.get(oid)
    if not o or o.customer_id != c.from_user.id or o.status != "open":
        await c.answer("Недоступно", show_alert=True)
        return
    price = o.bids.get(eid)
    if price is None:
        await c.answer("Предложение не найдено", show_alert=True)
        return
    commission = round(price * COMMISSION_PCT, 2)
    total = round(price + commission, 2)
    o.status = "matched"
    o.chosen_executor_id = eid
    ACTIVE_CHATS[o.customer_id] = (eid, o.id)
    ACTIVE_CHATS[eid] = (o.customer_id, o.id)
    await c.message.answer(
        f"✅ Исполнитель выбран. Общая сумма для клиента: *{total:.2f}*.\n"
        f"Оплату комиссии вы производите вне бота. Начинаем анонимный чат.\n"
        f"Команды: /reveal, /end, /contacts"
    )
    try:
        await bot.send_message(eid, f"✅ Вас выбрали по заказу #{oid}. Пишите сюда сообщения — бот передаст клиенту.")
    except Exception:
        pass
    await c.answer()

# --------------------- Anonymous chat, reveal, end ---------------------

@dp.message(Command("reveal"))
async def cmd_reveal(m: Message):
    link = ACTIVE_CHATS.get(m.from_user.id)
    if not link:
        await m.answer("Нет активного чата")
        return
    peer_id, oid = link
    mt = MATCHES.get(oid)
    if not mt:
        o = ORDERS.get(oid)
        if o:
            MATCHES[oid] = Match(order_id=oid, customer_id=o.customer_id, executor_id=o.chosen_executor_id or peer_id)
            mt = MATCHES[oid]
    mt.reveal_requested[m.from_user.id] = True
    both = len(mt.reveal_requested) == 2 and all(mt.reveal_requested.get(uid) for uid in [mt.customer_id, mt.executor_id])
    if both or mt.reveal_approved_by_dispatcher:
        cu, eu = USERS[mt.customer_id], USERS[mt.executor_id]
        await bot.send_message(mt.customer_id, f"🔓 Контакты раскрыты: {mention(eu.user_id, eu.username, eu.full_name)}")
        await bot.send_message(mt.executor_id, f"🔓 Контакты раскрыты: {mention(cu.user_id, cu.username, cu.full_name)}")
    else:
        await m.answer("Запрос принят. Раскроем контакты после согласия второй стороны или одобрения диспетчера.")
        await broadcast_to_dispatchers(f"🔔 Запрос на раскрытие контактов по заказу #{oid}. Одобрить: /approve_reveal {oid}")

@dp.message(Command("approve_reveal"))
async def cmd_approve_reveal(m: Message):
    u = await ensure_user(m)
    if not (u.role == "dispatcher" and is_dispatcher(u.user_id)):
        await m.answer("Команда только для диспетчеров.")
        return
    parts = (m.text or "").split()
    if len(parts) < 2:
        await m.answer("Используйте: /approve_reveal <order_id>")
        return
    try:
        order_id = int(parts[1])
    except Exception:
        await m.answer("Неверный order_id")
        return
    mt = MATCHES.get(order_id)
    if not mt:
        o = ORDERS.get(order_id)
        if not o or not o.chosen_executor_id:
            await m.answer("Матч не найден")
            return
        MATCHES[order_id] = Match(order_id=order_id, customer_id=o.customer_id, executor_id=o.chosen_executor_id)
        mt = MATCHES[order_id]
    mt.reveal_approved_by_dispatcher = True
    cu, eu = USERS[mt.customer_id], USERS[mt.executor_id]
    await bot.send_message(mt.customer_id, f"🔓 Диспетчер одобрил раскрытие: {mention(eu.user_id, eu.username, eu.full_name)}")
    await bot.send_message(mt.executor_id, f"🔓 Диспетчер одобрил раскрытие: {mention(cu.user_id, cu.username, cu.full_name)}")
    await m.answer("Одобрено")

@dp.message(Command("end"))
async def cmd_end(m: Message):
    link = ACTIVE_CHATS.pop(m.from_user.id, None)
    if not link:
        await m.answer("Нет активного чата")
        return
    peer_id, oid = link
    ACTIVE_CHATS.pop(peer_id, None)
    o = ORDERS.get(oid)
    if o:
        o.status = "closed"
    await m.answer("Чат завершён. Заказ закрыт.")
    try:
        await bot.send_message(peer_id, "Чат завершён. Заказ закрыт.")
    except Exception:
        pass

@dp.message(F.content_type.in_({"text", "photo", "document", "audio", "video", "voice", "video_note", "location", "sticker"}))
async def relay(m: Message):
    link = ACTIVE_CHATS.get(m.from_user.id)
    if not link:
        return
    peer_id, oid = link
    try:
        await bot.copy_message(chat_id=peer_id, from_chat_id=m.chat.id, message_id=m.message_id)
    except Exception:
        await m.answer("Не удалось доставить сообщение")

# --------------------- Help / Call (INLINE) ---------------------

@dp.callback_query(F.data == "help")
async def help_cb(c: CallbackQuery):
    await c.message.answer("Если запутались — нажмите ‘Связаться’. Мы перезвоним и всё подскажем.")
    await call_cb(c)

@dp.callback_query(F.data.startswith("call:"))
async def call_cb(c: CallbackQuery):
    await send_support_contacts(c.from_user.id)
    rows = [[InlineKeyboardButton(text="📲 Оставить мой номер (напишу сам)", callback_data="call:leave")]]
    await c.message.answer(
        "Можно также просто ответить сообщением с вашим телефоном.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await c.answer()

@dp.callback_query(F.data == "call:leave")
async def call_leave(c: CallbackQuery, state: FSMContext):
    await state.set_state(SharePhone.waiting_phone_text)
    await c.message.answer("Напишите цифрами ваш номер телефона. Мы перезвоним.")
    await c.answer()

@dp.message(SharePhone.waiting_phone_text)
async def receive_phone_text(m: Message, state: FSMContext):
    digits = only_digits_phone(m.text or "")
    if len(digits) < 7:
        await m.answer("Похоже, это не номер. Пример: +375291234567")
        return
    now = datetime.utcnow()
    last = LAST_PHONE_SHARE.get(m.from_user.id)
    if last and (now - last).total_seconds() < PHONE_SHARE_RATE_LIMIT:
        await m.answer("Мы недавно получили ваш номер. Скоро свяжемся. Спасибо!")
    else:
        LAST_PHONE_SHARE[m.from_user.id] = now
        u = USERS.get(m.from_user.id)
        await broadcast_to_dispatchers(f"📞 Просьба перезвонить: {mention(u.user_id, u.username, u.full_name)} — {digits}")
        await m.answer("Спасибо! Передал диспетчеру. Ожидайте звонка.")
    await state.clear()

# --------------------- Executor Availability ---------------------

@dp.callback_query(F.data == "e:avail")
async def e_avail(c: CallbackQuery, state: FSMContext):
    await state.set_state(Availability.waiting_text)
    await c.message.answer("Опишите вашу доступность (например: будни 9:00–18:00, ближайшая дата 02.09).")
    await c.answer()

@dp.message(Availability.waiting_text)
async def save_avail(m: Message, state: FSMContext):
    u = await ensure_user(m)
    u.availability_text = (m.text or "").strip()
    await state.clear()
    await m.answer("Доступность сохранена.")

# --------------------- Dispatcher Tools (упрощённо) ---------------------

@dp.callback_query(F.data == "d:open")
async def d_open(c: CallbackQuery):
    if not is_dispatcher(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    opens = [o for o in ORDERS.values() if o.status == "open"]
    if not opens:
        await c.message.answer("Открытых заказов нет.")
    else:
        text = "\n".join([
            f"#{o.id} — {o.when_dt.strftime('%d.%m %H:%M') if o.when_dt else '—'} — {o.description[:80]}"
            for o in sorted(opens, key=lambda x: (x.when_dt or datetime.max))
        ])
        await c.message.answer(text)
    await c.answer()

@dp.callback_query(F.data == "d:chats")
async def d_chats(c: CallbackQuery):
    if not is_dispatcher(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    act = []
    seen_pairs = set()
    for uid, (peer, oid) in list(ACTIVE_CHATS.items()):
        pair = tuple(sorted((uid, peer)))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        o = ORDERS.get(oid)
        if not o:
            continue
        cu = USERS.get(o.customer_id)
        eu = USERS.get(o.chosen_executor_id or peer)
        act.append(f"#{oid}: {mention(cu.user_id, cu.username, cu.full_name)} ↔ {mention(eu.user_id, eu.username, eu.full_name)}")
    await c.message.answer("\n".join(act) or "Активных чатов нет")
    await c.answer()

@dp.callback_query(F.data == "d:logs")
async def d_logs(c: CallbackQuery):
    if not is_dispatcher(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    await c.message.answer("Логи звонков будут добавлены в следующем релизе (упрощено в этом MVP).")
    await c.answer()

@dp.callback_query(F.data == "d:help")
async def d_help(c: CallbackQuery):
    if not is_dispatcher(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    await c.message.answer("Команды: /approve_reveal <order_id>, /end — завершить чат. Для связи с пользователем используйте /contacts.")
    await c.answer()

# --------------------- Entry ---------------------

async def main():
    print("Bot is running (Inline-first)…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
