"""SIJK SYSTEM — temporary voice rooms (discord.py 2.6+)."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger('sijk.voice')
RED = discord.Color.from_rgb(176, 35, 54)


class RoomControls(discord.ui.View):
    def __init__(self, manager: 'VoiceRooms'):
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(label='Название', emoji='✏️', style=discord.ButtonStyle.secondary, custom_id='sijk:voice:rename')
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.manager.is_owner(interaction):
            return
        await interaction.response.send_modal(RenameModal(self.manager))

    @discord.ui.button(label='Лимит', emoji='👥', style=discord.ButtonStyle.secondary, custom_id='sijk:voice:limit')
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.manager.is_owner(interaction):
            return
        await interaction.response.send_modal(LimitModal(self.manager))

    @discord.ui.button(label='Закрыть / открыть', emoji='🔒', style=discord.ButtonStyle.primary, custom_id='sijk:voice:lock')
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self.manager.owner_channel(interaction)
        if channel is None:
            return
        current = channel.overwrites_for(interaction.guild.default_role)
        locked = current.connect is False
        current.connect = None if locked else False
        try:
            await channel.set_permissions(interaction.guild.default_role, overwrite=current)
            await interaction.response.send_message('🔓 Комната открыта.' if locked else '🔒 Комната закрыта. Для друзей можно выдать доступ кнопкой «Пригласить».', ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message('Не удалось изменить доступ. Проверь права бота.', ephemeral=True)

    @discord.ui.button(label='Пригласить', emoji='➕', style=discord.ButtonStyle.success, custom_id='sijk:voice:invite')
    async def invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.manager.is_owner(interaction):
            return
        await interaction.response.send_modal(InviteModal(self.manager))


class RenameModal(discord.ui.Modal, title='Название комнаты'):
    name = discord.ui.TextInput(label='Новое название', min_length=1, max_length=80, placeholder='Например, Valorant | Пати')

    def __init__(self, manager: 'VoiceRooms'):
        super().__init__()
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction):
        channel = await self.manager.owner_channel(interaction)
        if channel is None:
            return
        try:
            await channel.edit(name=str(self.name).strip(), reason='Владелец изменил название временной комнаты')
            await interaction.response.send_message('Название комнаты изменено.', ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message('Не получилось переименовать комнату.', ephemeral=True)


class LimitModal(discord.ui.Modal, title='Лимит участников'):
    limit = discord.ui.TextInput(label='От 0 до 99 (0 — без лимита)', max_length=2, placeholder='5')

    def __init__(self, manager: 'VoiceRooms'):
        super().__init__()
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction):
        channel = await self.manager.owner_channel(interaction)
        if channel is None:
            return
        value = str(self.limit).strip()
        if not value.isdigit() or not 0 <= int(value) <= 99:
            await interaction.response.send_message('Введи целое число от 0 до 99.', ephemeral=True)
            return
        try:
            await channel.edit(user_limit=int(value))
            await interaction.response.send_message('Лимит обновлён.', ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message('Не получилось изменить лимит.', ephemeral=True)


class InviteModal(discord.ui.Modal, title='Разрешить вход участнику'):
    user_id = discord.ui.TextInput(label='Discord ID участника', placeholder='Включи режим разработчика → Копировать ID', min_length=17, max_length=21)

    def __init__(self, manager: 'VoiceRooms'):
        super().__init__()
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction):
        channel = await self.manager.owner_channel(interaction)
        if channel is None:
            return
        value = str(self.user_id).strip()
        if not value.isdecimal():
            await interaction.response.send_message('Нужен числовой Discord ID.', ephemeral=True)
            return
        member = interaction.guild.get_member(int(value))
        if member is None:
            await interaction.response.send_message('Участник не найден на сервере.', ephemeral=True)
            return
        try:
            await channel.set_permissions(member, view_channel=True, connect=True)
            await interaction.response.send_message(f'Доступ к комнате выдан: {member.mention}', ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message('Не удалось выдать доступ.', ephemeral=True)


class VoiceRooms(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: Path):
        self.bot = bot
        self.db_path = db_path
        self.lock = asyncio.Lock()
        with closing(self.db()) as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS voice_config (
                    guild_id INTEGER PRIMARY KEY, trigger_id INTEGER NOT NULL,
                    category_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voice_rooms (
                    channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL
                );
            ''')
            con.commit()
        bot.add_view(RoomControls(self))

    def db(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def setting(self, guild_id: int):
        with closing(self.db()) as con:
            return con.execute('SELECT * FROM voice_config WHERE guild_id=?', (guild_id,)).fetchone()

    def room(self, channel_id: int):
        with closing(self.db()) as con:
            return con.execute('SELECT * FROM voice_rooms WHERE channel_id=?', (channel_id,)).fetchone()

    async def owner_channel(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('Только на сервере.', ephemeral=True)
            return None
        voice = interaction.user.voice
        channel = voice.channel if voice else None
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message('Сначала зайди в свою временную голосовую комнату.', ephemeral=True)
            return None
        room = self.room(channel.id)
        if not room or room['owner_id'] != interaction.user.id:
            await interaction.response.send_message('Управлять комнатой может только её владелец.', ephemeral=True)
            return None
        return channel

    async def is_owner(self, interaction: discord.Interaction) -> bool:
        return await self.owner_channel(interaction) is not None

    @app_commands.command(name='voice_setup', description='Настроить создание временных голосовых комнат')
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(trigger='Голосовой канал «Создать комнату»', category='Категория для временных комнат')
    async def voice_setup(self, interaction: discord.Interaction, trigger: discord.VoiceChannel, category: discord.CategoryChannel):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Нужны права администратора.', ephemeral=True)
            return
        if trigger.guild.id != interaction.guild.id or category.guild.id != interaction.guild.id:
            await interaction.response.send_message('Каналы должны принадлежать этому серверу.', ephemeral=True)
            return
        with closing(self.db()) as con:
            con.execute('INSERT INTO voice_config(guild_id,trigger_id,category_id) VALUES (?,?,?) ON CONFLICT(guild_id) DO UPDATE SET trigger_id=excluded.trigger_id,category_id=excluded.category_id', (interaction.guild.id, trigger.id, category.id))
            con.commit()
        await interaction.response.send_message(f'Готово! Заход в {trigger.mention} создаёт временную комнату в категории **{category.name}**.', ephemeral=True)

    @app_commands.command(name='voice_panel', description='Опубликовать панель управления временными комнатами')
    @app_commands.default_permissions(administrator=True)
    async def voice_panel(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Нужны права администратора.', ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message('Используй команду в текстовом канале.', ephemeral=True)
            return
        embed = discord.Embed(title='✦ SIJK SYSTEM | Голосовые комнаты', description='Зайди в **➕ Создать комнату**, чтобы получить свою временную голосовую комнату.\n\nПока находишься в своей комнате, управляй ей кнопками ниже.', color=RED)
        embed.set_footer(text='Пустые временные комнаты удаляются автоматически')
        await interaction.response.defer(ephemeral=True)
        await interaction.channel.send(embed=embed, view=RoomControls(self))
        await interaction.followup.send('Панель опубликована.', ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot or before.channel == after.channel:
            return
        async with self.lock:
            if after.channel:
                setting = self.setting(member.guild.id)
                if setting and after.channel.id == setting['trigger_id']:
                    category = member.guild.get_channel(setting['category_id'])
                    if isinstance(category, discord.CategoryChannel):
                        try:
                            room = await member.guild.create_voice_channel(
                                name=f'Комната {member.display_name}'[:90], category=category,
                                reason=f'Временная комната для {member}')
                            with closing(self.db()) as con:
                                con.execute('INSERT INTO voice_rooms(channel_id,guild_id,owner_id) VALUES (?,?,?)', (room.id, member.guild.id, member.id))
                                con.commit()
                            try:
                                await member.move_to(room, reason='Создание временной комнаты')
                            except discord.HTTPException:
                                log.exception('Не удалось переместить пользователя в комнату')
                                await self.delete_if_empty(room)
                        except discord.HTTPException:
                            log.exception('Не удалось создать голосовую комнату')
            if before.channel and isinstance(before.channel, discord.VoiceChannel):
                await self.delete_if_empty(before.channel)

    async def delete_if_empty(self, channel: discord.VoiceChannel):
        if channel.members or not self.room(channel.id):
            return
        try:
            await channel.delete(reason='Пустая временная комната SIJK SYSTEM')
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.exception('Не удалось удалить пустую комнату %s', channel.id)
            return
        with closing(self.db()) as con:
            con.execute('DELETE FROM voice_rooms WHERE channel_id=?', (channel.id,))
            con.commit()

    @commands.Cog.listener()
    async def on_ready(self):
        # Clean up rooms left empty during downtime.
        async with self.lock:
            with closing(self.db()) as con:
                rooms = con.execute('SELECT channel_id,guild_id FROM voice_rooms').fetchall()
            for room in rooms:
                guild = self.bot.get_guild(room['guild_id'])
                if guild is None:
                    continue
                channel = guild.get_channel(room['channel_id'])
                if isinstance(channel, discord.VoiceChannel):
                    await self.delete_if_empty(channel)
                elif channel is None:
                    with closing(self.db()) as con:
                        con.execute('DELETE FROM voice_rooms WHERE channel_id=?', (room['channel_id'],))
                        con.commit()


async def setup_voice(bot: commands.Bot, db_path: Path):
    await bot.add_cog(VoiceRooms(bot, db_path))
