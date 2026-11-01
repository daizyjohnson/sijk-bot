"""SIJK SYSTEM — temporary voice rooms and private academy rooms (discord.py 2.6+)."""
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
        academy = self.manager.academy_for_room(channel)
        try:
            if academy:
                # Keep everyone blocked and keep staff access. Lock students only.
                role = interaction.guild.get_role(academy['student_role_id'])
                if role is None:
                    await interaction.response.send_message('Роль учеников не найдена. Проверь /academy_setup.', ephemeral=True)
                    return
                overwrite = channel.overwrites_for(role)
                locked = overwrite.connect is False
                overwrite.connect = True if locked else False
                await channel.set_permissions(role, overwrite=overwrite)
            else:
                overwrite = channel.overwrites_for(interaction.guild.default_role)
                locked = overwrite.connect is False
                overwrite.connect = None if locked else False
                await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
            await interaction.response.send_message(
                '🔓 Комната открыта для учеников.' if academy and locked else
                '🔒 Комната закрыта для учеников. Владелец и модераторы сохраняют доступ.' if academy else
                '🔓 Комната открыта.' if locked else
                '🔒 Комната закрыта. Для друзей можно выдать доступ кнопкой «Пригласить».',
                ephemeral=True,
            )
        except discord.HTTPException:
            await interaction.response.send_message('Не удалось изменить доступ. Проверь права бота.', ephemeral=True)

    @discord.ui.button(label='Пригласить', emoji='➕', style=discord.ButtonStyle.success, custom_id='sijk:voice:invite')
    async def invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.manager.is_owner(interaction):
            return
        await interaction.response.send_modal(InviteModal(self.manager))


