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
        with closing(self.db()) as con:
            con.execute('INSERT INTO automod_events(guild_id,user_id,channel_id,rule,action,created_at) VALUES (?,?,?,?,?,?)',
                        (guild.id, member.id, channel.id if channel else None, rule, action, int(time.time())))
            con.commit()
        target = self.log_channel(guild, settings)
        if not isinstance(target, discord.TextChannel):
            return
        # One notification per user/rule every 60s, except raid alerts (already 5m limited).
        now = time.monotonic()
        cooldown_key = (guild.id, member.id, 'spam' if ('флуд' in rule.lower() or 'Повторяющиеся сообщения' in rule) else rule)
        if not rule.startswith('Возможный рейд'):
            if now - self.log_cooldowns.get(cooldown_key, -1e12) < 60:
                return
            self.log_cooldowns[cooldown_key] = now
        embed = discord.Embed(title='✦ SIJK SYSTEM | АВТОМОДЕРАЦИЯ', color=discord.Color.orange() if settings['test_mode'] else discord.Color.red())
        embed.add_field(name='Участник', value=f'{member.mention} (`{member.id}`)', inline=False)
        embed.add_field(name='Правило', value=rule, inline=True)
        embed.add_field(name='Действие', value=action, inline=True)
        if channel:
            embed.add_field(name='Канал', value=channel.mention, inline=False)
        embed.timestamp = discord.utils.utcnow()
        try:
            async with self.log_gate:
                await target.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception('Не удалось отправить лог автомодерации')

    def exempt(self, member):
        return (member.bot or member.id == member.guild.owner_id or member.guild_permissions.administrator or
                member.guild_permissions.manage_messages or member.guild_permissions.moderate_members)

    async def process(self, message: discord.Message):
        if not message.guild or not isinstance(message.author, discord.Member) or self.exempt(message.author):
            return
        settings = self.settings(message.guild.id)
        if not settings['enabled']:
            return
        channel = message.channel
        if self.excluded(message.guild.id, channel.id) or self.excluded(message.guild.id, getattr(channel, 'category_id', 0)):
            return
        target = self.log_channel(message.guild, settings)
        if target and channel.id == target.id:
            return
        # Existing service channels are automatically ignored when configured.
        with closing(self.db()) as con:
            row = con.execute('SELECT mod_log_channel_id,message_log_channel_id,request_log_channel_id FROM guild_config WHERE guild_id=?', (message.guild.id,)).fetchone()
        if row and channel.id in (row['mod_log_channel_id'], row['message_log_channel_id'], row['request_log_channel_id']):
            return
        now = time.monotonic()
        content = message.content or ''
        key = (message.guild.id, message.author.id)
        reasons = []
        if len(message.mentions) >= 5 or message.mention_everyone:
            reasons.append('Массовые упоминания')
        if INVITE_RE.search(content):
            reasons.append('Discord-приглашение')
        if URL_RE.search(content) and SUSPICIOUS_RE.search(content):
            reasons.append('Подозрительная ссылка (эвристика)')
        async with self.lock:
            self.last_seen[key] = now
            # Periodically discard inactive users and old log-cooldown entries.
            if now - self.last_cleanup > 60:
                stale = [k for k, t in self.last_seen.items() if now - t > 90]
                for k in stale:
                    self.last_seen.pop(k, None)
                    self.recent.pop(k, None)
                self.log_cooldowns = {k: t for k, t in self.log_cooldowns.items() if now - t < 120}
                self.episode_last = {k: t for k, t in self.episode_last.items() if now - t < 120}
                self.last_cleanup = now
            # A hard cap prevents unbounded memory growth during large raids.
            if len(self.recent) > self.max_recent_users:
                oldest = sorted(self.last_seen, key=self.last_seen.get)[:len(self.recent)-self.max_recent_users]
                for k in oldest:
                    self.last_seen.pop(k, None)
                    self.recent.pop(k, None)
            recent = self.recent[key]
            while recent and now - recent[0][0] > 30:
                recent.popleft()
            normalized = ' '.join(content.casefold().split())
            # Rate-based flood: count every message, including one-character messages.
            if sum(1 for ts, txt, att in recent if now - ts <= 8) >= 5:
                reasons.append('Быстрый флуд (6 сообщений за 8 секунд)')
            # Repeated text: three matching messages in 30 seconds, even 'f' or '+'.
            if normalized and sum(
                1 for ts, txt, att in recent
                if txt == normalized and now - ts <= 30
            ) >= 2:
                reasons.append('Повторяющиеся сообщения')
            if message.attachments and sum(
                1 for ts, txt, att in recent if att and now - ts <= 20
            ) >= 3:
                reasons.append('Спам вложениями')
            recent.append((now, normalized, bool(message.attachments)))
            # A per-user cap also bounds memory if one account floods extremely fast.
            while len(recent) > 40:
                recent.popleft()
        if not reasons:
            return
        reason = ', '.join(reasons)
        if settings['test_mode']:
            await self.record(message.guild, message.author, channel, reason, 'Тест: без наказания', settings)
            return
        # One strike per user per 60-second episode.
        async with self.lock:
            episode_key = (message.guild.id, message.author.id)
            now = time.monotonic()
            new_episode = now - self.episode_last.get(episode_key, -1e12) >= self.episode_cooldown
            if new_episode:
                self.episode_last[episode_key] = now
        if not new_episode:
            try:
                await message.delete(reason=f'SIJK AutoMod: продолжение эпизода: {reason}')
            except discord.HTTPException:
                pass
            return

        with closing(self.db()) as con:
            count = con.execute(
                'SELECT COUNT(*) FROM automod_strikes WHERE guild_id=? AND user_id=? AND created_at>=?',
                (message.guild.id, message.author.id, int(time.time()) - 86400)
            ).fetchone()[0]
        duration = (0, 5, 30, 120)[min(count, 3)]
        try:
            await message.delete(reason=f'SIJK AutoMod: {reason}')
            action = 'Сообщение удалено; предупреждение'
        except discord.HTTPException:
            action = 'Предупреждение; не удалось удалить сообщение'
        if duration:
            me = message.guild.me
            if me and me.guild_permissions.moderate_members and me.top_role > message.author.top_role:
                try:
                    await message.author.timeout(timedelta(minutes=duration), reason=f'SIJK AutoMod: {reason}')
                    action += f'; тайм-аут {duration} мин'
                except discord.HTTPException:
                    action += '; тайм-аут не удался'
            else:
                action += '; нет прав на тайм-аут'
        with closing(self.db()) as con:
            con.execute(
                'INSERT INTO automod_strikes(guild_id,user_id,rule,action,created_at) VALUES (?,?,?,?,?)',
                (message.guild.id, message.author.id, reason, action, int(time.time()))
            )
            con.commit()
        await self.record(message.guild, message.author, channel, reason, action, settings)

    @commands.Cog.listener()
    async def on_message(self, message):
        try:
            await self.process(message)
        except Exception:
            log.exception('Ошибка проверки сообщения')

    @commands.Cog.listener()
    async def on_member_join(self, member):
        settings = self.settings(member.guild.id)
        if not settings['enabled']:
            return
        now = time.monotonic()
        async with self.lock:
            joins = self.join_times[member.guild.id]
            while joins and now - joins[0] > 30:
                joins.popleft()
            joins.append(now)
            count = len(joins)
            should_alert = count >= 10 and now - self.raid_alert_at[member.guild.id] > 300
            if should_alert:
                self.raid_alert_at[member.guild.id] = now
        if should_alert:
            await self.record(member.guild, member, None,
                              f'Возможный рейд: {count}+ входов за 30 секунд',
                              'Тревога администрации; участники не наказаны', settings)

    @staticmethod
    def admin(interaction):
        return bool(interaction.guild and isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator)

    @app_commands.command(name='automod_setup', description='Настроить канал логов и тестовый режим')
    @app_commands.default_permissions(administrator=True)
    async def automod_setup(self, interaction: discord.Interaction, logs: discord.TextChannel):
        if not self.admin(interaction):
            await interaction.response.send_message('Только администратор.', ephemeral=True); return
        with closing(self.db()) as con:
            con.execute('INSERT INTO automod_settings(guild_id,mod_log_channel_id) VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET mod_log_channel_id=excluded.mod_log_channel_id', (interaction.guild_id, logs.id))
            con.commit()
        await interaction.response.send_message(f'Автомодерация подключена к {logs.mention}. Тестовый режим включён по умолчанию.', ephemeral=True)

    @app_commands.command(name='automod_mode', description='Переключить тестовый и рабочий режим')
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(mode=[app_commands.Choice(name='Тест — только логи', value='test'), app_commands.Choice(name='Рабочий — удаление и тайм-ауты', value='live'), app_commands.Choice(name='Выключено', value='off')])
    async def automod_mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if not self.admin(interaction):
            await interaction.response.send_message('Только администратор.', ephemeral=True); return
        enabled, test = (0,1) if mode.value == 'off' else (1, 1 if mode.value == 'test' else 0)
        with closing(self.db()) as con:
            con.execute('INSERT INTO automod_settings(guild_id,enabled,test_mode) VALUES (?,?,?) ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled,test_mode=excluded.test_mode', (interaction.guild_id, enabled, test))
            con.commit()
        await interaction.response.send_message(f'Режим: **{mode.name}**', ephemeral=True)

    @app_commands.command(name='automod_exclude', description='Исключить канал или категорию из проверок')
    @app_commands.default_permissions(administrator=True)
    async def automod_exclude(self, interaction: discord.Interaction, channel: discord.TextChannel | discord.CategoryChannel):
        if not self.admin(interaction):
            await interaction.response.send_message('Только администратор.', ephemeral=True); return
        with closing(self.db()) as con:
            con.execute('INSERT OR IGNORE INTO automod_exclusions(guild_id,channel_id) VALUES (?,?)', (interaction.guild_id, channel.id))
            con.commit()
        await interaction.response.send_message(f'Исключение добавлено: **{channel.name}**', ephemeral=True)

    @app_commands.command(name='automod_include', description='Убрать исключение канала или категории')
    @app_commands.default_permissions(administrator=True)
    async def automod_include(self, interaction: discord.Interaction, channel: discord.TextChannel | discord.CategoryChannel):
        if not self.admin(interaction):
            await interaction.response.send_message('Только администратор.', ephemeral=True); return
        with closing(self.db()) as con:
            con.execute('DELETE FROM automod_exclusions WHERE guild_id=? AND channel_id=?', (interaction.guild_id, channel.id))
            con.commit()
        await interaction.response.send_message(f'Исключение убрано: **{channel.name}**', ephemeral=True)

    @app_commands.command(name='automod_history', description='Последние нарушения участника')
    @app_commands.default_permissions(moderate_members=True)
    async def automod_history(self, interaction: discord.Interaction, member: discord.Member):
        if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.moderate_members or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message('Нет доступа.', ephemeral=True); return
        with closing(self.db()) as con:
            rows = con.execute('SELECT id,rule,action,created_at FROM automod_events WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10', (interaction.guild_id, member.id)).fetchall()
        if not rows:
            await interaction.response.send_message('Нарушений пока нет.', ephemeral=True); return
        lines = [f"`#{r['id']}` <t:{r['created_at']}:f> — {r['rule']} → {r['action']}" for r in rows]
        await interaction.response.send_message('\n'.join(lines)[:1900], ephemeral=True)

    @app_commands.command(name='automod_status', description='Показать состояние автомодерации')
    @app_commands.default_permissions(administrator=True)
    async def automod_status(self, interaction: discord.Interaction):
        if not self.admin(interaction):
            await interaction.response.send_message('Только администратор.', ephemeral=True); return
        s = self.settings(interaction.guild_id)
        mode = 'выключено' if not s['enabled'] else ('тест' if s['test_mode'] else 'рабочий')
        await interaction.response.send_message(f'Режим: **{mode}**. Логи: <#{s["mod_log_channel_id"]}>.' if s['mod_log_channel_id'] else f'Режим: **{mode}**. Канал логов не задан.', ephemeral=True)

async def setup_automod(bot: commands.Bot, db_path: Path):
    await bot.add_cog(AutoMod(bot, db_path))
