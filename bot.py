#!/usr/bin/env python3
"""Telegram-бот, который пишет сценарии для Reels через Claude API.

Переменные окружения (можно положить в файл .env рядом с bot.py):
    TELEGRAM_BOT_TOKEN  — токен бота от @BotFather (обязательно)
    ANTHROPIC_API_KEY   — ключ Anthropic API (обязательно)
    ALLOWED_USER_IDS    — ID пользователей Telegram через запятую, которым разрешён доступ
                          (если пусто — бот доступен всем, и все запросы оплачиваешь ты)
"""

import asyncio
import io
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction, MessageLimit
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_CONTINUATIONS = 5
DATA_DIR = Path(__file__).resolve().parent / "data"
PROFILES_FILE = DATA_DIR / "profiles.json"

SYSTEM_PROMPT = """\
Ты — профессиональный сценарист коротких вертикальных видео (Instagram Reels, TikTok, YouTube Shorts).
Ты работаешь внутри Telegram-бота и пишешь готовые к съёмке сценарии, которые удерживают внимание
и ведут к цели автора.

Как ты работаешь:
- Опирайся на профиль автора (ниша, аудитория, тон, цель, формат съёмки, оффер, запреты).
- Если тема не указана или слишком размыта — задай 1–3 коротких уточняющих вопроса, затем пиши сценарий.
- Первые 1–3 секунды решают всё: хук должен создавать интригу, конфликт или обещание пользы.
- Пиши разговорным языком, короткими фразами — так, как автор реально скажет это в камеру.
- Укладывайся в длительность (по умолчанию 30 сек): ориентир ~2–2,5 слова озвучки на секунду.
- Не выдумывай факты, цифры, исследования и цитаты. Если утверждение требует проверки — пометь его
  «[проверить]». Если пользуешься веб-поиском — укажи ссылки на источники в конце.
- Не обещай гарантированных результатов (охваты, деньги, здоровье) и не давай медицинских/финансовых
  рекомендаций как истину.

Оформление: это Telegram, разметка не отображается. Не используй Markdown-таблицы, символы #, ** и _.
Структурируй текст эмодзи, переносами строк и нумерацией. Формат сценария:

🎬 <Рабочее название>
⏱ Длительность: N сек · 🎥 Формат: … · 🎯 Цель: …

🪝 ХУК (0–3 сек) — 3 варианта
1. …
2. …
3. …

📋 РАСКАДРОВКА
[0–3 с]
🎥 В кадре: …
🗣 Говорим: …
💬 Текст на экране: …

[3–8 с]
…

📣 ПРИЗЫВ К ДЕЙСТВИЮ
…

✍️ ПОДПИСЬ К ПОСТУ
…

#️⃣ ХЭШТЕГИ
5–10 релевантных хэштегов.

💡 СЪЁМКА И МОНТАЖ
3–5 коротких советов: музыка/звук, темп склеек, обложка.

В конце коротко предложи 2–3 варианта, как усилить или переделать сценарий.
Если пользователь просит правки — переписывай сценарий целиком в том же формате.
"""

HELP_TEXT = """\
🎬 Я пишу сценарии для Reels, TikTok и Shorts.

Просто напиши тему, например:
«3 ошибки новичков в зале, 20 секунд, с юмором»

После сценария можно просить правки: «короче», «добавь юмора», «другой хук».

Команды:
/profile — показать профиль блога
/profile <текст> — задать профиль: ниша, аудитория, тон, цель, формат съёмки, что продаёшь
/new — новая тема (забыть текущий диалог)
/web — включить/выключить поиск трендов и фактов в интернете
/save — прислать последний сценарий файлом
/myid — узнать свой Telegram ID
"""

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("reels_bot")

client = anthropic.AsyncAnthropic()


# ---------- Хранилище профилей ----------

def load_profiles() -> dict[str, str]:
    if not PROFILES_FILE.exists():
        return {}
    try:
        return json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.exception("Повреждён %s, начинаю с пустых профилей", PROFILES_FILE)
        return {}


