---
title: "ИИ-консультант Softline — руководство администратора"
subtitle: "Развёртывание, настройка, обновление, мониторинг и безопасность"
---

# 1. Состав системы

| Компонент | Технология | Назначение |
|---|---|---|
| app | Python 3.12, FastAPI, Uvicorn | API, агент, WebSocket, раздача фронтенда |
| Фронтенд | React 19, Vite, TypeScript | Витрина с чатом и кабинет менеджера |
| catalog.db | SQLite FTS5 (BM25) | Индекс каталога из фида Mindbox (~136 тыс. позиций) — источник RAG по товарам |
| app.db | SQLite (WAL) | Диалоги, эскалации, КП, отзывы, база знаний, журналы |
| LLM | GigaChat 2 Max (основная), YandexGPT Pro 5.1 (резерв) | Ответы и вызовы инструментов |
| PDF | WeasyPrint | КП и счета по шаблону ПАО «Софтлайн» |
| Оплата | ЮKassa MCP (`https://mcp.yookassa.ru`) | Создание и проверка платежей |
| caddy | Caddy 2 | HTTPS (Let's Encrypt), обратный прокси, WebSocket |

Минимальные требования к серверу: 1 vCPU, 1 ГБ RAM и 2 ГБ swap, 5 ГБ диска, Ubuntu 22.04 или новее, Docker и Docker Compose.

# 2. Установка

```bash
git clone https://github.com/Fighter90/support_agent.git /opt/support_agent
cd /opt/support_agent
cp .env.example .env        # заполните ключи и пароли (раздел 3)
docker compose up -d --build
docker compose logs -f app  # первый запуск загружает фид каталога (~1 мин)
```

A-запись домена должна указывать на сервер, сертификат Caddy выпустит сам. Домен задаётся в `deploy/Caddyfile` и в `PUBLIC_URL`.

# 3. Настройки (.env)

| Переменная | Назначение |
|---|---|
| `PUBLIC_URL` | Внешний адрес стенда (ссылки на оплату и PDF) |
| `LLM_PROVIDER` | `gigachat` (по умолчанию) или `yandex` |
| `GIGACHAT_AUTH_KEY` | Authorization key из личного кабинета developers.sber.ru |
| `GIGACHAT_SCOPE` | `GIGACHAT_API_PERS` (физлицо), `GIGACHAT_API_B2B` или `GIGACHAT_API_CORP` |
| `GIGACHAT_MODEL` | `GigaChat-2-Max` |
| `LLM_FALLBACK_PROVIDER` | Резервная модель (`yandex`) или пусто |
| `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` | Ключ сервисного аккаунта (роль `ai.languageModels.user`) и каталог |
| `SECRET_KEY` | Секрет подписи токенов входа (длинная случайная строка) |
| `MANAGER_LOGIN`, `MANAGER_PASSWORD` | Учётная запись менеджера |
| `JURY_LOGIN`, `JURY_PASSWORD` | Учётная запись только для просмотра |
| `YOOKASSA_MCP_TOKEN` | Токен магазина ЮKassa для MCP. Если пусто, оплата работает в демо-режиме |
| `LLM_ALLOW_MOCK` | В продакшене `false` (без заглушки модели) |

После изменения `.env` выполните `docker compose up -d app`. Сертификат НУЦ Минцифры, нужный для GigaChat, уже встроен в образ.

# 4. Обновление кода и данных

```bash
cd /opt/support_agent && git pull
docker compose build app && docker compose up -d app
```

- **Данные** (`data/runtime`: базы и PDF) лежат в томе Docker и при обновлении сохраняются.
- **Каталог (RAG по товарам).** Фид `https://store.softline.ru/feeds/Mindbox.yml.gz` загружается при старте и затем автоматически раз в сутки. Новый индекс строится во временном файле и подменяет старый атомарно, так что агент не останавливается. Принудительное обновление: `POST /api/manager/catalog/reload` с токеном менеджера.
- **База знаний (RAG по статьям).** Статьи из `data/kb/*.md` загружаются при старте. Статьи, добавленные менеджерами из эскалаций и отзывов, хранятся в `app.db` и начинают работать сразу.

# 5. Подключение оплаты ЮKassa

1. Зарегистрируйте магазин в ЮKassa и получите токен для MCP (https://yookassa.ru/developers/using-api/mcp).
2. Впишите его в `YOOKASSA_MCP_TOKEN` и перезапустите `app`.
3. Проверьте: `GET /api/health` должен вернуть `"payments_mode": "mcp"` (без токена — `"demo"`).

# 6. Мониторинг

- `GET /api/health` — состояние каталога (число позиций, время загрузки), модели и режима оплаты.
- `docker compose logs app --since 1h` — журналы.
- Кабинет менеджера → «Метрики» — диалоги, эскалации, CSAT, токены и стоимость.
- Таблицы `llm_calls` и `tool_calls` в `app.db` — подробный журнал вызовов модели и инструментов (без ПДн).
- Резервная копия раз в сутки по cron: `sqlite3 data/runtime/app.db ".backup /backup/app-$(date +%F).db"`.

# 7. Проверка качества

```bash
.venv/bin/python -m pytest -q backend/tests          # модульные тесты
.venv/bin/python eval/run_eval.py                    # 51 обращение: маршрутизация, задержка, стоимость
.venv/bin/python eval/rag_eval.py                    # RAGAS по базе знаний
.venv/bin/python eval/catalog_rag_eval.py 6          # RAGAS по каталогу из фида
.venv/bin/python scripts/e2e_realtime.py https://methodagent.ru   # сквозной тест реалтайма и оплаты
```

Проверки качества запускайте после каждого изменения промпта, модели или поиска. Метрики RAGAS не должны падать относительно отчётов `eval/*_report.json` в репозитории.

# 8. Безопасность и 152-ФЗ

- Все компоненты и модель работают в РФ: сервер Timeweb, GigaChat (ПАО Сбербанк). Трансграничной передачи данных нет.
- Персональные данные (телефон, e-mail, ИНН, КПП, ОГРН, карта, паспорт, ФИО) маскируются до отправки в модель. Оригиналы хранятся в таблице `pii` и подставляются только в ответ клиенту.
- `.env` не попадает в репозиторий. Меняйте пароли кабинета перед каждым показом.
- Число попыток входа ограничено. Служебные пути вроде `/.env` возвращают 404.
- На сервер заходите по SSH только с ключом, вход по паролю root отключите.
- Чтобы удалить данные клиента, удалите строки по `conv_id` из `messages`, `pii`, `quotes` и `escalations`.

# 9. Типовые проблемы

| Симптом | Причина и решение |
|---|---|
| Ассистент отвечает «не получается подобрать ответ» | Модель недоступна (429/5xx): проверьте ключ, scope и лимиты GigaChat, включите резервный YandexGPT |
| OAuth 400 «scope …» | `GIGACHAT_SCOPE` не соответствует типу ключа |
| В каталоге 0 позиций | Нет доступа к фиду: проверьте сеть и вызовите обновление каталога |
| PDF не формируется | Нет библиотек pango: используйте Docker-образ |
| Кабинет не обновляется сам | Прокси не пропускает WebSocket. Caddy пропускает его без настроек, в nginx добавьте заголовки `Upgrade` и `Connection` |
