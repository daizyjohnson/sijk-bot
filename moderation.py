from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger('sijk.moderation')
DEFAULT_APPEAL_FORM_URL = 'https://forms.gle/7pvXaR8FLHrGr2ar8'

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: Path):
        self.bot, self.db_path = bot, db_path
        with closing(self.db()) as con:
            con.executescript('''
            CREATE TABLE IF NOT EXISTS mod_cases (
              id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL,
              action TEXT NOT NULL, reason TEXT NOT NULL, created_at INTEGER NOT NULL,
              related_case INTEGER, duration_minutes INTEGER,
              dm_status TEXT DEFAULT 'не отправлялось'
            );
            CREATE INDEX IF NOT EXISTS idx_mod_cases_user ON mod_cases(guild_id,user_id,id);
            CREATE TABLE IF NOT EXISTS appeal_settings (
              guild_id INTEGER PRIMARY KEY, form_url TEXT, channel_id INTEGER,
              webhook_id INTEGER, secret TEXT
            );
            CREATE TABLE IF NOT EXISTS appeals (
              id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL, case_id INTEGER, explanation TEXT NOT NULL,
              created_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
              moderator_id INTEGER, decision_reason TEXT,
              UNIQUE(guild_id,user_id,case_id)
            );
            ''')
            con.commit()

    def db(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def config(self, guild_id):
        with closing(self.db()) as con:
            return con.execute('SELECT * FROM appeal_settings WHERE guild_id=?', (guild_id,)).fetchone()

    def log_channel(self, guild):
        with closing(self.db()) as con:
            row = con.execute('SELECT mod_log_channel_id FROM guild_config WHERE guild_id=?', (guild.id,)).fetchone()
        return guild.get_channel(row['mod_log_channel_id']) if row and row['mod_log_channel_id'] else None

    async def log_action(self, guild, title, text):
        ch = self.log_channel(guild)
        if ch and hasattr(ch, 'send'):
            try:
                await ch.send(embed=discord.Embed(title='✦ SIJK SYSTEM | ' + title, description=text[:3900], color=discord.Color.orange()), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.exception('Ошибка журнала модерации')

    def add_case(self, guild_id, user_id, moderator_id, action, reason, related=None, duration=None, dm='не отправлялось'):
        with closing(self.db()) as con:
            cur = con.execute('INSERT INTO mod_cases(guild_id,user_id,moderator_id,action,reason,created_at,related_case,duration_minutes,dm_status) VALUES (?,?,?,?,?,?,?,?,?)', (guild_id,user_id,moderator_id,action,reason,int(time.time()),related,duration,dm))
            con.commit()
            return cur.lastrowid

    async def dm(self, user, title, reason, guild, extra=''):
        embed = discord.Embed(title='✦ SIJK SYSTEM | ' + title, color=discord.Color.red() if title=='БЛОКИРОВКА' else discord.Color.orange())
        embed.description = f'Сервер: **{guild.name}**\n**Причина:** {reason[:900]}\n{extra}'
        try:
            await user.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return 'доставлено'
        except (discord.Forbidden, discord.HTTPException):
            return 'не доставлено'

    @staticmethod
    def actor(interaction, permission):
        m = interaction.user
        return isinstance(m, discord.Member) and bool(getattr(m.guild_permissions, permission, False))

    @staticmethod
    def hierarchy(interaction, member, permission):
        g = interaction.guild
        if not g or not Moderation.actor(interaction, permission):
            return 'Недостаточно прав.'
        if member.id in (g.owner_id, interaction.user.id) or member.bot:
            return 'Нельзя наказать этого участника.'
        if interaction.user.id != g.owner_id and interaction.user.top_role <= member.top_role:
            return 'Роль участника не ниже роли модератора.'
        if not g.me or g.me.top_role <= member.top_role or not getattr(g.me.guild_permissions, permission, False):
            return 'Недостаточно прав или высоты роли бота.'
        return None

    async def reject(self, interaction, error):
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return True
        return False

    @app_commands.command(name='ban', description='Забанить с уведомлением и записью в историю')
    @app_commands.guild_only()
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str, delete_messages_hours: app_commands.Range[int,0,168]=0):
        if await self.reject(interaction, self.hierarchy(interaction,member,'ban_members')): return
        if not reason.strip(): return await interaction.response.send_message('Причина обязательна.',ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        cfg=self.config(interaction.guild_id)
        url=cfg['form_url'] if cfg and cfg['form_url'] else DEFAULT_APPEAL_FORM_URL
        extra='**Срок:** бессрочно.\n'
        extra+=f'**Апелляция:** {url}' if url else 'Ссылка на апелляцию пока не настроена.'
        status=await self.dm(member,'БЛОКИРОВКА',reason,interaction.guild,extra)
        try:
            await interaction.guild.ban(member,reason=f'{interaction.user} | {reason}',delete_message_seconds=delete_messages_hours*3600)
        except discord.HTTPException as exc:
            return await interaction.followup.send(f'Бан не выполнен: {exc}',ephemeral=True)
        cid=self.add_case(interaction.guild_id,member.id,interaction.user.id,'ban',reason,dm=status)
        await self.log_action(interaction.guild,'БАН',f'Дело **#{cid}** · {member} (`{member.id}`)\nМодератор: {interaction.user.mention}\nПричина: {reason}\nЛичное сообщение: {status}')
        await interaction.followup.send(f'Забанен. Дело #{cid}. ЛС: {status}.',ephemeral=True)

    @app_commands.command(name='unban', description='Разбанить по Discord ID с обязательной причиной')
    @app_commands.guild_only()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str):
        if not self.actor(interaction,'ban_members') or not interaction.guild.me.guild_permissions.ban_members:
            return await interaction.response.send_message('Нет прав на разбан.',ephemeral=True)
        if not user_id.isdecimal() or not reason.strip():
            return await interaction.response.send_message('Укажи числовой Discord ID и причину.',ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        user=discord.Object(id=int(user_id))
        try:
            await interaction.guild.unban(user,reason=f'{interaction.user} | {reason}')
        except discord.NotFound:
            return await interaction.followup.send('Этот пользователь не забанен.',ephemeral=True)
        except discord.HTTPException as exc:
            return await interaction.followup.send(f'Ошибка разбана: {exc}',ephemeral=True)
        cid=self.add_case(interaction.guild_id,int(user_id),interaction.user.id,'unban',reason)
        try:
            resolved=await self.bot.fetch_user(int(user_id))
            status=await self.dm(resolved,'РАЗБЛОКИРОВКА',reason,interaction.guild,'Блокировка снята. Вы можете снова присоединиться по действующему приглашению.')
        except discord.HTTPException:
            status='не доставлено'
        await self.log_action(interaction.guild,'РАЗБАН',f'Дело **#{cid}** · `{user_id}`\nМодератор: {interaction.user.mention}\nПричина: {reason}\nЛС: {status}')
        await interaction.followup.send(f'Разбан выполнен. Дело #{cid}.',ephemeral=True)

    @app_commands.command(name='timeout', description='Тайм-аут с личным уведомлением')
    @app_commands.guild_only()
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int,1,40320], reason: str):
        if await self.reject(interaction,self.hierarchy(interaction,member,'moderate_members')): return
        if not reason.strip(): return await interaction.response.send_message('Причина обязательна.',ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try: await member.timeout(timedelta(minutes=minutes),reason=f'{interaction.user} | {reason}')
        except discord.HTTPException as exc: return await interaction.followup.send(f'Ошибка: {exc}',ephemeral=True)
        status=await self.dm(member,'ТАЙМ-АУТ',reason,interaction.guild,f'**Срок:** {minutes} мин.')
        cid=self.add_case(interaction.guild_id,member.id,interaction.user.id,'timeout',reason,duration=minutes,dm=status)
        await self.log_action(interaction.guild,'ТАЙМ-АУТ',f'Дело **#{cid}** · {member.mention}\n{minutes} мин. · {reason}\nМодератор: {interaction.user.mention} · ЛС: {status}')
        await interaction.followup.send(f'Тайм-аут выдан. Дело #{cid}.',ephemeral=True)

    @app_commands.command(name='untimeout', description='Снять тайм-аут с обязательной причиной')
    @app_commands.guild_only()
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if await self.reject(interaction,self.hierarchy(interaction,member,'moderate_members')): return
        if not reason.strip(): return await interaction.response.send_message('Причина обязательна.',ephemeral=True)
        try: await member.timeout(None,reason=f'{interaction.user} | {reason}')
        except discord.HTTPException as exc: return await interaction.response.send_message(f'Ошибка: {exc}',ephemeral=True)
        cid=self.add_case(interaction.guild_id,member.id,interaction.user.id,'untimeout',reason)
        await self.dm(member,'ТАЙМ-АУТ СНЯТ',reason,interaction.guild)
        await self.log_action(interaction.guild,'ОТМЕНА ТАЙМ-АУТА',f'Дело **#{cid}** · {member.mention}\n{reason}\nМодератор: {interaction.user.mention}')
        await interaction.response.send_message(f'Тайм-аут снят. Дело #{cid}.',ephemeral=True)

    @app_commands.command(name='warn', description='Ручное предупреждение с записью в историю')
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if await self.reject(interaction,self.hierarchy(interaction,member,'moderate_members')): return
        if not reason.strip(): return await interaction.response.send_message('Причина обязательна.',ephemeral=True)
        with closing(self.db()) as con:
            cur=con.execute('INSERT INTO warnings(guild_id,user_id,moderator_id,reason) VALUES (?,?,?,?)',(interaction.guild_id,member.id,interaction.user.id,reason))
            warning_id=cur.lastrowid
            count=con.execute('SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=? AND active=1',(interaction.guild_id,member.id)).fetchone()[0]
            con.commit()
        escalation={2:10,3:60,4:1440}.get(count)
        extra=f'**Активных предупреждений:** {count}.'
        if escalation:
            try:
                await member.timeout(timedelta(minutes=escalation),reason=f'Автоматическая эскалация предупреждений #{warning_id}: {reason}')
                extra+=f'\n**Тайм-аут:** {escalation} мин.'
                self.add_case(interaction.guild_id,member.id,interaction.user.id,'timeout_from_warn',f'Предупреждение #{warning_id}: {reason}',duration=escalation)
            except discord.HTTPException:
                extra+='\nНе удалось выдать тайм-аут.'
        elif count>=5:
            extra+='\nДальнейшие меры — на усмотрение модераторов.'
        status=await self.dm(member,'ПРЕДУПРЕЖДЕНИЕ',reason,interaction.guild,extra)
        cid=self.add_case(interaction.guild_id,member.id,interaction.user.id,'warn',reason,related=warning_id,dm=status)
        await self.log_action(interaction.guild,'ПРЕДУПРЕЖДЕНИЕ',f'Дело **#{cid}** · предупреждение **#{warning_id}** · {member.mention}\n{reason}\nМодератор: {interaction.user.mention}')
        await interaction.response.send_message(f'Предупреждение #{warning_id}, дело #{cid}.',ephemeral=True)

    @app_commands.command(name='unwarn', description='Снять предупреждение по номеру с причиной')
    @app_commands.guild_only()
    async def unwarn(self, interaction: discord.Interaction, warning_id: int, reason: str):
        if not self.actor(interaction,'moderate_members'): return await interaction.response.send_message('Нет прав.',ephemeral=True)
        if not reason.strip(): return await interaction.response.send_message('Причина обязательна.',ephemeral=True)
        with closing(self.db()) as con:
            row=con.execute('SELECT user_id FROM warnings WHERE guild_id=? AND id=? AND active=1',(interaction.guild_id,warning_id)).fetchone()
            if row:
                con.execute('UPDATE warnings SET active=0 WHERE guild_id=? AND id=?',(interaction.guild_id,warning_id))
                con.commit()
        if not row: return await interaction.response.send_message('Активное предупреждение не найдено.',ephemeral=True)
        cid=self.add_case(interaction.guild_id,row['user_id'],interaction.user.id,'unwarn',reason,related=warning_id)
        await self.log_action(interaction.guild,'СНЯТО ПРЕДУПРЕЖДЕНИЕ',f'Дело **#{cid}** · предупреждение **#{warning_id}**\nУчастник: <@{row["user_id"]}>\nМодератор: {interaction.user.mention}\nПричина: {reason}')
        await interaction.response.send_message(f'Предупреждение #{warning_id} снято. Дело #{cid}.',ephemeral=True)

    @app_commands.command(name='kick', description='Выгнать участника с записью в историю')
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if await self.reject(interaction,self.hierarchy(interaction,member,'kick_members')): return
        if not reason.strip(): return await interaction.response.send_message('Причина обязательна.',ephemeral=True)
        status=await self.dm(member,'ИСКЛЮЧЕНИЕ С СЕРВЕРА',reason,interaction.guild)
        try: await member.kick(reason=f'{interaction.user} | {reason}')
        except discord.HTTPException as exc: return await interaction.response.send_message(f'Ошибка: {exc}',ephemeral=True)
        cid=self.add_case(interaction.guild_id,member.id,interaction.user.id,'kick',reason,dm=status)
        await self.log_action(interaction.guild,'ИСКЛЮЧЕНИЕ',f'Дело **#{cid}** · {member.mention}\nМодератор: {interaction.user.mention}\nПричина: {reason}')
        await interaction.response.send_message(f'Участник исключён. Дело #{cid}.',ephemeral=True)

    @app_commands.command(name='modhistory', description='История ручных и автоматических наказаний')
    @app_commands.guild_only()
    async def modhistory(self, interaction: discord.Interaction, user_id: str):
        if not self.actor(interaction,'moderate_members'): return await interaction.response.send_message('Нет прав.',ephemeral=True)
        if not user_id.isdecimal(): return await interaction.response.send_message('Нужен числовой Discord ID.',ephemeral=True)
        with closing(self.db()) as con:
            manual=con.execute('SELECT id,action,reason,created_at,moderator_id FROM mod_cases WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 12',(interaction.guild_id,int(user_id))).fetchall()
            auto=con.execute("SELECT rule,action,created_at FROM automod_events WHERE guild_id=? AND user_id=? AND action NOT LIKE 'Тест:%' ORDER BY id DESC LIMIT 12",(interaction.guild_id,int(user_id))).fetchall()
        items=[(r['created_at'],f'`#{r["id"]}` **{r["action"]}** · {r["reason"][:100]} · <@{r["moderator_id"]}>') for r in manual]
        items += [(r['created_at'],f'**Автомод:** {r["rule"][:65]} · {r["action"][:75]}') for r in auto]
        items.sort(key=lambda x:x[0],reverse=True)
        description='\n'.join(f'<t:{t}:d> {line}' for t,line in items[:20]) or 'Наказаний не найдено.'
        await interaction.response.send_message(embed=discord.Embed(title=f'✦ SIJK SYSTEM | ИСТОРИЯ {user_id}',description=description[:3900],color=discord.Color.blurple()),ephemeral=True)

    @app_commands.command(name='case', description='Детали наказания по номеру дела')
    @app_commands.guild_only()
    async def case(self, interaction: discord.Interaction, case_id: int):
        if not self.actor(interaction,'moderate_members'): return await interaction.response.send_message('Нет прав.',ephemeral=True)
        with closing(self.db()) as con:
            row=con.execute('SELECT * FROM mod_cases WHERE guild_id=? AND id=?',(interaction.guild_id,case_id)).fetchone()
        if not row: return await interaction.response.send_message('Дело не найдено.',ephemeral=True)
        embed=discord.Embed(title=f'✦ SIJK SYSTEM | ДЕЛО #{case_id}',description=f'**Участник:** <@{row["user_id"]}> (`{row["user_id"]}`)\n**Действие:** {row["action"]}\n**Причина:** {row["reason"]}\n**Модератор:** <@{row["moderator_id"]}>\n**Дата:** <t:{row["created_at"]}:f>\n**ЛС:** {row["dm_status"]}',color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed,ephemeral=True)

    @app_commands.command(name='appeals_setup', description='Настроить форму и защищённую доставку апелляций')
    @app_commands.guild_only()
    async def appeals_setup(self, interaction: discord.Interaction, form_url: str, channel: discord.TextChannel, webhook_id: str, secret: str):
        if not isinstance(interaction.user,discord.Member) or not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message('Только администратор.',ephemeral=True)
        if not form_url.startswith(('https://docs.google.com/forms/','https://forms.gle/')) or not webhook_id.isdecimal() or len(secret)<24:
            return await interaction.response.send_message('Неверная ссылка/ID вебхука или секрет короче 24 символов.',ephemeral=True)
        with closing(self.db()) as con:
            con.execute('INSERT INTO appeal_settings(guild_id,form_url,channel_id,webhook_id,secret) VALUES (?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET form_url=excluded.form_url,channel_id=excluded.channel_id,webhook_id=excluded.webhook_id,secret=excluded.secret',(interaction.guild_id,form_url,channel.id,int(webhook_id),secret))
            con.commit()
        await interaction.response.send_message('Апелляции настроены. Секрет и адрес вебхука не публикуй.',ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or not message.webhook_id: return
        cfg=self.config(message.guild.id)
        if not cfg or not cfg['secret'] or message.webhook_id!=cfg['webhook_id'] or message.channel.id!=cfg['channel_id']: return
        if not message.content.startswith('SIJK_APPEAL|'): return
        try:
            _, signature, payload=message.content.split('|',2)
            expected=hmac.new(cfg['secret'].encode(),payload.encode(),hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature,expected): return
            data=json.loads(payload)
            uid=int(data['user_id'])
            explanation=str(data['explanation'])[:1800]
            if len(explanation)<5: return
            with closing(self.db()) as con:
                banned=await message.guild.fetch_ban(discord.Object(id=uid))
                last=con.execute("SELECT id,created_at FROM mod_cases WHERE guild_id=? AND user_id=? AND action IN ('ban','external_ban') ORDER BY id DESC LIMIT 1",(message.guild.id,uid)).fetchone()
                case_id=last['id'] if last else None
                if not banned: return
                if last and int(time.time())-last['created_at']>365*86400: return
                prior=con.execute('SELECT id FROM appeals WHERE guild_id=? AND user_id=? AND case_id IS ?',(message.guild.id,uid,case_id)).fetchone()
                if prior: return
                cur=con.execute('INSERT INTO appeals(guild_id,user_id,case_id,explanation,created_at) VALUES (?,?,?,?,?)',(message.guild.id,uid,case_id,explanation,int(time.time())))
                aid=cur.lastrowid
                con.commit()
            embed=discord.Embed(title=f'✦ SIJK SYSTEM | АПЕЛЛЯЦИЯ #{aid}',description=f'**Пользователь:** `{uid}`\n**Дело:** {"#"+str(case_id) if case_id else "бан через Discord"}\n**Объяснение:** {discord.utils.escape_mentions(explanation)}\n**Статус:** ожидает решения',color=discord.Color.gold())
            await message.channel.send(embed=embed,view=AppealView(self,aid),allowed_mentions=discord.AllowedMentions.none())
            try: await message.delete()
            except discord.HTTPException: pass
        except (ValueError,KeyError,TypeError,json.JSONDecodeError,discord.HTTPException,sqlite3.Error):
            log.exception('Не удалось принять апелляцию')

    async def decide(self, interaction, appeal_id, approve, reason):
        if not self.actor(interaction,'ban_members'):
            return await interaction.response.send_message('Нет права на управление банами.',ephemeral=True)
        if not reason.strip(): return await interaction.response.send_message('Укажи причину решения.',ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        with closing(self.db()) as con:
            row=con.execute('SELECT * FROM appeals WHERE id=? AND guild_id=?',(appeal_id,interaction.guild_id)).fetchone()
        if not row or row['status']!='pending':
            return await interaction.followup.send('Апелляция уже рассмотрена или не найдена.',ephemeral=True)
        if approve:
            if not interaction.guild.me.guild_permissions.ban_members:
                return await interaction.followup.send('У бота нет права на разбан.',ephemeral=True)
            try:
                await interaction.guild.unban(discord.Object(id=row['user_id']),reason=f'Апелляция #{appeal_id} одобрена: {reason}')
            except discord.HTTPException as exc:
                return await interaction.followup.send(f'Не удалось разбанить: {exc}',ephemeral=True)
        with closing(self.db()) as con:
            cur=con.execute("UPDATE appeals SET status=?,moderator_id=?,decision_reason=? WHERE id=? AND guild_id=? AND status='pending'",('approved' if approve else 'rejected',interaction.user.id,reason,appeal_id,interaction.guild_id))
            con.commit()
        if not cur.rowcount: return await interaction.followup.send('Уже рассмотрено другим модератором.',ephemeral=True)
        action='unban' if approve else 'appeal_rejected'
        cid=self.add_case(interaction.guild_id,row['user_id'],interaction.user.id,action,f'Апелляция #{appeal_id}: {reason}',related=row['case_id'])
        try:
            user=await self.bot.fetch_user(row['user_id'])
            await self.dm(user,'АПЕЛЛЯЦИЯ ОДОБРЕНА' if approve else 'АПЕЛЛЯЦИЯ ОТКЛОНЕНА',reason,interaction.guild)
        except discord.HTTPException: pass
        await self.log_action(interaction.guild,'РЕШЕНИЕ ПО АПЕЛЛЯЦИИ',f'Апелляция **#{appeal_id}** · дело **#{cid}**\nРешение: {"одобрена, разбан" if approve else "отклонена"}\nМодератор: {interaction.user.mention}\nПричина: {reason}')
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException: pass
        await interaction.followup.send(f'Апелляция #{appeal_id} рассмотрена.',ephemeral=True)

    async def external_ban_event(self, guild, user, action):
        # Only actions made outside SIJK SYSTEM are imported.
        await __import__('asyncio').sleep(2)
        actor_id=0
        reason='Действие через интерфейс Discord (причина не указана)'
        try:
            async for entry in guild.audit_logs(limit=8,action=discord.AuditLogAction.ban if action=='ban' else discord.AuditLogAction.unban):
                if entry.target.id==user.id and (discord.utils.utcnow()-entry.created_at).total_seconds()<90:
                    if entry.user and entry.user.id==self.bot.user.id: return
                    actor_id=entry.user.id
                    reason=entry.reason or reason
                    break
        except (discord.Forbidden,discord.HTTPException):
            log.info('Не удалось прочитать аудит-лог на сервере %s',guild.id)
        with closing(self.db()) as con:
            recent=con.execute('SELECT id FROM mod_cases WHERE guild_id=? AND user_id=? AND action=? AND created_at>=? ORDER BY id DESC LIMIT 1', (guild.id,user.id,action,int(time.time())-90)).fetchone()
        if recent: return
        cid=self.add_case(guild.id,user.id,actor_id,'external_'+action,reason)
        await self.log_action(guild,'БАН ЧЕРЕЗ DISCORD' if action=='ban' else 'РАЗБАН ЧЕРЕЗ DISCORD',f'Дело **#{cid}** · `{user.id}`\nМодератор: <@{actor_id}>\nПричина: {reason}')
        if action=='ban':
            cfg=self.config(guild.id)
            extra=f'**Апелляция:** {cfg["form_url"] if cfg and cfg["form_url"] else DEFAULT_APPEAL_FORM_URL}'
            await self.dm(user,'БЛОКИРОВКА',reason,guild,extra)

    @commands.Cog.listener()
    async def on_member_ban(self,guild,user):
        await self.external_ban_event(guild,user,'ban')

    @commands.Cog.listener()
    async def on_member_unban(self,guild,user):
        await self.external_ban_event(guild,user,'unban')

class DecisionModal(discord.ui.Modal):
    def __init__(self, cog, appeal_id, approve):
        super().__init__(title='Решение по апелляции')
        self.cog,self.appeal_id,self.approve=cog,appeal_id,approve
        self.reason=discord.ui.TextInput(label='Причина решения',style=discord.TextStyle.paragraph,min_length=3,max_length=500)
        self.add_item(self.reason)
    async def on_submit(self, interaction):
        await self.cog.decide(interaction,self.appeal_id,self.approve,str(self.reason.value))

class AppealView(discord.ui.View):
    def __init__(self,cog,appeal_id):
        super().__init__(timeout=None)
        self.cog,self.appeal_id=cog,appeal_id
        for label,approve,style in [('Одобрить',True,discord.ButtonStyle.success),('Отклонить',False,discord.ButtonStyle.danger)]:
            btn=discord.ui.Button(label=label,style=style,custom_id=f'sijk:appeal:{appeal_id}:{int(approve)}')
            async def callback(interaction, decision=approve):
                if not self.cog.actor(interaction,'ban_members'):
                    return await interaction.response.send_message('Нет прав.',ephemeral=True)
                await interaction.response.send_modal(DecisionModal(self.cog,self.appeal_id,decision))
            btn.callback=callback
            self.add_item(btn)

async def setup_moderation(bot: commands.Bot, db_path: Path):
    # Existing commands in bot.py are replaced by enhanced implementations.
    for name in ('ban','warn','unwarn','timeout','untimeout','kick'):
        bot.tree.remove_command(name)
    cog=Moderation(bot,db_path)
    await bot.add_cog(cog)
    # Restore review buttons after restart.
    with closing(cog.db()) as con:
        rows=con.execute("SELECT id FROM appeals WHERE status='pending' ORDER BY id DESC LIMIT 100").fetchall()
    for row in rows:
        bot.add_view(AppealView(cog,row['id']))
