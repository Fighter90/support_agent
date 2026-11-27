# ИИ-агент поддержки store.softline.ru

Прототип для хакатона **«ИИкузница»** (Софтлайн Решения), трек «Клиентские сценарии», задача «Агент поддержки».
ИИ-ассистент в чате интернет-магазина подбирает лицензионное ПО по реальному каталогу (136 тыс. позиций), сверяет версии,
отвечает на вопросы о покупке и после покупки, оформляет КП и счёт, а сложные случаи передаёт менеджеру.

| | |
|---|---|
| **Стенд** | https://methodagent.ru — чат клиента |
| **Кабинет менеджера** | https://methodagent.ru/manager (учётная запись для жюри — в описании продукта) |
| **API** | https://methodagent.ru/api/docs |
| **Модель** | GigaChat 2 Max (основная), YandexGPT Pro 5.1 (резерв) — серверы в РФ |
| **Материалы для жюри** | https://methodagent.ru/materials/ — видео, описание, расчёт, презентация, руководства, отчёты RAGAS |
| **Документация** | [docs/solution.md](docs/solution.md) · [docs/description.md](docs/description.md) · [docs/cost.xlsx](docs/cost.xlsx) · [презентация](docs/presentation/presentation.pdf) |
| **Руководства** | [пользователя](docs/guides/01_user_guide.pdf) · [менеджера и оператора](docs/guides/02_manager_guide.pdf) · [администратора](docs/guides/03_admin_guide.pdf) (есть DOCX) |
| **Качество** | RAGAS по фиду: recall 0,95 · precision 0,88 · faithfulness 0,90 · correctness 0,90 · цены 1,00 ([отчёт](eval/catalog_rag_report.json)); по базе знаний: faithfulness 0,91 ([отчёт](eval/rag_report.json)) |

## Что умеет

**Клиент (чат на сайте)**
- **Знает продукт** («нужен MDaemon Standard»): находит продуктовую линейку в фиде, сверяет версии и редакции (срок, число пользователей, тип организации, новая лицензия или продление), уточняет недостающее. Если точного названия нет — предлагает близкие продукты из каталога.
- **Не знает решения** («защитить почту и файлы на 50 сотрудников»): задаёт уточняющие вопросы, подбирает 1–2 варианта с обоснованием, находит российский аналог.
- **Вопросы о покупке и после покупки**: оплата, доставка, НДС, физлица, документы, личный кабинет, статус заказа, ключи, закрывающие документы — RAG по базе знаний (63 статьи + ответы менеджеров).
- **КП и счёт на оплату** по шаблону ПАО «Софтлайн»: реквизиты — в защищённой форме мимо модели, PDF сразу в чате.
- **Скидка до 5%** по просьбе клиента (в том числе вместо отсрочки) — правило заказчика; больше — только менеджер.
- **Кнопка «Позвать оператора»** и пометка «ИИ-ассистент» на каждом ответе.
- **Онлайн-оплата счёта** через MCP-сервер ЮKassa (на стенде — демо-режим без списания денег).
- **Поиск по каталогу на витрине** с подсказками и переходом к ассистенту.
- **Оценка ответа 👍/👎 и опрос** в конце диалога — на них агент учится.

**Менеджер (кабинет)**
- **Живой чат на WebSocket**: новые сообщения мгновенно, «печатает…», «Взять диалог» / «Вернуть агенту», быстрые ответы.
- Очередь эскалаций с причиной и резюме агента; ответ уходит клиенту в его чат.
- Отметка «добавить в базу знаний» — агент в следующий раз ответит сам.
- КП и счета: просмотр PDF, подтверждение, ссылка на оплату ЮKassa и статус оплаты.
- Отзывы 👎 → «Исправить и добавить в базу знаний»; CSAT и комментарии из опросов.
- Журнал всех диалогов: вызовы инструментов, токены, стоимость, задержка, замаскированные ПДн.
- Метрики: доля решённых без менеджера, эскалации по причинам, стоимость одного запроса, сценарии.

## Архитектура

```
Браузер клиента ──► /api/chat, /ws/chat ──► маскирование ПДн ──► агент (function calling, ≤ 6 шагов)
Браузер менеджера ─► /api/manager, /ws/manager         │  инструменты: search_catalog · get_product_variants ·
                                                        │  search_knowledge · create_quote · escalate_to_manager
                                                        ▼
                          GigaChat 2 Max ──(ошибка)──► YandexGPT Pro 5.1 ──(ошибка)──► режим без модели
SQLite: catalog.db (фид Mindbox, FTS5) · app.db (диалоги, эскалации, КП, база знаний, журнал)
Фид store.softline.ru/feeds/Mindbox.yml.gz — загрузка раз в сутки (13 с на 136 040 позиций)
Docker Compose: app (FastAPI + React) + Caddy (HTTPS) на одной ВМ Timeweb Cloud в РФ
```

**Почему ответам можно доверять**
- Цены, названия и ссылки — только из результатов инструментов. Каждая сумма в ответе проверяется кодом по фиду (допускается цена × количество); неподтверждённая цена → модель переписывает ответ.
- В КП и счёте цены и НДС считает код, а не модель; позиции без цены туда не попадают.
- Агент сам предлагает менеджера, если не уверен или данных нет.

## 152-ФЗ

