"""Private Google Sheets access using the owner's OAuth 2.0 refresh token.

Required environment: SIJK_CHECK_GOOGLE_CLIENT_ID, SIJK_CHECK_GOOGLE_CLIENT_SECRET,
SIJK_CHECK_GOOGLE_REFRESH_TOKEN. Never commit any of these secrets.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from urllib.parse import quote

import aiohttp
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

DEFAULT_SPREADSHEET_ID = "19Lhc8VIDr6eCL4wHf-jUR3JyjOIdpDHbk7LeXxs9ANY"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class CheckSheetError(Exception):
    pass


class CheckSheet:
    def __init__(self):
        self.sheet_id = os.getenv("SIJK_CHECK_SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID)
        self.client_id = os.getenv("SIJK_CHECK_GOOGLE_CLIENT_ID", "")
        self.client_secret = os.getenv("SIJK_CHECK_GOOGLE_CLIENT_SECRET", "")
        self.refresh_token = os.getenv("SIJK_CHECK_GOOGLE_REFRESH_TOKEN", "")
        self._cache = {}
        self._expires = 0.0
        self._lock = asyncio.Lock()

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def _token(self):
        creds = Credentials(
            token=None,
            refresh_token=self.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self.client_id,
            client_secret=self.client_secret,
            scopes=SCOPES,
        )
        creds.refresh(Request())
        return creds.token

    async def find(self, riot_key):
        if not self.configured:
            raise CheckSheetError("Подключение Google Sheets ещё не настроено.")
        async with self._lock:
            if time.monotonic() >= self._expires:
                try:
                    token = await asyncio.to_thread(self._token)
                    range_name = quote("'База игроков'!A4:G", safe="")
                    url = f"https://sheets.googleapis.com/v4/spreadsheets/{self.sheet_id}/values/{range_name}"
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                        async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as response:
                            if response.status != 200:
                                raise CheckSheetError(f"Google Sheets вернул HTTP {response.status}.")
                            data = await response.json()
                    records = {}
                    for row in data.get("values", []):
                        if not row or "#" not in row[0]:
                            continue
                        record = (row + [""] * 7)[:7]
                        records[row[0].strip().casefold()] = record
                    self._cache = records
                    self._expires = time.monotonic() + 120
                except CheckSheetError:
                    raise
                except (ValueError, KeyError, aiohttp.ClientError, TimeoutError) as exc:
                    raise CheckSheetError("Не удалось прочитать базу SIJK CHECK.") from exc
            return self._cache.get(riot_key)

    async def add_player(self, riot_id: str, score: int, status: str, evidence: str,
                         comment: str, date: str) -> str:
        """Add only if missing; never overwrite an existing evaluation.

        Returns 'added' or 'exists'. The spreadsheet remains private.
        """
        if not self.configured:
            raise CheckSheetError("Подключение Google Sheets не настроено.")
        async with self._lock:
            try:
                token = await asyncio.to_thread(self._token)
                root = f"https://sheets.googleapis.com/v4/spreadsheets/{self.sheet_id}/values"
                headers = {"Authorization": f"Bearer {token}"}
                read_range = quote("'База игроков'!A4:G", safe="")
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                    async with session.get(f"{root}/{read_range}", headers=headers) as response:
                        if response.status != 200:
                            raise CheckSheetError(f"Ошибка чтения таблицы: HTTP {response.status}")
                        data = await response.json()
                    for row in data.get("values", []):
                        if row and str(row[0]).strip().casefold() == riot_id.strip().casefold():
                            self._expires = 0
                            return "exists"
                    # Append only to A:G; do not change formatting, permissions or other sheets.
                    append_range = quote("'База игроков'!A:G", safe="")
                    url = f"{root}/{append_range}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
                    values = [[riot_id, score, status,
                               "https://tracker.gg/valorant/profile/riot/" + quote(riot_id, safe="") + "/overview",
                               evidence, comment, date]]
                    async with session.post(url, headers=headers, json={"values": values}) as response:
                        if response.status != 200:
                            raise CheckSheetError(f"Ошибка записи в таблицу: HTTP {response.status}")
                        result = await response.json()
                    if int(result.get("updates", {}).get("updatedRows", 0)) != 1:
                        raise CheckSheetError("Google Sheets не подтвердил добавление строки.")
                    self._expires = 0
                    return "added"
            except CheckSheetError:
                raise
            except (ValueError, KeyError, aiohttp.ClientError, TimeoutError) as exc:
                raise CheckSheetError("Не удалось записать оценку в таблицу.") from exc