def save_profiles(profiles: dict[str, str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp = PROFILES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROFILES_FILE)


PROFILES = load_profiles()


# ---------- Состояние пользователя ----------

class Session:
    """Диалог одного пользователя. Живёт в памяти — после перезапуска бота история сбрасывается."""

    def __init__(self) -> None:
        self.messages: list = []
        self.last_answer = ""
        self.use_web = False
        self.lock = asyncio.Lock()


SESSIONS: dict[int, Session] = {}


def get_session(user_id: int) -> Session:
    return SESSIONS.setdefault(user_id, Session())


def allowed_ids() -> set[int]:
    raw = os.environ.get("ALLOWED_USER_IDS", "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


def is_allowed(update: Update) -> bool:
    ids = allowed_ids()
    return not ids or (update.effective_user is not None and update.effective_user.id in ids)


# ---------- Claude ----------

def build_system(user_id: int) -> str:
    profile = PROFILES.get(str(user_id), "").strip()
    if profile:
        return SYSTEM_PROMPT + "\nПрофиль автора:\n" + profile
    return SYSTEM_PROMPT + "\nПрофиль автора не задан. Если ниша и аудитория важны для темы — уточни их."


async def run_turn(session: Session, system: str) -> str:
    parts: list[str] = []
    for _ in range(MAX_CONTINUATIONS):
        kwargs = dict(
            model=MODEL,
            max_tokens=64000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            cache_control={"type": "ephemeral"},
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            messages=session.messages,
        )
        if session.use_web:
            kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]
        async with client.beta.messages.stream(**kwargs) as stream:
            response = await stream.get_final_message()
        # История только дописывается: сохраняем ответ целиком, включая thinking и результаты поиска.
        session.messages.append({"role": "assistant", "content": response.content})
        parts.extend(b.text for b in response.content if b.type == "text")
        if response.stop_reason == "refusal":
            parts.append("\n\n⚠️ Модель отказалась отвечать на этот запрос. Переформулируй тему.")
            break
        if response.stop_reason == "max_tokens":
            parts.append("\n\n⚠️ Ответ обрезан по лимиту длины.")
            break
        if response.stop_reason != "pause_turn":
            break
        # pause_turn: сервер прервал долгий веб-поиск — повторяем запрос, он продолжит сам.
    return "".join(parts).strip()


async def keep_typing(chat, stop: asyncio.Event) -> None:
    # Статус «печатает…» в Telegram гаснет через ~5 секунд, поэтому обновляем его.
    while not stop.is_set():
        try:
            await chat.send_action(ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


def split_message(text: str, limit: int = MessageLimit.MAX_TEXT_LENGTH) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


# ---------- Обработчики ----------

async def deny(update: Update) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    await update.effective_message.reply_text(f"⛔️ Доступ закрыт. Твой ID: {uid} — передай его владельцу бота.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    await update.effective_message.reply_text(HELP_TEXT)


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    uid = str(update.effective_user.id)
    text = update.effective_message.text.partition(" ")[2].strip()
    if text:
        PROFILES[uid] = text
        save_profiles(PROFILES)
        await update.effective_message.reply_text("✅ Профиль сохранён. Теперь пиши тему для рилс.")
    elif PROFILES.get(uid):
        await update.effective_message.reply_text("📇 Твой профиль:\n\n" + PROFILES[uid])
    else:
        await update.effective_message.reply_text(
            "Профиль пока пуст. Отправь, например:\n\n"
            "/profile Блог про фитнес для занятых мам 25–40 лет. Тон дружелюбный, с юмором. "
            "Цель — заявки на онлайн-марафон. Снимаю говорящую голову и тренировки дома. "
            "Не обещать быстрое похудение."
        )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    session = get_session(update.effective_user.id)
    async with session.lock:
        session.messages.clear()
        session.last_answer = ""
    await update.effective_message.reply_text("🆕 Начинаем новую тему. Напиши, о чём снимаем.")


async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    session = get_session(update.effective_user.id)
    session.use_web = not session.use_web
    state = "включён 🌐 (ответы дольше и дороже)" if session.use_web else "выключен"
    await update.effective_message.reply_text(f"Веб-поиск {state}.")


async def cmd_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    session = get_session(update.effective_user.id)
    if not session.last_answer:
        return await update.effective_message.reply_text("Пока нечего сохранять — сначала попроси сценарий.")
    data = io.BytesIO(session.last_answer.encode("utf-8"))
    await update.effective_message.reply_document(data, filename=f"reels_{datetime.now():%Y-%m-%d_%H-%M}.txt")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    uid = update.effective_user.id
    session = get_session(uid)
    if session.lock.locked():
        return await update.effective_message.reply_text("⏳ Ещё пишу предыдущий ответ, подожди немного.")

    async with session.lock:
        start = len(session.messages)
        session.messages.append({"role": "user", "content": update.effective_message.text})
        stop = asyncio.Event()
        typing = asyncio.create_task(keep_typing(update.effective_chat, stop))
        try:
            answer = await run_turn(session, build_system(uid))
        except Exception as e:
            del session.messages[start:]  # откатываем незавершённый ход
            log.exception("Ошибка при генерации для %s", uid)
            await update.effective_message.reply_text(error_text(e))
            return
        finally:
            stop.set()
            await typing

        session.last_answer = answer
        for chunk in split_message(answer or "🤔 Пустой ответ, попробуй переформулировать."):
            await update.effective_message.reply_text(chunk)


def error_text(e: Exception) -> str:
    if isinstance(e, anthropic.AuthenticationError):
        return "⚠️ Неверный ключ Anthropic API. Владельцу бота: проверь ANTHROPIC_API_KEY."
    if isinstance(e, anthropic.RateLimitError):
        return "⚠️ Слишком много запросов, попробуй через минуту."
    if isinstance(e, anthropic.APIStatusError):
        return f"⚠️ Ошибка API ({e.status_code}). Попробуй ещё раз."
    if isinstance(e, anthropic.APIConnectionError):
        return "⚠️ Нет связи с API. Попробуй ещё раз."
    return "⚠️ Что-то пошло не так. Попробуй ещё раз."


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN (получи у @BotFather).")
    if not allowed_ids():
        log.warning("ALLOWED_USER_IDS не задан — ботом может пользоваться кто угодно за твой счёт.")

    app = Application.builder().token(token).concurrent_updates(True).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("web", cmd_web))
    app.add_handler(CommandHandler("save", cmd_save))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
