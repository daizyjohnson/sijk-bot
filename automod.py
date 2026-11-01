from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger('sijk.automod')
URL_RE = re.compile(r'https?://[^\s<>]+', re.I)
INVITE_RE = re.compile(r'(?:discord\.gg|discord(?:app)?\.com/invite)/[\w-]+', re.I)
# Deliberately narrow high-confidence patterns; not a comprehensive threat intelligence feed.
SUSPICIOUS_RE = re.compile(r'(?:discord[\w-]*(?:nitro|gift)|(?:free|claim)[\w-]*nitro|steam[\w-]*(?:gift|community)|(?:nitro|steam)[\w-]*free)', re.I)

class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: Path):
        self.bot, self.db_path = bot, db_path
        self.recent = defaultdict(deque)
        self.join_times = defaultdict(deque)
        self.raid_alert_at = defaultdict(float)
        self.lock = asyncio.Lock()
        # Keep burst detection bounded even on very busy servers.
        self.last_seen = {}
        self.log_cooldowns = {}
        self.log_gate = asyncio.Semaphore(2)
        self.last_cleanup = time.monotonic()
        self.max_recent_users = 5000
        self.episode_cooldown = 60
        self.episode_last = {}
        with closing(self.db()) as con:
            con.executescript('''
            CREATE TABLE IF NOT EXISTS automod_settings (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                test_mode INTEGER NOT NULL DEFAULT 1,
                mod_log_channel_id INTEGER,
                raid_until INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS automod_strikes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                rule TEXT NOT NULL, action TEXT NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_automod_strikes_user_time
                ON automod_strikes(guild_id, user_id, created_at);
            CREATE TABLE IF NOT EXISTS automod_exclusions (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY(guild_id,channel_id)
            );
            CREATE TABLE IF NOT EXISTS automod_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER,
                rule TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            ''')
            con.commit()

    def db(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def settings(self, guild_id):
        with closing(self.db()) as con:
            con.execute('INSERT OR IGNORE INTO automod_settings(guild_id) VALUES (?)', (guild_id,))
            con.commit()
            return con.execute('SELECT * FROM automod_settings WHERE guild_id=?', (guild_id,)).fetchone()

    def excluded(self, guild_id, channel_id):
        with closing(self.db()) as con:
            return con.execute('SELECT 1 FROM automod_exclusions WHERE guild_id=? AND channel_id=?', (guild_id, channel_id)).fetchone() is not None

    def log_channel(self, guild, settings):
        channel_id = settings['mod_log_channel_id']
        if not channel_id:
            with closing(self.db()) as con:
                row = con.execute('SELECT mod_log_channel_id FROM guild_config WHERE guild_id=?', (guild.id,)).fetchone()
                channel_id = row['mod_log_channel_id'] if row else None
        return guild.get_channel(channel_id) if channel_id else None

    async def record(self, guild, member, channel, rule, action, settings):
