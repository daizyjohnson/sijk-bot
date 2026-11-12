from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger('sijk.notify_roles')
KINDS = ('twitch', 'youtube', 'telegram')


def get_notify_role(db_path: Path, guild_id: int, kind: str) -> int | None:
    if kind not in KINDS:
        return None
    try:
        with closing(sqlite3.connect(db_path)) as con:
            row = con.execute(
                f'SELECT {kind}_role_id FROM notify_role_settings WHERE guild_id=?',
                (guild_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] else None
    except sqlite3.OperationalError:
        return None


def notify_mention(db_path: Path, guild: discord.Guild, kind: str) -> str:
    role_id = get_notify_role(db_path, guild.id, kind)
    role = guild.get_role(role_id) if role_id else None
    return role.mention if role else ''


class NotificationRoleSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            custom_id='sijk:notify:select:v1',
            placeholder='Выбери нужные уведомления',
            min_values=0,
            max_values=3,
            options=[
                discord.SelectOption(label='Twitch — прямые эфиры', value='twitch', emoji='💜'),
                discord.SelectOption(label='YouTube — новые видео', value='youtube', emoji='❤️'),
                discord.SelectOption(label='Telegram — новые посты', value='telegram', emoji='💙'),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog('NotifyRoles')
        if cog is None or interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('Панель недоступна.', ephemeral=True)
            return
        settings = cog.settings(interaction.guild.id)
        if not settings:
            await interaction.response.send_message('Администратор ещё не настроил роли.', ephemeral=True)
            return
        mapping = {
            'twitch': settings['twitch_role_id'],
            'youtube': settings['youtube_role_id'],
            'telegram': settings['telegram_role_id'],
        }
        roles = {kind: interaction.guild.get_role(role_id) for kind, role_id in mapping.items()}
        if not all(roles.values()):
            await interaction.response.send_message('Одна из ролей удалена. Попроси администратора повторить /notify_setup.', ephemeral=True)
            return
        me = interaction.guild.me
        if not me or not me.guild_permissions.manage_roles or any(role >= me.top_role for role in roles.values()):
            await interaction.response.send_message('У бота нет прав для управления этими ролями.', ephemeral=True)
            return
        selected = set(self.values)
        to_add = [role for kind, role in roles.items() if kind in selected and role not in interaction.user.roles]
        to_remove = [role for kind, role in roles.items() if kind not in selected and role in interaction.user.roles]
        try:
            if to_add:
                await interaction.user.add_roles(*to_add, reason='Подписка на уведомления SIJK')
            if to_remove:
                await interaction.user.remove_roles(*to_remove, reason='Изменение подписки SIJK')
        except discord.HTTPException:
            log.exception('Не удалось изменить роли уведомлений для %s', interaction.user.id)
            await interaction.response.send_message('Не получилось обновить роли. Попробуй ещё раз.', ephemeral=True)
            return
        labels = {'twitch': '💜 Twitch', 'youtube': '❤️ YouTube', 'telegram': '💙 Telegram'}
        result = ', '.join(labels[kind] for kind in KINDS if kind in selected) or 'никакие'
        await interaction.response.send_message(f'Готово! Твои уведомления: **{result}**.\nЧтобы изменить выбор, открой меню ещё раз.', ephemeral=True)


class NotificationRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(NotificationRoleSelect())


class NotifyRoles(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: Path):
        self.bot = bot
        self.db_path = Path(db_path)
        with closing(sqlite3.connect(self.db_path)) as con:
            con.execute('''CREATE TABLE IF NOT EXISTS notify_role_settings (
                guild_id INTEGER PRIMARY KEY,
                twitch_role_id INTEGER NOT NULL,
                youtube_role_id INTEGER NOT NULL,
                telegram_role_id INTEGER NOT NULL,
                panel_channel_id INTEGER,
                panel_message_id INTEGER
            )''')
            con.commit()
        bot.add_view(NotificationRoleView())

    def settings(self, guild_id: int):
        with closing(sqlite3.connect(self.db_path)) as con:
            con.row_factory = sqlite3.Row
            return con.execute('SELECT * FROM notify_role_settings WHERE guild_id=?', (guild_id,)).fetchone()

    @app_commands.command(name='notify_setup', description='Настроить роли уведомлений и опубликовать панель')
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel='Канал с панелью ролей', twitch='Роль Twitch', youtube='Роль YouTube', telegram='Роль Telegram')
    async def notify_setup(self, interaction: discord.Interaction, channel: discord.TextChannel,
                           twitch: discord.Role, youtube: discord.Role, telegram: discord.Role):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Команда только для администратора.', ephemeral=True)
            return
        if len({twitch.id, youtube.id, telegram.id}) != 3:
            await interaction.response.send_message('Выбери три разные роли.', ephemeral=True)
            return
        me = interaction.guild.me
        if not me or not me.guild_permissions.manage_roles or any(r >= me.top_role or r.managed for r in (twitch, youtube, telegram)):
            await interaction.response.send_message('Роли должны быть ниже роли бота, а у бота должно быть право «Управление ролями».', ephemeral=True)
            return
        perms = channel.permissions_for(me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message('Боту нужны права просмотра, отправки сообщений и Embed в этом канале.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            title='✦ SIJK SYSTEM | УВЕДОМЛЕНИЯ',
            description=(
                'Выбирай, о чём хочешь получать уведомления.\n\n'
                '💜 **Twitch** — начало прямых эфиров\n'
                '❤️ **YouTube** — новые видео\n'
                '💙 **Telegram** — новые посты\n\n'
                'Можно выбрать несколько вариантов или снять все отметки. '
                'Чтобы изменить подписки, открой меню ещё раз.\n\n'
                '*Важные объявления администрации приходят всем участникам.*'
            ),
            color=discord.Color.from_rgb(105, 200, 245),
        )
        old = self.settings(interaction.guild.id)
        try:
            msg = await channel.send(embed=embed, view=NotificationRoleView(), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await interaction.followup.send('Не удалось отправить панель. Проверь права бота.', ephemeral=True)
            return
        with closing(sqlite3.connect(self.db_path)) as con:
            con.execute('''INSERT INTO notify_role_settings
                (guild_id,twitch_role_id,youtube_role_id,telegram_role_id,panel_channel_id,panel_message_id)
                VALUES (?,?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET
                twitch_role_id=excluded.twitch_role_id,
                youtube_role_id=excluded.youtube_role_id,
                telegram_role_id=excluded.telegram_role_id,
                panel_channel_id=excluded.panel_channel_id,
                panel_message_id=excluded.panel_message_id''',
                (interaction.guild.id, twitch.id, youtube.id, telegram.id, channel.id, msg.id))
            con.commit()
        if old and old['panel_channel_id'] and old['panel_message_id']:
            old_channel = interaction.guild.get_channel(old['panel_channel_id'])
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old_msg = await old_channel.fetch_message(old['panel_message_id'])
                    await old_msg.delete()
                except discord.HTTPException:
                    pass
        await interaction.followup.send(f'Панель опубликована в {channel.mention}.', ephemeral=True)


async def setup_notify_roles(bot: commands.Bot, db_path: Path):
    await bot.add_cog(NotifyRoles(bot, db_path))
