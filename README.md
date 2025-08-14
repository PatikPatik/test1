
# Telegram Broker Bot — Render-ready (root layout)

Этот репозиторий содержит файлы **в корне**, чтобы Render запускал `python broker_bot.py` в `/opt/render/project/src`.

## Render (Blueprint)
- `render.yaml` уже настроен на rootDir: `.`
- Build: `pip install -r requirements.txt`
- Start: `python broker_bot.py`

## Переменные окружения
- `BOT_TOKEN`
- `ADMIN_IDS`
