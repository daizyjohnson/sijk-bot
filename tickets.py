from __future__ import annotations

import asyncio
import io
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger('sijk.tickets')
RED = discord.Color.from_rgb(170, 35, 49)
TYPES = {'complaint': 'Жалоба', 'suggestion': 'Предложение', 'support': 'Связь с администрацией'}


class TicketModal(discord.ui.Modal, title='Новое обращение'):
    details = discord.ui.TextInput(label='Опиши своё обращение', style=discord.TextStyle.paragraph,
                                   placeholder='Расскажи, что произошло или что ты предлагаешь…',
                                   min_length=10, max_length=1500)

    def __init__(self, cog: 'Tickets', kind: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.kind = kind

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.open_ticket(interaction, self.kind, str(self.details))


class TicketPanel(discord.ui.View):
    def __init__(self, cog: 'Tickets'):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label='Жалоба', emoji='🛡️', style=discord.ButtonStyle.danger,
                       custom_id='sijk:ticket:complaint')
    async def complaint(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketModal(self.cog, 'complaint'))

    @discord.ui.button(label='Предложение', emoji='💡', style=discord.ButtonStyle.secondary,
                       custom_id='sijk:ticket:suggestion')
    async def suggestion(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketModal(self.cog, 'suggestion'))

    @discord.ui.button(label='Администрация', emoji='💬', style=discord.ButtonStyle.secondary,
                       custom_id='sijk:ticket:support')
    async def support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketModal(self.cog, 'support'))


