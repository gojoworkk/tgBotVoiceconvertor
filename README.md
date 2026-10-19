# Voice → Summary Telegram Bot

Перешли боту голосовое, кружок, mp3/mp4/m4a/wav/ogg (до 20 МБ) — он расшифрует запись через OpenAI и сразу пришлёт короткую выжимку. Под ответом кнопки: **Коротко / Тезисы / Подробно / Задачи / Весь текст**.

## Настройка

1. `copy .env.example .env` и заполнить `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `ALLOWED_USERS` (свой id можно узнать у @userinfobot).
2. Установить и запустить:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python bot.py
```

## Как устроено

- `bot.py` — всё в одном файле: aiogram 3 (long polling), OpenAI `gpt-4o-mini-transcribe` для распознавания и `gpt-4o-mini` для выжимок (меняются через `.env`).
- Транскрипты хранятся в памяти (последние 500). После перезапуска старые кнопки перестают работать — достаточно переслать файл заново.
- Ограничение 20 МБ — это лимит Telegram Bot API на скачивание файлов ботом.
