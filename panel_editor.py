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

log = logging.getLogger('sijk.panel_editor')
MESSAGE_LINK = re.compile(r'^https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)/?$')
MAX_HISTORY = 20


def pack(embed: discord.Embed) -> str:
    return json.dumps(embed.to_dict(), ensure_ascii=False)


def unpack(data: str) -> discord.Embed:
    return discord.Embed.from_dict(json.loads(data))


def short(s: str | None, limit: int = 90) -> str:
    s = (s or 'Без заголовка').replace('\n', ' ')
    return s[:limit] + ('…' if len(s) > limit else '')


class PanelEditor(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: Path):
        self.bot = bot
        self.db_path = Path(db_path)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS panel_editor_registry (
                guild_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL, label TEXT NOT NULL,
                PRIMARY KEY(guild_id, message_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS panel_editor_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                embed_json TEXT NOT NULL, editor_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            db.commit()

    def registered(self, guild_id: int, message_id: int) -> bool:
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute('SELECT 1 FROM panel_editor_registry WHERE guild_id=? AND message_id=?',
                              (guild_id, message_id)).fetchone() is not None

    def save_version(self, guild_id: int, message_id: int, embed: discord.Embed, editor_id: int):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute('INSERT INTO panel_editor_history (guild_id,message_id,embed_json,editor_id) VALUES (?,?,?,?)',
                       (guild_id, message_id, pack(embed), editor_id))
            db.execute('''DELETE FROM panel_editor_history WHERE guild_id=? AND message_id=? AND id NOT IN (
                SELECT id FROM panel_editor_history WHERE guild_id=? AND message_id=? ORDER BY id DESC LIMIT ?)''',
                (guild_id, message_id, guild_id, message_id, MAX_HISTORY))
            db.commit()

    async def resolve(self, interaction: discord.Interaction, link: str, require_registered: bool = True):
        match = MESSAGE_LINK.fullmatch(link.strip())
        if not match or interaction.guild is None or int(match.group(1)) != interaction.guild.id:
            raise ValueError('Нужна ссылка на сообщение из этого сервера Discord.')
        channel_id, message_id = int(match.group(2)), int(match.group(3))
        if require_registered and not self.registered(interaction.guild.id, message_id):
            raise ValueError('Эта плашка не добавлена. Сначала используй /panel_add.')
        channel = interaction.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError('Текстовый канал не найден или недоступен.')
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException as exc:
            raise ValueError('Не удалось получить сообщение. Проверь ссылку и права бота.') from exc
        if not self.bot.user or message.author.id != self.bot.user.id:
            raise ValueError('Редактировать можно только сообщения SIJK SYSTEM.')
        if len(message.embeds) != 1:
            raise ValueError('Редактор поддерживает сообщения ровно с одним Embed.')
        return message

    @staticmethod
    def is_admin(interaction: discord.Interaction) -> bool:
        return bool(interaction.guild and isinstance(interaction.user, discord.Member)
                    and interaction.user.guild_permissions.administrator)

    @app_commands.command(name='panel_new', description='Создать новую статичную плашку SIJK SYSTEM')
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(name='Название панели в редакторе (необязательно)')
    async def panel_new(self, interaction: discord.Interaction, name: str = 'Новая панель'):
        if not self.is_admin(interaction):
            await interaction.response.send_message('Только для администраторов.', ephemeral=True)
            return
        name = name.strip()
        if not 1 <= len(name) <= 80:
            await interaction.response.send_message('Название должно быть от 1 до 80 символов.', ephemeral=True)
            return
        channel = interaction.channel
        if interaction.guild is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message('Используй команду в текстовом канале сервера.', ephemeral=True)
            return
        me = interaction.guild.me
        if me is None or not (channel.permissions_for(me).send_messages and
                              channel.permissions_for(me).embed_links):
            await interaction.response.send_message(
                'Боту нужны разрешения «Отправлять сообщения» и «Встраивать ссылки».', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = discord.Embed(
            title='✦ НОВАЯ ПАНЕЛЬ',
            description='Здесь будет текст твоей панели. Открой редактор, чтобы изменить оформление и содержание.',
            color=discord.Color(0xF5A623),
        )
        embed.set_footer(text='SIJK SYSTEM • Новая панель')
        try:
            msg = await channel.send(embed=embed)
        except discord.HTTPException:
            await interaction.followup.send('Не удалось отправить Embed. Проверь права бота.', ephemeral=True)
            return
        try:
            with closing(sqlite3.connect(self.db_path)) as db:
                db.execute('''INSERT INTO panel_editor_registry(guild_id,message_id,channel_id,label)
                    VALUES(?,?,?,?)''', (interaction.guild.id, msg.id, channel.id, name))
                db.commit()
        except sqlite3.Error:
            log.exception('Could not register new panel')
            try:
                await msg.delete()
            except discord.HTTPException:
                log.exception('Could not remove unregistered panel message')
            await interaction.followup.send('Не удалось зарегистрировать панель. Попробуй ещё раз.', ephemeral=True)
            return
        await interaction.followup.send(
            f'✅ Плашка **{discord.utils.escape_markdown(name)}** создана: {msg.jump_url}\n'
            'Чтобы изменить её, используй `/panel_edit` и вставь ссылку на сообщение.',
            ephemeral=True,
        )

    @app_commands.command(name='panel_add', description='Добавить статичную плашку SIJK SYSTEM в редактор')
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(link='Ссылка на сообщение бота', name='Название для списка редактора')
    async def panel_add(self, interaction: discord.Interaction, link: str, name: str):
        if not self.is_admin(interaction):
            await interaction.response.send_message('Только для администраторов.', ephemeral=True)
            return
        try:
            msg = await self.resolve(interaction, link, require_registered=False)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if not name.strip() or len(name) > 80:
            await interaction.response.send_message('Название должно быть от 1 до 80 символов.', ephemeral=True)
            return
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute('''INSERT INTO panel_editor_registry(guild_id,message_id,channel_id,label)
                VALUES(?,?,?,?) ON CONFLICT(guild_id,message_id) DO UPDATE SET
                channel_id=excluded.channel_id,label=excluded.label''',
                (interaction.guild.id, msg.id, msg.channel.id, name.strip()))
            db.commit()
        await interaction.response.send_message(f'✅ Плашка **{discord.utils.escape_markdown(name.strip())}** добавлена в редактор.', ephemeral=True)

    @app_commands.command(name='panel_list', description='Список зарегистрированных статичных плашек')
    @app_commands.default_permissions(administrator=True)
    async def panel_list(self, interaction: discord.Interaction):
        if not self.is_admin(interaction):
            await interaction.response.send_message('Только для администраторов.', ephemeral=True)
            return
        with closing(sqlite3.connect(self.db_path)) as db:
            rows = db.execute('SELECT label,channel_id,message_id FROM panel_editor_registry WHERE guild_id=? ORDER BY label LIMIT 30',
                              (interaction.guild.id,)).fetchall()
        if not rows:
            content = 'Плашек пока нет. Создай первую через `/panel_new` или добавь существующую через `/panel_add`.'
        else:
            content = '\n'.join(f'• **{discord.utils.escape_markdown(label)}** — https://discord.com/channels/{interaction.guild.id}/{cid}/{mid}'
                                for label, cid, mid in rows)
        await interaction.response.send_message(content[:1900], ephemeral=True)

    @app_commands.command(name='panel_edit', description='Открыть редактор статичной плашки')
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(link='Ссылка на зарегистрированную плашку')
    async def panel_edit(self, interaction: discord.Interaction, link: str):
        if not self.is_admin(interaction):
            await interaction.response.send_message('Только для администраторов.', ephemeral=True)
            return
        try:
            msg = await self.resolve(interaction, link)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        editor = EditorView(self, interaction.user.id, msg)
        await interaction.response.send_message('**Редактор SIJK SYSTEM**\nМеняем только Embed. Кнопки и меню остаются прежними.\nИзменения применяются только после «Сохранить».',
                                                embed=editor.draft, view=editor, ephemeral=True)

    @app_commands.command(name='panel_history', description='История оформления статичной плашки')
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(link='Ссылка на зарегистрированную плашку')
    async def panel_history(self, interaction: discord.Interaction, link: str):
        if not self.is_admin(interaction):
            await interaction.response.send_message('Только для администраторов.', ephemeral=True)
            return
        try:
            msg = await self.resolve(interaction, link)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        with closing(sqlite3.connect(self.db_path)) as db:
            rows = db.execute('''SELECT id,created_at,embed_json FROM panel_editor_history
                WHERE guild_id=? AND message_id=? ORDER BY id DESC LIMIT 20''',
                (interaction.guild.id, msg.id)).fetchall()
        if not rows:
            await interaction.response.send_message('Истории пока нет. Она появится после первого сохранения.', ephemeral=True)
            return
        view = HistoryView(self, interaction.user.id, msg, rows)
        await interaction.response.send_message('**История изменений**\nВыбери версию в меню, чтобы посмотреть её и при необходимости восстановить.',
                                                embed=unpack(rows[0][2]), view=view, ephemeral=True)


class EditorModal(discord.ui.Modal, title='Редактирование плашки'):
    def __init__(self, editor: 'EditorView'):
        super().__init__()
        self.editor = editor
        self.heading = discord.ui.TextInput(label='Заголовок', required=False, max_length=256,
                                            default=editor.draft.title or '')
        self.description = discord.ui.TextInput(label='Описание', style=discord.TextStyle.paragraph,
                                                required=False, max_length=4000,
                                                default=editor.draft.description or '')
        self.color = discord.ui.TextInput(label='Цвет HEX (например #F5A623)', max_length=7,
                                          default=f'#{editor.draft.color.value:06X}')
        self.footer = discord.ui.TextInput(label='Нижняя подпись (можно оставить пустой)', required=False,
                                           max_length=2048, default=editor.draft.footer.text or '')
        for field in (self.heading, self.description, self.color, self.footer):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.color.value.strip()
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', raw):
            await interaction.response.send_message('Цвет должен быть в формате `#F5A623`.', ephemeral=True)
            return
        draft = self.editor.draft.copy()
        draft.title = self.heading.value or None
        draft.description = self.description.value or None
        draft.color = discord.Color(int(raw[1:], 16))
        draft.set_footer(text=self.footer.value or None)
        self.editor.draft = draft
        await interaction.response.edit_message(embed=draft, view=self.editor)


class EditorView(discord.ui.View):
    def __init__(self, cog: PanelEditor, owner_id: int, message: discord.Message):
        super().__init__(timeout=600)
        self.cog, self.owner_id, self.message = cog, owner_id, message
        self.draft = message.embeds[0].copy()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or not self.cog.is_admin(interaction):
            await interaction.response.send_message('Этот редактор открыт другим администратором.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Изменить', style=discord.ButtonStyle.primary, emoji='✏️')
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EditorModal(self))

    @discord.ui.button(label='Сохранить', style=discord.ButtonStyle.success, emoji='💾')
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            current = await self.message.channel.fetch_message(self.message.id)
            if not current.embeds or not self.cog.bot.user or current.author.id != self.cog.bot.user.id:
                raise ValueError('Сообщение изменено или удалено')
            # Editing only embeds leaves the message components untouched.
            await current.edit(embed=self.draft)
        except (discord.HTTPException, ValueError):
            await interaction.followup.send('Не удалось сохранить. Проверь права бота и существование сообщения.', ephemeral=True)
            return
        self.cog.save_version(interaction.guild.id, self.message.id, current.embeds[0], interaction.user.id)
        self.message = current
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(content='✅ Сохранено! Исходное сообщение обновлено, кнопки не затронуты.',
                                                 embed=self.draft, view=self)

    @discord.ui.button(label='Отмена', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content='Изменения отменены.', embed=None, view=self)


class HistoryView(discord.ui.View):
    def __init__(self, cog: PanelEditor, owner_id: int, message: discord.Message, rows: list):
        super().__init__(timeout=600)
        self.cog, self.owner_id, self.message, self.rows = cog, owner_id, message, rows
        self.chosen = rows[0]
        selector = discord.ui.Select(placeholder='Выбери версию', options=[
            discord.SelectOption(label=f'Версия #{row[0]} · {row[1]} UTC', value=str(row[0]),
                                 description=short(unpack(row[2]).title)) for row in rows
        ])
        selector.callback = self.select_version
        self.selector = selector
        self.add_item(selector)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or not self.cog.is_admin(interaction):
            await interaction.response.send_message('Недостаточно прав.', ephemeral=True)
            return False
        return True

    async def select_version(self, interaction: discord.Interaction):
        self.chosen = next(row for row in self.rows if str(row[0]) == self.selector.values[0])
        await interaction.response.edit_message(embed=unpack(self.chosen[2]), view=self)

    @discord.ui.button(label='Восстановить выбранную', style=discord.ButtonStyle.danger, emoji='↩️')
    async def restore(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            current = await self.message.channel.fetch_message(self.message.id)
            if not current.embeds or not self.cog.bot.user or current.author.id != self.cog.bot.user.id:
                await interaction.followup.send('Сообщение не найдено или недоступно.', ephemeral=True)
                return
            restored = unpack(self.chosen[2])
            await current.edit(embed=restored)
        except discord.HTTPException:
            await interaction.followup.send('Не удалось восстановить оформление.', ephemeral=True)
            return
        self.cog.save_version(interaction.guild.id, self.message.id, current.embeds[0], interaction.user.id)
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(content='✅ Версия восстановлена. Кнопки и меню сообщения сохранены.',
                                                 embed=restored, view=self)


async def setup_panel_editor(bot: commands.Bot, db_path: Path):
    await bot.add_cog(PanelEditor(bot, db_path))
