"""SIJK SYSTEM: temporary welcome messages with SQLite-backed cleanup."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("sijk.welcome")
WELCOME_SECONDS = 15 * 60
BLUE = discord.Color.from_rgb(100, 185, 237)


class WelcomeManager(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: Path):
        self.bot = bot
        self.db_path = Path(db_path)
        self._init_db()
        self.cleanup.start()

    def cog_unload(self):
        self.cleanup.cancel()

    def db(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self):
        with closing(self.db()) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS welcome_settings (
                    guild_id INTEGER PRIMARY KEY,
                    welcome_channel_id INTEGER NOT NULL,
                    rules_channel_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS welcome_pending (
                    message_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    delete_after INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_welcome_due
                    ON welcome_pending(delete_after);
            """)
            con.commit()

    @app_commands.command(name="welcome_setup", description="Настроить временные приветствия новичков")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.describe(welcome="Канал приветствий", rules="Канал с правилами")
    async def welcome_setup(self, interaction: discord.Interaction,
                            welcome: discord.TextChannel, rules: discord.TextChannel):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Нужно право «Управлять сервером».", ephemeral=True)
            return
        if welcome.guild.id != interaction.guild.id or rules.guild.id != interaction.guild.id:
            await interaction.response.send_message("Оба канала должны быть на этом сервере.", ephemeral=True)
            return
        with closing(self.db()) as con:
            con.execute("""INSERT INTO welcome_settings(guild_id,welcome_channel_id,rules_channel_id)
                VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET
                welcome_channel_id=excluded.welcome_channel_id,
                rules_channel_id=excluded.rules_channel_id""",
                (interaction.guild.id, welcome.id, rules.id))
            con.commit()
        await interaction.response.send_message(
            f"Готово! Новички получат голубое приветствие в {welcome.mention} "
            f"со ссылкой на {rules.mention}. Сообщение удалится через 15 минут.", ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        with closing(self.db()) as con:
            row = con.execute("SELECT * FROM welcome_settings WHERE guild_id=?", (member.guild.id,)).fetchone()
        if row is None:
            return
        channel = member.guild.get_channel(row["welcome_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            log.warning("Welcome channel %s not found", row["welcome_channel_id"])
            return
        rules_mention = f'<#{row["rules_channel_id"]}>'
        embed = discord.Embed(
            title="✦ ДОБРО ПОЖАЛОВАТЬ В SIJK SQUAD",
            description=(
                f"Рады видеть тебя на сервере, {member.mention}! 🩵\n\n"
                f"Перед началом общения ознакомься с правилами в {rules_mention}. "
                "После подтверждения правил тебе откроются каналы сообщества.\n\n"
                "*Это приветствие исчезнет через 15 минут.*"
            ),
            color=BLUE,
        )
        embed.set_footer(text="SIJK SYSTEM • Welcome")
        try:
            message = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
            )
        except discord.HTTPException:
            log.exception("Failed to send welcome for member %s", member.id)
            return
        with closing(self.db()) as con:
            con.execute("INSERT OR REPLACE INTO welcome_pending(message_id,channel_id,delete_after) VALUES(?,?,?)",
                        (message.id, channel.id, int(time.time()) + WELCOME_SECONDS))
            con.commit()


    @commands.Cog.listener()
async def on_member_remove(self, member: discord.Member):
    log.info(
        "LEAVE EVENT: пользователь %s (%s) покинул сервер %s",
        member.name,
        member.id,
        member.guild.name,
    )

    if member.bot:
        return

        with closing(self.db()) as con:
            row = con.execute(
                "SELECT welcome_channel_id FROM welcome_settings WHERE guild_id=?",
                (member.guild.id,),
            ).fetchone()

        if row is None:
            return

        channel = member.guild.get_channel(row["welcome_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return

        embed = discord.Embed(
            title="✦ ТАКТИЧЕСКОЕ ОТСТУПЛЕНИЕ",
            description=(
                f"**{discord.utils.escape_markdown(member.display_name)}** "
                "покинул сервер.\n\n"
                "Назовём это ротацией."
            ),
            color=discord.Color.from_rgb(224, 68, 82),
        )
        embed.set_footer(text="SIJK SYSTEM • Goodbye")

        try:
            message = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("Не удалось отправить уведомление о выходе")
            return

        with closing(self.db()) as con:
            con.execute(
                "INSERT OR REPLACE INTO welcome_pending"
                "(message_id,channel_id,delete_after) VALUES(?,?,?)",
                (message.id, channel.id, int(time.time()) + WELCOME_SECONDS),
            )
            con.commit()

    
    @tasks.loop(seconds=30)
    async def cleanup(self):
        now = int(time.time())
        with closing(self.db()) as con:
            rows = con.execute("SELECT * FROM welcome_pending WHERE delete_after<=? ORDER BY delete_after LIMIT 50", (now,)).fetchall()
        for row in rows:
            channel = self.bot.get_channel(row["channel_id"])
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(row["channel_id"])
                except discord.NotFound:
                    self._forget(row["message_id"])
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not access welcome channel %s", row["channel_id"])
                    continue
            if not isinstance(channel, discord.TextChannel):
                self._forget(row["message_id"])
                continue
            try:
                await channel.get_partial_message(row["message_id"]).delete()
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not delete welcome %s", row["message_id"])
                continue
            self._forget(row["message_id"])

    def _forget(self, message_id: int):
        with closing(self.db()) as con:
            con.execute("DELETE FROM welcome_pending WHERE message_id=?", (message_id,))
            con.commit()

    @cleanup.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()


async def setup_welcome(bot: commands.Bot, db_path: Path):
    await bot.add_cog(WelcomeManager(bot, db_path))
