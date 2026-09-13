"""Статические bearer-токены, по одному на компонент (ADR 0018). admin может всё."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

ADMIN = "admin"
SCHEDULER = "scheduler"
DR_CONTROLLER = "dr-controller"


class Auth:
    def __init__(self, tokens: Mapping[str, str]):
        self._tokens = {role: token for role, token in tokens.items() if token}

    def role_for(self, token: str) -> str | None:
        found = None
        for role, expected in self._tokens.items():  # без раннего выхода: постоянное время
            if secrets.compare_digest(expected.encode(), token.encode()):
                found = role
        return found


def require(*roles: str) -> Callable:
    async def dependency(request: Request) -> str:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        role = request.app.state.auth.role_for(token.strip())
        if role is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown token")
        if role != ADMIN and role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"role {role} is not allowed here")
        return role

    return dependency


Role = Annotated[str, Depends(require())]
