"""Команда поддержки: администратор и менеджеры, статусы «в сети», автоматическое распределение обращений.

Роли:
  admin   — создаёт и отключает менеджеров, задаёт нагрузку и правила распределения, видит работу всей команды;
            может и сам вести диалоги (по умолчанию не получает обращения автоматически);
  manager — ведёт диалоги: получает обращения по очереди, берёт, передаёт коллеге, зовёт коллегу в диалог.

Распределение (dispatch, раз в 3 с и сразу после эскалации):
  1. очередь — открытые эскалации без исполнителя, по времени создания (FIFO);
  2. исполнитель — менеджер «в сети» с автоназначением, у которого нагрузка (активные диалоги + назначенные обращения)
     меньше лимита; из них — с наименьшей нагрузкой, при равенстве — дольше всех без нового обращения (round-robin);
  3. никого нет — обращение ждёт в очереди и видно всем; уведомление в Telegram получают все подключённые чаты;
  4. назначенный не взял обращение за время переназначения (по умолчанию 3 мин) — оно уходит следующему;
  5. менеджер ушёл «не в сети» или закрыл кабинет (нет сигнала дольше 15 мин и нет Telegram) — его невзятые обращения
     возвращаются в очередь.
Взять обращение может любой менеджер вручную — назначение лишь подсказка, кто отвечает первым.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from typing import Any

from app import db

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  login TEXT PRIMARY KEY, name TEXT, role TEXT DEFAULT 'manager',      -- admin | manager
  pw_hash TEXT DEFAULT '', active INTEGER DEFAULT 1,
  status TEXT DEFAULT 'offline',                                       -- online | away | offline
  routing INTEGER DEFAULT 1,                                           -- получать обращения автоматически
  max_load INTEGER DEFAULT 5,                                          -- одновременно диалогов/обращений
  last_seen REAL, last_assigned REAL DEFAULT 0, invite TEXT DEFAULT '', invite_until REAL DEFAULT 0,
  created_by TEXT DEFAULT '', created_at REAL
);
CREATE TABLE IF NOT EXISTS conv_managers (                             -- кто участвует в диалоге: owner ведёт, helper помогает
  conv_id TEXT, login TEXT, role TEXT DEFAULT 'owner', joined_at REAL, PRIMARY KEY (conv_id, login)
);
CREATE TABLE IF NOT EXISTS team_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, esc_id INTEGER, action TEXT, actor TEXT, target TEXT, created_at REAL
);
"""
MIGRATIONS = [
    "ALTER TABLE escalations ADD COLUMN assignee TEXT DEFAULT ''",
    'ALTER TABLE escalations ADD COLUMN assigned_at REAL',
    'ALTER TABLE escalations ADD COLUMN reassigns INTEGER DEFAULT 0',
    "ALTER TABLE conversations ADD COLUMN manager_login TEXT DEFAULT ''",
]
PRESENCE_TIMEOUT = 15 * 60      # без сигнала из кабинета дольше — считаем «не в сети» (если нет Telegram)
INVITE_TTL = 72 * 3600
STATUSES = {'online': 'В сети', 'away': 'Перерыв', 'offline': 'Не в сети'}
_lock = threading.RLock()


# ------------------------------------------------------------------ пароли и учётки

def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(8)
    return f'pbkdf2${salt}${hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()}'


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split('$')
    except ValueError:
        return False
    return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000).hex(), h)


def create_schema(c: Any) -> None:
    """Таблицы команды; вызывается и из db.init_db, чтобы схема была всегда (в том числе в тестах)."""
    c.executescript(SCHEMA)
    for sql in MIGRATIONS:
        try:
            c.execute(sql)
        except Exception:  # noqa: BLE001 — колонка уже есть
            pass


def init() -> None:
    """Таблицы и учётки из окружения: MANAGER_LOGIN — администратор (руководитель поддержки), JURY_LOGIN — администратор для жюри."""
    from app.config import get_settings

    with db.conn() as c:
        create_schema(c)
    s = get_settings()
    for login, pw, name in ((s.manager_login, s.manager_password, 'Менеджер Softline'), (s.jury_login, s.jury_password, 'Жюри ИИкузницы')):
        u = get(login)
        if not u:
            db.execute('INSERT INTO users (login, name, role, pw_hash, routing, created_by, created_at) VALUES (?,?,?,?,?,?,?)',
                       (login, name, 'admin', hash_pw(pw), 0, 'env', db.now()))
        elif not check_pw(pw, u['pw_hash']):  # пароль из .env — источник истины для этих двух учёток
            db.execute("UPDATE users SET pw_hash=?, role='admin' WHERE login=?", (hash_pw(pw), login))
    # диалоги и Telegram-чаты, взятые до появления учёток, — за администратором
    db.execute("UPDATE conversations SET manager_login=? WHERE mode='manager' AND (manager_login IS NULL OR manager_login='')", (s.manager_login,))


