"""SIJK SYSTEM: persistent rules acceptance and access role."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger('sijk.verification')
RED = discord.Color.from_rgb(168, 59, 80)
GRAY = discord.Color.from_rgb(116, 122, 131)


class AcceptRules(discord.ui.View):
    def __init__(self, cog: 'Verification'):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label='Принимаю правила', emoji='✅', style=discord.ButtonStyle.success,
                       custom_id='sijk:verification:accept:v1')
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('Кнопка доступна только участникам сервера.', ephemeral=True)
            return
        settings = self.cog.get_settings(interaction.guild.id)
        if settings is None:
            await interaction.response.send_message('Подтверждение правил пока не настроено.', ephemeral=True)
            return
        if interaction.channel_id != settings['channel_id']:
            await interaction.response.send_message('Подтверждай правила в канале с правилами сервера.', ephemeral=True)
            return
        role = interaction.guild.get_role(settings['role_id'])
        if role is None:
            await interaction.response.send_message('Роль доступа не найдена. Сообщи администрации.', ephemeral=True)
            return
        member = interaction.user
        if role in member.roles:
            await interaction.response.send_message('Ты уже приняла правила — доступ открыт! 🖤', ephemeral=True)
            return
        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_roles or role >= me.top_role:
            await interaction.response.send_message('Бот пока не может выдать роль. Сообщи администрации.', ephemeral=True)
            log.error('Cannot assign role %s in guild %s: permissions or hierarchy', role.id, interaction.guild.id)
            return
        try:
            await member.add_roles(role, reason='SIJK SYSTEM: участник принял правила')
        except discord.Forbidden:
            await interaction.response.send_message('Недостаточно прав для выдачи роли. Сообщи администрации.', ephemeral=True)
        except discord.HTTPException:
            log.exception('Failed to assign verification role')
            await interaction.response.send_message('Discord не смог выдать роль. Попробуй ещё раз.', ephemeral=True)
        else:
            await interaction.response.send_message('✅ Правила приняты! Роль **SIJK SQUAD** выдана. Добро пожаловать! 🖤', ephemeral=True)


class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: Path):
        self.bot = bot
        self.db_path = Path(db_path)
        with closing(self.db()) as con:
            con.execute('''CREATE TABLE IF NOT EXISTS verification_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL
            )''')
            con.commit()
        bot.add_view(AcceptRules(self))

    def db(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def get_settings(self, guild_id: int):
        with closing(self.db()) as con:
            return con.execute('SELECT * FROM verification_settings WHERE guild_id=?', (guild_id,)).fetchone()

    @app_commands.command(name='rules_setup', description='Привязать канал правил и роль доступа')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel='Канал с правилами', role='Роль, которую выдавать после подтверждения')
    async def rules_setup(self, interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role):
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Нужны права администратора.', ephemeral=True)
            return
        guild = interaction.guild
        if guild is None or channel.guild.id != guild.id or role.guild.id != guild.id:
            await interaction.response.send_message('Канал и роль должны быть с этого сервера.', ephemeral=True)
            return
        if role.is_default() or role.managed() or role.permissions.administrator:
            await interaction.response.send_message('Выбери обычную роль участника без административных прав.', ephemeral=True)
            return
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles or role >= me.top_role:
            await interaction.response.send_message('Боту нужно право «Управлять ролями», а его роль должна быть выше выбранной.', ephemeral=True)
            return
        with closing(self.db()) as con:
            con.execute('''INSERT INTO verification_settings(guild_id,channel_id,role_id) VALUES(?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, role_id=excluded.role_id''',
                (guild.id, channel.id, role.id))
            con.commit()
        await interaction.response.send_message(
            f'Готово! Подтверждение настроено: {channel.mention} → {role.mention}. '
            'Теперь выполни `/rules_panel` в канале правил.', ephemeral=True)

    @app_commands.command(name='rules_panel', description='Опубликовать правила с кнопкой подтверждения')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def rules_panel(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Нужны права администратора.', ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            return
        settings = self.get_settings(guild.id)
        if settings is None:
            await interaction.response.send_message('Сначала выполни `/rules_setup`.', ephemeral=True)
            return
        if interaction.channel_id != settings['channel_id']:
            await interaction.response.send_message('Выполни команду в настроенном канале правил.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        rules = discord.Embed(
            title='✦ ПРАВИЛА SIJK LA BYBA',
            description='**Уважение · Безопасность · Честная игра**\n\n'
                        '**01 / УВАЖЕНИЕ**\n'
                        'Без оскорблений, травли, дискриминации и провокаций. '
                        'Спорные ситуации решай спокойно, при необходимости через администрацию.\n\n'
                        '**02 / ОБЩЕНИЕ И КОНТЕНТ**\n'
                        'Пиши по теме каналов. Не публикуй шокирующий, порнографический '
                        'или иной запрещённый контент.\n\n'
                        '**03 / РЕКЛАМА И ССЫЛКИ**\n'
                        'Реклама и продвижение сторонних ресурсов — только с разрешения администрации.\n\n'
                        '**04 / ЧЕСТНАЯ ИГРА**\n'
                        'Не используй, не распространяй и не рекламируй читы и запрещённое ПО.\n\n'
                        '**05 / РЕШЕНИЕ КОНФЛИКТОВ**\n'
                        'Не устраивай публичных разборок. Для обращения к администрации '
                        'используй систему тикетов. Соблюдай требования модераторов.',
            color=RED)
        rules.set_footer(text='SIJK SYSTEM • Rules')
        sanctions = discord.Embed(
            title='✦ НАРУШЕНИЯ И САНКЦИИ',
            description='За нарушение правил возможны предупреждение, тайм-аут, '
                        'ограничение доступа или блокировка — в зависимости от ситуации.\n\n'
                        'Администрация может дополнять правила и рассматривает спорные случаи.',
            color=GRAY)
        confirm = discord.Embed(
            title='✦ ПОДТВЕРЖДЕНИЕ',
            description='Прочитал правила? Нажми **«Принимаю правила»** ниже, '
                        'чтобы получить роль **SIJK SQUAD** и открыть доступ к сообществу.',
            color=RED)
        try:
            await interaction.channel.send(embeds=[rules, sanctions, confirm], view=AcceptRules(self),
                                           allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception('Failed to publish rules panel')
            await interaction.followup.send('Не удалось опубликовать панель. Проверь права бота.', ephemeral=True)
            return
        await interaction.followup.send('Правила и кнопка опубликованы. Старые сообщения не удалялись.', ephemeral=True)


async def setup_verification(bot: commands.Bot, db_path: Path):
    await bot.add_cog(Verification(bot, db_path))
