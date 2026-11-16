"""SIJK CHECK: submissions, evidence history and a persistent public entry panel."""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import discord
from discord import app_commands
from discord.ext import commands

RED = 0xB8202B
RIOT_ID_PATTERN = re.compile(r"^(.{1,32})#(.{1,16})$", re.DOTALL)


def normalize_riot_id(value: str) -> tuple[str, str]:
    value = value.strip()
    match = RIOT_ID_PATTERN.fullmatch(value)
    if not match or not match.group(1).strip() or not match.group(2).strip():
        raise ValueError("Укажи Riot ID в формате Ник#Тег.")
    return value, value.casefold()


def tracker_url(riot_id: str) -> str:
    return "https://tracker.gg/valorant/profile/riot/" + quote(riot_id, safe="") + "/overview"


def parse_evidence(text: str) -> list[str]:
    links = []
    for line in text.splitlines():
        url = line.strip()
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Каждое доказательство должно быть отдельной HTTPS-ссылкой.")
        if len(url) > 1500:
            raise ValueError("Ссылка слишком длинная.")
        if url not in links:
            links.append(url)
    if not links:
        raise ValueError("Добавь хотя бы одну ссылку на доказательства.")
    return links


class CheckStore:
    def __init__(self, path: Path):
        self.path = path
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS check_settings (
                    guild_id INTEGER PRIMARY KEY,
                    public_channel_id INTEGER,
                    review_channel_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS check_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    riot_id TEXT NOT NULL,
                    riot_key TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    review_message_id INTEGER,
                    reviewer_id INTEGER,
                    reviewer_comment TEXT,
                    reviewed_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(guild_id, riot_key)
                );
                CREATE TABLE IF NOT EXISTS check_reporters (
                    case_id INTEGER NOT NULL REFERENCES check_cases(id),
                    user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(case_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS check_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(case_id, user_id, url)
                );
                CREATE TABLE IF NOT EXISTS check_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );
            """)
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def settings(self, guild_id: int):
        with closing(self.connect()) as db:
            return db.execute("SELECT * FROM check_settings WHERE guild_id=?", (guild_id,)).fetchone()

    def configure(self, guild_id: int, public_id: int, review_id: int):
        with closing(self.connect()) as db:
            db.execute("""INSERT INTO check_settings VALUES(?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                public_channel_id=excluded.public_channel_id,
                review_channel_id=excluded.review_channel_id""", (guild_id, public_id, review_id))
            db.commit()

    def submit(self, guild_id: int, user_id: int, riot_id: str, reason: str, links: list[str]):
        riot_id, key = normalize_riot_id(riot_id)
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO check_cases(guild_id,riot_id,riot_key,updated_at)
                VALUES(?,?,?,?)""", (guild_id, riot_id, key, now))
            case = db.execute("SELECT * FROM check_cases WHERE guild_id=? AND riot_key=?", (guild_id, key)).fetchone()
            previous = db.execute("SELECT 1 FROM check_reporters WHERE case_id=? AND user_id=?",
                                  (case["id"], user_id)).fetchone()
            if not previous:
                db.execute("INSERT INTO check_reporters VALUES(?,?,?,?)", (case["id"], user_id, reason, now))
            new_links = 0
            for link in links:
                cur = db.execute("INSERT OR IGNORE INTO check_evidence(case_id,user_id,url,created_at) VALUES(?,?,?,?)",
                                 (case["id"], user_id, link, now))
                new_links += cur.rowcount
            if previous and not new_links:
                db.commit()
                return case["id"], "unchanged"
            if case["state"] != "pending":
                db.execute("UPDATE check_cases SET state='pending',reviewer_id=NULL,reviewer_comment=NULL,reviewed_at=NULL WHERE id=?",
                           (case["id"],))
            db.execute("UPDATE check_cases SET updated_at=? WHERE id=?", (now, case["id"]))
            db.execute("INSERT INTO check_events(case_id,user_id,event,detail,created_at) VALUES(?,?,?,?,?)",
                       (case["id"], user_id, "supplement" if previous else "report", reason, now))
            db.commit()
            return case["id"], "supplement" if previous else "new"

    def case(self, case_id: int):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM check_cases WHERE id=?", (case_id,)).fetchone()
            count = db.execute("SELECT COUNT(*) FROM check_reporters WHERE case_id=?", (case_id,)).fetchone()[0]
            return row, count

    def my_cases(self, guild_id: int, user_id: int):
        with closing(self.connect()) as db:
            return db.execute("""SELECT c.* FROM check_cases c JOIN check_reporters r ON r.case_id=c.id
                WHERE c.guild_id=? AND r.user_id=? ORDER BY c.updated_at DESC LIMIT 10""",
                              (guild_id, user_id)).fetchall()


