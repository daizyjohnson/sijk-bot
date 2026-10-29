from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from urllib.parse import urlparse
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from tickets import setup_tickets
from voice import setup_voice
from welcome import setup_welcome

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0") or 0)
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "sijk.sqlite3"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sijk")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with closing(db()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                staff_role_id INTEGER,
                mod_log_channel_id INTEGER,
                message_log_channel_id INTEGER,
                request_log_channel_id INTEGER,
                complaint_form_url TEXT,
                suggestion_form_url TEXT,
                boosty_url TEXT DEFAULT 'https://boosty.to/sijk',
                twitch_url TEXT DEFAULT 'https://www.twitch.tv/sijkx',
                youtube_url TEXT DEFAULT 'https://www.youtube.com/@siJkx',
                telegram_url TEXT DEFAULT 'https://t.me/siJkgg'
            );
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                active INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        con.commit()


def ensure_guild(guild_id: int) -> None:
    with closing(db()) as con:
        con.execute("INSERT OR IGNORE INTO guild_config(guild_id) VALUES (?)", (guild_id,))
        con.commit()


def config(guild_id: int) -> sqlite3.Row:
    ensure_guild(guild_id)
    with closing(db()) as con:
        return con.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)).fetchone()


def set_config(guild_id: int, **values: object) -> None:
    ensure_guild(guild_id)
    allowed = {
        "staff_role_id", "mod_log_channel_id", "message_log_channel_id",
        "request_log_channel_id", "complaint_form_url", "suggestion_form_url",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key} = ?" for key in values)
    with closing(db()) as con:
        con.execute(
            f"UPDATE guild_config SET {assignments} WHERE guild_id = ?",
            (*values.values(), guild_id),
        )
        con.commit()


async def send_log(guild: discord.Guild, field: str, embed: discord.Embed) -> None:
    row = config(guild.id)
    channel_id = row[field]
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Не удалось отправить лог в %s", channel_id)


