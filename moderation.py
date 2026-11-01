from __future__ import annotations
import json
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from telegram_bridge import setup_telegram

log = logging.getLogger('sijk.moderation')
FORM_URL = 'https://forms.gle/7pvXaR8FLHrGr2ar8'
ID_RE = re.compile(r'^\d{17,20}$')


class RejectModal(discord.ui.Modal):
    reason = discord.ui.TextInput(label='Причина отказа', style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, cog, appeal_id):
        super().__init__(title=f'Отклонение апелляции #{appeal_id}')
        self.cog, self.appeal_id = cog, appeal_id

    async def on_submit(self, interaction):
        await self.cog.decide(interaction, self.appeal_id, False, str(self.reason))


class AppealView(discord.ui.View):
    def __init__(self, cog, appeal_id):
        super().__init__(timeout=None)
        self.cog = cog
        yes = discord.ui.Button(label='Одобрить', style=discord.ButtonStyle.success,
                                custom_id=f'sijk:appeal:yes:{appeal_id}')
        no = discord.ui.Button(label='Отклонить', style=discord.ButtonStyle.danger,
                               custom_id=f'sijk:appeal:no:{appeal_id}')

        async def accept(interaction):
            await cog.decide(interaction, appeal_id, True, 'Апелляция одобрена')

        async def reject(interaction):
            if not cog.staff(interaction):
                await interaction.response.send_message('Нет прав модератора.', ephemeral=True)
                return
            await interaction.response.send_modal(RejectModal(cog, appeal_id))

        yes.callback, no.callback = accept, reject
        self.add_item(yes)
        self.add_item(no)


