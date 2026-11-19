"""SIJK CHECK: submissions, evidence history and a persistent public entry panel."""
from __future__ import annotations

import asyncio
import re
import logging
from io import BytesIO
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import discord
from discord import app_commands
from discord.ext import commands
from sijk_check_sheets import CheckSheet, CheckSheetError
from sijk_check_visuals import meter_png, rating_color

LOG = logging.getLogger('sijk.check')
RED = 0xB8202B
RIOT_ID_PATTERN = re.compile(r"^([^#\r\n]{1,32})#([^#\r\n]{1,16})$")


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


PUBLIC_REPORT_STATUSES = {
    "pending": "🕒 НА РАССМОТРЕНИИ",
    "approved": "✅ ОЦЕНКА ОПУБЛИКОВАНА",
    "approved_pending_sync": "⏳ ОЖИДАЕТ ПУБЛИКАЦИИ",
    "insufficient": "🔎 НЕДОСТАТОЧНО ДОКАЗАТЕЛЬСТВ",
    "rejected": "⛔ ЗАЯВКА ОТКЛОНЕНА",
}


def public_report_embed(case: sqlite3.Row, reporter_count: int) -> discord.Embed:
    """Public status only: no reporter names, evidence URLs, or reasons."""
    status = PUBLIC_REPORT_STATUSES.get(case["state"], "🕒 НА РАССМОТРЕНИИ")
    embed = discord.Embed(
        title=discord.utils.escape_markdown(case["riot_id"]),
        description=(
            f"## {status}\n\n"
            f"**Обращение №{case['id']}**  •  **Заявителей: {reporter_count}**\n\n"
            "*Наличие заявки не доказывает нарушение правил игроком.*"
        ),
        color=discord.Color.from_rgb(128, 128, 128),
    )
    embed.set_footer(text="SIJK CHECK • Публичный реестр")
    return embed