class ReportModal(discord.ui.Modal, title="SIJK CHECK • Заявка"):
    riot_id = discord.ui.TextInput(label="Riot ID (Ник#Тег)", max_length=50)
    reason = discord.ui.TextInput(label="Причина подозрения", style=discord.TextStyle.paragraph, max_length=1000)
    evidence = discord.ui.TextInput(label="Ссылки на доказательства (по одной в строке)",
                                    style=discord.TextStyle.paragraph, max_length=4000)

    def __init__(self, cog: "SijkCheck", riot_id: str = ""):
        super().__init__()
        self.cog = cog
        if riot_id:
            self.riot_id.default = riot_id

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        settings = self.cog.store.settings(interaction.guild.id)
        if not settings or interaction.channel_id != settings["public_channel_id"]:
            await interaction.response.send_message("Заявки принимаются только в канале SIJK CHECK.", ephemeral=True)
            return
        try:
            links = parse_evidence(str(self.evidence))
            case_id, outcome = self.cog.store.submit(
                interaction.guild.id, interaction.user.id, str(self.riot_id), str(self.reason), links)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if outcome == "unchanged":
            await interaction.response.send_message(
                "Твоя заявка уже есть. Новых ссылок не обнаружено.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Доказательства добавлены к существующей заявке." if outcome == "supplement"
            else "Заявка принята! Она сохранена в SIJK CHECK.", ephemeral=True)
        await self.cog.notify_review(interaction.guild, case_id)


class CheckPanel(discord.ui.View):
    def __init__(self, cog: "SijkCheck"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Подать заявку", style=discord.ButtonStyle.danger,
                       custom_id="sijk_check:report", emoji="📝")
    async def report(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog.in_public_channel(interaction):
            await interaction.response.send_message("Открой канал SIJK CHECK.", ephemeral=True)
            return
        await interaction.response.send_modal(ReportModal(self.cog))

    @discord.ui.button(label="Мои заявки", style=discord.ButtonStyle.secondary,
                       custom_id="sijk_check:mine", emoji="📋")
    async def mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog.in_public_channel(interaction):
            await interaction.response.send_message("Открой канал SIJK CHECK.", ephemeral=True)
            return
        cases = self.cog.store.my_cases(interaction.guild.id, interaction.user.id)
        labels = {"pending": "На рассмотрении", "approved": "Добавлен в базу", "approved_pending_sync": "Ожидает внесения в базу",
                  "insufficient": "Недостаточно доказательств", "rejected": "Отклонена"}
        if not cases:
            message = "Ты пока не отправлял заявки."
        else:
            message = "\n\n".join(
                f"**{discord.utils.escape_markdown(row['riot_id'])}** — {labels.get(row['state'], 'На рассмотрении')}"
                + (f"\nКомментарий: {discord.utils.escape_mentions(row['reviewer_comment'])[:500]}"
                   if row["reviewer_comment"] else "") for row in cases)
        await interaction.response.send_message(message[:1900], ephemeral=True)



# Review workflow: only administrators can decide until a dedicated reviewer role is agreed.
def _check_review_allowed(cog, interaction):
    settings = cog.store.settings(interaction.guild.id) if interaction.guild else None
    return bool(settings and interaction.channel_id == settings["review_channel_id"]
                and isinstance(interaction.user, discord.Member)
                and interaction.user.guild_permissions.administrator)


class DecisionModal(discord.ui.Modal, title="SIJK CHECK • Решение"):
    comment = discord.ui.TextInput(label="Комментарий для заявителей",
                                    style=discord.TextStyle.paragraph, required=True, max_length=1000)

    def __init__(self, cog, case_id, state):
        super().__init__()
        self.cog, self.case_id, self.state = cog, case_id, state

    async def on_submit(self, interaction):
        if not _check_review_allowed(self.cog, interaction):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.cog.store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM check_cases WHERE id=? AND guild_id=?",
                             (self.case_id, interaction.guild.id)).fetchone()
            if not row or row["state"] != "pending":
                db.rollback()
                await interaction.response.send_message("Дело уже рассмотрено или изменилось.", ephemeral=True)
                return
            db.execute("""UPDATE check_cases SET state=?,reviewer_id=?,reviewer_comment=?,
                          reviewed_at=?,updated_at=? WHERE id=?""",
                       (self.state, interaction.user.id, str(self.comment), now, now, self.case_id))
            db.execute("INSERT INTO check_events(case_id,user_id,event,detail,created_at) VALUES(?,?,?,?,?)",
                       (self.case_id, interaction.user.id, "decision:" + self.state, str(self.comment), now))
            db.commit()
        await interaction.response.send_message(
            "Решение сохранено. Одобрение пока ожидает синхронизации с Google Sheets."
            if self.state == "approved_pending_sync" else "Решение сохранено, комментарий виден заявителям.",
            ephemeral=True)
        await self.cog.notify_review(interaction.guild, self.case_id)


