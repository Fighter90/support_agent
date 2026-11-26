"""Адаптеры языковых моделей: GigaChat (основная), YandexGPT (резервная), mock (тесты и работа без ключей).

Внутренний формат сообщений — OpenAI-подобный: {role: system|user|assistant|tool, content, tool_calls?, tool_call_id?, name?}.
Каждый адаптер переводит его в формат своего API. Обе модели работают на серверах в РФ: трансграничной
передачи персональных данных нет (152-ФЗ, ст. 12), а сами ПДн до модели не доходят — см. pii.py.

HTTP-клиент на httpx без сторонних SDK — подход перенесён из наработки автора methodAgent (GPL-3.0).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResult:
    text: str = ''
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    provider: str = ''
    model: str = ''
    ms: int = 0


def _args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or '{}')
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


# ------------------------------------------------------------------ GigaChat

class GigaChat:
    name = 'gigachat'

    def __init__(self) -> None:
        self.s = get_settings()
        self._token = ''
        self._exp = 0.0
        self._lock = threading.Lock()
        import os

        ca = Path(os.environ.get('GIGACHAT_CA_FILE', '/usr/local/share/ca-certificates/russian_trusted_root_ca.crt'))
        verify: Any = self.s.gigachat_verify_ssl
        if verify and ca.exists():
            import ssl

            import certifi

            ctx = ssl.create_default_context(cafile=certifi.where())
            ctx.load_verify_locations(cafile=str(ca))
            verify = ctx
        self.http = httpx.Client(timeout=self.s.llm_timeout_s, verify=verify)

    @property
    def model(self) -> str:
        return self.s.gigachat_model

    def available(self) -> bool:
        return bool(self.s.gigachat_auth_key)

    def _auth(self) -> str:
        with self._lock:
            if self._token and time.time() < self._exp - 60:
                return self._token
            r = self.http.post(
                self.s.gigachat_oauth_url,
                headers={'Authorization': f'Basic {self.s.gigachat_auth_key}', 'RqUID': str(uuid.uuid4()),
                         'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
                data={'scope': self.s.gigachat_scope},
            )
            if r.status_code >= 400:
                raise LLMError(f'GigaChat OAuth: HTTP {r.status_code} {r.text[:200]}')
            d = r.json()
            self._token = d['access_token']
            self._exp = d.get('expires_at', 0) / 1000 if d.get('expires_at', 0) > 1e11 else d.get('expires_at', time.time() + 1500)
            return self._token

    @staticmethod
    def _convert(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m['role'] == 'assistant' and m.get('tool_calls'):
                tc = m['tool_calls'][0]
                out.append({'role': 'assistant', 'content': m.get('content') or '',
                            'function_call': {'name': tc['name'], 'arguments': tc['arguments']}})
            elif m['role'] == 'tool':
                out.append({'role': 'function', 'name': m.get('name', ''), 'content': m['content']})
            else:
                out.append({'role': m['role'], 'content': m.get('content') or ''})
        return out

    # Ключ физлица (GIGACHAT_API_PERS) допускает один одновременный запрос: вызовы идут по очереди,
    # на 429 — повтор с нарастающей паузой (другой процесс с тем же ключом мог занять поток)
    _gate = threading.Semaphore(1)

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], temperature: float = 0.2, tool_choice: str = 'auto') -> LLMResult:
        t0 = time.time()
        payload: dict[str, Any] = {'model': self.model, 'messages': self._convert(messages),
                                   'temperature': temperature, 'max_tokens': 1500}
        if tools:
            payload['functions'] = [t['function'] for t in tools]
            payload['function_call'] = tool_choice  # 'none' — обязательный текстовый ответ по уже собранным данным
        url = f'{self.s.gigachat_api_url}/chat/completions'
        with self._gate:
            for attempt in range(5):
                r = self.http.post(url, json=payload, headers={'Authorization': f'Bearer {self._auth()}', 'Accept': 'application/json'})
                if r.status_code == 401:  # токен истёк раньше срока — повтор с новым
                    self._token = ''
                    continue
                if r.status_code in (429, 502, 503) and attempt < 4:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
        if r.status_code >= 400:
            raise LLMError(f'GigaChat: HTTP {r.status_code} {r.text[:300]}')
        d = r.json()
        msg = d['choices'][0]['message']
        usage = d.get('usage', {})
        res = LLMResult(text=msg.get('content') or '', tokens_in=usage.get('prompt_tokens', 0),
                        tokens_out=usage.get('completion_tokens', 0), provider=self.name, model=d.get('model', self.model),
                        ms=int((time.time() - t0) * 1000))
        if msg.get('function_call'):
            fc = msg['function_call']
            res.tool_calls.append(ToolCall(id='call_' + uuid.uuid4().hex[:8], name=fc['name'], arguments=_args(fc.get('arguments'))))
        return res


# ------------------------------------------------------------------ YandexGPT (OpenAI-совместимый API AI Studio)

class YandexGPT:
    name = 'yandex'

    def __init__(self) -> None:
        self.s = get_settings()
        self.http = httpx.Client(timeout=self.s.llm_timeout_s)

    @property
    def model(self) -> str:
        return f'gpt://{self.s.yandex_folder_id}/{self.s.yandex_model}'

    def available(self) -> bool:
        return bool(self.s.yandex_api_key and self.s.yandex_folder_id)

    @staticmethod
    def _convert(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m['role'] == 'assistant' and m.get('tool_calls'):
                out.append({'role': 'assistant', 'content': m.get('content') or '', 'tool_calls': [
                    {'id': tc['id'], 'type': 'function', 'function': {'name': tc['name'], 'arguments': json.dumps(tc['arguments'], ensure_ascii=False)}}
                    for tc in m['tool_calls']]})
            elif m['role'] == 'tool':
                out.append({'role': 'tool', 'tool_call_id': m.get('tool_call_id', ''), 'content': m['content']})
            else:
                out.append({'role': m['role'], 'content': m.get('content') or ''})
        return out

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], temperature: float = 0.2, tool_choice: str = 'auto') -> LLMResult:
        t0 = time.time()
        payload: dict[str, Any] = {'model': self.model, 'messages': self._convert(messages), 'temperature': temperature, 'max_tokens': 1500}
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = tool_choice
        r = self.http.post(f'{self.s.yandex_api_url}/chat/completions', json=payload,
                           headers={'Authorization': f'Api-Key {self.s.yandex_api_key}', 'OpenAI-Project': self.s.yandex_folder_id})
        if r.status_code >= 400:
            raise LLMError(f'YandexGPT: HTTP {r.status_code} {r.text[:300]}')
        d = r.json()
        msg = d['choices'][0]['message']
        usage = d.get('usage', {})
        res = LLMResult(text=msg.get('content') or '', tokens_in=usage.get('prompt_tokens', 0),
                        tokens_out=usage.get('completion_tokens', 0), provider=self.name, model=self.s.yandex_model,
                        ms=int((time.time() - t0) * 1000))
        for tc in msg.get('tool_calls') or []:
            res.tool_calls.append(ToolCall(id=tc.get('id') or 'call_' + uuid.uuid4().hex[:8], name=tc['function']['name'],
                                           arguments=_args(tc['function'].get('arguments'))))
        return res


# ------------------------------------------------------------------ Mock (детерминированный, без сети)

class MockLLM:
    """Простая детерминированная «модель» для тестов и аварийного режима: ищет по каталогу и честно
    перечисляет найденное с ценами из фида, вопросы о скидках и спецусловиях передаёт менеджеру."""

    name = 'mock'
    model = 'rule-based'

    def available(self) -> bool:
        return True

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], temperature: float = 0.0, tool_choice: str = 'auto') -> LLMResult:
        last = messages[-1]
        if tool_choice == 'none' and last['role'] != 'tool':
            last = next((m for m in reversed(messages) if m['role'] == 'tool'), last)
        user = next((m['content'] for m in reversed(messages) if m['role'] == 'user'), '')
        low = user.lower()
        if last['role'] == 'user':
            if any(w in low for w in ('скидк', 'менеджер', 'госзакуп', 'тендер', 'отсрочк', 'постоплат')):
                return LLMResult(tool_calls=[ToolCall('m1', 'escalate_to_manager', {
                    'reason': 'special_terms', 'summary': f'Клиент спрашивает о спецусловиях: {user[:200]}'})], provider='mock', model=self.model)
            if any(w in low for w in ('доставк', 'оплат', 'ндс', 'счёт', 'счет', 'физлиц')):
                return LLMResult(tool_calls=[ToolCall('m2', 'search_knowledge', {'query': user})], provider='mock', model=self.model)
            return LLMResult(tool_calls=[ToolCall('m3', 'search_catalog', {'query': user})], provider='mock', model=self.model)
        data = _args(last.get('content'))
        if 'results' in data:
            items = data['results'][:3]
            if not items:
                return LLMResult(text='Не нашёл такой продукт в каталоге. Уточните, пожалуйста, название или задачу, которую нужно решить.', provider='mock', model=self.model)
            lines = []
            for it in items:
                price = f"от {it['price_from_rub']:,.2f} ₽".replace(',', ' ') if it.get('price_from_rub') else 'цена по запросу'
                lines.append(f"• **{it['product']}** ({it['vendor']}) — {price}, {it['variants_available']} вариантов. {it['url']}")
            return LLMResult(text='Нашёл в каталоге:\n' + '\n'.join(lines) + '\n\nСколько пользователей и на какой срок нужна лицензия?', provider='mock', model=self.model)
        if 'articles' in data:
            arts = data['articles'][:2]
            if not arts:
                return LLMResult(text='Точного ответа в базе знаний нет — передам вопрос менеджеру.', provider='mock', model=self.model)
            return LLMResult(text='\n\n'.join(f"**{a['title']}**\n{a['text'][:500]}" for a in arts), provider='mock', model=self.model)
        if data.get('escalation_id'):
            return LLMResult(text='Передал ваш вопрос менеджеру — он ответит здесь же, в чате, в рабочее время.', provider='mock', model=self.model)
        return LLMResult(text='Готово.', provider='mock', model=self.model)


_providers: dict[str, Any] = {}


def provider(name: str) -> Any:
    if name not in _providers:
        _providers[name] = {'gigachat': GigaChat, 'yandex': YandexGPT, 'mock': MockLLM}[name]()
    return _providers[name]


def chain() -> list[Any]:
    """Основная модель → резервная → mock (агент отвечает даже при недоступности обеих моделей)."""
    s = get_settings()
    out = []
    names = [s.llm_provider, s.llm_fallback_provider] + (['mock'] if s.llm_allow_mock else [])
    for n in names:
        if n and n in ('gigachat', 'yandex', 'mock') and all(p.name != n for p in out):
            p = provider(n)
            if p.available():
                out.append(p)
    return out
