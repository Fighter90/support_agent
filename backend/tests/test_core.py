"""Модульные тесты без сети: маскирование ПДн, проверка цен, база знаний, поиск, сквозной диалог на mock-модели."""

import os
import tempfile
from pathlib import Path

import pytest

os.environ['DATA_DIR'] = tempfile.mkdtemp()
os.environ['LLM_PROVIDER'] = 'mock'
os.environ['LLM_FALLBACK_PROVIDER'] = ''
os.environ['GIGACHAT_AUTH_KEY'] = ''

from app import agent, catalog, db, kb, pii  # noqa: E402
from app.config import ROOT_DIR, get_settings  # noqa: E402

FEED = ROOT_DIR / 'backend' / 'tests' / 'feed_sample.xml'


@pytest.fixture(scope='module', autouse=True)
def setup():
    db.init_db()
    kb.load_base()
    catalog.ingest(str(FEED))
    yield


def test_pii_masking_and_unmask():
    cid = agent.new_conversation()
    t = pii.mask(cid, 'Иванов Иван Петрович, тел +7 (916) 123-45-67, почта ivan.petrov@mail.ru, ИНН 7701234567')
    assert '[ТЕЛ_1]' in t and '[EMAIL_1]' in t and '[ИНН_1]' in t and '[ФИО_1]' in t
    assert '916' not in t and 'mail.ru' not in t and 'Петрович' not in t
    assert 'ivan.petrov@mail.ru' in pii.unmask(cid, t)


def test_price_guard():
    known = {47808.0, 570.6}
    assert agent.unverified_prices('MDaemon — 47 808 ₽', known) == []
    assert agent.unverified_prices('50 × 570,60 ₽ = 28 530 ₽', known) == []  # цена × количество допустима
    assert agent.unverified_prices('Скидка, итого 40 000 ₽', known) == ['40 000 ₽']


def test_catalog_ingest_and_search():
    st = catalog.stats()
    assert st['offers'] == 4 and st['available_priced'] == 3
    res = catalog.search('MDaemon')
    assert res and res[0]['product'] == 'MDaemon' and res[0]['price_from_rub'] == 47808.0
    v = catalog.variants(group_id=res[0]['group_id'], filters={'users': 6})
    assert all(int(x['users_max']) >= 6 for x in v['variants'] if x.get('users_max'))
    dom = catalog.search('операционная система', {'only_domestic': True})
    assert dom and dom[0]['domestic']


def test_kb_search():
    hits = kb.search('цены с НДС')
    assert hits and 'НДС' in hits[0]['title']


def test_dialog_mock_end_to_end():
    cid = agent.new_conversation()
    r = agent.reply(cid, 'Нужен MDaemon, мой email test@example.com')
    assert 'MDaemon' in r['content'] and r['meta']['products']
    stored = db.one("SELECT content FROM messages WHERE conv_id=? AND role='user'", (cid,))
    assert 'example.com' not in stored['content']
    r2 = agent.reply(cid, 'Можно скидку 30%?')
    assert r2['meta'].get('escalation')
    assert db.one("SELECT status FROM conversations WHERE id=?", (cid,))['status'] == 'escalated'


def test_quote_prices_from_catalog():
    from app import quotes
    cid = agent.new_conversation()
    q = quotes.create(cid, [{'offer_id': 477682, 'quantity': 2}])
    assert q['total_rub'] == 95616.0 and q['vat_rub'] > 0
    with pytest.raises(quotes.QuoteError):
        quotes.create(cid, [{'offer_id': 999, 'quantity': 1}])  # позиция без цены — только через менеджера


def test_learning_from_feedback():
    from app import learning
    cid = agent.new_conversation()
    r = agent.reply(cid, 'Нужен MDaemon')
    learning.rate(cid, r['id'], 1)
    ex = learning.examples_for('Хочу купить MDaemon')
    assert ex and '₽' not in ex[0]['answer']  # цены в примерах вырезаны
    learning.rate(cid, r['id'], -1)
    neg = learning.negatives()
    assert neg and neg[0]['question'] == 'Нужен MDaemon'
    kb_id = learning.fix(neg[0]['id'], 'MDaemon продаётся в редакциях по числу пользователей, срок 2 или 3 года.', 'Тест')
    assert kb_id and kb.search('MDaemon редакции')
    learning.survey(cid, True, 5, 'Отлично')
    st = learning.stats()
    assert st['csat'] == 5 and st['resolved_share'] == 1


def test_discount_limit_and_invoice():
    from app import quotes
    cid = agent.new_conversation()
    inv = quotes.create(cid, [{'offer_id': 477682, 'quantity': 1}], discount_percent=5, kind='invoice')
    assert inv['kind'] == 'invoice' and inv['number'].startswith('СЧ-') and inv['total_rub'] == round(47808 * 0.95, 2)
    with pytest.raises(quotes.QuoteError):
        quotes.create(cid, [{'offer_id': 477682, 'quantity': 1}], discount_percent=15)  # больше 5% — только менеджер
    html = quotes.render_html({**quotes.get(inv['quote_id']), 'customer': {'company': 'ООО Тест', 'inn': '7701234567'}})
    assert 'Счёт на оплату' in html and '7736227885' in html and 'скидка 5%' in html.lower()


def test_promo_table_and_agent_cap():
    """Таблица скидок менеджера: акция применяется в КП сама и может быть больше лимита агента; лимит агента меняется."""
    from app import discounts, quotes
    cid = agent.new_conversation()
    o = catalog.get_offers([477682])[477682]
    r = discounts.save({'scope': 'vendor', 'match': o['vendor'][:6], 'percent': 7, 'title': 'Акция вендора'}, 'Менеджер')
    q = quotes.create(cid, [{'offer_id': 477682, 'quantity': 2}], kind='quote')
    assert q['items'][0]['discount'] == 7 and q['items'][0]['promo'] == 'Акция вендора'
    assert q['total_rub'] == round(round(47808 * 0.93, 2) * 2, 2)
    # своя скидка агента в пределах лимита, акция выгоднее — берётся акция
    q = quotes.create(cid, [{'offer_id': 477682, 'quantity': 1}], discount_percent=5)
    assert q['items'][0]['discount'] == 7
    discounts.delete(r['id'])
    discounts.set_agent_cap(3)
    with pytest.raises(quotes.QuoteError):
        quotes.create(cid, [{'offer_id': 477682, 'quantity': 1}], discount_percent=5)
    discounts.set_agent_cap(5)
    with pytest.raises(discounts.DiscountError):
        discounts.save({'scope': 'product', 'match': 'x', 'percent': 5}, 'Менеджер')  # слишком короткое условие
    hit = discounts.best_for({'vendor': 'Kaspersky', 'product': 'KES'}, [{'id': 1, 'scope': 'all', 'match': '', 'percent': 3, 'title': 'Все'}])
    assert hit and hit['percent'] == 3


def test_quote_issued_without_manager():
    """Ответ организаторов: КП выдаётся клиенту сразу, подтверждение менеджера — по желанию."""
    from app import quotes
    cid = agent.new_conversation()
    q = quotes.create(cid, [{'offer_id': 477682, 'quantity': 1}])
    try:
        q = quotes.set_requisites(q['quote_id'], {'company': 'ООО Тест', 'inn': '7701234567'})
    except OSError:  # PDF рендерится в Docker (WeasyPrint); статус проверяем по базе
        pass
    assert quotes.get(q['quote_id'])['status'] == 'issued'
