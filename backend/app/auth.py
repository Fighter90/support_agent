"""Вход в кабинет: JWT (HS256), учётки в таблице users (team.py). Роли admin и manager.

Схема JWT-авторизации перенесена из собственной наработки автора methodAgent (GPL-3.0) и расширена ролями.
Новый менеджер входит по одноразовой ссылке-приглашению и сам задаёт пароль — администратор его не знает.
"""

from __future__ import annotations

import time

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import team
from app.config import get_settings

_bearer = HTTPBearer(auto_error=False)
_failures: dict[str, list[float]] = {}


def _token(u: dict) -> dict[str, str]:
    s = get_settings()
    token = jwt.encode({'sub': u['login'], 'name': u['name'], 'role': u['role'], 'exp': int(time.time()) + s.token_ttl_hours * 3600},
                       s.secret_key, algorithm='HS256')
    return {'token': token, 'name': u['name'], 'login': u['login'], 'role': u['role']}


def _throttle(ip: str) -> list[float]:
    recent = [t for t in _failures.get(ip, []) if time.time() - t < 60]
    if len(recent) >= 5:
        raise HTTPException(429, 'Слишком много попыток входа, подождите минуту')
    return recent


def login(user: str, password: str, ip: str) -> dict[str, str]:
    recent = _throttle(ip)
    u = team.authenticate(user.strip().lower(), password)
    if not u:
        _failures[ip] = [*recent, time.time()]
        raise HTTPException(401, 'Неверный логин или пароль')
    team.heartbeat(u['login'], 'online')
    return _token(u)


def by_invite(code: str, ip: str) -> dict[str, str]:
    recent = _throttle(ip)
    u = team.use_invite(code)
    if not u:
        _failures[ip] = [*recent, time.time()]
        raise HTTPException(401, 'Ссылка-приглашение недействительна или истекла — попросите администратора выдать новую')
    team.heartbeat(u['login'], 'online')
    return _token(u)


def manager(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict[str, str]:
    if not cred:
        raise HTTPException(401, 'Требуется вход менеджера')
    try:
        data = jwt.decode(cred.credentials, get_settings().secret_key, algorithms=['HS256'])
    except jwt.PyJWTError:
        raise HTTPException(401, 'Сессия истекла, войдите снова') from None
    u = team.get(data['sub'])
    if not u or not u['active']:
        raise HTTPException(401, 'Учётная запись отключена администратором')
    return {'login': u['login'], 'name': u['name'], 'role': u['role']}


def admin(m: dict = Depends(manager)) -> dict[str, str]:
    if m['role'] != 'admin':
        raise HTTPException(403, 'Доступно только администратору')
    return m
