from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger('sijk.rank_roles')
RANKS = ('iron', 'bronze', 'silver', 'gold', 'platinum', 'diamond', 'ascendant', 'immortal', 'radiant')
LABELS = {r: r.upper() for r in RANKS}


class RankSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=LABELS[r], value=r) for r in RANKS]
        options.append(discord.SelectOption(label='Без ранга', value='none', description='Снять текущий ранг'))
        super().__init__(custom_id='sijk:ranks:select:v1', placeholder='Выбери свой ранг',
                         min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('Меню доступно только на сервере.', ephemeral=True)
            return
        cog = interaction.client.get_cog('RankRoles')
        if cog is None:
            await interaction.response.send_message('Система рангов недоступна.', ephemeral=True)
            return
        settings = cog.get_settings(interaction.guild.id)
        if not settings:
            await interaction.response.send_message('Ранги ещё не настроены.', ephemeral=True)
            return
        rank_roles = [interaction.guild.get_role(settings[f'{rank}_role_id']) for rank in RANKS]
        if any(role is None for role in rank_roles):
            await interaction.response.send_message('Одна из ролей рангов удалена. Сообщи администрации.', ephemeral=True)
            return
        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_roles or any(
            role >= me.top_role or role.managed for role in rank_roles
        ):
            await interaction.response.send_message('У бота нет прав управлять ролями рангов.', ephemeral=True)
            return
        chosen = self.values[0]
        target = None if chosen == 'none' else interaction.guild.get_role(settings[f'{chosen}_role_id'])
        rank_ids = {role.id for role in rank_roles}
        current_ids = {role.id for role in interaction.user.roles}
        desired_ids = (current_ids - rank_ids) | ({target.id} if target else set())
        if desired_ids == current_ids:
            await interaction.response.send_message('Этот ранг уже выбран.' if target else 'У тебя уже нет ранга.', ephemeral=True)
            return
        desired_roles = [role for role in interaction.user.roles if role.id not in rank_ids and not role.is_default()]
        if target:
            desired_roles.append(target)
        try:
            await interaction.user.edit(roles=desired_roles, reason='Самостоятельный выбор ранга SIJK')
        except discord.Forbidden:
            await interaction.response.send_message('Недостаточно прав для изменения ролей.', ephemeral=True)
            return
        except discord.HTTPException:
            log.exception('Ошибка изменения ранга для %s', interaction.user.id)
            await interaction.response.send_message('Не удалось изменить ранг. Попробуй ещё раз.', ephemeral=True)
            return
        await interaction.response.send_message(
            f'Твой ранг: **{LABELS[chosen]}**.' if target else 'Ранг снят.', ephemeral=True
        )


class RankView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RankSelect())


class RankRoles(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: Path):
        self.bot = bot
        self.db_path = Path(db_path)
        columns = ', '.join(f'{rank}_role_id INTEGER NOT NULL' for rank in RANKS)
        with closing(sqlite3.connect(self.db_path)) as con:
            con.execute(f'''CREATE TABLE IF NOT EXISTS rank_role_settings (
                guild_id INTEGER PRIMARY KEY, {columns},
                panel_channel_id INTEGER, panel_message_id INTEGER
            )''')
            con.commit()
        bot.add_view(RankView())

    def get_settings(self, guild_id: int):
        with closing(sqlite3.connect(self.db_path)) as con:
            con.row_factory = sqlite3.Row
            return con.execute('SELECT * FROM rank_role_settings WHERE guild_id=?', (guild_id,)).fetchone()

    @app_commands.command(name='rank_setup', description='Опубликовать панель самостоятельного выбора ранга')
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel='Канал для панели рангов', iron='Роль IRON', bronze='Роль BRONZE',
                           silver='Роль SILVER', gold='Роль GOLD', platinum='Роль PLATINUM',
                           diamond='Роль DIAMOND', ascendant='Роль ASCENDANT',
                           immortal='Роль IMMORTAL', radiant='Роль RADIANT')
    async def rank_setup(self, interaction: discord.Interaction, channel: discord.TextChannel,
                         iron: discord.Role, bronze: discord.Role, silver: discord.Role,
                         gold: discord.Role, platinum: discord.Role, diamond: discord.Role,
                         ascendant: discord.Role, immortal: discord.Role, radiant: discord.Role):
        if interaction.guild is None or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Команда только для администратора.', ephemeral=True)
            return
        roles = (iron, bronze, silver, gold, platinum, diamond, ascendant, immortal, radiant)
        if len({role.id for role in roles}) != len(RANKS):
            await interaction.response.send_message('Каждому рангу нужна отдельная роль.', ephemeral=True)
            return
        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_roles or any(
            role >= me.top_role or role.managed or role.is_default() for role in roles
        ):
            await interaction.response.send_message('Все роли должны быть ниже роли бота, а у бота должно быть право управления ролями.', ephemeral=True)
            return
        permissions = channel.permissions_for(me)
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links and permissions.read_message_history):
            await interaction.response.send_message('Боту нужны права просмотра, отправки, Embed и чтения истории сообщений.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            title='✦ SIJK SYSTEM | РАНГИ',
            description='Выбери свой ранг Valorant в меню ниже.\n\n'
                        '**Можно выбрать только один ранг.** При смене старый ранг снимается автоматически.\n\n'
                        '*Чтобы убрать ранг, выбери «Без ранга».*',
            color=discord.Color(0xF5A623),
        )
        try:
            message = await channel.send(embed=embed, view=RankView(), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await interaction.followup.send('Не удалось опубликовать панель. Проверь права бота.', ephemeral=True)
            return
        old = self.get_settings(interaction.guild.id)
        fields = ['guild_id'] + [f'{rank}_role_id' for rank in RANKS] + ['panel_channel_id', 'panel_message_id']
        placeholders = ','.join('?' for _ in fields)
        updates = ', '.join(f'{field}=excluded.{field}' for field in fields[1:])
        values = [interaction.guild.id] + [role.id for role in roles] + [channel.id, message.id]
        try:
            with closing(sqlite3.connect(self.db_path)) as con:
                con.execute(f'''INSERT INTO rank_role_settings ({','.join(fields)}) VALUES ({placeholders})
                    ON CONFLICT(guild_id) DO UPDATE SET {updates}''', values)
                con.commit()
        except sqlite3.Error:
            log.exception('Ошибка сохранения настроек рангов')
            await message.delete()
            await interaction.followup.send('Не удалось сохранить настройки рангов.', ephemeral=True)
            return
        if old and old['panel_channel_id'] and old['panel_message_id']:
            old_channel = interaction.guild.get_channel(old['panel_channel_id'])
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old_message = await old_channel.fetch_message(old['panel_message_id'])
                    await old_message.delete()
                except discord.HTTPException:
                    pass
        await interaction.followup.send(f'Панель рангов опубликована в {channel.mention}.', ephemeral=True)


async def setup_rank_roles(bot: commands.Bot, db_path: Path):
    await bot.add_cog(RankRoles(bot, db_path))