def get(login: str) -> dict[str, Any] | None:
    return db.one('SELECT * FROM users WHERE login=?', (login,))


def public(u: dict[str, Any]) -> dict[str, Any]:
    return {k: u.get(k) for k in ('login', 'name', 'role', 'active', 'status', 'routing', 'max_load', 'last_seen', 'created_at', 'created_by')} | {
        'online': is_online(u), 'has_password': bool(u.get('pw_hash')), 'status_ru': STATUSES.get(u.get('status') or 'offline', '—')}


def authenticate(login: str, pw: str) -> dict[str, Any] | None:
    u = get(login)
    if not u or not u['active'] or not u['pw_hash'] or not check_pw(pw, u['pw_hash']):
        return None
    return u


def create(login: str, name: str, role: str, max_load: int, routing: bool, by: str) -> dict[str, Any]:
    login = login.strip().lower()
    if get(login):
        raise ValueError('Логин уже занят')
    db.execute('INSERT INTO users (login, name, role, max_load, routing, created_by, created_at) VALUES (?,?,?,?,?,?,?)',
               (login, name.strip(), role, max_load, int(routing), by, db.now()))
    return {**public(get(login)), **new_invite(login)}


def new_invite(login: str) -> dict[str, Any]:
    """Одноразовая ссылка входа для нового менеджера (72 ч): пароль он задаёт сам в профиле — администратор его не знает."""
    code = secrets.token_urlsafe(18)
    db.execute('UPDATE users SET invite=?, invite_until=? WHERE login=?', (hashlib.sha256(code.encode()).hexdigest(), db.now() + INVITE_TTL, login))
    return {'invite_code': code, 'invite_expires': db.now() + INVITE_TTL}


def use_invite(code: str) -> dict[str, Any] | None:
    h = hashlib.sha256(code.encode()).hexdigest()
    u = db.one('SELECT * FROM users WHERE invite=? AND active=1', (h,))
    if not u or u['invite_until'] < db.now():
        return None
    db.execute("UPDATE users SET invite='', invite_until=0 WHERE login=?", (u['login'],))
    return u


def update(login: str, **f: Any) -> dict[str, Any]:
    allowed = {k: v for k, v in f.items() if k in ('name', 'role', 'max_load', 'routing', 'active', 'status') and v is not None}
    if allowed:
        db.execute(f'UPDATE users SET {", ".join(k + "=?" for k in allowed)} WHERE login=?', (*allowed.values(), login))
    if allowed.get('active') == 0 or allowed.get('status') in ('offline', 'away'):
        release(login)
    dispatch()
    return public(get(login))


def set_password(login: str, pw: str) -> None:
    db.execute('UPDATE users SET pw_hash=? WHERE login=?', (hash_pw(pw), login))


# ------------------------------------------------------------------ присутствие и нагрузка

def _has_telegram(login: str) -> bool:
    try:
        return bool(db.one('SELECT 1 AS x FROM tg_chats WHERE login=?', (login,)))
    except Exception:  # noqa: BLE001 — таблица Telegram ещё не создана
        return False


def is_online(u: dict[str, Any]) -> bool:
    if not u.get('active') or u.get('status') != 'online':
        return False
    return (db.now() - (u.get('last_seen') or 0) < PRESENCE_TIMEOUT) or _has_telegram(u['login'])


def heartbeat(login: str, status: str | None = None) -> dict[str, Any]:
    if status and status in STATUSES:
        db.execute('UPDATE users SET status=?, last_seen=? WHERE login=?', (status, db.now(), login))
        if status != 'online':
            release(login)
    else:
        db.execute('UPDATE users SET last_seen=? WHERE login=?', (db.now(), login))
    dispatch()
    return public(get(login))


def load(login: str) -> int:
    a = db.one("SELECT count(*) AS n FROM conversations WHERE mode='manager' AND manager_login=?", (login,))['n']
    b = db.one("SELECT count(*) AS n FROM escalations WHERE status='open' AND assignee=?", (login,))['n']
    return a + b


def roster() -> list[dict[str, Any]]:
    """Команда для кабинета: статус, нагрузка, сколько взято сегодня."""
    day = time.time() - 86400
    out = []
    for u in db.rows('SELECT * FROM users ORDER BY role, name'):
        p = public(u)
        p['load'] = load(u['login'])
        p['assigned_open'] = db.one("SELECT count(*) AS n FROM escalations WHERE status='open' AND assignee=?", (u['login'],))['n']
        p['active_dialogs'] = db.one("SELECT count(*) AS n FROM conversations WHERE mode='manager' AND manager_login=?", (u['login'],))['n']
        p['answered_24h'] = db.one('SELECT count(*) AS n FROM escalations WHERE answered_at>? AND manager=?', (day, u['name']))['n']
        p['telegram'] = _has_telegram(u['login'])
        out.append(p)
    return out


