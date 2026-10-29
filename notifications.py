"""SIJK SYSTEM: YouTube and Twitch announcements in one Discord NEWS channel.

Requires: discord.py, aiohttp (installed with discord.py).
Optional environment: TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger('sijk.notifications')
ATOM = {'atom': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}
TWITCH_COLOR = 0x9146FF
YOUTUBE_COLOR = 0xFF4242
TELEGRAM_COLOR = 0x2AABEE


class Notifications(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: Path):
        self.bot = bot
        self.db_path = Path(db_path)
        self.session: aiohttp.ClientSession | None = None
        self.access_token: str | None = None
        self.token_expires = 0.0
        with closing(self.db()) as con:
            con.execute('''CREATE TABLE IF NOT EXISTS news_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                youtube_channel_id TEXT NOT NULL,
                twitch_login TEXT NOT NULL
            )''')
            con.execute('''CREATE TABLE IF NOT EXISTS news_state (
                guild_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                last_id TEXT NOT NULL,
                PRIMARY KEY(guild_id, source)
            )''')
            con.commit()
        self.poll.start()

    def db(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    async def cog_unload(self):
        self.poll.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    async def http(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self.session

    def state(self, guild_id: int, source: str):
        with closing(self.db()) as con:
            row = con.execute('SELECT last_id FROM news_state WHERE guild_id=? AND source=?',
                              (guild_id, source)).fetchone()
        return row['last_id'] if row else None

    def save_state(self, guild_id: int, source: str, value: str):
        with closing(self.db()) as con:
            con.execute('INSERT OR REPLACE INTO news_state VALUES (?,?,?)',
                        (guild_id, source, value))
            con.commit()

    async def resolve_youtube(self, value: str) -> str:
        value = value.strip()
        found = re.search(r'UC[A-Za-z0-9_-]{22}', value)
        if found:
            return found.group(0)
        if not re.fullmatch(r'https://(?:www\.)?youtube\.com/@[\w.-]+/?', value):
            raise ValueError('Нужна ссылка https://www.youtube.com/@имя или UC-ID канала.')
        session = await self.http()
        async with session.get(value, headers={'User-Agent': 'Mozilla/5.0'}) as resp:
            resp.raise_for_status()
            html = await resp.text()
        found = re.search(r'"(?:channelId|externalId)"\s*:\s*"(UC[A-Za-z0-9_-]{22})"', html)
        if not found:
            raise ValueError('Не удалось получить ID YouTube. Введи UC-ID канала.')
        return found.group(1)

    async def check_youtube(self, row, channel: discord.TextChannel):
        session = await self.http()
        feed = f'https://www.youtube.com/feeds/videos.xml?channel_id={row["youtube_channel_id"]}'
        async with session.get(feed) as resp:
            resp.raise_for_status()
            xml = await resp.text()
        root = ET.fromstring(xml)
        entries = root.findall('atom:entry', ATOM)
        if not entries:
            return
        # Process entries oldest first, but do not repost historical items on first setup.
        items = []
        for entry in entries:
            video_id = entry.findtext('yt:videoId', namespaces=ATOM)
            if video_id:
                title = entry.findtext('atom:title', default='Новое видео', namespaces=ATOM)
                items.append((video_id, title))
        if not items:
            return
        guild_id = row['guild_id']
        previous = self.state(guild_id, 'youtube')
        if previous is None:
            self.save_state(guild_id, 'youtube', items[0][0])
            return
        ids = [item[0] for item in items]
        if previous == ids[0]:
            return
        if previous in ids:
            unseen = list(reversed(items[:ids.index(previous)]))
        else:
            # Old checkpoint fell outside the feed: post only latest item to avoid spam.
            unseen = [items[0]]
        for video_id, title in unseen:
            url = f'https://www.youtube.com/watch?v={video_id}'
            embed = discord.Embed(
                title='✦ НОВОЕ ВИДЕО', url=url,
                description=f'**{discord.utils.escape_markdown(title[:250])}**\n\nСвежий ролик от SIJK уже на YouTube! ❤️',
                color=YOUTUBE_COLOR,
            )
            embed.set_image(url=f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg')
            embed.set_footer(text='YOUTUBE • SIJK')
            await channel.send(
                content="@everyone",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(everyone=True)
            )
            self.save_state(guild_id, 'youtube', video_id)

    
    async def check_twitch(self, row, channel: discord.TextChannel):
        login = row['twitch_login']
        session = await self.http()

        async with session.get(
            f'https://decapi.me/twitch/uptime/{login}',
            params={'offline_msg': 'OFFLINE'}
        ) as resp:
            resp.raise_for_status()
            status = (await resp.text()).strip()

        current = 'offline' if not status or status.upper() == 'OFFLINE' else 'live'
        guild_id = row['guild_id']
        previous = self.state(guild_id, 'twitch')

        if previous is None:
            self.save_state(guild_id, 'twitch', current)
            return

        if current == 'offline':
            if previous != 'offline':
                self.save_state(guild_id, 'twitch', 'offline')
            return

        if previous == 'live':
            return

        async with session.get(
            f'https://decapi.me/twitch/title/{login}'
        ) as resp:
            resp.raise_for_status()
            title = (await resp.text()).strip()

        async with session.get(
            f'https://decapi.me/twitch/game/{login}'
        ) as resp:
            resp.raise_for_status()
            game = (await resp.text()).strip()

        url = f'https://www.twitch.tv/{login}'

        embed = discord.Embed(
            title='✦ SIJK В ЭФИРЕ',
            url=url,
            description=(
                f'**{discord.utils.escape_markdown(title[:250] or "Стрим начался")}**'
                '\n\nЗалетай смотреть! 💜'
            ),
            color=TWITCH_COLOR,
        )

        if game:
            embed.add_field(
                name='Категория',
                value=game[:100],
                inline=False,
            )

        embed.set_image(
            url=(
                f'https://static-cdn.jtvnw.net/previews-ttv/'
                f'live_user_{login}-1280x720.jpg'
                f'?t={int(time.time())}'
            )
        )
        embed.set_footer(text='TWITCH • SIJK')

        await channel.send(
            content='@everyone',
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )

        self.save_state(guild_id, 'twitch', 'live')


    @tasks.loop(minutes=3)
    async def poll(self):
        with closing(self.db()) as con:
            settings = con.execute('SELECT * FROM news_settings').fetchall()
        for row in settings:
            guild = self.bot.get_guild(row['guild_id'])
            channel = guild.get_channel(row['channel_id']) if guild else None
            if not isinstance(channel, discord.TextChannel):
                continue
            for source, check in [('YouTube', self.check_youtube), ('Twitch', self.check_twitch)]:
                try:
                    await check(row, channel)
                except Exception:
                    log.exception('Ошибка %s для сервера %s', source, row['guild_id'])

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name='news_setup', description='Подключить YouTube и Twitch к каналу NEWS')
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel='Канал NEWS', youtube='Ссылка YouTube или UC-ID', twitch='Логин Twitch')
    async def news_setup(self, interaction: discord.Interaction, channel: discord.TextChannel,
                         youtube: str = 'https://www.youtube.com/@siJkx', twitch: str = 'sijkx'):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Нужны права администратора.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            youtube_id = await self.resolve_youtube(youtube)
        except (ValueError, aiohttp.ClientError, TimeoutError) as exc:
            await interaction.followup.send(f'Ошибка YouTube: {exc}', ephemeral=True)
            return
        twitch = twitch.strip().lower().removeprefix('@')
        if not re.fullmatch(r'[a-z0-9_]{3,25}', twitch):
            await interaction.followup.send('Нужен логин Twitch, например sijkx.', ephemeral=True)
            return
        with closing(self.db()) as con:
            old = con.execute('SELECT * FROM news_settings WHERE guild_id=?',
                              (interaction.guild.id,)).fetchone()
            con.execute('INSERT OR REPLACE INTO news_settings VALUES (?,?,?,?)',
                        (interaction.guild.id, channel.id, youtube_id, twitch))
            if old and old['youtube_channel_id'] != youtube_id:
                con.execute('DELETE FROM news_state WHERE guild_id=? AND source=?',
                            (interaction.guild.id, 'youtube'))
            if old and old['twitch_login'] != twitch:
                con.execute('DELETE FROM news_state WHERE guild_id=? AND source=?',
                            (interaction.guild.id, 'twitch'))
            con.commit()
        await interaction.followup.send(
            f'Готово! Уведомления настроены в {channel.mention}.\n'
            f'YouTube: `{youtube_id}`\nTwitch: `{twitch}`\n'
            'Первый опрос запомнит текущие публикации, не отправляя старые. '
            'Для Twitch нужны TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET на Bothost.',
            ephemeral=True,
        )

    @app_commands.command(name='news_test', description='Отправить три тестовые плашки в NEWS')
    @app_commands.default_permissions(administrator=True)
    async def news_test(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Нужны права администратора.', ephemeral=True)
            return
        with closing(self.db()) as con:
            row = con.execute('SELECT channel_id FROM news_settings WHERE guild_id=?',
                              (interaction.guild.id,)).fetchone()
        channel = interaction.guild.get_channel(row['channel_id']) if row else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message('Сначала используй /news_setup.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        samples = [
            ('✦ SIJK В ЭФИРЕ', 'Тестовое уведомление о стриме 💜', TWITCH_COLOR, 'TWITCH • SIJK'),
            ('✦ НОВОЕ ВИДЕО', 'Тестовое уведомление о ролике ❤️', YOUTUBE_COLOR, 'YOUTUBE • SIJK'),
            ('✦ НОВОСТИ ОТ SIJK', 'Тестовое уведомление о посте 🩵', TELEGRAM_COLOR, 'TELEGRAM • SIJK'),
        ]
        for title, description, color, footer in samples:
            embed = discord.Embed(title=title, description=description, color=color)
            embed.set_footer(text=footer + ' • ТЕСТ')
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await interaction.followup.send(f'Три тестовые плашки отправлены в {channel.mention}.', ephemeral=True)


async def setup_notifications(bot: commands.Bot, db_path: Path):
    await bot.add_cog(Notifications(bot, db_path))