class CloseTicket(discord.ui.View):
    def __init__(self, cog: 'Tickets'):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label='Закрыть обращение', emoji='🔒', style=discord.ButtonStyle.danger,
                       custom_id='sijk:ticket:close')
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.close_ticket(interaction)


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: Path, config):
        self.bot = bot
        self.db_path = db_path
        self.config = config
        self.lock = asyncio.Lock()
        with closing(self.connect()) as con:
            con.execute('''CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                channel_id INTEGER UNIQUE, kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT)''')
            con.commit()
        bot.add_view(TicketPanel(self))
        bot.add_view(CloseTicket(self))

    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    @app_commands.command(name='ticketpanel', description='Опубликовать панель обращений SIJK SYSTEM')
    @app_commands.default_permissions(administrator=True)
    async def ticketpanel(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message('Команда доступна в текстовом канале сервера.', ephemeral=True)
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Нужны права администратора.', ephemeral=True)
            return
        if not interaction.channel.category:
            await interaction.response.send_message('Перемести этот канал в категорию Tickets, затем повтори команду.', ephemeral=True)
            return
        embed = discord.Embed(title='✦ SIJK SYSTEM | Обращения',
                              description='Нужна помощь, хочешь пожаловаться или предложить идею?\n\n'
                                          'Выбери тип обращения ниже. Бот создаст приватный канал для тебя и модераторов.',
                              color=RED)
        embed.set_footer(text='Одно активное обращение на участника')
        await interaction.response.defer(ephemeral=True)
        await interaction.channel.send(embed=embed, view=TicketPanel(self))
        await interaction.followup.send('Панель обращений опубликована.', ephemeral=True)

    async def open_ticket(self, interaction: discord.Interaction, kind: str, details: str):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('Тикеты доступны только на сервере.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            with closing(self.connect()) as con:
                existing = con.execute('SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status=?',
                                       (guild.id, interaction.user.id, 'open')).fetchone()
            if existing:
                channel = guild.get_channel(existing['channel_id'])
                if channel:
                    await interaction.followup.send(f'У тебя уже есть открытое обращение: {channel.mention}', ephemeral=True)
                    return
                with closing(self.connect()) as con:
                    con.execute('UPDATE tickets SET status=? WHERE guild_id=? AND user_id=? AND status=?',
                                ('closed', guild.id, interaction.user.id, 'open'))
                    con.commit()
            settings = self.config(guild.id)
            role = guild.get_role(settings['staff_role_id']) if settings['staff_role_id'] else None
            if role is None:
                await interaction.followup.send('Администрация ещё не настроила роль модераторов через `/setup staff`.', ephemeral=True)
                return
            panel_channel = interaction.channel
            if not isinstance(panel_channel, discord.TextChannel) or not panel_channel.category:
                await interaction.followup.send('Панель должна находиться внутри категории Tickets.', ephemeral=True)
                return
            category = panel_channel.category
            me = guild.me
            if me is None:
                await interaction.followup.send('Не удалось определить права бота.', ephemeral=True)
                return
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
                role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
            }
            try:
                channel = await guild.create_text_channel(
                    name=f'ticket-{interaction.user.id}', category=category, overwrites=overwrites,
                    topic=f'SIJK ticket | author={interaction.user.id} | type={kind}',
                    reason=f'Обращение от {interaction.user}')
            except discord.HTTPException:
                log.exception('Не удалось создать тикет')
                await interaction.followup.send('Не удалось создать канал. Проверь права бота на управление каналами.', ephemeral=True)
                return
            with closing(self.connect()) as con:
                cur = con.execute('INSERT INTO tickets(guild_id,user_id,channel_id,kind) VALUES (?,?,?,?)',
                                  (guild.id, interaction.user.id, channel.id, kind))
                ticket_id = cur.lastrowid
                con.commit()
                names = {
                "complaint": "жалоба",
                "suggestion": "предложение",
                "support": "помощь",
            }
            prefix = names.get(kind, "тикет")
            try:
                await channel.edit(name=f"{prefix}-{ticket_id:04d}")
            except discord.HTTPException:
                log.exception("Не удалось переименовать тикет")
            embed = discord.Embed(title=f'Обращение #{ticket_id} · {TYPES[kind]}',
                                  description=details, color=RED)
            embed.add_field(name='Автор', value=interaction.user.mention, inline=False)
            embed.set_footer(text='Чтобы завершить обращение, нажми кнопку ниже')
            try:
                await channel.send(content=interaction.user.mention, embed=embed, view=CloseTicket(self),
                                   allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            except discord.HTTPException:
                log.exception('Не удалось отправить первое сообщение тикета')
            await interaction.followup.send(f'Обращение создано: {channel.mention}', ephemeral=True)

    async def close_ticket(self, interaction: discord.Interaction):
        guild, channel = interaction.guild, interaction.channel
        if not guild or not isinstance(channel, discord.TextChannel):
            return
        with closing(self.connect()) as con:
            ticket = con.execute('SELECT * FROM tickets WHERE guild_id=? AND channel_id=? AND status=?',
                                 (guild.id, channel.id, 'open')).fetchone()
        if ticket is None:
            await interaction.response.send_message('Это не открытый тикет.', ephemeral=True)
            return
        settings = self.config(guild.id)
        role_id = settings['staff_role_id']
        is_staff = (isinstance(interaction.user, discord.Member) and
                    (interaction.user.guild_permissions.administrator or
                     (role_id and any(r.id == role_id for r in interaction.user.roles))))
        if interaction.user.id != ticket['user_id'] and not is_staff:
            await interaction.response.send_message('Закрыть тикет может только автор или модератор.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            with closing(self.connect()) as con:
                changed = con.execute('UPDATE tickets SET status=?, closed_at=CURRENT_TIMESTAMP WHERE id=? AND status=?',
                                      ('closed', ticket['id'], 'open'))
                con.commit()
                if not changed.rowcount:
                    await interaction.followup.send('Этот тикет уже закрывается.', ephemeral=True)
                    return
            lines = [f'SIJK SYSTEM | Тикет #{ticket["id"]} | {TYPES.get(ticket["kind"], ticket["kind"])}',
                     f'Автор: {ticket["user_id"]} | Закрыл: {interaction.user.id}', '']
            try:
                async for msg in channel.history(limit=1000, oldest_first=True):
                    body = msg.content or ''
                    if msg.embeds:
                        body += ' [embed: ' + ' | '.join((e.title or '') + ' ' + (e.description or '') for e in msg.embeds) + ']'
                    if msg.attachments:
                        body += ' [вложения: ' + ', '.join(a.url for a in msg.attachments) + ']'
                    lines.append(f'{msg.created_at.isoformat()} | {msg.author} ({msg.author.id}): {body}')
            except discord.HTTPException:
                lines.append('[Не удалось прочитать историю канала]')
            transcript = '\n'.join(lines).encode('utf-8')
            log_channel = guild.get_channel(settings['request_log_channel_id']) if settings['request_log_channel_id'] else None
            if not isinstance(log_channel, discord.TextChannel):
                await interaction.followup.send('Тикет отмечен закрытым, но канал логов не найден. Канал обращения оставлен для сохранности переписки.', ephemeral=True)
                return
            embed = discord.Embed(title=f'Тикет #{ticket["id"]} закрыт', color=RED)
            embed.add_field(name='Тип', value=TYPES.get(ticket['kind'], ticket['kind']))
            embed.add_field(name='Автор', value=f'<@{ticket["user_id"]}>')
            embed.add_field(name='Закрыл', value=interaction.user.mention)
            embed.timestamp = discord.utils.utcnow()
            try:
                await log_channel.send(embed=embed, file=discord.File(io.BytesIO(transcript), filename=f'ticket-{ticket["id"]}.txt'),
                                       allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.exception('Не удалось сохранить историю тикета')
                await interaction.followup.send('Тикет закрыт, но история не сохранилась. Канал оставлен для ручной проверки.', ephemeral=True)
                return
            await interaction.followup.send('Тикет закрыт, история сохранена в логах.', ephemeral=True)
            try:
                await channel.delete(reason=f'Тикет #{ticket["id"]} закрыт')
            except discord.HTTPException:
                log.exception('Не удалось удалить закрытый тикет')


async def setup_tickets(bot: commands.Bot, db_path: Path, config):
    await bot.add_cog(Tickets(bot, db_path, config))
