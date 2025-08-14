# Telegram Broker Bot (Construction Services) — MVP

Посредник между заказчиком и исполнителями строительной техники/бригад.
Контакты скрыты до принятия оффера. Без платежей. Геофильтр. Приоритет «своих».

## Быстрый старт (локально)
1. Python 3.11+
2. Создайте `.env` (см. `.env.example`).
3. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```
4. Запуск:
   ```bash
   python broker_bot.py
   ```

## Переменные окружения
- `BOT_TOKEN` — токен бота от @BotFather
- `ADMIN_IDS` — список Telegram ID админов через запятую (например: `123,456`)

Пример: см. `.env.example`

## Команды
- `/start` — выбор роли (заказчик/исполнитель/админ)
- `/new_request` — создать заявку (категория → ТЗ → геолокация → радиус)
- `/me` — профиль исполнителя
- `/admin` — помощь по админ-командам

### Админ-команды
- `/admin prefer_owner on|off` — приоритет «своих» (is_owner) при рассылке
- `/admin add_executor @username "Город" 50 "кат1,кат2" [--owner]` — добавить исполнителя
- `/admin list_exec` — список исполнителей
- `/admin set_loc <exec_id>` — затем ответьте сообщением с геолокацией
- `/admin assign <request_id> <executor_id>` — вручную назначить заявку

## Геофильтр
Кандидат должен:
1) Иметь нужную категорию
2) Быть в своём радиусе обслуживания
3) Находиться в радиусе поиска клиента

## Деплой на Render (polling)
Используйте **Background Worker** (без порта). Варианты:

### A) Через Blueprint
1. Залейте репозиторий в GitHub (содержимое этого архива).
2. В Render: **New → Blueprint** и укажите свою репу.
3. Render прочитает `render.yaml` и создаст Worker.
4. Задайте `BOT_TOKEN` и `ADMIN_IDS` в **Environment**.
5. Deploy.

### B) Через Worker вручную
1. New → **Background Worker**
2. Build Command: `pip install -r requirements.txt`
3. Start Command: `python broker_bot.py`
4. Environment:
   - `BOT_TOKEN=...`
   - `ADMIN_IDS=...`

> БД — SQLite `broker.db` (эпhemeral на Render free). Для продакшена перенесите на PostgreSQL.

## Замечания
- Это MVP для проверки гипотезы. Логика анти-обхода: маскируем контакты в текстах.
- Следующий шаг: миграция на PostgreSQL + webhooks, роли доступа, отчёты.