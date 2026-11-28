"""Команда поддержки: роли, приглашения, статусы, автоназначение по очереди, переназначение, передача и помощник."""

import os
import tempfile

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp())
os.environ.setdefault('LLM_PROVIDER', 'mock')

from fastapi.testclient import TestClient  # noqa: E402

from app import db, handoff, team  # noqa: E402
from app.config import get_settings  # noqa: E402


def _esc(text: str = 'Нужна скидка') -> tuple[str, int]:
    cid = db.new_id('c')
    db.execute('INSERT INTO conversations (id, created_at, updated_at) VALUES (?,?,?)', (cid, db.now(), db.now()))
    eid = db.execute('INSERT INTO escalations (conv_id, reason, category, summary, created_at) VALUES (?,?,?,?,?)',
                     (cid, 'special_terms', 'Скидка', text, db.now()))
    return cid, eid


def _assignee(eid: int) -> str:
    return db.one('SELECT assignee FROM escalations WHERE id=?', (eid,))['assignee']


def test_routing_queue_and_handoff():
    db.init_db()
    team.init()
    db.execute("UPDATE escalations SET status='answered'")  # чистая очередь
    for lg in ('anna', 'igor', 'olga'):
        if not team.get(lg):
            team.create(lg, lg.title(), 'manager', 2, True, 'test')
    # никого в сети — обращение ждёт в очереди
    cid1, e1 = _esc()
    team.dispatch()
    assert _assignee(e1) == ''
    # двое в сети — назначение по наименьшей нагрузке, по очереди (round-robin)
    team.heartbeat('anna', 'online')
    team.heartbeat('igor', 'online')
    a1 = _assignee(e1)
    assert a1 in ('anna', 'igor')
    cid2, e2 = _esc()
    team.dispatch()
    assert _assignee(e2) not in ('', a1)
    # лимит нагрузки: у обоих по 1 из 2, третье — снова распределится, пятое — в очередь
    ids = [_esc()[1] for _ in range(3)]
    team.dispatch()
    assert [bool(_assignee(i)) for i in ids] == [True, True, False]
    # перерыв — невзятые обращения возвращаются в очередь, их забирает коллега в сети
    team.heartbeat('olga', 'online')
    team.heartbeat('anna', 'away')
    assert all(_assignee(i) != 'anna' for i in (e1, e2, *ids))
    # не взял вовремя — переназначение (у Ольги есть запас нагрузки)
    team.update('olga', max_load=6)
    db.execute("UPDATE escalations SET assigned_at=? WHERE assignee='igor'", (db.now() - 999,))
    team.dispatch()
    assert db.one("SELECT max(reassigns) AS n FROM escalations")['n'] >= 1
    # взять диалог, позвать помощника, передать коллеге
    handoff.take(cid1, 'Ольга', True, login='olga')
    assert db.one('SELECT status FROM escalations WHERE id=?', (e1,))['status'] == 'answered'
    handoff.invite(cid1, {'login': 'olga', 'name': 'Ольга'}, 'igor')
    assert {p['login']: p['role'] for p in team.participants(cid1)} == {'olga': 'owner', 'igor': 'helper'}
    handoff.transfer(cid1, {'login': 'olga', 'name': 'Ольга'}, 'igor')
    assert db.one('SELECT manager_login FROM conversations WHERE id=?', (cid1,))['manager_login'] == 'igor'
    assert [p['login'] for p in team.participants(cid1)] == ['igor']
    texts = [m['content'] for m in db.rows("SELECT content FROM messages WHERE conv_id=? AND role='system'", (cid1,))]
    assert any('передан коллеге' in t for t in texts) and any('ещё один специалист' in t for t in texts)


def test_api_roles_and_invite():
    from app.main import app

    s = get_settings()
    with TestClient(app) as cl:
        adm = cl.post('/api/auth/login', json={'login': s.manager_login, 'password': s.manager_password}).json()
        assert adm['role'] == 'admin'
        h = {'Authorization': 'Bearer ' + adm['token']}
        r = cl.post('/api/admin/users', headers=h, json={'login': 'petr.ivanov', 'name': 'Пётр Иванов', 'max_load': 3})
        assert r.status_code == 200, r.text
        code = r.json()['invite_code']
        mg = cl.post('/api/auth/invite', json={'code': code}).json()
        assert mg['role'] == 'manager' and mg['login'] == 'petr.ivanov'
        assert cl.post('/api/auth/invite', json={'code': code}).status_code == 401  # одноразовая
        hm = {'Authorization': 'Bearer ' + mg['token']}
        assert cl.get('/api/admin/users', headers=hm).status_code == 403
        assert cl.post('/api/manager/settings', headers=hm, json={'agent_discount_cap': 10}).status_code == 403
        assert cl.post('/api/manager/password', headers=hm, json={'password': 'Новый-пароль-1'}).status_code == 200
        assert cl.post('/api/auth/login', json={'login': 'petr.ivanov', 'password': 'Новый-пароль-1'}).status_code == 200
        assert cl.post('/api/manager/presence', headers=hm, json={'status': 'away'}).json()['status'] == 'away'
        users = cl.get('/api/admin/users', headers=h).json()['users']
        assert any(u['login'] == 'petr.ivanov' and u['status'] == 'away' for u in users)
        # отключённый менеджер теряет доступ сразу
        cl.patch('/api/admin/users/petr.ivanov', headers=h, json={'active': False})
        assert cl.get('/api/manager/me', headers=hm).status_code == 401
        assert cl.patch(f'/api/admin/users/{s.manager_login}', headers=h, json={'active': False}).status_code == 400


