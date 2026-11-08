# CASEFORGE (musor_drop)

Браузерная игра про кейсы: открывай кейсы, собирай инвентарь, играй в мини-игры и поднимайся в рейтинге. Работает как PWA — можно «установить» на телефон.

## Возможности

- **Кейсы** — платные и бесплатные, с шансами выпадения и анимацией рулетки
- **Мини-игры** — Mines, Ракета, Кости
- **Батлы, апгрейд, контракт, обмен** между игроками
- **Бонусы** — колесо удачи, ежедневная серия, задания, промокоды
- **Рейтинги** по периодам, живая лента дропов
- Адаптивный интерфейс: на телефоне — нижняя панель навигации и шторка «Ещё»

## Стек

- Бэкенд: Python, FastAPI, PostgreSQL (`asyncpg`), JWT
- Фронтенд: один файл `static/index.html` (HTML + CSS + JS без сборки)

## Запуск

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://user:pass@host/dbname   # Windows: set DATABASE_URL=...
uvicorn server:app --host 0.0.0.0 --port 8000
```

После запуска сайт доступен на <http://localhost:8000>.

## Структура

```
server.py            API и раздача статики
data/game_data.json  данные игры
static/index.html    интерфейс
static/manifest.json PWA-манифест
static/service-worker.js
```
