"""Telegram-бот: голосовое / аудио / видео -> транскрипт -> выжимка через Gemini CLI (подписка Google)."""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
from collections import OrderedDict
from pathlib import Path

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

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "")
ALLOWED_USERS = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}

MAX_FILE_SIZE = 20 * 1024 * 1024  # лимит Telegram Bot API на скачивание файлов
MAX_STORED = 500  # сколько последних транскриптов держим в памяти
GEMINI_TIMEOUT = 600
TG_LIMIT = 4000
AUDIO_EXT = {"aac", "aiff", "flac", "m4a", "mov", "mp3", "mp4", "mpeg", "oga", "ogg", "wav", "webm"}

TRANSCRIBE_PROMPT = (
    "Расшифруй дословно речь из файла @{name}. Выведи только текст расшифровки на языке оригинала, "
    "без вступлений, комментариев и таймкодов. Если речи нет, выведи ровно: NO_SPEECH"
)
SYSTEM_PROMPT = (
    "Ты помогаешь быстро понять, о чём аудиозапись. Ниже (во входных данных) её транскрипт. "
    "Отвечай на языке транскрипта. Пиши простым текстом без Markdown-разметки "
    "(не используй *, #, _), списки оформляй символом «• ». Задание: "
)
FORMATS = {
    "short": ("📝 Коротко", "Сделай очень краткую выжимку: 2–3 предложения, только суть."),
    "points": ("📌 Тезисы", "Выдели ключевые тезисы списком, 5–10 пунктов."),
    "full": ("📖 Подробно", "Сделай подробный структурированный конспект: тема, основные части с подзаголовками, выводы."),
    "todo": ("✅ Задачи", "Выпиши договорённости, задачи, сроки и ответственных, если они упоминаются. Если ничего такого нет — так и скажи одной фразой."),
}


def gemini_command() -> list[str]:
    exe = shutil.which("gemini")
    if not exe:
        raise SystemExit("Не найден Gemini CLI: npm install -g @google/gemini-cli, затем один раз запусти `gemini` и войди в Google.")
    if os.name == "nt":  # на Windows .cmd/.ps1-обёртки ломают аргументы, запускаем бандл напрямую через node
        return [shutil.which("node"), str(Path(exe).parent / "node_modules/@google/gemini-cli/bundle/gemini.js")]
    return [exe]


GEMINI = gemini_command()
gemini_slots = asyncio.Semaphore(2)  # не больше двух процессов Gemini одновременно
dp = Dispatcher()
transcripts: OrderedDict[str, str] = OrderedDict()

if ALLOWED_USERS:
    dp.message.filter(F.from_user.id.in_(ALLOWED_USERS))
    dp.callback_query.filter(F.from_user.id.in_(ALLOWED_USERS))


async def run_gemini(prompt: str, cwd: str, stdin: str = "") -> str:
    # plan = режим только для чтения: модель не сможет запускать команды или менять файлы на компьютере
    args = [*GEMINI, "--skip-trust", "--approval-mode", "plan", "-o", "json", "-p", prompt]
    if GEMINI_MODEL:
        args += ["-m", GEMINI_MODEL]
    async with gemini_slots:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode("utf-8")), GEMINI_TIMEOUT)
        except TimeoutError:
            proc.kill()
            raise RuntimeError("Gemini не ответил за 10 минут")
    try:
        data = json.loads(out.decode("utf-8"))
    except ValueError:
        raise RuntimeError((err or out).decode("utf-8", "replace").strip()[-500:] or f"Gemini завершился с кодом {proc.returncode}")
    if "error" in data:
        raise RuntimeError(data["error"].get("message", data["error"]))
    return (data.get("response") or "").strip()


async def transcribe(data: bytes, name: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, name).write_bytes(data)
        text = await run_gemini(TRANSCRIBE_PROMPT.format(name=name), cwd=tmp)
    return "" if text == "NO_SPEECH" else text


async def summarize(text: str, fmt: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        return await run_gemini(SYSTEM_PROMPT + FORMATS[fmt][1], cwd=tmp, stdin=text)


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
    """Возвращает (файл, имя для Gemini) или (файл, None), если формат не поддерживается."""
    if message.voice:
        return message.voice, "voice.ogg"
    if message.video_note:
        return message.video_note, "video_note.mp4"
    media = message.audio or message.video or message.document
    name = getattr(media, "file_name", None) or ("video.mp4" if message.video else "audio.mp3")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return media, (f"media.{ext}" if ext in AUDIO_EXT else None)


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
        text = await transcribe(data.read(), name)
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