def test_admin_creates_user_with_password_and_resets_it():
    """Форма «Новый сотрудник»: кириллица в логине, пустой лимит — не 422; администратор получает логин и пароль."""
    from app.main import app

    s = get_settings()
    with TestClient(app) as cl:
        h = {'Authorization': 'Bearer ' + cl.post('/api/auth/login', json={'login': s.manager_login, 'password': s.manager_password}).json()['token']}
        r = cl.post('/api/admin/users', headers=h, json={'login': 'Мария Петрова', 'name': 'Мария Петрова', 'role': 'manager', 'max_load': '', 'routing': True})
        assert r.status_code == 200, r.text
        u = r.json()
        assert u['login'] == 'mariya.petrova' and u['max_load'] == 5 and len(u['password']) == 14
        assert cl.post('/api/auth/login', json={'login': u['login'], 'password': u['password']}).json()['role'] == 'manager'
        r2 = cl.post('/api/admin/users', headers=h, json={'name': 'Мария Петрова', 'max_load': 99}).json()  # тёзка — свой логин
        assert r2['login'] == 'mariya.petrova2' and r2['max_load'] == 30
        pw = cl.post(f"/api/admin/users/{u['login']}/password", headers=h).json()['password']
        assert pw != u['password']
        assert cl.post('/api/auth/login', json={'login': u['login'], 'password': u['password']}).status_code == 401
        assert cl.post('/api/auth/login', json={'login': u['login'], 'password': pw}).status_code == 200
        assert cl.post(f'/api/admin/users/{s.manager_login}/password', headers=h).status_code == 400  # учётка из .env
        bad = cl.post('/api/admin/users', headers=h, json={'name': 'А'})
        assert bad.status_code == 400 and 'имя' in bad.json()['detail']
        v = cl.post('/api/admin/users', headers=h, json={'max_load': 3})
        assert v.status_code == 422 and v.json()['detail'].startswith('Проверьте поля')


def test_chat_history_and_forget():
    from app import pii
    from app.main import app, purge_pii

    with TestClient(app) as cl:
        cid = cl.post('/api/chat/start', json={'page_url': 'https://methodagent.ru/'}).json()['conversation_id']
        db.execute("UPDATE conversations SET title=? WHERE id=?", (pii.mask(cid, 'Счёт на ivan@mail.ru'), cid))
        db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)', (cid, 'user', 'привет', '{}', db.now()))
        items = cl.post('/api/chat/history', json={'ids': [cid, 'bad id!', 'cnotexists1']}).json()['items']
        assert [i['id'] for i in items] == [cid] and 'ivan@mail.ru' in items[0]['title']
        assert cl.post('/api/chat/forget', json={'ids': [cid]}).json()['forgotten'] == 1
        assert 'ivan@mail.ru' not in cl.post('/api/chat/history', json={'ids': [cid]}).json()['items'][0]['title']
        # срок хранения: ПДн старых диалогов удаляются, переписка остаётся обезличенной
        cid2 = cl.post('/api/chat/start', json={}).json()['conversation_id']
        pii.mask(cid2, 'тел +7 916 123-45-67')
        db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (db.now() - 200 * 86400, cid2))
        assert purge_pii() >= 1 and not db.rows('SELECT 1 FROM pii WHERE conv_id=?', (cid2,))


def test_vat_label_from_card():
    from app import card, quotes

    assert card.vat_from_text('Количество лицензий Возврат НДС 22% Физическая доставка') == 22
    assert card.vat_from_text('Размер НДС (22%) 575,93 ₽') is None and card.vat_from_text('22% НДС можно вернуть') == 22
    assert card.vat_from_text('Цена 1 000 ₽ Без НДС') == 0
    assert card.vat_from_text('Описание продукта') is None
    assert quotes._vat(3194.0, False, 22) == round(3194 * 22 / 122, 2) and quotes._vat(1000.0, False, 0) == 0.0


def test_catalog_ingest_in_separate_process():
    """Фид разбирается в отдельном процессе — память не остаётся в веб-сервере; веб-сервер видит новый каталог."""
    from app import catalog
    from app.config import ROOT_DIR

    st = catalog.ingest_isolated(str(ROOT_DIR / 'backend' / 'tests' / 'feed_sample.xml'))
    assert st['offers'] == 4 and catalog.stats()['offers'] == 4