def has_staff_access(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.moderate_members:
        return True
    role_id = config(interaction.guild.id)["staff_role_id"]
    return bool(role_id and any(role.id == role_id for role in interaction.user.roles))


async def staff_check(interaction: discord.Interaction) -> bool:
    if has_staff_access(interaction):
        return True
    await interaction.response.send_message("Эта команда доступна только модерации.", ephemeral=True)
    return False


def can_act(interaction: discord.Interaction, member: discord.Member, permission: str) -> str | None:
    """Check both actor and bot hierarchy, not just a custom staff role."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return "Команда доступна только на сервере."
    actor = interaction.user
    bot_member = interaction.guild.me
    if not getattr(actor.guild_permissions, permission, False):
        return f"Для этого действия требуется право `{permission}`."
    if member.id == interaction.guild.owner_id or member.guild_permissions.administrator:
        return "Нельзя применять наказание к владельцу или администратору."
    if member.id == actor.id or member.bot:
        return "Нельзя применять наказание к себе или боту."
    if actor.id != interaction.guild.owner_id and actor.top_role <= member.top_role:
        return "Роль участника должна быть ниже вашей роли."
    if bot_member is None or bot_member.top_role <= member.top_role:
        return "Роль бота должна находиться выше роли участника."
    if not getattr(bot_member.guild_permissions, permission, False):
        return f"У бота отсутствует право `{permission}`."
    return None

def valid_form_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in {"docs.google.com", "forms.gle"}

async def reject(interaction: discord.Interaction, reason: str | None) -> bool:
    if reason:
        await interaction.response.send_message(reason, ephemeral=True)
        return True
    return False


class LinkPanel(discord.ui.View):
    def __init__(self, row: sqlite3.Row):
        super().__init__(timeout=None)
        links = [
            ("Boosty", row["boosty_url"], discord.ButtonStyle.link, "💜"),
            ("Twitch", row["twitch_url"], discord.ButtonStyle.link, "🟣"),
            ("YouTube", row["youtube_url"], discord.ButtonStyle.link, "🔴"),
            ("Telegram", row["telegram_url"], discord.ButtonStyle.link, "✈️"),
        ]
        for label, url, style, emoji in links:
            self.add_item(discord.ui.Button(label=label, url=url, style=style, emoji=emoji))


class FormPanel(discord.ui.View):
    def __init__(self, *, label: str, url: str, emoji: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label=label, url=url, emoji=emoji))


class SijkBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.synced = False

    async def setup_hook(self) -> None:
        init_db()
        await setup_tickets(self, DB_PATH, config)
        await setup_voice(self, DB_PATH)
        await setup_welcome(self, DB_PATH)
        if DEV_GUILD_ID:
            guild = discord.Object(id=DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Команды синхронизированы для тестового сервера %s", DEV_GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Глобальные команды синхронизированы")


bot = SijkBot()


@bot.event
async def on_ready() -> None:
    log.info("Бот запущен как %s (%s)", bot.user, bot.user.id if bot.user else "?")
    await bot.change_presence(
        activity=discord.CustomActivity(name="✦ Бот-сиджик")
    )


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if not message.guild or message.author.bot:
        return
    settings = config(message.guild.id)
    if message.channel.id in {settings["mod_log_channel_id"], settings["message_log_channel_id"], settings["request_log_channel_id"]}:
        return
    text = message.content or "*(текст недоступен или сообщение содержало только вложение)*"
    embed = discord.Embed(title="Сообщение удалено", description=text[:4000], color=discord.Color.red())
    embed.add_field(name="Автор", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
    embed.add_field(name="Канал", value=message.channel.mention, inline=False)
    if message.attachments:
        embed.add_field(name="Вложения", value="\n".join(a.url for a in message.attachments)[:1000], inline=False)
    embed.timestamp = discord.utils.utcnow()
    await send_log(message.guild, "message_log_channel_id", embed)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if not before.guild or before.author.bot or before.content == after.content:
        return
    settings = config(before.guild.id)
    if before.channel.id in {settings["mod_log_channel_id"], settings["message_log_channel_id"], settings["request_log_channel_id"]}:
        return
    embed = discord.Embed(title="Сообщение изменено", color=discord.Color.orange(), url=after.jump_url)
    embed.add_field(name="Автор", value=f"{before.author.mention} (`{before.author.id}`)", inline=False)
    embed.add_field(name="Было", value=(before.content or "—")[:1000], inline=False)
    embed.add_field(name="Стало", value=(after.content or "—")[:1000], inline=False)
    embed.add_field(name="Канал", value=before.channel.mention, inline=False)
    embed.timestamp = discord.utils.utcnow()
    await send_log(before.guild, "message_log_channel_id", embed)


setup_group = app_commands.Group(name="setup", description="Настройка sijk System")


@setup_group.command(name="channels", description="Привязать закрытые каналы логов")
@app_commands.describe(mod_logs="Логи модерации", message_logs="Удалённые и изменённые сообщения", requests="Заявки из форм")
@app_commands.default_permissions(administrator=True)
async def setup_channels(
    interaction: discord.Interaction,
    mod_logs: discord.TextChannel,
    message_logs: discord.TextChannel,
    requests: Optional[discord.TextChannel] = None,
) -> None:
    if not interaction.guild or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Нужны права администратора.", ephemeral=True)
        return
    set_config(
        interaction.guild.id,
        mod_log_channel_id=mod_logs.id,
        message_log_channel_id=message_logs.id,
        request_log_channel_id=requests.id if requests else None,
    )
    await interaction.response.send_message("Каналы логов сохранены.", ephemeral=True)


@setup_group.command(name="staff", description="Указать роль модераторов")
@app_commands.default_permissions(administrator=True)
async def setup_staff(interaction: discord.Interaction, role: discord.Role) -> None:
    if not interaction.guild or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Нужны права администратора.", ephemeral=True)
        return
    set_config(interaction.guild.id, staff_role_id=role.id)
    await interaction.response.send_message(f"Роль модераторов: {role.mention}", ephemeral=True)


@setup_group.command(name="forms", description="Сохранить ссылки на Google Формы")
@app_commands.default_permissions(administrator=True)
async def setup_forms(interaction: discord.Interaction, complaints_url: str, suggestions_url: str) -> None:
    if not interaction.guild or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Нужны права администратора.", ephemeral=True)
        return
    if not valid_form_url(complaints_url) or not valid_form_url(suggestions_url):
        await interaction.response.send_message("Нужны ссылки на Google Формы (docs.google.com или forms.gle) по HTTPS.", ephemeral=True)
        return
    set_config(interaction.guild.id, complaint_form_url=complaints_url, suggestion_form_url=suggestions_url)
    await interaction.response.send_message("Ссылки на формы сохранены.", ephemeral=True)


@setup_group.command(name="status", description="Показать текущую настройку")
@app_commands.default_permissions(administrator=True)
async def setup_status(interaction: discord.Interaction) -> None:
    if not interaction.guild or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Нужны права администратора.", ephemeral=True)
        return
    row = config(interaction.guild.id)
    def channel_value(key: str) -> str:
        return f"<#{row[key]}>" if row[key] else "не задан"
    embed = discord.Embed(title="Настройка sijk System", color=discord.Color.blurple())
    embed.add_field(name="Роль модераторов", value=f"<@&{row['staff_role_id']}>" if row["staff_role_id"] else "не задана", inline=False)
    embed.add_field(name="Логи модерации", value=channel_value("mod_log_channel_id"))
    embed.add_field(name="Логи сообщений", value=channel_value("message_log_channel_id"))
    embed.add_field(name="Логи заявок", value=channel_value("request_log_channel_id"))
    embed.add_field(name="Форма жалоб", value="задана" if row["complaint_form_url"] else "не задана")
    embed.add_field(name="Форма предложений", value="задана" if row["suggestion_form_url"] else "не задана")
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(setup_group)


@bot.tree.command(name="warn", description="Выдать предупреждение участнику")
@app_commands.check(staff_check)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return
    if await reject(interaction, can_act(interaction, member, "moderate_members")):
        return
    with closing(db()) as con:
        cursor = con.execute(
            "INSERT INTO warnings(guild_id,user_id,moderator_id,reason) VALUES (?,?,?,?)",
            (interaction.guild.id, member.id, interaction.user.id, reason),
        )
        warning_id = cursor.lastrowid
        count = con.execute(
            "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=? AND active=1",
            (interaction.guild.id, member.id),
        ).fetchone()[0]
        con.commit()

    action = "без автоматического наказания"
    duration = {2: timedelta(minutes=10), 3: timedelta(hours=1), 4: timedelta(hours=24)}.get(count)
    if duration:
        try:
            await member.timeout(duration, reason=f"Предупреждение #{warning_id}: {reason}")
            action = f"тайм-аут на {discord.utils.format_dt(discord.utils.utcnow() + duration, style='R')}"
        except discord.HTTPException:
            action = "не удалось выдать автоматический тайм-аут"
    elif count >= 5:
        action = "требуется решение модератора о бане"

    embed = discord.Embed(title=f"Предупреждение #{warning_id}", color=discord.Color.orange())
    embed.add_field(name="Участник", value=f"{member.mention} (`{member.id}`)", inline=False)
    embed.add_field(name="Причина", value=reason[:1024], inline=False)
    embed.add_field(name="Активных предупреждений", value=str(count))
    embed.add_field(name="Результат", value=action)
    embed.set_footer(text=f"Модератор: {interaction.user}")
    embed.timestamp = discord.utils.utcnow()
    await interaction.response.send_message(embed=embed)
    await send_log(interaction.guild, "mod_log_channel_id", embed)
    try:
        await member.send(f"На сервере **{interaction.guild.name}** вам выдано предупреждение.\nПричина: {reason}\nАктивных предупреждений: {count}.")
    except discord.HTTPException:
        pass


@bot.tree.command(name="warnings", description="Посмотреть предупреждения участника")
@app_commands.check(staff_check)
async def warnings(interaction: discord.Interaction, member: discord.Member) -> None:
    with closing(db()) as con:
        rows = con.execute(
            "SELECT id,moderator_id,reason,created_at FROM warnings WHERE guild_id=? AND user_id=? AND active=1 ORDER BY id DESC LIMIT 10",
            (interaction.guild_id, member.id),
        ).fetchall()
    if not rows:
        await interaction.response.send_message(f"У {member.mention} нет активных предупреждений.", ephemeral=True)
        return
    lines = [f"`#{r['id']}` {r['reason']} — <@{r['moderator_id']}> ({r['created_at']} UTC)" for r in rows]
    embed = discord.Embed(title=f"Предупреждения: {member}", description="\n".join(lines), color=discord.Color.orange())
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="unwarn", description="Снять предупреждение по номеру")
@app_commands.check(staff_check)
async def unwarn(interaction: discord.Interaction, warning_id: int, reason: str = "Снято модератором") -> None:
    with closing(db()) as con:
        cur = con.execute(
            "UPDATE warnings SET active=0 WHERE id=? AND guild_id=? AND active=1",
            (warning_id, interaction.guild_id),
        )
        con.commit()
    if not cur.rowcount:
        await interaction.response.send_message("Активное предупреждение с таким номером не найдено.", ephemeral=True)
        return
    await interaction.response.send_message(f"Предупреждение `#{warning_id}` снято. Причина: {reason}")


@bot.tree.command(name="timeout", description="Выдать участнику тайм-аут")
@app_commands.check(staff_check)
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str) -> None:
    if await reject(interaction, can_act(interaction, member, "moderate_members")):
        return
    await member.timeout(timedelta(minutes=minutes), reason=reason)
    embed = discord.Embed(title="Выдан тайм-аут", color=discord.Color.orange())
    embed.description = f"{member.mention} — **{minutes} мин.**\nПричина: {reason}\nМодератор: {interaction.user.mention}"
    await interaction.response.send_message(embed=embed)
    if interaction.guild:
        await send_log(interaction.guild, "mod_log_channel_id", embed)


@bot.tree.command(name="untimeout", description="Снять тайм-аут")
@app_commands.check(staff_check)
async def untimeout(interaction: discord.Interaction, member: discord.Member, reason: str = "Снято модератором") -> None:
    if await reject(interaction, can_act(interaction, member, "moderate_members")):
        return
    await member.timeout(None, reason=reason)
    await interaction.response.send_message(f"Тайм-аут с {member.mention} снят.")


@bot.tree.command(name="kick", description="Выгнать участника")
@app_commands.check(staff_check)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
    if await reject(interaction, can_act(interaction, member, "kick_members")):
        return
    await member.kick(reason=reason)
    await interaction.response.send_message(f"{member} выгнан. Причина: {reason}")


@bot.tree.command(name="ban", description="Заблокировать участника")
@app_commands.check(staff_check)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str, delete_messages_hours: app_commands.Range[int, 0, 168] = 0) -> None:
    if await reject(interaction, can_act(interaction, member, "ban_members")):
        return
    await interaction.guild.ban(member, reason=reason, delete_message_seconds=delete_messages_hours * 3600)
    await interaction.response.send_message(f"{member} заблокирован. Причина: {reason}")


@bot.tree.command(name="purge", description="Удалить последние сообщения в канале")
@app_commands.check(staff_check)
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]) -> None:
    if not isinstance(interaction.channel, discord.TextChannel):
        return
    if not interaction.user.guild_permissions.manage_messages or not interaction.channel.permissions_for(interaction.guild.me).manage_messages:
        await interaction.response.send_message("Нужно право управления сообщениями у модератора и бота.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Удалено сообщений: {len(deleted)}", ephemeral=True)


@bot.tree.command(name="panel", description="Опубликовать одну из панелей")
@app_commands.choices(kind=[
    app_commands.Choice(name="Поддержать", value="donate"),
    app_commands.Choice(name="Жалобы", value="complaints"),
    app_commands.Choice(name="Предложения", value="suggestions"),
])
@app_commands.default_permissions(administrator=True)
async def panel(interaction: discord.Interaction, kind: app_commands.Choice[str]) -> None:
    if not interaction.guild:
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Панели публикует только администрация.", ephemeral=True)
        return
    row = config(interaction.guild.id)
    if kind.value == "donate":
        embed = discord.Embed(
            title="Поддержать sijk",
            description="Спасибо за поддержку! Выберите удобную площадку ниже.",
            color=discord.Color.purple(),
        )
        await interaction.channel.send(embed=embed, view=LinkPanel(row))
    else:
        is_complaint = kind.value == "complaints"
        url = row["complaint_form_url" if is_complaint else "suggestion_form_url"]
        if not url:
            await interaction.response.send_message("Сначала задайте ссылки командой `/setup forms`.", ephemeral=True)
            return
        title = "Подать жалобу" if is_complaint else "Предложить идею"
        description = "Заполните форму максимально подробно. Ответ увидит только администрация." if is_complaint else "Расскажите, что можно добавить или улучшить на сервере."
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        view = FormPanel(label=title, url=url, emoji="🛡️" if is_complaint else "💡")
        await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("Панель опубликована.", ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    log.exception("Ошибка команды", exc_info=error)
    text = "Команда не выполнена. Проверьте права бота и расположение его роли."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


if not TOKEN:
    raise RuntimeError("Не задан DISCORD_TOKEN или BOT_TOKEN")

bot.run(TOKEN, log_handler=None)

