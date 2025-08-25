# Mogilev Build Broker Bot — Inline-first (Aiogram v3.7+)

Упрощённый бот с инлайн-кнопками под аудиторию 50+: простой выбор даты/времени, адрес текстом, звонок по кнопке, оставить номер текстом.

## Быстрый запуск

1) Python 3.11+
2) Установить зависимости:
   ```bash
   pip install -r requirements.txt
   ```
3) Создать `.env` по `.env.example` (указать `BOT_TOKEN`, телефон диспетчера и т.д.)
4) Запуск:
   ```bash
   python main.py
   ```

## Render (Background Worker)

- Build: `pip install -r requirements.txt`
- Start: `python main.py`
- Env: `BOT_TOKEN`, `SUPPORT_PHONE`, `SUPPORT_NAME`, `ADMIN_IDS`, `PHONE_SHARE_RATE_LIMIT`, `COMMISSION_PCT`

## Что изменено
- Все действия — инлайн-кнопками.
- Даты: Сегодня/Завтра/7 дней + слоты 09:00/13:00/18:00, «Другое» — ввести `10:30`.
- «Связаться»: кнопка tel: + ввод номера цифрами (без request_contact).
- Короткие подсказки.

> Это MVP с хранением в памяти. Для продакшена — Postgres, SLA-таймеры, push-рассылка.
