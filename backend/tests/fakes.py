"""httpx2 test double: route requests by (METHOD, url-without-query) and record every call."""

from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2

Handler = Callable[[httpx2.Request], httpx2.Response | Awaitable[httpx2.Response]] | httpx2.Response


class FakeHttp:
    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Handler] = {}
        self.calls: dict[tuple[str, str], list[httpx2.Request]] = defaultdict(list)

    def route(self, method: str, url: str, handler: Handler) -> None:
        self._routes[(method.upper(), url)] = handler

    def count(self, method: str, url: str) -> int:
        return len(self.calls[(method.upper(), url)])

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        key = (request.method, str(request.url.copy_with(query=None)))
        self.calls[key].append(request)
        handler = self._routes.get(key)
        if handler is None:
            return httpx2.Response(599, text=f"no fake route for {key}")
        if isinstance(handler, httpx2.Response):
            return handler
        result: Any = handler(request)
        return await result if inspect.isawaitable(result) else result

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=self.transport())
