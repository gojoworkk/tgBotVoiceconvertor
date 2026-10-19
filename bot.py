"""Telegram-бот: голосовое / аудио / видео -> транскрипт -> выжимка через OpenAI."""

import asyncio
import logging
import os
import uuid
from collections import OrderedDict

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "gpt-4o-mini")
ALLOWED_USERS = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}

MAX_FILE_SIZE = 20 * 1024 * 1024  # лимит Telegram Bot API на скачивание файлов
MAX_STORED = 500  # сколько последних транскриптов держим в памяти
TG_LIMIT = 4000
AUDIO_EXT = {"flac", "m4a", "mp3", "mp4", "mpeg", "mpga", "oga", "ogg", "wav", "webm"}

SYSTEM_PROMPT = (
    "Ты помогаешь быстро понять, о чём аудиозапись. Тебе дают её транскрипт. "
    "Отвечай на языке транскрипта. Пиши простым текстом без Markdown-разметки "
    "(не используй *, #, _), списки оформляй символом «• »."
)
FORMATS = {
    "short": ("📝 Коротко", "Сделай очень краткую выжимку: 2–3 предложения, только суть."),
    "points": ("📌 Тезисы", "Выдели ключевые тезисы списком, 5–10 пунктов."),
    "full": ("📖 Подробно", "Сделай подробный структурированный конспект: тема, основные части с подзаголовками, выводы."),
    "todo": ("✅ Задачи", "Выпиши договорённости, задачи, сроки и ответственных, если они упоминаются. Если ничего такого нет — так и скажи одной фразой."),
}

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
dp = Dispatcher()
transcripts: OrderedDict[str, str] = OrderedDict()

if ALLOWED_USERS:
    dp.message.filter(F.from_user.id.in_(ALLOWED_USERS))
    dp.callback_query.filter(F.from_user.id.in_(ALLOWED_USERS))


def remember(text: str) -> str:
    key = uuid.uuid4().hex[:12]
    transcripts[key] = text
    while len(transcripts) > MAX_STORED:
        transcripts.popitem(last=False)
    return key


def keyboard(key: str) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=title, callback_data=f"{fmt}:{key}") for fmt, (title, _) in FORMATS.items()]
    buttons.append(InlineKeyboardButton(text="📄 Весь текст", callback_data=f"text:{key}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i:i + 2] for i in range(0, len(buttons), 2)])


def pick_media(message: Message):
    """Возвращает (файл, имя для OpenAI) или (файл, None), если формат не поддерживается."""
    if message.voice:
        return message.voice, "voice.ogg"
    if message.video_note:
        return message.video_note, "video_note.mp4"
    media = message.audio or message.video or message.document
    name = getattr(media, "file_name", None) or ("video.mp4" if message.video else "audio.mp3")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return media, (name if ext in AUDIO_EXT else None)


async def summarize(text: str, fmt: str) -> str:
    response = await client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{FORMATS[fmt][1]}\n\nТранскрипт:\n{text}"},
        ],
    )
    return response.choices[0].message.content.strip()


async def send_long(message: Message, text: str) -> None:
    for i in range(0, len(text), TG_LIMIT):
        await message.answer(text[i:i + TG_LIMIT])


@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "Пришли или перешли мне голосовое, кружок, аудио или видео (mp3, mp4, m4a, wav, ogg…) до 20 МБ — "
        "я расшифрую и сделаю выжимку. Под ответом будут кнопки с другими форматами."
    )


@dp.message(F.voice | F.audio | F.video | F.video_note | F.document)
async def on_media(message: Message, bot: Bot) -> None:
    media, name = pick_media(message)
    if name is None:
        await message.reply("Это не похоже на аудио или видео. Поддерживаю: " + ", ".join(sorted(AUDIO_EXT)))
        return
    if media.file_size and media.file_size > MAX_FILE_SIZE:
        await message.reply("Файл больше 20 МБ — Telegram не даёт ботам скачивать такие. Сожми или обрежь запись.")
        return

    status = await message.reply("⏳ Слушаю…")
    try:
        data = await bot.download(media)
        result = await client.audio.transcriptions.create(model=TRANSCRIBE_MODEL, file=(name, data.read()))
        text = result.text.strip()
        if not text:
            await status.edit_text("🤷 Не удалось разобрать речь в этой записи.")
            return
        key = remember(text)
        await status.edit_text("🧠 Делаю выжимку…")
        summary = await summarize(text, "short")
        await status.edit_text(f"{FORMATS['short'][0]}\n\n{summary}"[:TG_LIMIT], reply_markup=keyboard(key))
    except Exception as e:
        logging.exception("Ошибка обработки файла")
        await status.edit_text(f"❌ Ошибка: {e}"[:TG_LIMIT])


@dp.callback_query(F.data.contains(":"))
async def on_format(call: CallbackQuery) -> None:
    fmt, key = call.data.split(":", 1)
    text = transcripts.get(key)
    if text is None:
        await call.answer("Запись устарела (бот перезапускался) — пришли файл ещё раз.", show_alert=True)
        return
    await call.answer("Готовлю…")
    try:
        if fmt == "text":
            if len(text) > TG_LIMIT:
                await call.message.answer_document(BufferedInputFile(text.encode("utf-8"), "transcript.txt"))
            else:
                await call.message.answer(text)
        elif fmt in FORMATS:
            await send_long(call.message, f"{FORMATS[fmt][0]}\n\n{await summarize(text, fmt)}")
    except Exception as e:
        logging.exception("Ошибка генерации выжимки")
        await call.message.answer(f"❌ Ошибка: {e}"[:TG_LIMIT])


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(Bot(TELEGRAM_BOT_TOKEN))


if __name__ == "__main__":
    asyncio.run(main())