class EvidencePages(discord.ui.View):
    def __init__(self, cog, case_id, user_id):
        super().__init__(timeout=180)
        self.cog, self.case_id, self.user_id, self.page = cog, case_id, user_id, 0

    def render(self):
        with closing(self.cog.store.connect()) as db:
            reporters = db.execute("SELECT * FROM check_reporters WHERE case_id=? ORDER BY created_at",
                                   (self.case_id,)).fetchall()
            evidence = db.execute("SELECT * FROM check_evidence WHERE case_id=? ORDER BY created_at",
                                  (self.case_id,)).fetchall()
        pages = max(1, (len(reporters) + 2) // 3)
        self.page = max(0, min(self.page, pages - 1))
        blocks = []
        for r in reporters[self.page * 3:self.page * 3 + 3]:
            links = [e["url"] for e in evidence if e["user_id"] == r["user_id"]]
            block = (f"**Заявитель:** <@{r['user_id']}> (ID {r['user_id']})\\n"
                     f"**Причина:** {discord.utils.escape_mentions(r['reason'])[:450]}\\n"
                     f"**Дата:** {r['created_at'][:16]} UTC\\n"
                     "**Доказательства:**\\n" +
                     ("\\n".join(discord.utils.escape_mentions(u)[:220] for u in links[:8]) or "—"))
            if len(links) > 8:
                block += f"\\n… ещё {len(links) - 8}"
            blocks.append(block)
        embed = discord.Embed(title=f"SIJK CHECK • Дело #{self.case_id}",
                              description="\\n\\n".join(blocks)[:3900] or "Пусто", color=RED)
        embed.set_footer(text=f"Страница {self.page + 1}/{pages} • Всего заявителей: {len(reporters)}")
        self.back.disabled = self.page == 0
        self.forward.disabled = self.page == pages - 1
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id or not _check_review_allowed(self.cog, interaction):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        self.page -= 1
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Далее", style=discord.ButtonStyle.secondary)
    async def forward(self, interaction, button):
        self.page += 1
        await interaction.response.edit_message(embed=self.render(), view=self)


class ReviewPanel(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def find_case(self, interaction):
        if not _check_review_allowed(self.cog, interaction):
            await interaction.response.send_message("Нет доступа к рассмотрению.", ephemeral=True)
            return None
        with closing(self.cog.store.connect()) as db:
            row = db.execute("SELECT * FROM check_cases WHERE guild_id=? AND review_message_id=?",
                             (interaction.guild.id, interaction.message.id)).fetchone()
        if not row:
            await interaction.response.send_message("Карточка не найдена.", ephemeral=True)
        return row

    @discord.ui.button(label="Заявители и доказательства", style=discord.ButtonStyle.secondary,
                       custom_id="sijk_check:review:reporters", row=0)
    async def reporters(self, interaction, button):
        row = await self.find_case(interaction)
        if row:
            view = EvidencePages(self.cog, row["id"], interaction.user.id)
            await interaction.response.send_message(embed=view.render(), view=view, ephemeral=True)

    @discord.ui.button(label="Добавить в базу", style=discord.ButtonStyle.success,
                       custom_id="sijk_check:review:approve", row=1)
    async def approve(self, interaction, button):
        row = await self.find_case(interaction)
        if row:
            await interaction.response.send_modal(DecisionModal(self.cog, row["id"], "approved_pending_sync"))

    @discord.ui.button(label="Недостаточно доказательств", style=discord.ButtonStyle.secondary,
                       custom_id="sijk_check:review:insufficient", row=1)
    async def insufficient(self, interaction, button):
        row = await self.find_case(interaction)
        if row:
            await interaction.response.send_modal(DecisionModal(self.cog, row["id"], "insufficient"))

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger,
                       custom_id="sijk_check:review:reject", row=2)
    async def reject(self, interaction, button):
        row = await self.find_case(interaction)
        if row:
            await interaction.response.send_modal(DecisionModal(self.cog, row["id"], "rejected"))


class SijkCheck(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: Path):
        self.bot = bot
        self.store = CheckStore(db_path)
        self.bot.add_view(CheckPanel(self))
        self.bot.add_view(ReviewPanel(self))

    def in_public_channel(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        settings = self.store.settings(interaction.guild.id)
        return bool(settings and interaction.channel_id == settings["public_channel_id"])

    async def notify_review(self, guild: discord.Guild, case_id: int):
        row, count = self.store.case(case_id)
        settings = self.store.settings(guild.id)
        channel = guild.get_channel(settings["review_channel_id"]) if settings else None
        if not isinstance(channel, discord.TextChannel):
            return
        labels = {"pending": "На рассмотрении", "approved_pending_sync": "Ожидает внесения в базу",
                  "insufficient": "Недостаточно доказательств", "rejected": "Отклонена"}
        embed = discord.Embed(title="SIJK CHECK • " + labels.get(row["state"], "На рассмотрении"),
                              description=f"**{discord.utils.escape_markdown(row['riot_id'])}**\\n"
                              f"Уникальных заявителей: **{count}**\\n"
                              f"[Tracker.gg]({tracker_url(row['riot_id'])})", color=RED)
        if row["reviewer_comment"]:
            embed.add_field(name="Комментарий решения",
                            value=discord.utils.escape_mentions(row["reviewer_comment"])[:1000], inline=False)
        embed.set_footer(text=f"Дело #{case_id}")
        try:
            if row["review_message_id"]:
                try:
                    message = await channel.fetch_message(row["review_message_id"])
                    await message.edit(embed=embed, view=ReviewPanel(self))
                    return
                except discord.NotFound:
                    pass
            message = await channel.send(embed=embed, view=ReviewPanel(self),
                                         allowed_mentions=discord.AllowedMentions.none())
            with closing(self.store.connect()) as db:
                db.execute("UPDATE check_cases SET review_message_id=? WHERE id=?", (message.id, case_id))
                db.commit()
        except discord.HTTPException:
            pass

    @app_commands.command(name="check_setup", description="Настроить каналы SIJK CHECK")
    @app_commands.default_permissions(administrator=True)
    async def check_setup(self, interaction: discord.Interaction,
                          public_channel: discord.TextChannel, review_channel: discord.TextChannel):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Нужны права администратора.", ephemeral=True)
            return
        if public_channel.id == review_channel.id:
            await interaction.response.send_message("Публичный и закрытый каналы должны отличаться.", ephemeral=True)
            return
        self.store.configure(interaction.guild.id, public_channel.id, review_channel.id)
        await interaction.response.send_message(
            f"Каналы сохранены: {public_channel.mention} и {review_channel.mention}. "
            "Проверь, что закрытый канал недоступен обычным участникам.", ephemeral=True)

    @app_commands.command(name="check_panel", description="Опубликовать панель заявок SIJK CHECK")
    @app_commands.default_permissions(administrator=True)
    async def check_panel(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Нужны права администратора.", ephemeral=True)
            return
        if not self.in_public_channel(interaction):
            await interaction.response.send_message("Используй команду в настроенном публичном канале.", ephemeral=True)
            return
        embed = discord.Embed(
            title="SIJK CHECK • Заявки на проверку",
            description="Подозреваешь игрока в винтрейдинге? Отправь Riot ID, причину и ссылки на доказательства.\n\n"
            "**Только Расцвет 1 (Ascendant 1) и выше.** Заявки на игроков более низкого ранга "
            "будут отклонены без рассмотрения.\n\n"
            "Оценки отражают личное мнение SIJK и не являются доказательством нарушения.",
            color=RED)
        await interaction.channel.send(embed=embed, view=CheckPanel(self))
        await interaction.response.send_message("Панель опубликована.", ephemeral=True)


async def setup_sijk_check(bot: commands.Bot, db_path: Path):
    await bot.add_cog(SijkCheck(bot, db_path))
