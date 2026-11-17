"""Auto-updating member statistics panel for the server info channel."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("sijk.member_stats")
WHITE = discord.Color.from_rgb(242, 242, 242)


class MemberStats(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: Path):
        self.bot = bot
        self.db_path = Path(db_path)
        self.dirty: set[int] = set()
        self._init_db()
        self.refresh.start()

    def cog_unload(self):
        self.refresh.cancel()

    def connect(self):
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self):
        with closing(self.connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS member_stats (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                joins INTEGER NOT NULL DEFAULT 0,
                leaves INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL
            )""")
            db.commit()

    def _row(self, guild_id: int):
        with closing(self.connect()) as db:
            return db.execute(
                "SELECT * FROM member_stats WHERE guild_id=?", (guild_id,)
            ).fetchone()

    @staticmethod
    def _count(guild: discord.Guild) -> int:
        # A full member cache requires the Server Members privileged intent.
        # If it is incomplete, avoid presenting a partial count as accurate.
        if guild.member_count is not None and len(guild.members) >= guild.member_count:
            return sum(not member.bot for member in guild.members)
        return guild.member_count or len(guild.members)

    def _embed(self, guild: discord.Guild, row: sqlite3.Row) -> discord.Embed:
        embed = discord.Embed(
            title="СТАТИСТИКА СЕРВЕРА",
            description=(
                f"**Участников сейчас:** {self._count(guild)}\n"
                f"**Присоединилось:** {row['joins']}\n"
                f"**Покинуло сервер:** {row['leaves']}\n\n"
                "Счётчики входов и выходов ведутся с момента подключения статистики."
            ),
            color=WHITE,
        )
        embed.set_footer(text="SIJK SYSTEM • Обновляется автоматически")
        return embed

    async def publish(self, guild: discord.Guild) -> bool:
        row = self._row(guild.id)
        if row is None:
            return False
        channel = guild.get_channel(row["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            log.warning("Stats channel %s missing in guild %s", row["channel_id"], guild.id)
            return False
        embed = self._embed(guild, row)
        if row["message_id"]:
            try:
                message = await channel.fetch_message(row["message_id"])
                await message.edit(embed=embed)
                return True
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.exception("Cannot edit member stats in guild %s", guild.id)
                return False
        try:
            message = await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Cannot publish member stats in guild %s", guild.id)
            return False
        with closing(self.connect()) as db:
            db.execute("UPDATE member_stats SET message_id=? WHERE guild_id=?",
                       (message.id, guild.id))
            db.commit()
        return True

    @app_commands.command(name="stats_panel", description="Создать или обновить плашку статистики участников")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Канал, в котором должна находиться плашка")
    async def stats_panel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Нужно право «Управлять сервером».", ephemeral=True)
            return
        if interaction.guild is None or channel.guild.id != interaction.guild.id:
            await interaction.response.send_message("Выбери канал этого сервера.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        with closing(self.connect()) as db:
            db.execute(
                """INSERT INTO member_stats(guild_id,channel_id,started_at)
                   VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET
                   channel_id=excluded.channel_id,
                   message_id=CASE WHEN member_stats.channel_id=excluded.channel_id
                                   THEN member_stats.message_id ELSE NULL END""",
                (interaction.guild.id, channel.id, datetime.now(timezone.utc).isoformat()))
            db.commit()
        if await self.publish(interaction.guild):
            await interaction.followup.send(
                f"Плашка статистики работает в {channel.mention}. "
                "Счётчики входов и выходов сохраняются после перезапуска.", ephemeral=True)
        else:
            await interaction.followup.send(
                "Настройки сохранены, но сообщение отправить не удалось. "
                "Проверь права бота в выбранном канале и логи.", ephemeral=True)

    def _record(self, guild_id: int, column: str):
        # Column is only ever a hard-coded internal name.
        if column not in ("joins", "leaves"):
            return
        with closing(self.connect()) as db:
            cursor = db.execute(
                f"UPDATE member_stats SET {column}={column}+1 WHERE guild_id=?",
                (guild_id,))
            db.commit()
            if cursor.rowcount:
                self.dirty.add(guild_id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not member.bot:
            self._record(member.guild.id, "joins")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not member.bot:
            self._record(member.guild.id, "leaves")

    @tasks.loop(seconds=30)
    async def refresh(self):
        guild_ids = set(self.dirty)
        self.dirty.clear()
        for guild_id in guild_ids:
            guild = self.bot.get_guild(guild_id)
            if guild is not None and not await self.publish(guild):
                self.dirty.add(guild_id)

    @refresh.before_loop
    async def before_refresh(self):
        await self.bot.wait_until_ready()
        with closing(self.connect()) as db:
            rows = db.execute("SELECT guild_id FROM member_stats").fetchall()
        self.dirty.update(row["guild_id"] for row in rows)


async def setup_member_stats(bot: commands.Bot, db_path: Path):
    await bot.add_cog(MemberStats(bot, db_path))