def name_of(login: str) -> str:
    u = get(login)
    return (u or {}).get('name') or login


# ------------------------------------------------------------------ распределение

def setting(key: str, default: int) -> int:
    r = db.one('SELECT value FROM settings WHERE key=?', (key,))
    try:
        return int(float(r['value'])) if r else default
    except (TypeError, ValueError):
        return default


def auto_assign() -> bool:
    return bool(setting('auto_assign', 1))


def reassign_seconds() -> int:
    return setting('reassign_seconds', 180)


def _log(conv_id: str, esc_id: int | None, action: str, actor: str, target: str = '') -> None:
    db.execute('INSERT INTO team_log (conv_id, esc_id, action, actor, target, created_at) VALUES (?,?,?,?,?,?)',
               (conv_id, esc_id, action, actor, target, db.now()))


def _candidates(exclude: set[str] = frozenset()) -> list[dict[str, Any]]:
    out = []
    for u in db.rows("SELECT * FROM users WHERE active=1 AND routing=1 AND status='online'"):
        if u['login'] in exclude or not is_online(u):
            continue
        n = load(u['login'])
        if n < (u['max_load'] or 1):
            out.append({**u, 'load': n})
    return sorted(out, key=lambda u: (u['load'], u['last_assigned'] or 0))


def assign(esc: dict[str, Any], login: str, actor: str = 'система') -> None:
    db.execute('UPDATE escalations SET assignee=?, assigned_at=? WHERE id=?', (login, db.now(), esc['id']))
    db.execute('UPDATE users SET last_assigned=? WHERE login=?', (db.now(), login))
    _log(esc['conv_id'], esc['id'], 'assign', actor, login)


def release(login: str) -> int:
    """Менеджер ушёл: его невзятые обращения — обратно в очередь."""
    rs = db.rows("SELECT id, conv_id FROM escalations WHERE status='open' AND assignee=?", (login,))
    for e in rs:
        db.execute("UPDATE escalations SET assignee='', assigned_at=NULL WHERE id=?", (e['id'],))
        _log(e['conv_id'], e['id'], 'release', 'система', login)
    return len(rs)


def dispatch() -> int:
    """Назначает обращения из очереди; переназначает просроченные. Возвращает число назначений."""
    if not auto_assign():
        return 0
    n = 0
    with _lock:
        # ушедшие из сети (закрыли кабинет без Telegram) — освобождаем
        for u in db.rows("SELECT * FROM users WHERE status='online'"):
            if not is_online(u):
                release(u['login'])
        timeout = reassign_seconds()
        for e in db.rows("SELECT * FROM escalations WHERE status='open' AND assignee!='' AND assigned_at < ?", (db.now() - timeout,)):
            c = _candidates(exclude={e['assignee']})
            if c:
                db.execute('UPDATE escalations SET reassigns=reassigns+1 WHERE id=?', (e['id'],))
                _log(e['conv_id'], e['id'], 'timeout', 'система', e['assignee'])
                assign(e, c[0]['login'])
                n += 1
        for e in db.rows("SELECT * FROM escalations WHERE status='open' AND (assignee IS NULL OR assignee='') ORDER BY id"):
            # диалог уже ведёт менеджер — обращение за ним
            conv = db.one("SELECT mode, manager_login FROM conversations WHERE id=?", (e['conv_id'],)) or {}
            if conv.get('mode') == 'manager' and conv.get('manager_login'):
                assign(e, conv['manager_login'])
                n += 1
                continue
            c = _candidates()
            if not c:
                break
            assign(e, c[0]['login'])
            n += 1
    return n


def start() -> None:
    def loop() -> None:
        while True:
            try:
                dispatch()
            except Exception:  # noqa: BLE001
                logger.exception('Ошибка распределения обращений')
            time.sleep(3)

    threading.Thread(target=loop, name='team-dispatch', daemon=True).start()


# ------------------------------------------------------------------ участники диалога

def participants(cid: str) -> list[dict[str, Any]]:
    rs = db.rows('SELECT p.login, p.role, p.joined_at, u.name FROM conv_managers p LEFT JOIN users u ON u.login=p.login WHERE p.conv_id=? ORDER BY p.joined_at', (cid,))
    return rs


def join(cid: str, login: str, role: str = 'owner') -> None:
    if role == 'owner':
        db.execute("UPDATE conv_managers SET role='helper' WHERE conv_id=? AND role='owner' AND login!=?", (cid, login))
    db.execute('INSERT INTO conv_managers (conv_id, login, role, joined_at) VALUES (?,?,?,?) '
               'ON CONFLICT(conv_id, login) DO UPDATE SET role=excluded.role', (cid, login, role, db.now()))


def leave(cid: str, login: str) -> None:
    db.execute('DELETE FROM conv_managers WHERE conv_id=? AND login=?', (cid, login))