class RenameModal(discord.ui.Modal, title='Название комнаты'):
    name = discord.ui.TextInput(label='Новое название', min_length=1, max_length=80, placeholder='seuchas poigraem')

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
            try:
                member = await interaction.guild.fetch_member(int(value))
            except (discord.NotFound, discord.HTTPException):
                await interaction.response.send_message('Участник не найден на сервере.', ephemeral=True)
                return
        academy = self.manager.academy_for_room(channel)
        if academy and not self.manager.academy_allowed(member, academy):
            await interaction.response.send_message('В академию можно пригласить только STUDENT, byba или владельца сервера.', ephemeral=True)
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
                CREATE TABLE IF NOT EXISTS academy_config (
                    guild_id INTEGER PRIMARY KEY,
                    trigger_id INTEGER NOT NULL,
                    category_id INTEGER NOT NULL,
                    student_role_id INTEGER NOT NULL,
                    moderator_role_id INTEGER NOT NULL
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

    def academy_setting(self, guild_id: int):
        with closing(self.db()) as con:
            return con.execute('SELECT * FROM academy_config WHERE guild_id=?', (guild_id,)).fetchone()

    def room(self, channel_id: int):
        with closing(self.db()) as con:
            return con.execute('SELECT * FROM voice_rooms WHERE channel_id=?', (channel_id,)).fetchone()

    def academy_for_room(self, channel: discord.VoiceChannel):
        setting = self.academy_setting(channel.guild.id)
        if setting and channel.category_id == setting['category_id'] and self.room(channel.id):
            return setting
        return None

    @staticmethod
    def academy_allowed(member: discord.Member, setting) -> bool:
        if member.id == member.guild.owner_id:
            return True
        return any(role.id in (setting['student_role_id'], setting['moderator_role_id']) for role in member.roles)

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

    @app_commands.command(name='voice_setup', description='Настроить обычные временные голосовые комнаты')
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(trigger='Голосовой канал «Создать комнату»', category='Категория для обычных комнат')
    async def voice_setup(self, interaction: discord.Interaction, trigger: discord.VoiceChannel, category: discord.CategoryChannel):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Нужны права администратора.', ephemeral=True)
            return
        if trigger.guild.id != interaction.guild.id or category.guild.id != interaction.guild.id:
            await interaction.response.send_message('Каналы должны принадлежать этому серверу.', ephemeral=True)
            return
        academy = self.academy_setting(interaction.guild.id)
        if academy and (trigger.id == academy['trigger_id'] or category.id == academy['category_id']):
            await interaction.response.send_message('Эта точка создания или категория уже используется академией.', ephemeral=True)
            return
        with closing(self.db()) as con:
            con.execute('INSERT INTO voice_config(guild_id,trigger_id,category_id) VALUES (?,?,?) ON CONFLICT(guild_id) DO UPDATE SET trigger_id=excluded.trigger_id,category_id=excluded.category_id', (interaction.guild.id, trigger.id, category.id))
            con.commit()
        await interaction.response.send_message(f'Готово! Заход в {trigger.mention} создаёт комнату в категории **{category.name}**.', ephemeral=True)

    @app_commands.command(name='academy_setup', description='Настроить приватные голосовые комнаты академии')
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        trigger='Голосовой канал «Создать комнату» в академии',
        category='Категория АКАДЕМИЯ',
        student_role='Роль учеников STUDENT',
        moderator_role='Роль модераторов byba',
    )
    async def academy_setup(
        self, interaction: discord.Interaction, trigger: discord.VoiceChannel,
        category: discord.CategoryChannel, student_role: discord.Role, moderator_role: discord.Role,
    ):
        guild = interaction.guild
        if not guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Нужны права администратора.', ephemeral=True)
            return
        if (trigger.guild.id != guild.id or category.guild.id != guild.id or
                student_role.guild.id != guild.id or moderator_role.guild.id != guild.id):
            await interaction.response.send_message('Каналы и роли должны быть с этого сервера.', ephemeral=True)
            return
        if trigger.category_id != category.id:
            await interaction.response.send_message('Канал создания должен находиться в категории АКАДЕМИЯ.', ephemeral=True)
            return
        if student_role == guild.default_role or moderator_role == guild.default_role or student_role == moderator_role:
            await interaction.response.send_message('Выбери две разные роли: STUDENT и byba.', ephemeral=True)
            return
        normal = self.setting(guild.id)
        if normal and (trigger.id == normal['trigger_id'] or category.id == normal['category_id']):
            await interaction.response.send_message('Эта точка создания или категория уже используется обычными комнатами.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            # Restrict the trigger too: otherwise anyone might create a student room.
            await trigger.set_permissions(guild.default_role, view_channel=False, connect=False)
            await trigger.set_permissions(student_role, view_channel=True, connect=True)
            await trigger.set_permissions(moderator_role, view_channel=True, connect=True)
        except discord.HTTPException:
            await interaction.followup.send('Не удалось настроить права канала создания. Проверь «Управление каналами» у бота.', ephemeral=True)
            return
        with closing(self.db()) as con:
            con.execute('''INSERT INTO academy_config(guild_id,trigger_id,category_id,student_role_id,moderator_role_id)
                VALUES (?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET
                trigger_id=excluded.trigger_id, category_id=excluded.category_id,
                student_role_id=excluded.student_role_id, moderator_role_id=excluded.moderator_role_id''',
                (guild.id, trigger.id, category.id, student_role.id, moderator_role.id))
            con.commit()
        await interaction.followup.send(
            f'✅ Академия настроена! {trigger.mention} → **{category.name}**. Доступ: {student_role.mention}, {moderator_role.mention} и владелец сервера.\n'
            'Для полной приватности проверь также права самой категории и текстового чата академии.', ephemeral=True,
        )

    @app_commands.command(name='voice_panel', description='Опубликовать панель управления временными комнатами')
    @app_commands.default_permissions(administrator=True)
    async def voice_panel(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message('Нужны права администратора.', ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message('Используй команду в текстовом канале.', ephemeral=True)
            return
        academy = self.academy_setting(interaction.guild.id)
        is_academy = bool(academy and interaction.channel.category_id == academy['category_id'])
        description = (
            'Зайди в **✦ Создать комнату** в категории **АКАДЕМИЯ**.\n\n'
            'Управлять созданной комнатой можно кнопками ниже. Доступ только для STUDENT, byba и владельца сервера.'
            if is_academy else
            'Зайди в **➕ Создать комнату**, чтобы получить свою временную голосовую комнату.\n\n'
            'Пока находишься в своей комнате, управляй ей кнопками ниже.'
        )
        embed = discord.Embed(title='✦ SIJK SYSTEM | Академия' if is_academy else '✦ SIJK SYSTEM | Голосовые комнаты', description=description, color=RED)
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
                academy = self.academy_setting(member.guild.id)
                is_academy = bool(academy and after.channel.id == academy['trigger_id'])
                is_normal = bool(setting and after.channel.id == setting['trigger_id'])
                if is_academy or is_normal:
                    config = academy if is_academy else setting
                    category = member.guild.get_channel(config['category_id'])
                    if isinstance(category, discord.CategoryChannel):
                        if is_academy and not self.academy_allowed(member, academy):
                            log.warning('Пользователь %s без роли пытался создать комнату академии', member.id)
                            try:
                                await member.move_to(None, reason='Нет доступа к академии')
                            except discord.HTTPException:
                                log.exception('Не удалось отключить пользователя без доступа к академии')
                        else:
                            try:
                                overwrites = None
                                if is_academy:
                                    student = member.guild.get_role(academy['student_role_id'])
                                    moderator = member.guild.get_role(academy['moderator_role_id'])
                                    if not student or not moderator:
                                        log.error('Не найдены роли STUDENT/byba для академии на сервере %s', member.guild.id)
                                        return
                                    overwrites = {
                                        member.guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
                                        student: discord.PermissionOverwrite(view_channel=True, connect=True),
                                        moderator: discord.PermissionOverwrite(view_channel=True, connect=True),
                                        member: discord.PermissionOverwrite(view_channel=True, connect=True),
                                    }
                                room = await member.guild.create_voice_channel(
                                    name=f'Комната {member.display_name}'[:90],
                                    category=category, overwrites=overwrites,
                                    reason=f'Временная {"учебная " if is_academy else ""}комната для {member}',
                                )
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
