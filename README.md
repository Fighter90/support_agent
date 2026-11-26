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
| **Документация** | [docs/solution.md](docs/solution.md) · [docs/description.md](docs/description.md) · [docs/cost.xlsx](docs/cost.xlsx) · [docs/video_script.md](docs/video_script.md) |

## Что умеет

**Клиент (чат на сайте)**
- **Знает продукт** («нужен MDaemon Standard»): находит продуктовую линейку в фиде, сверяет версии и редакции (срок, число пользователей, тип организации, новая лицензия или продление), уточняет недостающее. Если точного названия нет — предлагает близкие продукты из каталога.
- **Не знает решения** («защитить почту и файлы на 50 сотрудников»): задаёт уточняющие вопросы, подбирает 1–2 варианта с обоснованием, находит российский аналог.
- **Вопросы о покупке и после покупки**: оплата, доставка, НДС, физлица, документы, личный кабинет, статус заказа, ключи, закрывающие документы — RAG по базе знаний (65 статей).
- **КП и счёт на оплату** по шаблону ПАО «Софтлайн»: реквизиты — в защищённой форме мимо модели, PDF сразу в чате.
- **Скидка до 5%** по просьбе клиента (в том числе вместо отсрочки) — правило заказчика; больше — только менеджер.
- **Кнопка «Позвать оператора»** и пометка «ИИ-ассистент» на каждом ответе.

**Менеджер (кабинет)**
- Очередь эскалаций с причиной и резюме агента; ответ уходит клиенту в его чат.
- Отметка «добавить в базу знаний» — агент в следующий раз ответит сам.
- КП и счета: просмотр PDF, подтверждение.
- Журнал всех диалогов: вызовы инструментов, токены, стоимость, задержка, замаскированные ПДн.
- Метрики: доля решённых без менеджера, эскалации по причинам, стоимость одного запроса, сценарии.

## Архитектура

```
Браузер клиента ──► /api/chat ──► маскирование ПДн ──► агент (function calling, ≤ 6 шагов)
Браузер менеджера ─► /api/manager                      │  инструменты: search_catalog · get_product_variants ·
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
eval/           оценка на обезличенных переписках (run_eval.py, report.json)
scripts/        build_kb.py (сборка базы знаний), build_cost_xlsx.py (расчёт стоимости по шаблону организатора)
deploy/         Caddyfile
docs/           описание решения, описание для сдачи, расчёт стоимости, сценарий видео
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
.venv/bin/python scripts/build_cost_xlsx.py            # пересчёт docs/cost.xlsx по замерам
```

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `GIGACHAT_AUTH_KEY`, `GIGACHAT_SCOPE`, `GIGACHAT_MODEL` | Ключ авторизации GigaChat API, scope (`GIGACHAT_API_PERS` / `_B2B` / `_CORP`), модель |
| `YANDEX_API_KEY`, `YANDEX_FOLDER_ID`, `YANDEX_MODEL` | Резервная модель YandexGPT |
| `LLM_PROVIDER`, `LLM_FALLBACK_PROVIDER` | `gigachat` / `yandex` / `mock` |
| `SECRET_KEY`, `MANAGER_LOGIN/PASSWORD`, `JURY_LOGIN/PASSWORD` | Кабинет менеджера |
| `FEED_URL` | Фид каталога (по умолчанию Mindbox.yml.gz store.softline.ru) |

## Наработки и компоненты третьих лиц

- Собственная наработка автора [methodAgent](https://github.com/Fighter90/methodAgent) (GPL-3.0): перенесены и адаптированы схема настроек, подход к HTTP-клиенту LLM, JWT-авторизация, многостадийный Dockerfile, каркас React + Vite. Агент, поиск, КП, интерфейсы и база знаний написаны в ходе спринта.
- FastAPI, Uvicorn, Pydantic, httpx, lxml, PyJWT, WeasyPrint, openpyxl, React, React Router, Vite, lucide-react, Caddy, SQLite.
- Модели: GigaChat 2 Max (Сбер), YandexGPT Pro 5.1 (Яндекс). Ассистент разработки: Claude Code.

Автор: Емельянов Сергей.