Трансграничной передачи нет: GigaChat и YandexGPT работают на серверах в РФ, сервер приложения и БД — в РФ.
До отправки в модель телефоны, e-mail, ИНН, ФИО и номера документов заменяются метками `[ТЕЛ_1]`, `[EMAIL_1]`;
реквизиты для КП в модель не передаются. Подробно — [docs/solution.md, раздел 3](docs/solution.md#3-защита-данных-152-фз-и-трансграничная-передача).

## Структура

```
backend/app/
  agent.py      цикл агента, 5 инструментов, проверка цен, эскалация
  prompt.md     системный промпт (сценарии, коммерческие правила заказчика)
  catalog.py    загрузка фида (lxml.iterparse) и поиск (FTS5 + bm25), версии по group_id
  kb.py         база знаний (RAG), пополнение ответами менеджеров
  llm.py        адаптеры GigaChat (OAuth, functions) / YandexGPT (OpenAI-совместимый) / mock
  pii.py        маскирование ПДн
  quotes.py     КП и счёт на оплату (HTML → PDF, WeasyPrint)
  main.py       API клиента и менеджера, раздача интерфейса
  auth.py       JWT-вход менеджера
backend/tests/  pytest: ПДн, проверка цен, фид, поиск, база знаний, диалог, КП, скидка, счёт
frontend/src/   React + Vite: Store/Chat (клиент), Manager (кабинет) — фирменный стиль Softline
data/kb/        база знаний: оплата и доставка store.softline.ru, справка docs.softline.com, FAQ по перепискам
eval/           регресс на переписках (run_eval.py), RAGAS по каталогу (catalog_rag_eval.py) и базе знаний (rag_eval.py)
scripts/        build_kb.py, build_cost_xlsx.py (расчёт по шаблону организатора), build_guides.py, build_materials.py,
                e2e_realtime.py (сквозной тест), video/ (запись демо одним дублем: Playwright + озвучка + субтитры)
deploy/         Caddyfile
docs/           описание решения и для сдачи, расчёт стоимости, руководства (guides/), презентация, public/ — папка материалов
```

## Запуск

```bash
cp .env.example .env              # GIGACHAT_AUTH_KEY, GIGACHAT_SCOPE, пароли кабинета
docker compose up -d --build      # app + Caddy; при первом старте каталог загрузится из фида (~1 мин)
```

Локально без Docker:

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -r backend/requirements.txt
cd backend && ../.venv/bin/python -c "from app import catalog; catalog.ingest()"   # каталог из фида
../.venv/bin/uvicorn app.main:app --reload                                          # API на :8000
cd ../frontend && npm install --include=dev && npm run dev                          # интерфейс на :5173
```

Тесты и оценка:

```bash
cd backend && ../.venv/bin/python -m pytest -q tests
cd .. && .venv/bin/python eval/run_eval.py --n 60      # нужны переписки в data/raw/task/chats
.venv/bin/python eval/catalog_rag_eval.py 8          # RAGAS по каталогу из фида (40 вопросов, судья GigaChat)
.venv/bin/python eval/rag_eval.py                      # RAGAS по базе знаний
.venv/bin/python scripts/e2e_realtime.py https://methodagent.ru   # реалтайм, кабинет, оплата
.venv/bin/python scripts/build_cost_xlsx.py            # пересчёт docs/cost.xlsx по замерам
```

Демо-видео (один дубль на стенде, ускоряется только ожидание модели):

```bash
cd scripts/video && npm install && node login.mjs   # вход в кабинет выполняет человек
python3 make_audio.py && node record.mjs && python3 assemble.py   # → out/demo.mp4, out/demo.srt
```

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `GIGACHAT_AUTH_KEY`, `GIGACHAT_SCOPE`, `GIGACHAT_MODEL` | Ключ авторизации GigaChat API, scope (`GIGACHAT_API_PERS` / `_B2B` / `_CORP`), модель |
| `YANDEX_API_KEY`, `YANDEX_FOLDER_ID`, `YANDEX_MODEL` | Резервная модель YandexGPT |
| `LLM_PROVIDER`, `LLM_FALLBACK_PROVIDER` | `gigachat` / `yandex` / `mock` |
| `SECRET_KEY`, `MANAGER_LOGIN/PASSWORD`, `JURY_LOGIN/PASSWORD` | Кабинет менеджера |
| `FEED_URL` | Фид каталога (по умолчанию Mindbox.yml.gz store.softline.ru) |
| `YOOKASSA_MCP_TOKEN` | Токен магазина ЮKassa для MCP; пусто — демо-режим оплаты |

## Наработки и компоненты третьих лиц

- Собственная наработка автора [methodAgent](https://github.com/Fighter90/methodAgent) (GPL-3.0): перенесены и адаптированы схема настроек, подход к HTTP-клиенту LLM, JWT-авторизация, многостадийный Dockerfile, каркас React + Vite. Агент, поиск, КП, интерфейсы и база знаний написаны в ходе спринта.
- FastAPI, Uvicorn, Pydantic, httpx, lxml, PyJWT, WeasyPrint, openpyxl, React, React Router, Vite, lucide-react, Caddy, SQLite.
- Модели: GigaChat 2 Max (Сбер), YandexGPT Pro 5.1 (Яндекс). Ассистент разработки: Claude Code.

Автор: Емельянов Сергей.
