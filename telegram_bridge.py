from __future__ import annotations
import asyncio
import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from notify_roles import notify_mention

log = logging.getLogger('sijk.telegram')


class TelegramBridge(commands.Cog):
    def __init__(self, bot, db_path):
        self.bot, self.path = bot, str(db_path)
        self.token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        self.task = None
        with closing(self.db()) as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS telegram_bridge_config (
                    guild_id INTEGER PRIMARY KEY, discord_channel_id INTEGER NOT NULL,
                    telegram_channel TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS telegram_bridge_posts (
                    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    PRIMARY KEY(chat_id,message_id));
                CREATE TABLE IF NOT EXISTS telegram_bridge_state (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            con.commit()

    def db(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    async def cog_load(self):
        if self.token:
            self.task = asyncio.create_task(self.poll())
        else:
            log.warning('TELEGRAM_BOT_TOKEN не задан; Telegram отключён')

    def cog_unload(self):
        if self.task:
            self.task.cancel()

    @app_commands.command(name='telegram_setup', description='Настроить уведомления о постах Telegram')
    @app_commands.default_permissions(administrator=True)
    async def telegram_setup(self, interaction: discord.Interaction, channel: discord.TextChannel,
                             telegram_channel: str = 'siJkgg'):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Только администратор.', ephemeral=True)
            return
        slug = telegram_channel.strip().replace('https://t.me/', '').lstrip('@').strip('/')
        if not slug or '/' in slug:
            await interaction.response.send_message('Укажите username канала, например siJkgg.', ephemeral=True)
            return
        with closing(self.db()) as con:
            con.execute('''INSERT INTO telegram_bridge_config
                (guild_id,discord_channel_id,telegram_channel,enabled) VALUES (?,?,?,1)
                ON CONFLICT(guild_id) DO UPDATE SET
                discord_channel_id=excluded.discord_channel_id,
                telegram_channel=excluded.telegram_channel,enabled=1''',
                (interaction.guild_id, channel.id, slug))
            con.commit()
        await interaction.response.send_message(
            f'Новые публикации @{slug} будут появляться в {channel.mention}.', ephemeral=True)

    @app_commands.command(name='telegram_off', description='Отключить уведомления Telegram')
    @app_commands.default_permissions(administrator=True)
    async def telegram_off(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Только администратор.', ephemeral=True)
            return
        with closing(self.db()) as con:
            con.execute('UPDATE telegram_bridge_config SET enabled=0 WHERE guild_id=?',
                        (interaction.guild_id,))
            con.commit()
        await interaction.response.send_message('Уведомления отключены.', ephemeral=True)

    async def poll(self):
        await self.bot.wait_until_ready()
        with closing(self.db()) as con:
            row = con.execute("SELECT value FROM telegram_bridge_state WHERE key='offset'").fetchone()
        offset = int(row['value']) if row else None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
            while not self.bot.is_closed():
                try:
                    params = {'timeout': 25, 'allowed_updates': json.dumps(['channel_post'])}
                    if offset is not None:
                        params['offset'] = offset
                    async with session.get(f'https://api.telegram.org/bot{self.token}/getUpdates',
                                           params=params) as response:
                        data = await response.json(content_type=None)
                    if not data.get('ok'):
                        log.warning('Telegram API: %s', str(data)[:250])
                        await asyncio.sleep(20)
                        continue
                    for update in data.get('result', []):
                        await self.handle(update)
                        offset = update['update_id'] + 1
                        with closing(self.db()) as con:
                            con.execute("""INSERT INTO telegram_bridge_state(key,value)
                                VALUES ('offset',?) ON CONFLICT(key) DO UPDATE SET
                                value=excluded.value""", (str(offset),))
                            con.commit()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception('Ошибка Telegram-моста')
                    await asyncio.sleep(10)

    async def handle(self, update):
        post = update.get('channel_post')
        if not post:
            return
        chat = post.get('chat', {})
        slug = str(chat.get('username', '')).lower()
        chat_id, message_id = chat.get('id'), post.get('message_id')
        if not slug or not chat_id or not message_id:
            return
        with closing(self.db()) as con:
            if con.execute('SELECT 1 FROM telegram_bridge_posts WHERE chat_id=? AND message_id=?',
                           (chat_id, message_id)).fetchone():
                return
            targets = con.execute('''SELECT * FROM telegram_bridge_config
                WHERE enabled=1 AND lower(telegram_channel)=?''', (slug,)).fetchall()
        if not targets:
            return
        body = (post.get('text') or post.get('caption') or '').strip()
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        # Skip a standalone heading if the post begins with one.
        if len(lines) > 1 and (
            lines[0].startswith('#')
            or ('|' in lines[0] and lines[0] == lines[0].upper())
        ):
            lines = lines[1:]
        preview = lines[0][:350] if lines else 'В Telegram-канале вышел новый пост.'
        link = f'https://t.me/{slug}/{message_id}'
        embed = discord.Embed(
            title='✈️ SIJK SYSTEM | НОВЫЙ ПОСТ',
            description=discord.utils.escape_markdown(preview),
            url=link,
            color=discord.Color.from_rgb(105, 200, 245)
        )

        view = discord.ui.View()
        view.add_item(discord.ui.Button(
        label='Читать в Telegram',
        style=discord.ButtonStyle.link,
        url=link
        ))
        success = False
        for target in targets:
            guild = self.bot.get_guild(target['guild_id'])
            channel = guild.get_channel(target['discord_channel_id']) if guild else None

            if isinstance(channel, discord.TextChannel):
                try:
                    mention = notify_mention(
                        Path(self.path), guild, 'telegram'
                    )

                    await channel.send(
                        content=f'✈️ **Новый пост в Telegram!** {mention}'.strip(),
                        embed=embed,
                        view=view,
                        allowed_mentions=discord.AllowedMentions(
                            roles=True,
                            everyone=False
                        )
                    )
                    success = True

                except discord.HTTPException:
                    log.exception('Не удалось отправить Telegram-пост')
        if success:
            with closing(self.db()) as con:
                con.execute('INSERT OR IGNORE INTO telegram_bridge_posts(chat_id,message_id) VALUES (?,?)',
                            (chat_id, message_id))
                con.commit()


async def setup_telegram(bot: commands.Bot, db_path: Path):
    await bot.add_cog(TelegramBridge(bot, db_path))