class CheckStore:
    def __init__(self, path: Path):
        self.path = path
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS check_settings (
                    guild_id INTEGER PRIMARY KEY,
                    public_channel_id INTEGER,
                    review_channel_id INTEGER,
                    reviewer_role_id INTEGER
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
            columns = {row[1] for row in db.execute("PRAGMA table_info(check_settings)")}
            if "reviewer_role_id" not in columns:
                db.execute("ALTER TABLE check_settings ADD COLUMN reviewer_role_id INTEGER")
            if "feed_channel_id" not in columns:
                db.execute("ALTER TABLE check_settings ADD COLUMN feed_channel_id INTEGER")
            if "feed_panel_message_id" not in columns:
                db.execute("ALTER TABLE check_settings ADD COLUMN feed_panel_message_id INTEGER")
            case_columns = {row[1] for row in db.execute("PRAGMA table_info(check_cases)")}
            if "feed_message_id" not in case_columns:
                db.execute("ALTER TABLE check_cases ADD COLUMN feed_message_id INTEGER")
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def settings(self, guild_id: int):
        with closing(self.connect()) as db:
            return db.execute("SELECT * FROM check_settings WHERE guild_id=?", (guild_id,)).fetchone()

    def configure(self, guild_id: int, public_id: int, review_id: int, reviewer_role_id: int):
        with closing(self.connect()) as db:
            db.execute("""INSERT INTO check_settings (guild_id,public_channel_id,review_channel_id,reviewer_role_id) VALUES(?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                public_channel_id=excluded.public_channel_id,
                review_channel_id=excluded.review_channel_id,
                reviewer_role_id=excluded.reviewer_role_id""", (guild_id, public_id, review_id, reviewer_role_id))
            db.commit()

    def set_feed_channel(self, guild_id: int, channel_id: int):
        with closing(self.connect()) as db:
            db.execute("INSERT OR IGNORE INTO check_settings(guild_id) VALUES(?)", (guild_id,))
            previous = db.execute(
                "SELECT feed_channel_id FROM check_settings WHERE guild_id=?", (guild_id,)
            ).fetchone()
            if previous and previous["feed_channel_id"] != channel_id:
                db.execute("UPDATE check_cases SET feed_message_id=NULL WHERE guild_id=?", (guild_id,))
                db.execute("UPDATE check_settings SET feed_panel_message_id=NULL WHERE guild_id=?", (guild_id,))
            db.execute("UPDATE check_settings SET feed_channel_id=? WHERE guild_id=?",
                       (channel_id, guild_id))
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
            db.execute("UPDATE check_cases SET updated_at=? WHERE id=?", (now, case["id"]))
            db.execute("INSERT INTO check_events(case_id,user_id,event,detail,created_at) VALUES(?,?,?,?,?)",
                       (case["id"], user_id, "supplement" if previous else "report", reason, now))
            outcome = "supplement" if previous else "new"
            if case["state"] in ("insufficient", "rejected"):
                # Reopen automatically on genuinely new evidence or a new reporter.
                # The earlier decision and reviewer comment stay in check_events.
                db.execute(
                    """UPDATE check_cases SET state='pending', reviewer_id=NULL,
                       reviewer_comment=NULL, reviewed_at=NULL, updated_at=? WHERE id=?""",
                    (now, case["id"]),
                )
                db.execute(
                    "INSERT INTO check_events(case_id,user_id,event,detail,created_at) VALUES(?,?,?,?,?)",
                    (case["id"], user_id, "auto_reopened",
                     "Новые материалы после закрытия; прежний статус: " + case["state"], now),
                )
                outcome = "reopened"
            elif case["state"] == "approved":
                # Existing Google Sheets assessment remains published; notify reviewers
                # without pretending a new approval was performed.
                db.execute(
                    "INSERT INTO check_events(case_id,user_id,event,detail,created_at) VALUES(?,?,?,?,?)",
                    (case["id"], user_id, "approved_update",
                     "Новое обращение после публикации оценки; пересмотр не выполнен.", now),
                )
                outcome = "approved_update"
            db.commit()
            return case["id"], outcome

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
            row, _ = self.cog.store.case(case_id)
            closed = row["state"] not in ("pending", "approved_pending_sync")
            await interaction.response.send_message(
                f"**Твоё обращение №{case_id}.** " +
                ("Дело уже закрыто. Повторная отправка тех же ссылок ничего не меняет. "
                 "Для автоматического повторного рассмотрения нужны новые доказательства."
                 if closed else "Твоя заявка уже есть. Новых ссылок не обнаружено.")
                + "\nНайти своё обращение можно через кнопку «Мои заявки».",
                ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        delivered = await self.cog.notify_review(interaction.guild, case_id)
        if delivered:
            await self.cog.notify_public_report(interaction.guild, case_id)
            if outcome in ("reopened", "approved_update"):
                await self.cog.notify_report_activity(interaction.guild, case_id, outcome)
            messages = {
                "supplement": "Доказательства добавлены к существующей заявке.",
                "reopened": (
                    "Новые материалы сохранены. Закрытое дело автоматически открыто повторно "
                    "и отправлено модераторам на рассмотрение."
                ),
                "approved_update": (
                    "Новое обращение сохранено. Игрок уже есть в опубликованной базе, "
                    "поэтому оценка не сброшена; модераторы получили отдельное уведомление."
                ),
            }
            message = messages.get(
                outcome, "Заявка принята! Она сохранена в SIJK CHECK и передана проверяющим."
            )
        else:
            message = ("Заявка сохранена в базе, но карточка не появилась у модераторов. "
                       "Попроси проверяющего выполнить /check_reopen с этим Riot ID. "
                       "Администратор может проверить причину в логах Bothost.")
        row, _ = self.cog.store.case(case_id)
        settings = self.cog.store.settings(interaction.guild.id)
        report_link = (
            f"https://discord.com/channels/{interaction.guild.id}/"
            f"{settings['feed_channel_id']}/{row['feed_message_id']}"
            if settings and settings["feed_channel_id"] and row["feed_message_id"] else None
        )
        message += f"\n\n**Номер обращения: №{case_id}.**"
        if report_link:
            message += f" [Посмотреть статус в WINTRADE-REPORTS]({report_link})"
        message += "\nТакже его можно найти через кнопку «Мои заявки»."
        await interaction.followup.send(message, ephemeral=True)


class LookupModal(discord.ui.Modal, title="SIJK CHECK • Проверить игрока"):
    riot_id = discord.ui.TextInput(label="Riot ID (Ник#Тег)", max_length=50)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction):
        if not self.cog.in_public_channel(interaction):
            await interaction.response.send_message("Проверка доступна только в канале SIJK CHECK.",
                                                    ephemeral=True)
            return
        await self.cog.lookup(interaction, str(self.riot_id))


class MissingPlayerView(discord.ui.View):
    def __init__(self, cog, riot_id):
        super().__init__(timeout=600)
        self.cog, self.riot_id = cog, riot_id

    @discord.ui.button(label="Оформить заявку", style=discord.ButtonStyle.primary, emoji="📝")
    async def report(self, interaction, button):
        if not self.cog.in_public_channel(interaction):
            await interaction.response.send_message("Открой канал SIJK CHECK.", ephemeral=True)
            return
        await interaction.response.send_modal(ReportModal(self.cog, self.riot_id))


class CheckPanel(discord.ui.View):
    def __init__(self, cog: "SijkCheck"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Проверить игрока", style=discord.ButtonStyle.primary,
                       custom_id="sijk_check:lookup", emoji="🔎")
    async def lookup(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog.in_public_channel(interaction):
            await interaction.response.send_message("Открой канал SIJK CHECK.", ephemeral=True)
            return
        await interaction.response.send_modal(LookupModal(self.cog))

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
            settings = self.cog.store.settings(interaction.guild.id)
            def describe_case(row):
                block = (
                    f"**Обращение №{row['id']}** · "
                    f"**{discord.utils.escape_markdown(row['riot_id'])}**\n"
                    f"Статус: **{labels.get(row['state'], 'На рассмотрении')}**"
                )
                if settings and settings["feed_channel_id"] and row["feed_message_id"]:
                    link = (f"https://discord.com/channels/{interaction.guild.id}/"
                            f"{settings['feed_channel_id']}/{row['feed_message_id']}")
                    block += f"\n[Открыть карточку в WINTRADE-REPORTS]({link})"
                if row["reviewer_comment"]:
                    block += ("\nКомментарий: "
                              + discord.utils.escape_mentions(row["reviewer_comment"])[:350])
                return block
            message = "\n\n".join(describe_case(row) for row in cases)
        await interaction.response.send_message(message[:1900], ephemeral=True)



# Review workflow: only administrators can decide until a dedicated reviewer role is agreed.
def _check_review_allowed(cog, interaction):
    settings = cog.store.settings(interaction.guild.id) if interaction.guild else None
    return bool(settings and interaction.channel_id == settings["review_channel_id"]
                and isinstance(interaction.user, discord.Member)
                and settings['reviewer_role_id']
                and any(role.id == settings['reviewer_role_id'] for role in interaction.user.roles))


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
        await self.cog.notify_public_report(interaction.guild, self.case_id)


class ApprovalModal(discord.ui.Modal, title="SIJK CHECK • Добавить в базу"):
    score = discord.ui.TextInput(label="Оценка от 1 до 10", max_length=2, placeholder="10")
    status = discord.ui.TextInput(label="Статус", max_length=32,
                                  placeholder="Да / Вероятнее да / Под вопросом / Вероятнее нет / Нет")
    comment = discord.ui.TextInput(label="Комментарий для базы и заявителей",
                                    style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, cog, case_id):
        super().__init__()
        self.cog, self.case_id = cog, case_id

    async def on_submit(self, interaction):
        if not _check_review_allowed(self.cog, interaction):
            await interaction.response.send_message("Нет доступа.", ephemeral=True)
            return
        try:
            score = int(str(self.score).strip())
        except ValueError:
            score = 0
        status = str(self.status).strip()
        if score not in range(1, 11) or status not in {
            "Да", "Вероятнее да", "Под вопросом", "Вероятнее нет", "Нет"
        }:
            await interaction.response.send_message(
                "Оценка — число от 1 до 10; статус — один из пяти вариантов из подсказки.",
                ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Only one active writer per bot process. Google Sheets append is not transactional.
        async with self.cog.approval_lock:
            with closing(self.cog.store.connect()) as db:
                row = db.execute("SELECT * FROM check_cases WHERE id=? AND guild_id=?",
                                 (self.case_id, interaction.guild.id)).fetchone()
                if not row or row["state"] not in ("pending", "approved_pending_sync"):
                    await interaction.followup.send("Дело уже рассмотрено.", ephemeral=True)
                    return
                evidence = db.execute("SELECT url FROM check_evidence WHERE case_id=? ORDER BY id",
                                      (self.case_id,)).fetchall()
            now = datetime.now(timezone.utc).isoformat()
            links = "\n".join(item["url"] for item in evidence)
            try:
                result = await self.cog.sheet.add_player(
                    row["riot_id"], score, status, links[:40000], str(self.comment), now[:10])
            except CheckSheetError:
                # Retain the case and allow manual retry. Never claim successful publication.
                with closing(self.cog.store.connect()) as db:
                    db.execute("""UPDATE check_cases SET state='approved_pending_sync',
                                  reviewer_id=?, reviewer_comment=?, reviewed_at=?, updated_at=?
                                  WHERE id=? AND state IN ('pending','approved_pending_sync')""",
                               (interaction.user.id, str(self.comment), now, now, self.case_id))
                    db.commit()
                await interaction.followup.send(
                    "Google Sheets не подтвердил запись. Дело сохранено как ожидающее синхронизации. "
                    "Повтори «Добавить в базу» позже.", ephemeral=True)
                await self.cog.notify_review(interaction.guild, self.case_id)
                await self.cog.notify_public_report(interaction.guild, self.case_id)
                return
            with closing(self.cog.store.connect()) as db:
                db.execute("""UPDATE check_cases SET state='approved',
                              reviewer_id=?, reviewer_comment=?, reviewed_at=?, updated_at=?
                              WHERE id=?""", (interaction.user.id, str(self.comment), now, now, self.case_id))
                db.execute("INSERT INTO check_events(case_id,user_id,event,detail,created_at) VALUES(?,?,?,?,?)",
                           (self.case_id, interaction.user.id, "approved:" + result,
                            f"{score}/10 | {status} | {str(self.comment)}", now))
                db.commit()
            await interaction.followup.send(
                "Игрок добавлен в закрытую таблицу, решение сохранено." if result == "added"
                else "Игрок уже есть в таблице — существующая оценка не изменена. "
                     "Дело помечено как рассмотренное.", ephemeral=True)
            await self.cog.notify_review(interaction.guild, self.case_id)
            await self.cog.notify_public_report(interaction.guild, self.case_id)


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
            block = (f"**Заявитель:** <@{r['user_id']}> (ID {r['user_id']})\n"
                     f"**Причина:** {discord.utils.escape_mentions(r['reason'])[:450]}\n"
                     f"**Дата:** {r['created_at'][:16]} UTC\n"
                     "**Доказательства:**\n" +
                     ("\n".join(discord.utils.escape_mentions(u)[:220] for u in links[:8]) or "—"))
            if len(links) > 8:
                block += f"\n… ещё {len(links) - 8}"
            blocks.append(block)
        embed = discord.Embed(title=f"SIJK CHECK • Дело #{self.case_id}",
                              description="\n\n".join(blocks)[:3900] or "Пусто", color=RED)
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
            return None
        # Old messages/interactions may survive an edit or restart. Never allow
        # a new decision for a case that is already closed.
        if row["state"] not in ("pending", "approved_pending_sync"):
            await interaction.response.send_message(
                "Заявка уже закрыта. Решение можно посмотреть в карточке.", ephemeral=True)
            return None
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
            await interaction.response.send_modal(ApprovalModal(self.cog, row["id"]))

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


class ClosedReviewPanel(discord.ui.View):
    """Persistent evidence access for already reviewed cases, including approved."""
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Заявители и доказательства",
        style=discord.ButtonStyle.secondary,
        custom_id="sijk_check:review:closed_evidence",
    )
    async def evidence(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _check_review_allowed(self.cog, interaction):
            await interaction.response.send_message("Нет доступа к доказательствам.", ephemeral=True)
            return
        with closing(self.cog.store.connect()) as db:
            row = db.execute(
                "SELECT id FROM check_cases WHERE guild_id=? AND review_message_id=?",
                (interaction.guild.id, interaction.message.id),
            ).fetchone()
        if not row:
            await interaction.response.send_message("Дело не найдено.", ephemeral=True)
            return
        view = EvidencePages(self.cog, row["id"], interaction.user.id)
        await interaction.response.send_message(embed=view.render(), view=view, ephemeral=True)


class SijkCheck(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: Path):
        self.bot = bot
        self.store = CheckStore(db_path)
        self.sheet = CheckSheet()
        self.approval_lock = asyncio.Lock()
        self.feed_lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task] = set()
        self.bot.add_view(CheckPanel(self))
        self.bot.add_view(ReviewPanel(self))
        self.bot.add_view(ClosedReviewPanel(self))

    def in_public_channel(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        settings = self.store.settings(interaction.guild.id)
        return bool(settings and interaction.channel_id == settings["public_channel_id"])

    def _schedule_lookup_cleanup(self, interaction: discord.Interaction, delay: int = 180):
        async def cleanup():
            try:
                await asyncio.sleep(delay)
                await interaction.delete_original_response()
            except (discord.NotFound, discord.Forbidden):
                pass
            except discord.HTTPException:
                LOG.warning("Failed to delete ephemeral lookup", exc_info=True)

        task = asyncio.create_task(cleanup())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def cog_unload(self):
        for task in self._cleanup_tasks:
            task.cancel()

    async def notify_public_report(self, guild: discord.Guild, case_id: int) -> bool:
        """Publish a neutral public status: no reporter identities, reasons or evidence."""
        settings = self.store.settings(guild.id)
        if not settings or not settings["feed_channel_id"]:
            return False
        channel = guild.get_channel(settings["feed_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            LOG.error("Missing SIJK CHECK public report channel %s in guild %s",
                      settings["feed_channel_id"], guild.id)
            return False
        row, count = self.store.case(case_id)
        embed = public_report_embed(row, count)
        async with self.feed_lock:
            # Re-read the message ID to avoid duplicate posts from simultaneous reports.
            row, _ = self.store.case(case_id)
            try:
                if row["feed_message_id"]:
                    try:
                        message = await channel.fetch_message(row["feed_message_id"])
                        await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                        return True
                    except discord.NotFound:
                        pass
                message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                with closing(self.store.connect()) as db:
                    db.execute("UPDATE check_cases SET feed_message_id=? WHERE id=?",
                               (message.id, case_id))
                    db.commit()
                return True
            except discord.HTTPException:
                LOG.exception("SIJK CHECK public report delivery failed: case=%s", case_id)
                return False

    async def notify_report_activity(
        self, guild: discord.Guild, case_id: int, outcome: str
    ) -> bool:
        """Alert the private review channel about submissions after a closed decision."""
        settings = self.store.settings(guild.id)
        channel = guild.get_channel(settings["review_channel_id"]) if settings else None
        if not isinstance(channel, discord.TextChannel):
            LOG.error("Cannot notify reviewers for case %s: review channel not found", case_id)
            return False
        row, count = self.store.case(case_id)
        reopened = outcome == "reopened"
        title = (
            "🔔 SIJK CHECK • ПОВТОРНО ОТКРЫТО"
            if reopened else "🔔 SIJK CHECK • НОВЫЕ МАТЕРИАЛЫ ПО ИГРОКУ В БАЗЕ"
        )
        description = (
            f"**{discord.utils.escape_markdown(row['riot_id'])}** · Дело №{case_id}\\n"
            f"Заявителей: **{count}**\\n"
            + (
                "По ранее закрытому делу поступили новые материалы. "
                "**Статус автоматически изменён на «На рассмотрении»**."
                if reopened else
                "Поступили новые материалы после публикации оценки. "
                "**Оценка в базе сохранена**, требуется внимание проверяющих."
            )
        )
        embed = discord.Embed(title=title, description=description, color=RED)
        if row["review_message_id"]:
            link = (f"https://discord.com/channels/{guild.id}/"
                    f"{channel.id}/{row['review_message_id']}")
            embed.add_field(name="Карточка для модераторов", value=f"[Открыть дело]({link})")
        role_id = settings["reviewer_role_id"] if settings else None
        try:
            # Mention the reviewer role only on this exceptional closed-case event.
            await channel.send(
                content=f"<@&{role_id}>" if role_id else None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False,
                    roles=[discord.Object(id=role_id)] if role_id else False,
                ),
            )
            return True
        except discord.HTTPException:
            LOG.warning("Reviewer role ping failed, retrying without mention: case=%s", case_id)
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                return True
            except discord.HTTPException:
                LOG.exception("Could not send private activity alert for case %s", case_id)
                return False

    async def notify_review(self, guild: discord.Guild, case_id: int):
        row, count = self.store.case(case_id)
        settings = self.store.settings(guild.id)
        channel = guild.get_channel(settings["review_channel_id"]) if settings else None
        if not isinstance(channel, discord.TextChannel):
            LOG.error("Review channel unavailable: guild=%s case=%s channel=%s",
                      guild.id, case_id, settings["review_channel_id"] if settings else None)
            return False
        labels = {"pending": "На рассмотрении", "approved": "Добавлен в базу", "approved_pending_sync": "Ожидает внесения в базу",
                  "insufficient": "Недостаточно доказательств", "rejected": "Отклонена"}
        embed = discord.Embed(title="SIJK CHECK • " + labels.get(row["state"], "На рассмотрении"),
                              description=f"**{discord.utils.escape_markdown(row['riot_id'])}**\n"
                              f"Уникальных заявителей: **{count}**\n"
                              f"[Tracker.gg]({tracker_url(row['riot_id'])})", color=RED)
        if row["reviewer_comment"]:
            embed.add_field(name="Комментарий решения",
                            value=discord.utils.escape_mentions(row["reviewer_comment"])[:1000], inline=False)
        if row["reviewer_id"] and row["state"] not in ("pending", "approved_pending_sync"):
            embed.add_field(name="Проверяющий", value=f"<@{row['reviewer_id']}>", inline=False)
        embed.set_footer(text=f"Дело #{case_id}")
        # Closed reviews expose evidence, but never show decision buttons.
        view = (ReviewPanel(self) if row["state"] in ("pending", "approved_pending_sync")
                else ClosedReviewPanel(self))
        try:
            if row["review_message_id"]:
                try:
                    message = await channel.fetch_message(row["review_message_id"])
                    await message.edit(embed=embed, view=view)
                    return True
                except discord.NotFound:
                    pass
            message = await channel.send(embed=embed, view=view,
                                         allowed_mentions=discord.AllowedMentions.none())
            with closing(self.store.connect()) as db:
                db.execute("UPDATE check_cases SET review_message_id=? WHERE id=?", (message.id, case_id))
                db.commit()
            return True
        except (discord.HTTPException, discord.Forbidden) as exc:
            LOG.exception("Cannot publish SIJK CHECK review: guild=%s case=%s channel=%s",
                          guild.id, case_id, channel.id)
            return False

    async def lookup(self, interaction: discord.Interaction, riot_id: str):
        try:
            riot_id, key = normalize_riot_id(riot_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        # Lookups are private and temporary so the panel stays visible.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            record = await self.sheet.find(key)
        except CheckSheetError:
            await interaction.edit_original_response(
                content="База временно недоступна. Это не означает, что игрок отсутствует в ней. Попробуй позже.")
            self._schedule_lookup_cleanup(interaction)
            return
        if not record:
            embed = discord.Embed(
                title=discord.utils.escape_markdown(riot_id),
                description=(
                    "У SIJK пока нет опубликованной оценки этого игрока. "
                    "Отсутствие записи ничего не говорит о его поведении."
                ),
                color=discord.Color.from_rgb(128, 128, 128),
            )
            await interaction.edit_original_response(embed=embed, view=MissingPlayerView(self, riot_id))
            self._schedule_lookup_cleanup(interaction)
            return
        name, score, status, tracker, evidence, comment, date = record
        try:
            rating = int(float(str(score).replace(",", ".")))
        except (ValueError, TypeError):
            rating = None
        if rating is not None and not 1 <= rating <= 10:
            rating = None
        # The player's Riot ID is the embed title, which Discord renders larger.
        embed = discord.Embed(
            title=discord.utils.escape_markdown(name),
            description=(
                f"Статус: **{discord.utils.escape_markdown(status or 'Не указан')}**\n"
                + (f"Подозрительность: **{rating}/10**" if rating is not None else "Оценка отсутствует")
            ),
            color=discord.Color.from_rgb(*rating_color(rating)) if rating is not None else RED,
        )
        file = None
        if rating is not None:
            file = discord.File(BytesIO(meter_png(rating)), filename="sijk_check_meter.png")
            embed.set_image(url="attachment://sijk_check_meter.png")
        if comment:
            embed.add_field(name="Комментарий SIJK",
                            value=discord.utils.escape_mentions(str(comment))[:1000],
                            inline=False)
        if date:
            embed.add_field(name="Дата оценки", value=str(date)[:100], inline=False)
        embed.set_footer(text="Субъективная оценка. Не доказательство нарушения правил.")
        view = discord.ui.View()
        url = tracker if isinstance(tracker, str) and tracker.startswith("https://tracker.gg/") else tracker_url(name)
        view.add_item(discord.ui.Button(label="Чекнуть трекер", url=url, style=discord.ButtonStyle.link))
        await interaction.edit_original_response(
            embed=embed, view=view, attachments=[file] if file else [])
        self._schedule_lookup_cleanup(interaction)

    @app_commands.command(name="check_reopen", description="Повторно открыть закрытое дело и восстановить карточку")
    @app_commands.describe(riot_id="Riot ID игрока (Ник#Тег)")
    async def check_reopen(self, interaction: discord.Interaction, riot_id: str):
        if not _check_review_allowed(self, interaction):
            await interaction.response.send_message(
                "Команда доступна только проверяющим в закрытом канале заявок.", ephemeral=True)
            return
        try:
            _, key = normalize_riot_id(riot_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM check_cases WHERE guild_id=? AND riot_key=?",
                (interaction.guild.id, key)).fetchone()
            if row is None:
                db.rollback()
                await interaction.response.send_message(
                    "Дело не найдено. Для новой заявки используй публичную панель.", ephemeral=True)
                return
            if row["state"] in ("pending", "approved_pending_sync"):
                db.rollback()
                await interaction.response.send_message(
                    "Дело уже открыто. Восстанавливаю карточку, если она была удалена.",
                    ephemeral=True)
                await self.notify_review(interaction.guild, row["id"])
                await self.notify_public_report(interaction.guild, row["id"])
                return
            if row["state"] == "approved":
                db.rollback()
                await interaction.response.send_message(
                    "Игрок уже добавлен в базу. Повторное рассмотрение требует отдельного решения, "
                    "чтобы не расходиться с Google-таблицей.", ephemeral=True)
                return
            db.execute(
                """UPDATE check_cases SET state='pending', reviewer_id=NULL,
                   reviewer_comment=NULL, reviewed_at=NULL, updated_at=? WHERE id=?""",
                (now, row["id"]))
            db.execute(
                "INSERT INTO check_events(case_id,user_id,event,detail,created_at) VALUES(?,?,?,?,?)",
                (row["id"], interaction.user.id, "reopened",
                 "Дело повторно открыто проверяющим; прежнее решение сохранено в истории.", now))
            db.commit()
        await interaction.response.send_message(
            "Дело повторно открыто. Создаю или обновляю карточку в закрытом канале. "
            "Предыдущие заявки, ссылки и решения остались в истории.", ephemeral=True)
        await self.notify_review(interaction.guild, row["id"])
        await self.notify_public_report(interaction.guild, row["id"])

    @app_commands.command(name="check_history", description="История рассмотрения игрока (роль проверяющего)")
    @app_commands.describe(
        riot_id="Riot ID в формате Ник#Тег",
        page="Страница истории (по 8 событий)")
    async def check_history(self, interaction: discord.Interaction, riot_id: str, page: int = 1):
        if not _check_review_allowed(self, interaction):
            await interaction.response.send_message(
                "История доступна только проверяющим в закрытом канале заявок.", ephemeral=True)
            return
        try:
            _, key = normalize_riot_id(riot_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if page < 1:
            await interaction.response.send_message("Номер страницы должен быть от 1.", ephemeral=True)
            return
        with closing(self.store.connect()) as db:
            case = db.execute(
                "SELECT * FROM check_cases WHERE guild_id=? AND riot_key=?",
                (interaction.guild.id, key)).fetchone()
            if case is None:
                await interaction.response.send_message(
                    "Заявок на этот Riot ID в архиве нет.", ephemeral=True)
                return
            total = db.execute(
                "SELECT COUNT(*) FROM check_events WHERE case_id=?", (case["id"],)
            ).fetchone()[0]
            reporters = db.execute(
                "SELECT COUNT(*) FROM check_reporters WHERE case_id=?", (case["id"],)
            ).fetchone()[0]
            evidence_count = db.execute(
                "SELECT COUNT(*) FROM check_evidence WHERE case_id=?", (case["id"],)
            ).fetchone()[0]
            events = db.execute(
                "SELECT * FROM check_events WHERE case_id=? ORDER BY created_at DESC, id DESC LIMIT 8 OFFSET ?",
                (case["id"], (page - 1) * 8)).fetchall()

        pages = max(1, (total + 7) // 8)
        if page > pages:
            await interaction.response.send_message(
                f"Всего страниц: {pages}. Укажи страницу от 1 до {pages}.", ephemeral=True)
            return
        labels = {
            "pending": "На рассмотрении",
            "approved": "Добавлен в базу",
            "approved_pending_sync": "Ожидает записи в базу",
            "insufficient": "Недостаточно доказательств",
            "rejected": "Отклонена",
        }
        actions = {
            "auto_reopened": "Автоматически открыта повторно",
            "approved_update": "Новые материалы после публикации оценки",
            "report": "Новая заявка",
            "supplement": "Дополнение доказательств",
            "decision:insufficient": "Недостаточно доказательств",
            "decision:rejected": "Отклонена",
        }
        blocks = []
        for event in events:
            action = event["event"]
            if action.startswith("approved:"):
                label = "Добавлен в базу"
            else:
                label = actions.get(action, "Событие")
            stamp = event["created_at"][:16].replace("T", " ")
            detail = discord.utils.escape_mentions(str(event["detail"] or "")).replace("\n", " ")
            if len(detail) > 220:
                detail = detail[:217] + "..."
            blocks.append(
                f"**{stamp} UTC · {label}**\n"
                f"Участник: <@{event['user_id']}>"
                + (f"\n{detail}" if detail else "")
            )
        description = (
            f"**{discord.utils.escape_markdown(case['riot_id'])}** · Дело #{case['id']}\n"
            f"Статус: **{labels.get(case['state'], 'Неизвестен')}**\n"
            f"Заявителей: **{reporters}** · Ссылок: **{evidence_count}**"
        )
        if case["reviewer_id"]:
            description += f"\nПроверяющий: <@{case['reviewer_id']}>"
        if case["reviewer_comment"]:
            comment = discord.utils.escape_mentions(str(case["reviewer_comment"]))[:400]
            description += f"\nКомментарий решения: {comment}"
        embed = discord.Embed(
            title="SIJK CHECK · ИСТОРИЯ ЗАЯВКИ",
            description=description[:1900],
            color=RED,
        )
        embed.add_field(
            name="Хронология (сначала новые)",
            value="\n\n".join(blocks)[:3900] or "Событий пока нет.",
            inline=False,
        )
        embed.set_footer(text=f"Страница {page}/{pages} · Всего событий: {total}")
        await interaction.response.send_message(
            embed=embed, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="check", description="Проверить игрока по базе SIJK CHECK")
    @app_commands.describe(riot_id="Riot ID в формате Ник#Тег")
    async def check(self, interaction: discord.Interaction, riot_id: str):
        if not self.in_public_channel(interaction):
            await interaction.response.send_message("Команда доступна только в канале SIJK CHECK.",
                                                    ephemeral=True)
            return
        await self.lookup(interaction, riot_id)

    @app_commands.command(name="check_setup", description="Настроить каналы SIJK CHECK")
    @app_commands.default_permissions(administrator=True)
    async def check_setup(self, interaction: discord.Interaction,
                          public_channel: discord.TextChannel, review_channel: discord.TextChannel,
                          reviewer_role: discord.Role):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Нужны права администратора.", ephemeral=True)
            return
        if public_channel.id == review_channel.id:
            await interaction.response.send_message("Публичный и закрытый каналы должны отличаться.", ephemeral=True)
            return
        self.store.configure(interaction.guild.id, public_channel.id, review_channel.id, reviewer_role.id)
        await interaction.response.send_message(
            f"Каналы сохранены: {public_channel.mention} и {review_channel.mention}. "
            f"Проверяющие: {reviewer_role.mention}. "
            "Проверь, что закрытый канал недоступен обычным участникам.", ephemeral=True)

    @app_commands.command(name="check_feed_setup", description="Выбрать публичный канал статусов заявок")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(channel="Канал для публичных статусов без доказательств и имён заявителей")
    async def check_feed_setup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Нужны права администратора.", ephemeral=True)
            return
        settings = self.store.settings(interaction.guild.id)
        if settings is None:
            await interaction.response.send_message("Сначала выполни /check_setup.", ephemeral=True)
            return
        if channel.id in (settings["public_channel_id"], settings["review_channel_id"]):
            await interaction.response.send_message(
                "Лента должна быть отдельным каналом, не CHECK и не модераторским.", ephemeral=True)
            return
        self.store.set_feed_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(
            f"Публичная лента подключена: {channel.mention}. "
            "Новые обращения и изменения статусов будут отображаться здесь. "
            "Сведения заявителей и доказательства останутся у модераторов. "
            "Старые тестовые заявки автоматически не публикуются. "
            "Убедись, что этот канал открыт для участников.",
            ephemeral=True)

    @app_commands.command(name="check_feed_refresh", description="Обновить формат уже опубликованных заявок")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def check_feed_refresh(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Нужны права администратора.", ephemeral=True)
            return
        settings = self.store.settings(interaction.guild.id)
        if not settings or interaction.channel_id != settings["feed_channel_id"]:
            await interaction.response.send_message(
                "Команду нужно выполнить в настроенном WINTRADE-REPORTS.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        with closing(self.store.connect()) as db:
            rows = db.execute(
                "SELECT id FROM check_cases WHERE guild_id=? AND feed_message_id IS NOT NULL "
                "ORDER BY id DESC LIMIT 100", (interaction.guild.id,)
            ).fetchall()
        updated = 0
        failed = 0
        for row in rows:
            if await self.notify_public_report(interaction.guild, row["id"]):
                updated += 1
            else:
                failed += 1
        await interaction.followup.send(
            f"Обновлено карточек: {updated}. Ошибок: {failed}. "
            "Карточки редактируются без повторной публикации.", ephemeral=True)

    @app_commands.command(name="check_feed_panel", description="Опубликовать закрепляемую информацию о ленте заявок")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def check_feed_panel(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Нужны права администратора.", ephemeral=True)
            return
        settings = self.store.settings(interaction.guild.id)
        if not settings or not settings["feed_channel_id"] or not settings["public_channel_id"]:
            await interaction.response.send_message(
                "Сначала настрой /check_setup и /check_feed_setup.", ephemeral=True)
            return
        if interaction.channel_id != settings["feed_channel_id"]:
            await interaction.response.send_message(
                "Используй эту команду в настроенном публичном WINTRADE-REPORTS.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = discord.Embed(
            title="SIJK CHECK • РЕЕСТР ЗАЯВОК",
            description=(
                "Здесь автоматически появляются обращения на проверку игроков VALORANT. "
                "У каждой заявки отображаются **Riot ID**, **статус** и "
                "**количество уникальных заявителей**. При новых обращениях "
                "или решении модерации карточка обновляется.\n\n"
                "**СТАТУСЫ ЗАЯВОК**\n"
                "🕒 **На рассмотрении** — проверяющие изучают обращение.\n"
                "🔎 **Недостаточно доказательств** — представленных материалов не хватило.\n"
                "⛔ **Заявка отклонена** — проверяющие отклонили обращение.\n"
                "✅ **Оценка опубликована** — решение внесено в базу SIJK CHECK.\n"
                "⏳ **Ожидает публикации оценки** — запись в базу ещё не завершена.\n\n"
                "**КАК ОТПРАВИТЬ ЗАЯВКУ?**\n"
                f"Перейди в <#{settings['public_channel_id']}> и нажми **«Подать заявку»**. "
                "Приложи Riot ID и ссылки на доказательства. "
                "Рассматриваются игроки от **Расцвета 1 (Ascendant 1)**.\n\n"
                "*Заявка не доказывает нарушение. Личности заявителей и "
                "доказательства доступны только проверяющим.*"
            ),
            color=discord.Color.from_rgb(242, 242, 242),
        )
        link = f"https://discord.com/channels/{interaction.guild.id}/{settings['public_channel_id']}"
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Перейти к проверке", style=discord.ButtonStyle.link, url=link, emoji="🔎"))
        async with self.feed_lock:
            settings = self.store.settings(interaction.guild.id)
            message = None
            try:
                if settings["feed_panel_message_id"]:
                    try:
                        message = await interaction.channel.fetch_message(settings["feed_panel_message_id"])
                        await message.edit(embed=embed, view=view,
                                           allowed_mentions=discord.AllowedMentions.none())
                    except discord.NotFound:
                        message = None
                if message is None:
                    message = await interaction.channel.send(
                        embed=embed, view=view,
                        allowed_mentions=discord.AllowedMentions.none())
                    with closing(self.store.connect()) as db:
                        db.execute(
                            "UPDATE check_settings SET feed_panel_message_id=? WHERE guild_id=?",
                            (message.id, interaction.guild.id))
                        db.commit()
            except discord.HTTPException:
                LOG.exception("Cannot publish SIJK CHECK feed intro")
                await interaction.followup.send(
                    "Не удалось опубликовать плашку. Проверь права бота на отправку сообщений "
                    "и встраивание ссылок.", ephemeral=True)
                return
        await interaction.followup.send(
            "Информационная плашка готова. Закрепи её в канале через меню сообщения. "
            "Повторный вызов команды обновит ту же плашку.", ephemeral=True)

    @app_commands.command(name="check_cleanup", description="Убрать старые публичные карточки /check")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def check_cleanup(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Нужны права администратора.", ephemeral=True)
            return
        if not self.in_public_channel(interaction):
            await interaction.response.send_message("Запусти в канале WINTRADE-CHECK.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted = 0
        async for message in interaction.channel.history(limit=500):
            if message.author.id != self.bot.user.id or not message.embeds:
                continue
            if any(embed.title == "ПРОВЕРКА НА ВИНТРЕЙД" for embed in message.embeds):
                continue
            if not any(
                ("У SIJK пока нет опубликованной оценки" in (embed.description or ""))
                or ((embed.footer.text or "") == "Субъективная оценка. Не доказательство нарушения правил.")
                for embed in message.embeds
            ):
                continue
            try:
                await message.delete()
                deleted += 1
            except discord.HTTPException:
                LOG.warning("Failed to delete old lookup %s", message.id, exc_info=True)
        await interaction.followup.send(
            f"Убрано старых карточек: {deleted}. Главная панель сохранена. "
            "Проверены последние 500 сообщений.", ephemeral=True)

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
            title="ПРОВЕРКА НА ВИНТРЕЙД",
            description=(
                "Проверь игрока по **Riot ID** в базе подозрительных игроков Сиджа.\n\n"
                "Для быстрой проверки вместо кнопки можно использовать команду **`/check`**.\n\n"
                "**Рассматриваются игроки от ранга Расцвет 1 (Ascendant 1) и выше.** "
                "Заявки на игроков более низкого ранга отклоняются без рассмотрения.\n\n"
                "Если ты столкнулся с предполагаемым винтрейдером в матче, "
                "можешь **подать заявку** и приложить доказательства. "
                "После проверки игрок может быть добавлен в базу.\n\n"
                "*Результаты проверки — субъективная оценка Сиджа, "
                "а не подтверждённый факт нарушения правил.*"
            ),
            color=discord.Color.from_rgb(242, 242, 242))
        await interaction.channel.send(embed=embed, view=CheckPanel(self))
        await interaction.response.send_message("Панель опубликована.", ephemeral=True)


async def setup_sijk_check(bot: commands.Bot, db_path: Path):
    await bot.add_cog(SijkCheck(bot, db_path))
