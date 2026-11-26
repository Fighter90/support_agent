"""Вход в кабинет менеджера: JWT (HS256), учётные записи из окружения (менеджер и учётка жюри).

Схема JWT-авторизации перенесена из собственной наработки автора methodAgent (GPL-3.0) и упрощена.
"""

from __future__ import annotations

import hmac
import time

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

_bearer = HTTPBearer(auto_error=False)
_failures: dict[str, list[float]] = {}


def accounts() -> dict[str, tuple[str, str]]:
    s = get_settings()
    return {s.manager_login: (s.manager_password, 'Менеджер Softline'), s.jury_login: (s.jury_password, 'Жюри ИИкузницы')}


def login(user: str, password: str, ip: str) -> dict[str, str]:
    recent = [t for t in _failures.get(ip, []) if time.time() - t < 60]
    if len(recent) >= 5:
        raise HTTPException(429, 'Слишком много попыток входа, подождите минуту')
    acc = accounts().get(user)
    if not acc or not hmac.compare_digest(acc[0], password):
        _failures[ip] = [*recent, time.time()]
        raise HTTPException(401, 'Неверный логин или пароль')
    s = get_settings()
    token = jwt.encode({'sub': user, 'name': acc[1], 'exp': int(time.time()) + s.token_ttl_hours * 3600}, s.secret_key, algorithm='HS256')
    return {'token': token, 'name': acc[1], 'login': user}


def manager(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict[str, str]:
    if not cred:
        raise HTTPException(401, 'Требуется вход менеджера')
    try:
        data = jwt.decode(cred.credentials, get_settings().secret_key, algorithms=['HS256'])
    except jwt.PyJWTError:
        raise HTTPException(401, 'Сессия истекла, войдите снова') from None
    return {'login': data['sub'], 'name': data.get('name', data['sub'])}
