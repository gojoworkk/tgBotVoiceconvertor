# tgBotVoiceconvertor

Telegram-бот для себя и пары друзей: голосовое / аудио / видео → транскрипт → выжимка с кнопками форматов. Всё через **Gemini CLI** в headless-режиме (подписка Google пользователя), без платных API.

## Правила

- **В конце каждой итерации обязательно делать `git commit` и `git push`** в https://github.com/gojoworkk/tgBotVoiceconvertor. Без исключений.
- Репозиторий **публичный** (аккаунт не может создавать приватные из-за GitHub trade controls) — поэтому особенно следить, чтобы секреты не попадали в коммиты.
- Архитектура минималистичная: один файл `bot.py`, состояние в памяти, без БД, очередей и Docker, пока это реально не понадобится.
- Секреты только в `.env` (в `.gitignore`), никогда не коммитить токены.

## Запуск

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python bot.py
```