class Moderation(commands.Cog):
    def __init__(self, bot, path):
        self.bot, self.path = bot, str(path)
        with closing(self.db()) as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS moderation_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL,
                    action TEXT NOT NULL, reason TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS moderation_appeals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL, nickname TEXT, statement TEXT NOT NULL,
                    evidence TEXT, status TEXT NOT NULL DEFAULT 'pending',
                    moderator_id INTEGER, decision_reason TEXT, message_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS moderation_appeal_config (
                    guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
                    webhook_id INTEGER NOT NULL);
            ''')
            con.commit()

    def db(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def staff(self, interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        p = interaction.user.guild_permissions
        if p.administrator or p.ban_members or p.moderate_members:
            return True
        with closing(self.db()) as con:
            row = con.execute('SELECT staff_role_id FROM guild_config WHERE guild_id=?',
                              (interaction.guild.id,)).fetchone()
        return bool(row and row['staff_role_id'] and any(
            role.id == row['staff_role_id'] for role in interaction.user.roles))

    def record(self, guild_id, user_id, moderator_id, action, reason):
        with closing(self.db()) as con:
            cur = con.execute('''INSERT INTO moderation_cases
                (guild_id,user_id,moderator_id,action,reason) VALUES (?,?,?,?,?)''',
                (guild_id, user_id, moderator_id, action, reason[:1500]))
            con.commit()
            return cur.lastrowid

    async def cog_load(self):
        with closing(self.db()) as con:
            rows = con.execute('''SELECT id FROM moderation_appeals
                WHERE status='pending' AND message_id IS NOT NULL''').fetchall()
        for row in rows:
            self.bot.add_view(AppealView(self, row['id']))

    async def decide(self, interaction, appeal_id, approved, reason):
        if not self.staff(interaction):
            await interaction.response.send_message('Нет прав модератора.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        with closing(self.db()) as con:
            row = con.execute('SELECT * FROM moderation_appeals WHERE id=? AND guild_id=?',
                              (appeal_id, interaction.guild_id)).fetchone()
        if not row or row['status'] != 'pending':
            await interaction.followup.send('Заявка уже рассмотрена.', ephemeral=True)
            return
        if approved:
            if not interaction.user.guild_permissions.ban_members or not interaction.guild.me.guild_permissions.ban_members:
                await interaction.followup.send('Для разбана нужно право Ban Members.', ephemeral=True)
                return
            try:
                await interaction.guild.unban(discord.Object(id=row['user_id']),
                                              reason=f'Апелляция #{appeal_id}: {reason}')
            except discord.NotFound:
                await interaction.followup.send('Пользователь уже не заблокирован.', ephemeral=True)
                return
            except discord.HTTPException as exc:
                await interaction.followup.send(f'Разбан не удался: {exc}', ephemeral=True)
                return
        with closing(self.db()) as con:
            cur = con.execute('''UPDATE moderation_appeals SET status=?,moderator_id=?,decision_reason=?
                WHERE id=? AND status='pending' ''',
                ('approved' if approved else 'rejected', interaction.user.id, reason, appeal_id))
            con.commit()
        if not cur.rowcount:
            await interaction.followup.send('Заявка уже рассмотрена.', ephemeral=True)
            return
        self.record(interaction.guild_id, row['user_id'], interaction.user.id,
                    'appeal_approved' if approved else 'appeal_rejected', reason)
        try:
            embed = interaction.message.embeds[0].copy()
            embed.color = discord.Color.green() if approved else discord.Color.red()
            embed.add_field(name='Решение', value=(
                ('Одобрено' if approved else 'Отклонено') +
                f' — {interaction.user.mention}\n{discord.utils.escape_markdown(reason)[:900]}'), inline=False)
            await interaction.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass
        try:
            user = await self.bot.fetch_user(row['user_id'])
            await user.send(f'SIJK SYSTEM | Апелляция #{appeal_id}: '
                            f'{"одобрена" if approved else "отклонена"}.\nПричина: {reason}')
        except discord.HTTPException:
            pass
        await interaction.followup.send('Решение сохранено.', ephemeral=True)

    @app_commands.command(name='appeals_setup', description='Настроить канал апелляций и вебхук Google Forms')
    @app_commands.default_permissions(administrator=True)
    async def appeals_setup(self, interaction: discord.Interaction, channel: discord.TextChannel,
                            webhook_id: str):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Только администратор.', ephemeral=True)
            return
        if not webhook_id.isdecimal():
            await interaction.response.send_message('Нужен числовой ID вебхука.', ephemeral=True)
            return
        with closing(self.db()) as con:
            con.execute('''INSERT INTO moderation_appeal_config(guild_id,channel_id,webhook_id)
                VALUES (?,?,?) ON CONFLICT(guild_id) DO UPDATE SET
                channel_id=excluded.channel_id,webhook_id=excluded.webhook_id''',
                (interaction.guild_id, channel.id, int(webhook_id)))
            con.commit()
        await interaction.response.send_message('Канал апелляций настроен.', ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or not message.webhook_id or not message.content.startswith('SIJK_APPEAL_V1\n'):
            return
        with closing(self.db()) as con:
            cfg = con.execute('SELECT * FROM moderation_appeal_config WHERE guild_id=?',
                              (message.guild.id,)).fetchone()
        if not cfg or message.channel.id != cfg['channel_id'] or message.webhook_id != cfg['webhook_id']:
            return
        try:
            payload = json.loads(message.content.split('\n', 1)[1])
            user_id = str(payload['discord_id']).strip()
            if not ID_RE.fullmatch(user_id):
                return
            user_id = int(user_id)
            nickname = str(payload.get('nickname', ''))[:150]
            statement = str(payload.get('reason', ''))[:1100]
            evidence = str(payload.get('evidence', ''))[:1000]
            if not statement:
                return
        except (ValueError, KeyError, TypeError):
            return
        with closing(self.db()) as con:
            if con.execute('''SELECT 1 FROM moderation_appeals WHERE
                guild_id=? AND user_id=? AND status='pending' ''',
                (message.guild.id, user_id)).fetchone():
                return
            cur = con.execute('''INSERT INTO moderation_appeals
                (guild_id,user_id,nickname,statement,evidence) VALUES (?,?,?,?,?)''',
                (message.guild.id, user_id, nickname, statement, evidence))
            appeal_id = cur.lastrowid
            con.commit()
        embed = discord.Embed(title=f'✦ SIJK SYSTEM | АПЕЛЛЯЦИЯ #{appeal_id}',
                              color=discord.Color.from_rgb(105, 200, 245))
        for label, value in [('Discord ID', str(user_id)), ('Ник', nickname),
                             ('Обращение', statement), ('Доказательства', evidence)]:
            embed.add_field(name=label, value=discord.utils.escape_markdown(value)[:1024] or '—',
                            inline=False)
        embed.set_footer(text='✦ Бот-сиджик')
        try:
            sent = await message.channel.send(embed=embed, view=AppealView(self, appeal_id),
                                              allowed_mentions=discord.AllowedMentions.none())
            with closing(self.db()) as con:
                con.execute('UPDATE moderation_appeals SET message_id=? WHERE id=?',
                            (sent.id, appeal_id))
                con.commit()
        except discord.HTTPException:
            log.exception('Не удалось опубликовать апелляцию')

    @app_commands.command(name='modhistory', description='История наказаний по Discord ID')
    @app_commands.default_permissions(moderate_members=True)
    async def modhistory(self, interaction: discord.Interaction, user_id: str):
        if not self.staff(interaction):
            await interaction.response.send_message('Нет доступа.', ephemeral=True)
            return
        if not ID_RE.fullmatch(user_id):
            await interaction.response.send_message('Неверный Discord ID.', ephemeral=True)
            return
        with closing(self.db()) as con:
            rows = con.execute('''SELECT * FROM moderation_cases WHERE guild_id=? AND user_id=?
                ORDER BY id DESC LIMIT 15''', (interaction.guild_id, int(user_id))).fetchall()
            auto = con.execute('''SELECT rule,action,created_at FROM automod_events
                WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 5''',
                (interaction.guild_id, int(user_id))).fetchall()
        lines = [f"#{r['id']} | {r['action']} | {r['reason'][:100]} | {r['created_at']} UTC"
                 for r in rows]
        lines += [f"AutoMod | {r['rule'][:80]} | <t:{r['created_at']}:d>" for r in auto]
        await interaction.response.send_message('\n'.join(lines)[:1900] if lines else 'История пуста.',
                                                ephemeral=True)

    @app_commands.command(name='case', description='Посмотреть наказание по номеру')
    @app_commands.default_permissions(moderate_members=True)
    async def case(self, interaction: discord.Interaction, case_id: int):
        if not self.staff(interaction):
            await interaction.response.send_message('Нет доступа.', ephemeral=True)
            return
        with closing(self.db()) as con:
            row = con.execute('SELECT * FROM moderation_cases WHERE guild_id=? AND id=?',
                              (interaction.guild_id, case_id)).fetchone()
        if not row:
            await interaction.response.send_message('Дело не найдено.', ephemeral=True)
            return
        await interaction.response.send_message(
            f"**Дело #{case_id}**\nПользователь: {row['user_id']}\n"
            f"Модератор: <@{row['moderator_id']}>\nДействие: {row['action']}\n"
            f"Причина: {row['reason']}\nДата: {row['created_at']} UTC",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='unban', description='Разбан по Discord ID с причиной')
    @app_commands.default_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str):
        if not interaction.guild or not interaction.user.guild_permissions.ban_members or not interaction.guild.me.guild_permissions.ban_members:
            await interaction.response.send_message('Нет прав на разбан.', ephemeral=True)
            return
        if not ID_RE.fullmatch(user_id):
            await interaction.response.send_message('Неверный Discord ID.', ephemeral=True)
            return
        try:
            await interaction.guild.unban(discord.Object(id=int(user_id)), reason=reason)
        except discord.HTTPException as exc:
            await interaction.response.send_message(f'Разбан не удался: {exc}', ephemeral=True)
            return
        case_id = self.record(interaction.guild_id, int(user_id), interaction.user.id, 'unban', reason)
        await interaction.response.send_message(f'Разбан выполнен. Дело #{case_id}.', ephemeral=True)


def enhance_existing_commands(bot, cog):
    """Wrap existing commands without duplicating slash-command registrations."""
    for name in ('ban', 'timeout', 'untimeout', 'kick', 'warn', 'unwarn'):
        command = bot.tree.get_command(name)
        if command is None:
            log.warning('Команда %s не найдена, пропускаем', name)
            continue
        original = command.callback

        async def wrapped(interaction, *args, _name=name, _original=original, **kwargs):
            member = kwargs.get('member')
            reason = str(kwargs.get('reason', ''))
            if (_name == 'ban' and member is not None and interaction.guild
                    and interaction.user.guild_permissions.ban_members
                    and interaction.guild.me.guild_permissions.ban_members
                    and interaction.guild.me.top_role > member.top_role
                    and (interaction.user.id == interaction.guild.owner_id or interaction.user.top_role > member.top_role)):
                try:
                    await member.send(embed=discord.Embed(
                        title='✦ SIJK SYSTEM | БЛОКИРОВКА',
                        description=f'Сервер: **{interaction.guild.name}**\n'
                                    f'**Причина:** {reason}\n**Срок:** бессрочно.\n\n'
                                    f'**Апелляция:** {FORM_URL}',
                        color=discord.Color.red()))
                except discord.HTTPException:
                    pass
            if _name == 'timeout' and member is not None:
                try:
                    await member.send(embed=discord.Embed(
                        title='✦ SIJK SYSTEM | ТАЙМ-АУТ',
                        description=f'Причина: {reason}\nСрок: {kwargs.get("minutes", "?")} мин.',
                        color=discord.Color.orange()))
                except discord.HTTPException:
                    pass
            await _original(interaction, *args, **kwargs)
            if interaction.guild and interaction.response.is_done() and member is not None:
                # A response can be an error; only successful command completion is guaranteed here.
                cog.record(interaction.guild.id, member.id, interaction.user.id, _name, reason or '—')
        command._callback = wrapped


async def setup_moderation(bot: commands.Bot, db_path: Path):
    cog = Moderation(bot, db_path)
    await bot.add_cog(cog)
    enhance_existing_commands(bot, cog)
    await setup_telegram(bot, db_path)
