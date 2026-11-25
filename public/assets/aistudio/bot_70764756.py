"""
GeoQuiz — Telegram-бот для мини-аппа «викторина о странах и курортах».

Что умеет:
  • /start — приветствие на языке пользователя + кнопка «Играть» (открывает мини-апп)
  • кнопка «Играть» в меню слева от поля ввода
  • сам выставляет описание бота, «About» и команды на RU и EN
  • считает пользователей в SQLite (users.db рядом с ботом)
  • /stats — статистика (только для ADMIN_ID)
  • ссылки вида t.me/<bot>?start=<метка> — видно, откуда пришли люди

Запуск:
  pip install -r requirements.txt
  заполнить .env (см. .env.example)
  python bot.py
"""
import asyncio
import html
import logging
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

# ===================== НАСТРОЙКИ =====================
def load_env(path: str = ".env") -> None:
    """Простейшее чтение .env без лишних библиотек."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://dimedrolex.github.io/capitals/")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)       # твой Telegram ID — для /stats
BOT_NAME = os.getenv("BOT_NAME", "GeoQuiz")          # название игры в текстах
NOTIFY_NEW = os.getenv("NOTIFY_NEW", "0") == "1"       # 1 — присылать тебе сообщение о каждом новом игроке
DB_PATH = os.getenv("DB_PATH", "users.db")

if not BOT_TOKEN:
    raise SystemExit("Нет BOT_TOKEN. Впиши его в файл .env (см. .env.example)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ===================== ТЕКСТЫ =====================
HELLO = {
    "ru": lambda name: (
        f"👋 Привет, {name}!\n\n"
        f"Я <b>{BOT_NAME}</b> — проверим, как хорошо ты знаешь мир 🌍\n\n"
        "🔥 Начни с <b>ежедневного челленджа</b> — 10 вопросов, одинаковых для всех. "
        "Потом сравни результат с друзьями!\n\n"
        "Жми «Играть» 👇"
    ),
    "en": lambda name: (
        f"👋 Hi, {name}!\n\n"
        f"I'm <b>{BOT_NAME}</b> — let's see how well you know the world 🌍\n\n"
        "🔥 Start with the <b>daily challenge</b> — 10 questions, the same for everyone. "
        "Then compare your score with friends!\n\n"
        "Tap “Play” 👇"
    ),
}
FACTS = {
    "ru": [
        "Столица Австралии — не Сидней, а Канберра!",
        "Столица Турции — Анкара, а не Стамбул.",
        "Стамбул стоит сразу в двух частях света — в Европе и в Азии.",
        "В России 11 часовых поясов.",
        "Каспийское море на самом деле — самое большое озеро на Земле.",
        "Байкал — самое глубокое озеро планеты: больше 1600 метров.",
        "Ватикан — самая маленькая страна мира: меньше половины квадратного километра.",
        "На Дунае стоят сразу 4 столицы: Вена, Братислава, Будапешт и Белград.",
        "У Канады самая длинная береговая линия в мире.",
        "В Африке 54 страны — больше, чем на любом другом континенте.",
        "У Индонезии больше 17 000 островов.",
        "Тихий океан больше, чем вся суша Земли вместе взятая.",
        "Казахстан — самая большая страна в мире без выхода к океану.",
        "Флаг Непала — единственный в мире непрямоугольный государственный флаг.",
        "В Бразилии говорят по-португальски, а не по-испански.",
        "Пустыня Сахара по площади почти как США.",
        "Гренландия — самый большой остров в мире.",
        "Монголия — самая малонаселённая страна мира.",
        "У Боливии официальная столица — Сукре, а правительство работает в Ла-Пасе.",
        "Новая Зеландия первой в мире дала женщинам право голосовать — в 1893 году.",
    ],
    "en": [
        "Australia's capital is Canberra, not Sydney!",
        "Turkey's capital is Ankara, not Istanbul.",
        "Istanbul lies on two continents at once — Europe and Asia.",
        "Russia spans 11 time zones.",
        "The Caspian Sea is actually the largest lake on Earth.",
        "Lake Baikal is the deepest lake on the planet — over 1,600 metres.",
        "Vatican City is the world's smallest country — less than half a square kilometre.",
        "Four capitals stand on the Danube: Vienna, Bratislava, Budapest and Belgrade.",
        "Canada has the longest coastline in the world.",
        "Africa has 54 countries — more than any other continent.",
        "Indonesia has more than 17,000 islands.",
        "The Pacific Ocean is bigger than all of Earth's land put together.",
        "Kazakhstan is the world's largest landlocked country.",
        "Nepal's flag is the only non-rectangular national flag.",
        "People in Brazil speak Portuguese, not Spanish.",
        "The Sahara Desert is almost as big as the USA.",
        "Greenland is the largest island in the world.",
        "Mongolia is the most sparsely populated country on Earth.",
        "Bolivia's official capital is Sucre, but the government sits in La Paz.",
        "New Zealand was the first country to give women the vote — in 1893.",
    ],
}


def greeting(lang: str, user) -> str:
    name = html.escape(user.first_name or ("друг" if lang == "ru" else "friend"))
    fact_title = "💡 Факт дня" if lang == "ru" else "💡 Fact of the day"
    return f"{HELLO[lang](name)}\n\n<i>{fact_title}: {random.choice(FACTS[lang])}</i>"


TEXTS = {
    "ru": {
        "start": (
            f"🌍 <b>{BOT_NAME}</b>\n\n"
            "Насколько хорошо ты знаешь мир?\n\n"
            "🏛 Столицы, страны и их контуры — 192 страны\n"
            "🏖 Курорты: от Хургады до Бора-Бора и Куршевеля\n"
            "👶 Детский режим без очков и таймера\n"
            "⚡ Блиц на 60 секунд и 💀 хардкор для знатоков\n"
            "🔥 Ежедневный челлендж — сравнивай с друзьями\n\n"
            "Жми «Играть» 👇"
        ),
        "play": "▶️ Играть",
        "menu": "Играть",
        "help": (
            "Нажми «Играть» под этим сообщением или кнопку слева от поля ввода — игра откроется прямо в Telegram.\n\n"
            "Прогресс сохраняется в твоём аккаунте Telegram, на любом устройстве."
        ),
        "cmd_start": "Начать игру",
        "cmd_help": "Как играть",
        "cmd_lang": "Язык / Language",
        "lang_ask": "Выбери язык 👇",
        "lang_set": "Готово! Теперь я говорю по-русски 🇷🇺",
        "short": "Угадывай столицы, флаги, контуры стран и курорты мира. Звания, достижения и ежедневный челлендж 🌍",
        "desc": (
            "🌍 Насколько хорошо ты знаешь мир?\n\n"
            "🏛 Столицы, страны и их контуры — 192 страны\n"
            "🏖 Курорты: от Хургады до Бора-Бора и Куршевеля\n"
            "👶 Детский режим: флаги и столицы без очков и таймера\n"
            "⚡ Блиц на 60 секунд и 💀 хардкор для знатоков\n"
            "🔥 Ежедневный челлендж — одинаковый для всех\n"
            "🎖 11 званий: от Следопыта до Покорителя семи морей\n\n"
            "Жми «Старт» 👇"
        ),
    },
    "en": {
        "start": (
            f"🌍 <b>{BOT_NAME}</b>\n\n"
            "How well do you know the world?\n\n"
            "🏛 Capitals, countries and their shapes — 192 countries\n"
            "🏖 Resorts: from Bali to Bora Bora and Courchevel\n"
            "👶 Kids mode with no score and no timer\n"
            "⚡ 60-second blitz and 💀 hardcore for experts\n"
            "🔥 Daily challenge — compete with friends\n\n"
            "Tap “Play” 👇"
        ),
        "play": "▶️ Play",
        "menu": "Play",
        "help": (
            "Tap “Play” below this message or the button next to the input field — the game opens right inside Telegram.\n\n"
            "Your progress is saved to your Telegram account on every device."
        ),
        "cmd_start": "Start the game",
        "cmd_help": "How to play",
        "cmd_lang": "Language / Язык",
        "lang_ask": "Choose your language 👇",
        "lang_set": "Done! I speak English now 🇬🇧",
        "short": "Guess capitals, flags, country shapes and famous resorts. Ranks, achievements and a daily challenge 🌍",
        "desc": (
            "🌍 How well do you know the world?\n\n"
            "🏛 Capitals, countries and their shapes — 192 countries\n"
            "🏖 Resorts: from Bali to Bora Bora and Courchevel\n"
            "👶 Kids mode: flags & capitals, no score, no timer\n"
            "⚡ 60-second blitz and 💀 hardcore for experts\n"
            "🔥 Daily challenge — the same for everyone\n"
            "🎖 11 ranks: from Pathfinder to Conqueror of the Seven Seas\n\n"
            "Tap “Start” 👇"
        ),
    },
}


def lang_of(code: str | None) -> str:
    if not code:
        return "ru"  # Telegram не передал язык (бывает в Desktop/Web) — по умолчанию русский
    return "ru" if code.lower().startswith(("ru", "uk", "be", "kk")) else "en"


VERSION = str(int(time.time()))  # меняется при каждом перезапуске бота — Telegram не покажет старую версию игры


def app_url(lang: str) -> str:
    sep = "&" if "?" in WEBAPP_URL else "?"
    return f"{WEBAPP_URL}{sep}lang={lang}&v={VERSION}"


def play_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=TEXTS[lang]["play"], web_app=WebAppInfo(url=app_url(lang)))
    ]])


def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
    ]])


# ===================== БАЗА ПОЛЬЗОВАТЕЛЕЙ =====================
db = sqlite3.connect(DB_PATH)
db.execute("""
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    lang       TEXT,
    source     TEXT,
    first_seen TEXT,
    last_seen  TEXT,
    starts     INTEGER DEFAULT 0
)""")
try:
    db.execute("ALTER TABLE users ADD COLUMN ulang TEXT")  # язык, выбранный через /language
except sqlite3.OperationalError:
    pass
db.commit()


def user_lang(user) -> str:
    row = db.execute("SELECT ulang FROM users WHERE id = ?", (user.id,)).fetchone()
    if row and row[0] in ("ru", "en"):
        return row[0]
    return lang_of(user.language_code)


async def set_menu(chat_id: int, lang: str) -> None:
    """Кнопка «Играть/Play» слева от поля ввода — на языке конкретного пользователя."""
    try:
        await bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(text=TEXTS[lang]["menu"], web_app=WebAppInfo(url=app_url(lang))),
        )
    except Exception as e:
        log.warning("menu button: %s", e)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_user(m: Message, source: str | None) -> bool:
    """Записывает/обновляет пользователя. Возвращает True, если он новый."""
    u = m.from_user
    row = db.execute("SELECT id FROM users WHERE id = ?", (u.id,)).fetchone()
    if row:
        db.execute(
            "UPDATE users SET username=?, first_name=?, lang=?, last_seen=?, starts=starts+1 WHERE id=?",
            (u.username, u.first_name, u.language_code, now_iso(), u.id),
        )
        db.commit()
        return False
    db.execute(
        "INSERT INTO users (id, username, first_name, lang, source, first_seen, last_seen, starts) VALUES (?,?,?,?,?,?,?,1)",
        (u.id, u.username, u.first_name, u.language_code, source, now_iso(), now_iso()),
    )
    db.commit()
    return True


def stats_text() -> str:
    day = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    new_day = db.execute("SELECT COUNT(*) FROM users WHERE first_seen >= ?", (day,)).fetchone()[0]
    new_week = db.execute("SELECT COUNT(*) FROM users WHERE first_seen >= ?", (week,)).fetchone()[0]
    active_day = db.execute("SELECT COUNT(*) FROM users WHERE last_seen >= ?", (day,)).fetchone()[0]
    langs = db.execute(
        "SELECT COALESCE(lang,'?'), COUNT(*) c FROM users GROUP BY 1 ORDER BY c DESC LIMIT 8"
    ).fetchall()
    sources = db.execute(
        "SELECT source, COUNT(*) c FROM users WHERE source IS NOT NULL AND source != '' GROUP BY 1 ORDER BY c DESC LIMIT 8"
    ).fetchall()
    lines = [
        "📊 <b>Статистика бота</b>",
        f"👥 Всего пользователей: <b>{total}</b>",
        f"🆕 Новых за сутки: <b>{new_day}</b> · за неделю: <b>{new_week}</b>",
        f"🔁 Заходили за сутки: <b>{active_day}</b>",
        "",
        "🌐 Языки: " + (", ".join(f"{l} — {c}" for l, c in langs) or "—"),
        "📣 Откуда пришли: " + (", ".join(f"{s} — {c}" for s, c in sources) or "—"),
    ]
    return "\n".join(lines)


# ===================== БОТ =====================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


@dp.message(CommandStart())
async def on_start(m: Message, command: CommandObject):
    source = (command.args or "").strip()[:32] or None   # t.me/<bot>?start=<метка>
    is_new = save_user(m, source)
    lang = user_lang(m.from_user)
    await set_menu(m.chat.id, lang)
    await m.answer(greeting(lang, m.from_user), reply_markup=play_kb(lang))
    if is_new and NOTIFY_NEW and ADMIN_ID:
        u = m.from_user
        who = f"@{u.username}" if u.username else u.first_name
        try:
            await bot.send_message(ADMIN_ID, f"🆕 Новый игрок: {who} ({u.language_code}){' · ' + source if source else ''}")
        except Exception:
            pass


@dp.message(Command("language", "lang"))
async def on_lang(m: Message):
    await m.answer(TEXTS[user_lang(m.from_user)]["lang_ask"], reply_markup=lang_kb())


@dp.callback_query(F.data.startswith("lang:"))
async def on_lang_pick(c: CallbackQuery):
    lang = c.data.split(":", 1)[1]
    if lang not in TEXTS:
        return await c.answer()
    db.execute("UPDATE users SET ulang = ? WHERE id = ?", (lang, c.from_user.id))
    db.commit()
    await set_menu(c.message.chat.id, lang)
    await c.answer()
    await c.message.answer(TEXTS[lang]["lang_set"])
    await c.message.answer(greeting(lang, c.from_user), reply_markup=play_kb(lang))


@dp.message(Command("help"))
async def on_help(m: Message):
    lang = user_lang(m.from_user)
    await m.answer(TEXTS[lang]["help"], reply_markup=play_kb(lang))


@dp.message(Command("stats"))
async def on_stats(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer(stats_text())


@dp.message(Command("myid"))
async def on_myid(m: Message):
    # помогает узнать свой ID для ADMIN_ID
    await m.answer(f"Твой Telegram ID: <code>{m.from_user.id}</code>")


@dp.message()
async def on_any(m: Message):
    # на любое другое сообщение — просто снова кнопка «Играть»
    lang = user_lang(m.from_user)
    await m.answer(TEXTS[lang]["help"], reply_markup=play_kb(lang))


async def setup_profile():
    """Описание, «About», команды и кнопку меню выставляем сами — на RU и EN."""
    for lang, code in (("en", None), ("ru", "ru")):
        t = TEXTS[lang]
        try:
            await bot.set_my_description(description=t["desc"], language_code=code)
            await bot.set_my_short_description(short_description=t["short"], language_code=code)
            await bot.set_my_commands(
                [BotCommand(command="start", description=t["cmd_start"]),
                 BotCommand(command="language", description=t["cmd_lang"]),
                 BotCommand(command="help", description=t["cmd_help"])],
                language_code=code,
            )
        except Exception as e:
            log.warning("Не удалось обновить профиль (%s): %s", lang, e)
    try:
        await bot.set_chat_menu_button(  # общая кнопка по умолчанию; после /start у каждого своя
            menu_button=MenuButtonWebApp(text="▶️ Play", web_app=WebAppInfo(url=app_url("en")))
        )
    except Exception as e:
        log.warning("Не удалось поставить кнопку меню: %s", e)


async def main():
    await setup_profile()
    me = await bot.get_me()
    log.info("Бот @%s запущен. Мини-апп: %s", me.username, WEBAPP_URL)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
