"""Private SIJK CHECK access through a secret-protected Google Apps Script web app.

Required environment:
    SIJK_CHECK_SCRIPT_URL=https://script.google.com/macros/s/.../exec
    SIJK_CHECK_SCRIPT_KEY=<random secret in Apps Script properties>

Never publish the key, and never enable public spreadsheet sharing.
"""
from __future__ import annotations

import asyncio
import os
import time

import aiohttp


class CheckSheetError(Exception):
    pass


class CheckSheet:
    def __init__(self):
        self.url = os.getenv("SIJK_CHECK_SCRIPT_URL", "").strip()
        self.key = os.getenv("SIJK_CHECK_SCRIPT_KEY", "").strip()
        self._cache = {}
        self._expires = 0.0
        self._lock = asyncio.Lock()

    @property
    def configured(self):
        return self.url.startswith("https://script.google.com/macros/s/") and self.url.endswith("/exec") and len(self.key) >= 32

    async def _call(self, payload):
        if not self.configured:
            raise CheckSheetError("Подключение Apps Script не настроено.")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as session:
                async with session.post(
                    self.url, json={**payload, "key": self.key},
                    allow_redirects=True,
                    max_redirects=5,
                ) as response:
                    if response.status != 200:
                        raise CheckSheetError("Apps Script временно недоступен.")
                    data = await response.json(content_type=None)
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise CheckSheetError("Apps Script отклонил запрос или вернул ошибку.")
            return data
        except CheckSheetError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise CheckSheetError("Не удалось связаться с Apps Script.") from exc

    async def find(self, riot_key):
        # The caller supplies a casefolded Riot ID. Exact lookup is case-insensitive.
        async with self._lock:
            now = time.monotonic()
            if now < self._expires and riot_key in self._cache:
                return self._cache[riot_key]
            data = await self._call({"action": "lookup", "riot_id": riot_key})
            record = data.get("record")
            if record is not None and (not isinstance(record, list) or len(record) < 2):
                raise CheckSheetError("Некорректный ответ базы.")
            result = (record + [""] * 7)[:7] if record else None
            self._cache[riot_key] = result
            self._expires = time.monotonic() + 120
            return result

    async def add_player(self, riot_id: str, score: int, status: str,
                         evidence: str, comment: str, date: str) -> str:
        async with self._lock:
            data = await self._call({
                "action": "add", "riot_id": riot_id, "score": score,
                "status": status, "evidence": evidence,
                "comment": comment, "date": date,
            })
            result = data.get("result")
            if result not in ("added", "exists"):
                raise CheckSheetError("Apps Script не подтвердил запись.")
            self._cache.clear()
            self._expires = 0.0
            return result
