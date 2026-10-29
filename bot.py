import os
import json
import sqlite3
import logging
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('zordx')
TOKEN = os.getenv('DISCORD_TOKEN', '').strip()
GUILD_ID = int(os.getenv('GUILD_ID', '0') or 0)
DB_PATH = os.getenv('DB_PATH', 'zordx.sqlite3')
SHOP_NAME = os.getenv('SHOP_NAME', 'ZORDX STORE')
DEFAULT_COLOR = os.getenv('SHOP_COLOR', '8B35FF').lstrip('#')
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS shop_products (\n            name TEXT PRIMARY KEY COLLATE NOCASE, description TEXT DEFAULT '',\n            price REAL NOT NULL DEFAULT 0, stock INTEGER NOT NULL DEFAULT 0,\n            link TEXT DEFAULT '')")
        c.execute('CREATE TABLE IF NOT EXISTS coupons (\n            code TEXT PRIMARY KEY COLLATE NOCASE, percent INTEGER NOT NULL,\n            uses_left INTEGER NOT NULL DEFAULT 0, created_by INTEGER,\n            created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        c.execute('CREATE TABLE IF NOT EXISTS shop_config (\n            key TEXT PRIMARY KEY, value TEXT)')
        c.execute("CREATE TABLE IF NOT EXISTS zordx_panels (\n            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,\n            title TEXT NOT NULL, description TEXT DEFAULT '', category TEXT DEFAULT 'Geral',\n            image_url TEXT DEFAULT '', color TEXT DEFAULT '8B35FF', buttons_json TEXT DEFAULT '[]',\n            channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,\n            created_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS zordx_withdrawals (\n            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,\n            user_id INTEGER NOT NULL, amount REAL NOT NULL,\n            status TEXT NOT NULL DEFAULT 'Pendente', created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        c.execute('CREATE TABLE IF NOT EXISTS zordx_balances (\n            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, balance REAL NOT NULL DEFAULT 0,\n            PRIMARY KEY(guild_id, user_id))')
        c.execute("CREATE TABLE IF NOT EXISTS zordx_tickets (\n            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,\n            channel_id INTEGER NOT NULL UNIQUE, user_id INTEGER NOT NULL,\n            status TEXT NOT NULL DEFAULT 'Aberto', created_at TEXT DEFAULT CURRENT_TIMESTAMP,\n            closed_at TEXT DEFAULT '', assigned_staff_id INTEGER DEFAULT 0,\n            voice_channel_id INTEGER DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS zordx_ticket_ratings (\n            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,\n            ticket_id INTEGER NOT NULL, user_id INTEGER NOT NULL, rating INTEGER NOT NULL,\n            feedback TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        columns = {row[1] for row in c.execute('PRAGMA table_info(zordx_tickets)').fetchall()}
        if 'assigned_staff_id' not in columns:
            c.execute('ALTER TABLE zordx_tickets ADD COLUMN assigned_staff_id INTEGER DEFAULT 0')
        if 'voice_channel_id' not in columns:
            c.execute('ALTER TABLE zordx_tickets ADD COLUMN voice_channel_id INTEGER DEFAULT 0')

def color_from(value):
    try:
        return discord.Color(int((value or DEFAULT_COLOR).replace('#', ''), 16))
    except (ValueError, AttributeError):
        return discord.Color.purple()

def money(value):
    return f'R$ {float(value):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')

def is_staff(interaction: discord.Interaction):
    return isinstance(interaction.user, discord.Member) and (interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.manage_messages)

def is_ticket_staff(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        return False
    if is_staff(interaction):
        return True
    guild_id = interaction.guild_id
    if not guild_id:
        return False
    raw = cfg(guild_id, 'ticket_support_role_ids', '')
    try:
        role_ids = [int(x) for x in json.loads(raw)] if raw else []
    except (ValueError, TypeError, json.JSONDecodeError):
        role_ids = []
    legacy = int(cfg(guild_id, 'ticket_support_role_id', '0') or 0)
    role_ids.append(legacy)
    return any(role.id in role_ids for role in interaction.user.roles)


def ticket_role_ids(guild_id):
    raw = cfg(guild_id, 'ticket_support_role_ids', '')
    try:
        ids = [int(x) for x in json.loads(raw)] if raw else []
    except (ValueError, TypeError, json.JSONDecodeError):
        ids = []
    legacy = int(cfg(guild_id, 'ticket_support_role_id', '0') or 0)
    if legacy and legacy not in ids:
        ids.append(legacy)
    return ids


def is_premium(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.administrator or any((role.name.lower().strip() == 'premium' for role in interaction.user.roles))

def staff_only():

    async def predicate(interaction: discord.Interaction):
        return is_staff(interaction)
    return app_commands.check(predicate)

def premium_only():

    async def predicate(interaction: discord.Interaction):
        return is_premium(interaction)
    return app_commands.check(predicate)

def cfg(guild_id, key, default=''):
    with db() as c:
        row = c.execute('SELECT value FROM shop_config WHERE key=?', (f'{guild_id}:{key}',)).fetchone()
    return row[0] if row else default

def set_cfg(guild_id, key, value):
    with db() as c:
        c.execute('INSERT OR REPLACE INTO shop_config(key,value) VALUES(?,?)', (f'{guild_id}:{key}', str(value)))

def get_products():
    with db() as c:
        return c.execute('SELECT name, description, price, stock, link FROM shop_products ORDER BY name').fetchall()

def panel_embed(title, description, category, image_url, color):
    e = discord.Embed(title=title[:256], description=description[:4000], color=color_from(color))
    e.add_field(name='Categoria', value=(category or 'Geral')[:100], inline=True)
    e.set_footer(text=f'{SHOP_NAME} • Painel da loja')
    if image_url:
        e.set_image(url=image_url)
    return e

def status_embed(title, description, *, success=None):
    """Resposta curta no visual embed da ZORDX STORE."""
    color = color_from(DEFAULT_COLOR)
    if success is True:
        color = discord.Color.green()
    elif success is False:
        color = discord.Color.red()
    embed = discord.Embed(title=title[:256], description=description[:4000], color=color)
    embed.set_footer(text=f'{SHOP_NAME} • Sistema da loja')
    return embed

class PanelActionView(discord.ui.View):

    def __init__(self, buttons=None):
        super().__init__(timeout=None)
        names = buttons or ['Ver produtos', 'Cupons', 'Suporte']
        for index, label in enumerate(names[:5]):
            label = label.strip()[:80]
            if not label:
                continue
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id=f'zordx:panel:{index}:{label.lower()[:20]}')

            async def callback(interaction: discord.Interaction, chosen=label):
                key = chosen.casefold()
                if 'produto' in key:
                    rows = get_products()
                    if not rows:
                        text_out = 'Ainda não há produtos cadastrados na loja.'
                    else:
                        text_out = '\n'.join((f"**{n}** — {money(p)} • Estoque: {s}\n{d or 'Sem descrição'}" for n, d, p, s, _ in rows[:15]))
                    await interaction.response.send_message(embed=status_embed('🛍️ Produtos da loja', text_out[:3800]), ephemeral=True)
                elif 'cupom' in key:
                    with db() as c:
                        rows = c.execute('SELECT code, percent, uses_left FROM coupons ORDER BY code').fetchall()
                    text_out = '\n'.join((f'`{code}` — {pct}% de desconto • usos restantes: {uses}' for code, pct, uses in rows[:15])) or 'Não há cupons cadastrados.'
                    await interaction.response.send_message(embed=status_embed('🎟️ Cupons da loja', text_out[:3800]), ephemeral=True)
                else:
                    support_id = int(cfg(interaction.guild_id or 0, 'support_channel_id', '0') or 0)
                    channel = interaction.guild.get_channel(support_id) if interaction.guild and support_id else None
                    message = f'Para falar com a equipe, acesse {channel.mention}.' if channel else 'Entre em contato com a equipe da loja para receber suporte.'
                    await interaction.response.send_message(embed=status_embed('🎧 Suporte da loja', message), ephemeral=True)
            button.callback = callback
            self.add_item(button)

class PublishPanelView(discord.ui.View):

    def __init__(self, guild_id, channel_id, title, description, category, image_url, color, buttons, creator_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.title_text = title
        self.description_text = description
        self.category = category
        self.image_url = image_url
        self.color = color
        self.buttons = buttons
        self.creator_id = creator_id

    @discord.ui.button(label='Publicar painel', style=discord.ButtonStyle.success, emoji='📣')
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.creator_id and (not is_staff(interaction)):
            return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Somente quem iniciou ou a equipe pode publicar este painel.'))
        guild = interaction.guild
        channel = guild.get_channel(self.channel_id) if guild else None
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'O canal escolhido não está disponível. Tente novamente.'))
        try:
            message = await channel.send(embed=panel_embed(self.title_text, self.description_text, self.category, self.image_url, self.color), view=PanelActionView(self.buttons))
            with db() as c:
                c.execute('INSERT INTO zordx_panels(guild_id,title,description,category,image_url,color,buttons_json,channel_id,message_id,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)', (self.guild_id, self.title_text, self.description_text, self.category, self.image_url, self.color, json.dumps(self.buttons, ensure_ascii=False), channel.id, message.id, self.creator_id))
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(content=f'✅ Painel **{self.title_text}** publicado em {channel.mention} e registrado para edição futura.', embed=None, view=self)
        except discord.Forbidden:
            await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Não tenho permissão para enviar mensagens ou embeds nesse canal.'))

    @discord.ui.button(label='Cancelar', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.creator_id and (not is_staff(interaction)):
            return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Somente quem iniciou pode cancelar.'))
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content='Criação do painel cancelada.', embed=None, view=self)

class PanelSelect(discord.ui.Select):

    def __init__(self, rows):
        options = [discord.SelectOption(label=(row[1] or 'Painel')[:100], description=f'Canal ID: {row[2]} • Painel #{row[0]}'[:100], value=str(row[0])) for row in rows[:25]]
        super().__init__(placeholder='Escolha o painel que deseja editar', min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        panel_id = int(self.values[0])
        with db() as c:
            row = c.execute('SELECT id,title,description,category,image_url,color,buttons_json,channel_id,message_id,guild_id FROM zordx_panels WHERE id=? AND guild_id=?', (panel_id, interaction.guild_id)).fetchone()
        if not row:
            return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Não encontrei esse painel neste servidor.'))
        await interaction.response.send_modal(EditPanelModal(row))

class PanelSelectView(discord.ui.View):

    def __init__(self, rows):
        super().__init__(timeout=180)
        self.add_item(PanelSelect(rows))

class EditPanelModal(discord.ui.Modal):

    def __init__(self, row):
        super().__init__(title='Editar painel da loja')
        self.panel_id, self.old_title, self.old_description, self.old_category, self.old_image, self.old_color, buttons_json, self.channel_id, self.message_id, self.guild_id = row
        self.title_input = discord.ui.TextInput(label='Título', default=self.old_title[:256], max_length=256)
        self.description_input = discord.ui.TextInput(label='Descrição', default=(self.old_description or '')[:1000], style=discord.TextStyle.paragraph, required=False, max_length=1000)
        self.category_input = discord.ui.TextInput(label='Categoria', default=(self.old_category or 'Geral')[:100], max_length=100)
        self.image_input = discord.ui.TextInput(label='URL da imagem (opcional)', default=(self.old_image or '')[:400], required=False, max_length=400)
        self.color_input = discord.ui.TextInput(label='Cor hexadecimal (ex.: 8B35FF)', default=(self.old_color or DEFAULT_COLOR)[:7], max_length=7)
        self.buttons_json = buttons_json or '[]'
        self.add_item(self.title_input)
        self.add_item(self.description_input)
        self.add_item(self.category_input)
        self.add_item(self.image_input)
        self.add_item(self.color_input)

    async def on_submit(self, interaction: discord.Interaction):
        title = self.title_input.value.strip()
        description = self.description_input.value.strip()
        category = self.category_input.value.strip() or 'Geral'
        image_url = self.image_input.value.strip()
        color = self.color_input.value.strip().lstrip('#') or DEFAULT_COLOR
        if image_url and (not image_url.startswith(('https://', 'http://'))):
            return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'A imagem precisa ser um link HTTP ou HTTPS válido.'))
        try:
            color_from(color)
            buttons = json.loads(self.buttons_json)
            channel = interaction.guild.get_channel(self.channel_id) if interaction.guild else None
            if not isinstance(channel, discord.TextChannel):
                return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'O canal original não foi encontrado. Não alterei o painel.'))
            message = await channel.fetch_message(self.message_id)
            await message.edit(embed=panel_embed(title, description, category, image_url, color), view=PanelActionView(buttons))
            with db() as c:
                c.execute('UPDATE zordx_panels SET title=?,description=?,category=?,image_url=?,color=? WHERE id=? AND guild_id=?', (title, description, category, image_url, color, self.panel_id, self.guild_id))
            await interaction.response.send_message(ephemeral=True, view=EditButtonsView(self.panel_id, self.guild_id, self.channel_id, self.message_id, buttons), embed=status_embed('ZORDX STORE', f'✅ O painel **{title}** foi atualizado na mensagem original em {channel.mention}. Se quiser, você também pode alterar os botões.'))
        except discord.NotFound:
            await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'A mensagem original não existe mais. O registro foi mantido para você conferir.'))
        except (discord.Forbidden, discord.HTTPException, ValueError, json.JSONDecodeError) as exc:
            log.exception('Falha ao editar painel: %s', exc)
            await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Não consegui editar o painel. Confira as permissões, a cor e o link da imagem.'))

class EditButtonsModal(discord.ui.Modal):

    def __init__(self, panel_id, guild_id, channel_id, message_id, current_buttons):
        super().__init__(title='Editar botões do painel')
        self.panel_id = panel_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message_id = message_id
        self.buttons_input = discord.ui.TextInput(label='Botões separados por vírgula', default=', '.join(current_buttons)[:400] or 'Ver produtos, Cupons, Suporte', max_length=400, required=True, placeholder='Ver produtos, Cupons, Suporte')
        self.add_item(self.buttons_input)

    async def on_submit(self, interaction: discord.Interaction):
        buttons = [item.strip()[:80] for item in self.buttons_input.value.split(',') if item.strip()][:5]
        if not buttons:
            return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Adicione pelo menos um botão.'))
        with db() as c:
            row = c.execute('SELECT title,description,category,image_url,color FROM zordx_panels WHERE id=? AND guild_id=?', (self.panel_id, self.guild_id)).fetchone()
        if not row:
            return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Esse painel não foi encontrado.'))
        channel = interaction.guild.get_channel(self.channel_id) if interaction.guild else None
        try:
            if not isinstance(channel, discord.TextChannel):
                return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'O canal original não foi encontrado.'))
            message = await channel.fetch_message(self.message_id)
            await message.edit(embed=panel_embed(row[0], row[1], row[2], row[3], row[4]), view=PanelActionView(buttons))
            with db() as c:
                c.execute('UPDATE zordx_panels SET buttons_json=? WHERE id=? AND guild_id=?', (json.dumps(buttons, ensure_ascii=False), self.panel_id, self.guild_id))
            await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', '✅ Botões atualizados na mensagem original.'))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.exception('Falha ao atualizar botões: %s', exc)
            await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Não consegui atualizar os botões. Confira as permissões do bot.'))

class EditButtonsView(discord.ui.View):

    def __init__(self, panel_id, guild_id, channel_id, message_id, buttons):
        super().__init__(timeout=180)
        self.panel_id, self.guild_id, self.channel_id, self.message_id, self.buttons = (panel_id, guild_id, channel_id, message_id, buttons)

    @discord.ui.button(label='Editar botões', style=discord.ButtonStyle.primary, emoji='🔘')
    async def edit_buttons(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EditButtonsModal(self.panel_id, self.guild_id, self.channel_id, self.message_id, self.buttons))

class TicketRenameModal(discord.ui.Modal):
    def __init__(self, channel: discord.TextChannel):
        super().__init__(title='Alterar nome do ticket')
        self.channel_id = channel.id
        self.nome = discord.ui.TextInput(label='Novo nome do canal', default=channel.name[:100], max_length=100)
        self.add_item(self.nome)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not is_ticket_staff(interaction):
            return await interaction.response.send_message(embed=status_embed('🔒 Acesso restrito', 'Somente a equipe autorizada pode renomear tickets.'), ephemeral=True)
        channel = interaction.guild.get_channel(self.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(embed=status_embed('Ticket não encontrado', 'O canal não existe mais.'), ephemeral=True)
        name = ''.join(ch.lower() if ch.isalnum() or ch in '-_' else '-' for ch in self.nome.value).strip('-')[:100]
        if not name:
            return await interaction.response.send_message(embed=status_embed('Nome inválido', 'Digite um nome válido para o canal.'), ephemeral=True)
        await channel.edit(name=name, reason=f'Nome alterado por {interaction.user}')
        await interaction.response.send_message(embed=status_embed('✏️ Ticket renomeado', f'Novo nome: {channel.mention}', success=True), ephemeral=True)


class TicketTransferSelect(discord.ui.UserSelect):
    def __init__(self, channel_id: int):
        super().__init__(placeholder='Escolha quem receberá o atendimento', min_values=1, max_values=1)
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not is_ticket_staff(interaction):
            return await interaction.response.send_message(embed=status_embed('🔒 Acesso restrito', 'Somente a equipe autorizada pode transferir tickets.'), ephemeral=True)
        target = self.values[0]
        target_is_staff = isinstance(target, discord.Member) and (
            target.guild_permissions.administrator or target.guild_permissions.manage_guild or
            target.guild_permissions.manage_messages or
            any(role_id in [r.id for r in target.roles] for role_id in ticket_role_ids(interaction.guild_id))
        )
        if not target_is_staff:
            return await interaction.response.send_message(embed=status_embed('Cargo não autorizado', 'Escolha alguém que tenha um cargo autorizado para atender tickets.'), ephemeral=True)
        channel = interaction.guild.get_channel(self.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(embed=status_embed('Ticket não encontrado', 'O canal não existe mais.'), ephemeral=True)
        with db() as c:
            row = c.execute("SELECT id, user_id FROM zordx_tickets WHERE channel_id=? AND guild_id=? AND status='Aberto'", (channel.id, interaction.guild_id)).fetchone()
            if row:
                c.execute('UPDATE zordx_tickets SET assigned_staff_id=? WHERE channel_id=?', (target.id, channel.id))
        if not row:
            return await interaction.response.send_message(embed=status_embed('Ticket indisponível', 'Este ticket não está aberto.'), ephemeral=True)
        topic = channel.topic or ''
        marker = ' • Atendido por:'
        if marker in topic:
            topic = topic.split(marker, 1)[0]
        await channel.edit(topic=f'{topic}{marker} {target} ({target.id})'[:1024], reason=f'Atendimento transferido por {interaction.user}')
        await interaction.response.send_message(embed=status_embed('🔁 Atendimento transferido', f'{interaction.user.mention} transferiu o ticket para {target.mention}.', success=True))


class TicketTransferView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=180)
        self.add_item(TicketTransferSelect(channel_id))


class TicketRatingView(discord.ui.View):
    def __init__(self, guild_id: int, ticket_id: int, owner_id: int):
        super().__init__(timeout=86400)
        self.guild_id, self.ticket_id, self.owner_id = guild_id, ticket_id, owner_id
        for score in range(1, 6):
            button = discord.ui.Button(label=f'{score} ⭐', style=discord.ButtonStyle.secondary, custom_id=f'zordx:rating:{ticket_id}:{score}')
            async def callback(interaction: discord.Interaction, value=score):
                if interaction.user.id != self.owner_id:
                    return await interaction.response.send_message(embed=status_embed('Avaliação privada', 'Somente quem abriu o ticket pode avaliar.'), ephemeral=True)
                with db() as c:
                    c.execute('INSERT INTO zordx_ticket_ratings(guild_id,ticket_id,user_id,rating) VALUES(?,?,?,?)', (self.guild_id, self.ticket_id, self.owner_id, value))
                log_id = int(cfg(self.guild_id, 'ticket_evaluation_channel_id', '0') or 0)
                if not log_id:
                    log_id = int(cfg(self.guild_id, 'ticket_logs_channel_id', '0') or 0)
                guild = bot.get_guild(self.guild_id)
                channel = guild.get_channel(log_id) if guild and log_id else None
                if isinstance(channel, discord.TextChannel):
                    await channel.send(embed=status_embed('⭐ Avaliação de atendimento', f'**Ticket:** #{self.ticket_id}\n**Cliente:** <@{self.owner_id}>\n**Nota:** {value}/5 estrelas'))
                for child in self.children:
                    child.disabled = True
                await interaction.response.edit_message(embed=status_embed('⭐ Obrigado pela avaliação!', f'Você avaliou este atendimento com **{value}/5 estrelas**.', success=True), view=self)
            button.callback = callback
            self.add_item(button)


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Assumir ticket', style=discord.ButtonStyle.success, emoji='🙋', custom_id='zordx:ticket:claim', row=0)
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message(embed=status_embed('ZORDX STORE', 'Este botão só funciona dentro de um ticket.'), ephemeral=True)
        if not is_ticket_staff(interaction):
            return await interaction.response.send_message(embed=status_embed('🔒 Acesso restrito', 'Somente cargos autorizados podem assumir este ticket.'), ephemeral=True)
        with db() as c:
            row = c.execute("SELECT id FROM zordx_tickets WHERE channel_id=? AND guild_id=? AND status='Aberto'", (interaction.channel.id, interaction.guild_id)).fetchone()
            if row:
                c.execute('UPDATE zordx_tickets SET assigned_staff_id=? WHERE channel_id=?', (interaction.user.id, interaction.channel.id))
        if not row:
            return await interaction.response.send_message(embed=status_embed('ZORDX STORE', 'Este ticket não está aberto ou não foi registrado.'), ephemeral=True)
        topic = interaction.channel.topic or ''
        marker = ' • Atendido por:'
        if marker in topic:
            topic = topic.split(marker, 1)[0]
        await interaction.channel.edit(topic=f'{topic}{marker} {interaction.user} ({interaction.user.id})'[:1024], reason=f'Ticket assumido por {interaction.user}')
        await interaction.response.send_message(embed=status_embed('🙋 Ticket assumido', f'{interaction.user.mention} assumiu este atendimento.', success=True))

    @discord.ui.button(label='Passar atendimento', style=discord.ButtonStyle.primary, emoji='🔁', custom_id='zordx:ticket:transfer', row=0)
    async def transfer_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) or not is_ticket_staff(interaction):
            return await interaction.response.send_message(embed=status_embed('🔒 Acesso restrito', 'Somente a equipe autorizada pode transferir tickets.'), ephemeral=True)
        await interaction.response.send_message(embed=status_embed('🔁 Transferir atendimento', 'Escolha manualmente o próximo atendente no menu abaixo.'), view=TicketTransferView(interaction.channel.id), ephemeral=True)

    @discord.ui.button(label='Alterar nome', style=discord.ButtonStyle.secondary, emoji='✏️', custom_id='zordx:ticket:rename', row=1)
    async def rename_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) or not is_ticket_staff(interaction):
            return await interaction.response.send_message(embed=status_embed('🔒 Acesso restrito', 'Somente a equipe autorizada pode renomear tickets.'), ephemeral=True)
        await interaction.response.send_modal(TicketRenameModal(interaction.channel))

    @discord.ui.button(label='Criar call', style=discord.ButtonStyle.primary, emoji='🔊', custom_id='zordx:ticket:call', row=1)
    async def create_call(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) or not is_ticket_staff(interaction):
            return await interaction.response.send_message(embed=status_embed('🔒 Acesso restrito', 'Somente a equipe autorizada pode criar calls.'), ephemeral=True)
        with db() as c:
            row = c.execute("SELECT user_id, voice_channel_id FROM zordx_tickets WHERE channel_id=? AND guild_id=? AND status='Aberto'", (interaction.channel.id, interaction.guild_id)).fetchone()
        if not row:
            return await interaction.response.send_message(embed=status_embed('Ticket indisponível', 'Este ticket não está aberto.'), ephemeral=True)
        if row[1]:
            existing = interaction.guild.get_channel(int(row[1]))
            if existing:
                return await interaction.response.send_message(embed=status_embed('🔊 Call já existe', f'A call deste ticket: {existing.mention}'), ephemeral=True)
        category = interaction.channel.category
        overwrites = dict(interaction.channel.overwrites)
        try:
            voice = await interaction.guild.create_voice_channel(name=f'call-{interaction.channel.name}'[:100], category=category, overwrites=overwrites, reason=f'Call criada por {interaction.user} para o ticket {interaction.channel.name}')
            with db() as c:
                c.execute('UPDATE zordx_tickets SET voice_channel_id=? WHERE channel_id=?', (voice.id, interaction.channel.id))
            await interaction.response.send_message(embed=status_embed('🔊 Call criada', f'Canal de voz privado: {voice.mention}', success=True))
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(embed=status_embed('Não foi possível criar a call', 'Confira se o bot tem permissão para gerenciar canais.'), ephemeral=True)

    @discord.ui.button(label='Fechar ticket', style=discord.ButtonStyle.danger, emoji='🔒', custom_id='zordx:ticket:close', row=1)
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Este botão só funciona dentro de um ticket.'))
        with db() as c:
            row = c.execute("SELECT id, user_id, voice_channel_id FROM zordx_tickets WHERE channel_id=? AND guild_id=? AND status='Aberto'", (interaction.channel.id, interaction.guild_id)).fetchone()
        if not row:
            return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Este ticket já foi fechado ou não está registrado.'))
        ticket_id, owner_id, voice_id = int(row[0]), int(row[1]), int(row[2] or 0)
        if interaction.user.id != owner_id and not is_ticket_staff(interaction):
            return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Só quem abriu o ticket ou a equipe autorizada pode fechá-lo.'))
        await interaction.response.send_message(embed=status_embed('🔒 Ticket encerrado', 'O ticket será arquivado e o canal será removido. O cliente receberá uma opção de avaliação.'), ephemeral=False)
        log_channel_id = int(cfg(interaction.guild_id, 'ticket_logs_channel_id', '0') or 0)
        log_channel = interaction.guild.get_channel(log_channel_id)
        if isinstance(log_channel, discord.TextChannel):
            try:
                await log_channel.send(embed=status_embed('🔒 Ticket fechado', f'**Ticket:** {interaction.channel.name}\n**Aberto por:** <@{owner_id}>\n**Fechado por:** {interaction.user.mention}\n**ID:** {ticket_id}'))
            except discord.HTTPException:
                pass
        with db() as c:
            c.execute("UPDATE zordx_tickets SET status='Fechado', closed_at=? WHERE channel_id=?", (datetime.now(timezone.utc).isoformat(), interaction.channel.id))
        owner = interaction.guild.get_member(owner_id)
        if owner:
            try:
                await owner.send(embed=status_embed('⭐ Avalie seu atendimento', 'Como foi sua experiência com a equipe? Escolha uma nota de 1 a 5 estrelas.'), view=TicketRatingView(interaction.guild_id, ticket_id, owner_id))
            except discord.HTTPException:
                pass
        if voice_id:
            voice = interaction.guild.get_channel(voice_id)
            if voice:
                try:
                    await voice.delete(reason='Ticket relacionado foi fechado')
                except discord.HTTPException:
                    pass
        try:
            await interaction.channel.delete(reason=f'Ticket fechado por {interaction.user}')
        except discord.Forbidden:
            await interaction.followup.send(embed=status_embed('Permissão necessária', 'Não consegui apagar o canal. Confira a permissão **Gerenciar canais**.'), ephemeral=True)

class TicketPanelView(discord.ui.View):
    """Painel público persistente: o membro escolhe o assunto antes de criar o ticket."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Abrir ticket', style=discord.ButtonStyle.primary, emoji='🎫', custom_id='zordx:ticket:open')
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            return await interaction.response.send_message(
                embed=status_embed('ZORDX STORE', 'Este painel só funciona dentro de um servidor.'), ephemeral=True)
        embed = discord.Embed(
            title='📝 Sobre o que você precisa de ajuda?',
            description='Escolha uma opção abaixo. Um canal privado será criado para você e a equipe autorizada.',
            color=color_from(DEFAULT_COLOR))
        embed.set_footer(text=f'{SHOP_NAME} • Central de atendimento')
        await interaction.response.send_message(embed=embed, view=TicketSubjectView(), ephemeral=True)


class TicketSubjectSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label='Compra ou pedido', value='Compra ou pedido', emoji='🛍️', description='Ajuda com uma compra ou pedido'),
            discord.SelectOption(label='Pagamento', value='Pagamento', emoji='💳', description='Dúvidas sobre pagamento'),
            discord.SelectOption(label='Entrega ou produto', value='Entrega ou produto', emoji='📦', description='Problemas com entrega ou produto'),
            discord.SelectOption(label='Suporte geral', value='Suporte geral', emoji='🎧', description='Outras dúvidas e solicitações'),
        ]
        super().__init__(placeholder='Selecione o assunto do ticket', min_values=1, max_values=1, options=options, custom_id='zordx:ticket:subject')

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(embed=status_embed('ZORDX STORE', 'Use este menu dentro do servidor.'), ephemeral=True)

        category_id = int(cfg(guild.id, 'ticket_category_id', '0') or 0)
        support_role_ids = ticket_role_ids(guild.id)
        support_roles = [guild.get_role(role_id) for role_id in support_role_ids]
        support_roles = [role for role in support_roles if role is not None]
        support_role = support_roles[0] if support_roles else None
        category = guild.get_channel(category_id) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message(embed=status_embed('⚙️ Tickets não configurados', 'A equipe precisa configurar a categoria em `/config-ticket` antes de abrir tickets.'), ephemeral=True)

        with db() as c:
            existing = c.execute("SELECT channel_id FROM zordx_tickets WHERE guild_id=? AND user_id=? AND status='Aberto'", (guild.id, interaction.user.id)).fetchall()
        active_channels = [guild.get_channel(int(row[0])) for row in existing]
        active_channels = [channel for channel in active_channels if isinstance(channel, discord.TextChannel)]
        limit = max(1, min(5, int(cfg(guild.id, 'ticket_open_limit', '1') or 1)))
        if len(active_channels) >= limit:
            mentions = ', '.join(channel.mention for channel in active_channels[:5])
            return await interaction.response.send_message(embed=status_embed('🎫 Você já tem ticket aberto', f'Limite: **{limit}** ticket(s) por pessoa.\n{mentions}'), ephemeral=True)

        subject = self.values[0]
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot_member = guild.me
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
        }
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True)
        for authorized_role in support_roles:
            overwrites[authorized_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)

        base = ''.join(ch.lower() if ch.isalnum() else '-' for ch in interaction.user.name).strip('-')[:30] or 'usuario'
        slug = ''.join(ch.lower() if ch.isalnum() else '-' for ch in subject).strip('-')[:18]
        name_format = cfg(guild.id, 'ticket_name_format', 'ticket-{assunto}-{usuario}')
        channel_name = name_format.replace('{usuario}', base).replace('{assunto}', slug)
        channel_name = '-'.join(part for part in channel_name.split('-') if part)[:100] or f'ticket-{base}'
        try:
            channel = await guild.create_text_channel(
                name=channel_name, category=category, overwrites=overwrites,
                topic=f'Ticket de {interaction.user} ({interaction.user.id}) | Assunto: {subject}',
                reason=f'Ticket aberto por {interaction.user} | Assunto: {subject}')
            with db() as c:
                c.execute('INSERT INTO zordx_tickets(guild_id,channel_id,user_id) VALUES(?,?,?)', (guild.id, channel.id, interaction.user.id))

            greeting = cfg(guild.id, 'ticket_greeting', 'Explique como podemos ajudar. A equipe responderá assim que possível.')
            embed = discord.Embed(title='🎫 Atendimento aberto', description=(
                f'Olá {interaction.user.mention}! Seu atendimento foi criado.\n\n'
                f'**Assunto:** {subject}\n\n{greeting}\n\n'
                'Envie os detalhes necessários e aguarde a equipe.'), color=color_from(DEFAULT_COLOR))
            embed.add_field(name='🔒 Privacidade', value='Este canal é visível apenas para você, a equipe autorizada e o bot.', inline=False)
            embed.set_footer(text=f'{SHOP_NAME} • Sistema de tickets')
            opening_image = cfg(guild.id, 'ticket_opening_image', '')
            if opening_image.startswith(('https://', 'http://')):
                embed.set_image(url=opening_image)
            support_text = ' '.join(role.mention for role in support_roles) or 'Equipe de suporte'
            await channel.send(content=f'{interaction.user.mention} • {support_text}', embed=embed, view=TicketCloseView())

            log_id = int(cfg(guild.id, 'ticket_logs_channel_id', '0') or 0)
            log_channel = guild.get_channel(log_id) if log_id else None
            if isinstance(log_channel, discord.TextChannel):
                try:
                    await log_channel.send(embed=status_embed('📨 Novo ticket', f'**Canal:** {channel.mention}\n**Pessoa:** {interaction.user.mention}\n**Assunto:** {subject}'))
                except discord.HTTPException:
                    pass
            await interaction.followup.send(embed=status_embed('✅ Ticket criado', f'Seu ticket foi criado: {channel.mention}', success=True), ephemeral=True)
        except (discord.Forbidden, discord.HTTPException, sqlite3.Error):
            log.exception('Não foi possível criar ticket para %s', interaction.user.id)
            await interaction.followup.send(embed=status_embed('❌ Não consegui criar o ticket', 'Confira se o bot tem **Gerenciar canais**, **Ver canais**, **Enviar mensagens** e acesso à categoria configurada.'), ephemeral=True)


class TicketSubjectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(TicketSubjectSelect())

add_group = app_commands.Group(name='add', description='Adicionar itens à loja')
edit_group = app_commands.Group(name='editar', description='Editar painéis da loja')
remove_group = app_commands.Group(name='tirar', description='Remover itens da loja')
spin_group = app_commands.Group(name='girar', description='Gerenciar produtos da loja')
embed_group = app_commands.Group(name='embed', description='Criar embeds personalizados')

@embed_group.command(name='criar', description='Criar uma mensagem embed personalizada')
@app_commands.describe(titulo='Título do embed', descricao='Descrição do embed', cor='Cor hexadecimal, por exemplo 8B35FF', imagem='URL opcional da imagem')
@staff_only()
async def embed_criar(interaction: discord.Interaction, titulo: str, descricao: str, cor: str='8B35FF', imagem: str=''):
    if imagem and (not imagem.startswith(('https://', 'http://'))):
        return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'A imagem precisa ser um link HTTP ou HTTPS.'))
    e = panel_embed(titulo, descricao, 'Embed personalizado', imagem, cor)
    await interaction.response.send_message('Prévia do embed:', embed=e, ephemeral=True, view=EmbedPublishView(interaction.guild_id, titulo, descricao, cor, imagem, interaction.user.id))

class EmbedPublishView(discord.ui.View):

    def __init__(self, guild_id, title, description, color, image, creator_id):
        super().__init__(timeout=180)
        self.guild_id, self.title_text, self.description_text, self.color, self.image, self.creator_id = (guild_id, title, description, color, image, creator_id)

    @discord.ui.button(label='Publicar neste canal', style=discord.ButtonStyle.success)
    async def publish_embed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.creator_id and (not is_staff(interaction)):
            return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Somente quem criou ou a equipe pode publicar.'))
        await interaction.channel.send(embed=panel_embed(self.title_text, self.description_text, 'Embed personalizado', self.image, self.color))
        await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', '✅ Embed publicado.'))

@add_group.command(name='painel', description='Criar um painel de loja do zero')
@app_commands.describe(titulo='Nome do painel', descricao='Texto exibido no painel', categoria='Categoria do painel', canal='Canal onde publicar', imagem='URL opcional da imagem', cor='Cor hexadecimal', botoes='Nomes separados por vírgula; padrão: Ver produtos,Cupons,Suporte')
@staff_only()
async def add_painel(interaction: discord.Interaction, titulo: str, descricao: str, categoria: str, canal: discord.TextChannel, imagem: str='', cor: str='8B35FF', botoes: str='Ver produtos,Cupons,Suporte'):
    if imagem and (not imagem.startswith(('https://', 'http://'))):
        return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'A imagem precisa ser um link HTTP ou HTTPS.'))
    buttons = [x.strip()[:80] for x in botoes.split(',') if x.strip()][:5]
    if not buttons:
        buttons = ['Ver produtos', 'Cupons', 'Suporte']
    e = panel_embed(titulo, descricao, categoria, imagem, cor)
    view = PublishPanelView(interaction.guild_id, canal.id, titulo, descricao, categoria, imagem, cor, buttons, interaction.user.id)
    await interaction.response.send_message(f'Confira a prévia abaixo. O painel será publicado em {canal.mention} somente quando você confirmar.', embed=e, view=view, ephemeral=True)

@edit_group.command(name='painel', description='Editar qualquer painel criado por este bot no servidor')
@premium_only()
async def editar_painel(interaction: discord.Interaction):
    with db() as c:
        rows = c.execute('SELECT id,title,channel_id FROM zordx_panels WHERE guild_id=? ORDER BY id DESC', (interaction.guild_id,)).fetchall()
    if not rows:
        return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Ainda não há painéis criados por este bot registrados neste servidor. Use `/add painel` primeiro.'))
    e = status_embed('✏️ Editar painel', 'Selecione um painel criado por este bot. As alterações serão aplicadas à mensagem original, sem publicar uma cópia.')
    await interaction.response.send_message(embed=e, view=PanelSelectView(rows), ephemeral=True)

@bot.tree.command(name='ajuda-vendas', description='Ajuda para usar os comandos de vendas')
async def ajuda_vendas(interaction: discord.Interaction):
    text = '**Comandos da ZORDX STORE BOT**\n`/produtos` — ver catálogo e estoque\n`/addproduto` — cadastrar produto (equipe)\n`/addestoque` — adicionar estoque (equipe)\n`/girar produto` — remover produto (equipe)\n`/add painel` — criar painel com prévia\n`/editar painel` — editar painel existente (Premium)\n`/cupons` — listar cupons (Premium)\n`/addcupom` — criar cupom (Premium)\n`/tirar cupom` — remover cupom (Premium)\n`/saldo` e `/sacar` — consultar saldo e solicitar saque\n`/config` — configurar a loja\n`/config-ticket` — configurar categoria, suporte, logs e painel de tickets.'
    e = status_embed('📚 Ajuda de vendas', text)
    e.add_field(name='Painéis', value='`/add painel` • `/editar painel` • `/tirarpainel`', inline=False)
    e.add_field(name='Tickets', value='`/config-ticket` configura categoria, vários cargos de atendimento, logs, painel e limite. Nos tickets: assumir, transferir, renomear, criar call, fechar e avaliar.', inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name='produtos', description='Mostrar catálogo, preços e estoque')
async def produtos(interaction: discord.Interaction):
    rows = get_products()
    if not rows:
        return await interaction.response.send_message(embed=status_embed('🛍️ Catálogo de produtos', 'Ainda não há produtos cadastrados na loja.'), ephemeral=True)
    e = discord.Embed(title=f'🛍️ Produtos — {SHOP_NAME}', description='Confira os produtos, preços e disponibilidade abaixo.', color=color_from(DEFAULT_COLOR))
    for name, description, price, stock, link in rows[:25]:
        value = f"**Preço:** {money(price)}\n**Estoque:** {stock}\n{description or 'Sem descrição'}"
        if link:
            value += f'\n[Ver produto]({link})'
        e.add_field(name=name[:256], value=value[:1024], inline=False)
    await interaction.response.send_message(embed=e, ephemeral=False)

@bot.tree.command(name='addproduto', description='Cadastrar um produto na loja')
@app_commands.describe(nome='Nome do produto', preco='Preço em reais', estoque='Quantidade inicial', descricao='Descrição', link='Link opcional do produto')
@staff_only()
async def addproduto(interaction: discord.Interaction, nome: str, preco: app_commands.Range[float, 0.01, 100000000.0], estoque: app_commands.Range[int, 0, 100000000], descricao: str='', link: str=''):
    if link and (not link.startswith(('https://', 'http://'))):
        return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'O link precisa começar com http:// ou https://.'))
    try:
        with db() as c:
            c.execute('INSERT INTO shop_products(name,description,price,stock,link) VALUES(?,?,?,?,?)', (nome.strip(), descricao[:1000], float(preco), int(estoque), link))
        await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', f'✅ Produto **{nome}** cadastrado por {money(preco)} com estoque {estoque}.'))
    except sqlite3.IntegrityError:
        await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Já existe um produto com esse nome. Use `/addestoque` para adicionar unidades.'))

@bot.tree.command(name='addestoque', description='Adicionar unidades ao estoque de um produto')
@staff_only()
async def addestoque(interaction: discord.Interaction, nome: str, quantidade: app_commands.Range[int, 1, 100000000]):
    with db() as c:
        cur = c.execute('UPDATE shop_products SET stock=stock+? WHERE name=? COLLATE NOCASE', (int(quantidade), nome.strip()))
    if cur.rowcount == 0:
        return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Não encontrei esse produto. Confira o nome em `/produtos`.'))
    await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', f'✅ Estoque de **{nome}** aumentado em **{quantidade}** unidade(s).'))

@spin_group.command(name='produto', description='Remover um produto da loja')
@staff_only()
async def girar_produto(interaction: discord.Interaction, nome: str):
    with db() as c:
        cur = c.execute('DELETE FROM shop_products WHERE name=? COLLATE NOCASE', (nome.strip(),))
    if cur.rowcount == 0:
        return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Não encontrei esse produto.'))
    await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', f'🗑️ Produto **{nome}** removido do catálogo.'))

@bot.tree.command(name='cupons', description='Listar cupons da loja (Premium)')
@premium_only()
async def cupons(interaction: discord.Interaction):
    with db() as c:
        rows = c.execute('SELECT code,percent,uses_left FROM coupons ORDER BY code').fetchall()
    e = discord.Embed(title=f'🎟️ Cupons — {SHOP_NAME}', description='Cupons disponíveis e usos restantes.', color=color_from(DEFAULT_COLOR))
    if not rows:
        e.description = 'Nenhum cupom cadastrado.'
    else:
        e.description = '\n'.join((f'**{code}** — {percent}% de desconto • usos restantes: {uses}' for code, percent, uses in rows[:25]))
    await interaction.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name='addcupom', description='Criar cupom de desconto (Premium)')
@app_commands.describe(codigo='Código do cupom', desconto='Percentual de desconto', usos='Quantidade de usos permitidos')
@premium_only()
async def addcupom(interaction: discord.Interaction, codigo: str, desconto: app_commands.Range[int, 1, 100], usos: app_commands.Range[int, 1, 1000000]):
    try:
        with db() as c:
            c.execute('INSERT INTO coupons(code,percent,uses_left,created_by) VALUES(?,?,?,?)', (codigo.strip().upper(), int(desconto), int(usos), interaction.user.id))
        await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', f'✅ Cupom `{codigo.upper()}` criado: {desconto}% de desconto, {usos} uso(s).'))
    except sqlite3.IntegrityError:
        await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Esse código já existe. Escolha outro.'))

@remove_group.command(name='cupom', description='Remover um cupom da loja (Premium)')
@premium_only()
async def tirar_cupom(interaction: discord.Interaction, codigo: str):
    with db() as c:
        cur = c.execute('DELETE FROM coupons WHERE code=? COLLATE NOCASE', (codigo.strip(),))
    if cur.rowcount == 0:
        return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Não encontrei esse cupom.'))
    await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', f'🗑️ Cupom `{codigo}` removido.'))

class RemovePanelSelect(discord.ui.Select):

    def __init__(self, rows):
        options = [discord.SelectOption(label=(row[1] or 'Painel')[:100], description=f'Canal ID: {row[2]} • Painel #{row[0]}'[:100], value=str(row[0])) for row in rows[:25]]
        super().__init__(placeholder='Escolha o painel que deseja remover', min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        panel_id = int(self.values[0])
        with db() as c:
            row = c.execute('SELECT channel_id,message_id,title FROM zordx_panels WHERE id=? AND guild_id=?', (panel_id, interaction.guild_id)).fetchone()
        if not row:
            return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Não encontrei esse painel.'))
        channel = interaction.guild.get_channel(row[0]) if interaction.guild else None
        try:
            if isinstance(channel, discord.TextChannel):
                message = await channel.fetch_message(row[1])
                await message.delete()
            with db() as c:
                c.execute('DELETE FROM zordx_panels WHERE id=? AND guild_id=?', (panel_id, interaction.guild_id))
            await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', f'🗑️ O painel **{row[2]}** foi removido.'))
        except discord.NotFound:
            with db() as c:
                c.execute('DELETE FROM zordx_panels WHERE id=? AND guild_id=?', (panel_id, interaction.guild_id))
            await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'A mensagem já não existia; removi o registro do painel.'))
        except discord.Forbidden:
            await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Não tenho permissão para apagar a mensagem original.'))

class RemovePanelView(discord.ui.View):

    def __init__(self, rows):
        super().__init__(timeout=180)
        self.add_item(RemovePanelSelect(rows))

@bot.tree.command(name='tirarpainel', description='Remover um painel criado pelo bot')
@staff_only()
async def tirarpainel(interaction: discord.Interaction):
    with db() as c:
        rows = c.execute('SELECT id,title,channel_id FROM zordx_panels WHERE guild_id=? ORDER BY id DESC', (interaction.guild_id,)).fetchall()
    if not rows:
        return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', 'Não há painéis registrados para remover.'))
    e = status_embed('🗑️ Remover painel', 'Selecione o painel que deseja remover. A mensagem original será apagada.', success=False)
    await interaction.response.send_message(embed=e, view=RemovePanelView(rows), ephemeral=True)

@bot.tree.command(name='entregas', description='Configurar canal de avisos de entrega (Premium)')
@app_commands.describe(canal='Canal que receberá avisos de entrega; deixe vazio para consultar')
@premium_only()
async def entregas(interaction: discord.Interaction, canal: discord.TextChannel=None):
    if canal:
        set_cfg(interaction.guild_id, 'delivery_channel_id', canal.id)
        return await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', f'✅ Canal de avisos de entrega definido para {canal.mention}.'))
    current = int(cfg(interaction.guild_id, 'delivery_channel_id', '0') or 0)
    channel = interaction.guild.get_channel(current) if interaction.guild and current else None
    await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', f"Canal atual de entregas: {(channel.mention if channel else 'não configurado')}. Use `/entregas canal:#canal` para definir."))

@bot.tree.command(name='config', description='Configurar canais e opções da loja')
@app_commands.describe(canal_loja='Canal padrão da loja', canal_suporte='Canal de suporte', canal_saques='Canal para receber solicitações de saque')
@staff_only()
async def config(interaction: discord.Interaction, canal_loja: discord.TextChannel=None, canal_suporte: discord.TextChannel=None, canal_saques: discord.TextChannel=None):
    changed = []
    if canal_loja:
        set_cfg(interaction.guild_id, 'shop_channel_id', canal_loja.id)
        changed.append(f'Canal da loja: {canal_loja.mention}')
    if canal_suporte:
        set_cfg(interaction.guild_id, 'support_channel_id', canal_suporte.id)
        changed.append(f'Canal de suporte: {canal_suporte.mention}')
    if canal_saques:
        set_cfg(interaction.guild_id, 'withdrawal_channel_id', canal_saques.id)
        changed.append(f'Canal de saques: {canal_saques.mention}')
    if not changed:
        shop = interaction.guild.get_channel(int(cfg(interaction.guild_id, 'shop_channel_id', '0') or 0)) if interaction.guild else None
        support = interaction.guild.get_channel(int(cfg(interaction.guild_id, 'support_channel_id', '0') or 0)) if interaction.guild else None
        withdrawal = interaction.guild.get_channel(int(cfg(interaction.guild_id, 'withdrawal_channel_id', '0') or 0)) if interaction.guild else None
        changed = [f"Canal da loja: {(shop.mention if shop else 'não definido')}", f"Suporte: {(support.mention if support else 'não definido')}", f"Saques: {(withdrawal.mention if withdrawal else 'não definido')}"]
    e = status_embed('⚙️ Configurações da loja', '\n'.join((f'• {item}' for item in changed)), success=True)
    e.add_field(name='Como usar', value='Passe os parâmetros do comando `/config` para alterar os canais.', inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)

class TicketTextModal(discord.ui.Modal):
    def __init__(self, guild_id: int):
        super().__init__(title='Personalizar painel de tickets')
        self.guild_id = guild_id
        self.titulo = discord.ui.TextInput(label='Título do painel', default=cfg(guild_id, 'ticket_panel_title', '🎫 Central de Atendimento'), max_length=100, required=True)
        self.descricao = discord.ui.TextInput(label='Descrição do painel', default=cfg(guild_id, 'ticket_panel_description', 'Precisa de ajuda? Clique abaixo para abrir um ticket privado.'), style=discord.TextStyle.paragraph, max_length=1000, required=True)
        self.boas_vindas = discord.ui.TextInput(label='Mensagem de boas-vindas', default=cfg(guild_id, 'ticket_greeting', 'Explique como podemos ajudar. A equipe responderá assim que possível.'), style=discord.TextStyle.paragraph, max_length=1000, required=True)
        self.add_item(self.titulo)
        self.add_item(self.descricao)
        self.add_item(self.boas_vindas)

    async def on_submit(self, interaction: discord.Interaction):
        set_cfg(self.guild_id, 'ticket_panel_title', self.titulo.value)
        set_cfg(self.guild_id, 'ticket_panel_description', self.descricao.value)
        set_cfg(self.guild_id, 'ticket_greeting', self.boas_vindas.value)
        await interaction.response.send_message(embed=status_embed('✅ Configurações salvas', 'Título, descrição e mensagem de boas-vindas foram atualizados.', success=True), ephemeral=True)

class TicketLimitModal(discord.ui.Modal):
    def __init__(self, guild_id: int):
        super().__init__(title='Limite de tickets por pessoa')
        self.guild_id = guild_id
        self.limite = discord.ui.TextInput(label='Máximo de tickets abertos (1 a 5)', default=cfg(guild_id, 'ticket_open_limit', '1'), min_length=1, max_length=1, required=True)
        self.add_item(self.limite)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.limite.value)
            if value < 1 or value > 5:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(embed=status_embed('Valor inválido', 'Digite um número de 1 a 5.', success=False), ephemeral=True)
        set_cfg(self.guild_id, 'ticket_open_limit', value)
        await interaction.response.send_message(embed=status_embed('✅ Limite atualizado', f'Cada pessoa poderá ter até **{value}** ticket(s) aberto(s).', success=True), ephemeral=True)

async def open_ticket_setting(interaction: discord.Interaction, guild_id: int, value: str):
    view = TicketSetupSubView(guild_id)
    if value == 'category':
        view.add_item(TicketCategorySelect(guild_id))
        return await interaction.response.edit_message(embed=status_embed('📁 Categoria dos tickets', 'Selecione a categoria onde os tickets serão criados.'), view=view)
    if value == 'role':
        view.add_item(TicketRoleSelect(guild_id))
        return await interaction.response.edit_message(embed=status_embed('👥 Cargos de suporte', 'Selecione todos os cargos autorizados a atender tickets.'), view=view)
    if value == 'logs':
        view.add_item(TicketLogsSelect(guild_id))
        return await interaction.response.edit_message(embed=status_embed('📋 Canal de logs', 'Selecione o canal para registrar abertura e fechamento dos tickets.'), view=view)
    if value == 'evaluation':
        view.add_item(TicketEvaluationChannelSelect(guild_id))
        return await interaction.response.edit_message(embed=status_embed('⭐ Canal de avaliação', 'Selecione o canal onde as avaliações serão publicadas.'), view=view)
    if value == 'name':
        view.add_item(TicketNameFormatSelect(guild_id))
        return await interaction.response.edit_message(embed=status_embed('✏️ Nome do canal', 'Escolha o formato do nome dos novos tickets.'), view=view)
    if value == 'type':
        view.add_item(TicketChannelTypeSelect(guild_id))
        return await interaction.response.edit_message(embed=status_embed('📌 Tipo de canal', 'Escolha o tipo de canal disponível para os tickets.'), view=view)
    if value == 'image':
        return await interaction.response.send_modal(TicketOpeningImageModal(guild_id))
    if value == 'limit':
        return await interaction.response.send_modal(TicketLimitModal(guild_id))

class TicketSetupView(discord.ui.View):
    """Menu visual de Configurações de Tickets; todas as opções ficam na mesma mensagem."""
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    @discord.ui.button(label='Cargos de Suporte', emoji='👥', style=discord.ButtonStyle.secondary, row=0)
    async def roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_ticket_setting(interaction, self.guild_id, 'role')

    @discord.ui.button(label='Categoria dos Tickets', emoji='📁', style=discord.ButtonStyle.secondary, row=0)
    async def category(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_ticket_setting(interaction, self.guild_id, 'category')

    @discord.ui.button(label='Canal de Logs', emoji='📋', style=discord.ButtonStyle.secondary, row=1)
    async def logs(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_ticket_setting(interaction, self.guild_id, 'logs')

    @discord.ui.button(label='Canal de Avaliação', emoji='⭐', style=discord.ButtonStyle.secondary, row=1)
    async def evaluation(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_ticket_setting(interaction, self.guild_id, 'evaluation')

    @discord.ui.button(label='Nome do Canal', emoji='✏️', style=discord.ButtonStyle.secondary, row=2)
    async def name(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_ticket_setting(interaction, self.guild_id, 'name')

    @discord.ui.button(label='Tipo de Canal', emoji='📌', style=discord.ButtonStyle.secondary, row=2)
    async def channel_type(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_ticket_setting(interaction, self.guild_id, 'type')

    @discord.ui.button(label='Imagem ao Abrir', emoji='🖼️', style=discord.ButtonStyle.secondary, row=3)
    async def image(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_ticket_setting(interaction, self.guild_id, 'image')

    @discord.ui.button(label='Limite por Usuário', emoji='👤', style=discord.ButtonStyle.secondary, row=3)
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_ticket_setting(interaction, self.guild_id, 'limit')

    @discord.ui.button(label='Voltar', emoji='⬅️', style=discord.ButtonStyle.primary, row=4)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=status_embed('🔧 | Configurações Gerais', 'Ajustes que controlam o funcionamento do sistema de tickets.\n\n🎫 **Atendimento** — cargos, categoria, logs, avaliação e opções dos tickets.\n✏️ **Personalização** — título, descrição e mensagem de boas-vindas.\n📨 **Publicação** — enviar o painel para um canal.', success=True),
            view=TicketSetupView(self.guild_id)
        )

class TicketSetupSubView(discord.ui.View):
    """Mantém cada configuração no mesmo painel e oferece retorno ao menu Atendimento."""
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    @discord.ui.button(label='Voltar', emoji='⬅️', style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=status_embed(
                '🎫 Atendimento — Configurações',
                'Escolha uma opção para configurar.\n\n'
                '• Cargos de Suporte\n'
                '• Categoria dos Tickets\n'
                '• Canal de Logs\n'
                '• Canal de Avaliação\n'
                '• Nome do Canal\n'
                '• Tipo de Canal\n'
                '• Imagem ao Abrir\n'
                '• Limite por Usuário'            ),
            view=TicketSetupView(self.guild_id)
        )

class TicketEvaluationChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        super().__init__(placeholder='Selecione o canal de avaliações', channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        set_cfg(self.guild_id, 'ticket_evaluation_channel_id', channel.id)
        await interaction.response.edit_message(embed=status_embed('✅ Canal de avaliação salvo', f'As avaliações serão publicadas em {channel.mention}.', success=True), view=TicketSetupView(self.guild_id))

class TicketNameFormatSelect(discord.ui.Select):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        current = cfg(guild_id, 'ticket_name_format', 'ticket-{assunto}-{usuario}')
        options = [
            discord.SelectOption(label='ticket-assunto-usuario', value='ticket-{assunto}-{usuario}', description='Ex.: ticket-compra-neymar'),
            discord.SelectOption(label='usuario-ticket', value='{usuario}-ticket', description='Ex.: neymar-ticket'),
            discord.SelectOption(label='assunto-usuario', value='{assunto}-{usuario}', description='Ex.: compra-neymar'),
            discord.SelectOption(label='atendimento-usuario', value='atendimento-{usuario}', description='Ex.: atendimento-neymar'),
        ]
        for option in options:
            option.default = option.value == current
        super().__init__(placeholder='Escolha o formato do nome', min_values=1, max_values=1, options=options)
    async def callback(self, interaction: discord.Interaction):
        pattern = self.values[0]
        set_cfg(self.guild_id, 'ticket_name_format', pattern)
        await interaction.response.edit_message(embed=status_embed('✅ Formato do nome salvo', f'Novos tickets usarão o formato `{pattern}`.', success=True), view=TicketSetupView(self.guild_id))

class TicketChannelTypeSelect(discord.ui.Select):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        super().__init__(placeholder='Tipo de canal suportado', min_values=1, max_values=1, options=[
            discord.SelectOption(label='Canal de texto privado', value='text', emoji='📝', description='Canal privado com permissões individuais', default=True)
        ])
    async def callback(self, interaction: discord.Interaction):
        set_cfg(self.guild_id, 'ticket_channel_type', self.values[0])
        await interaction.response.edit_message(embed=status_embed('✅ Tipo de canal salvo', 'Os tickets serão criados como canais de texto privados dentro da categoria configurada.', success=True), view=TicketSetupView(self.guild_id))

class TicketOpeningImageModal(discord.ui.Modal, title='Imagem ao abrir ticket'):
    image_url = discord.ui.TextInput(label='URL da imagem (vazio para remover)', placeholder='https://...', required=False, max_length=500)
    def __init__(self, guild_id):
        super().__init__()
        self.guild_id = guild_id
        self.image_url.default = cfg(guild_id, 'ticket_opening_image', '')[:500]
    async def on_submit(self, interaction: discord.Interaction):
        url = self.image_url.value.strip()
        if url and not url.startswith(('https://', 'http://')):
            return await interaction.response.send_message(embed=status_embed('❌ URL inválida', 'Informe uma URL começando com https:// ou http://, ou deixe vazio para remover.', success=False), ephemeral=True)
        set_cfg(self.guild_id, 'ticket_opening_image', url)
        await interaction.response.send_message(embed=status_embed('✅ Imagem atualizada', 'A imagem será exibida na mensagem de boas-vindas dos próximos tickets.' if url else 'A imagem de boas-vindas foi removida.', success=True), ephemeral=True)

class TicketCategorySelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        super().__init__(placeholder='Selecione a categoria', channel_types=[discord.ChannelType.category], min_values=1, max_values=1)
    async def callback(self, interaction):
        set_cfg(self.guild_id, 'ticket_category_id', self.values[0].id)
        await interaction.response.edit_message(embed=status_embed('✅ Categoria salva', f'Categoria definida para **{self.values[0].name}**.', success=True), view=TicketSetupView(self.guild_id))

class TicketRoleSelect(discord.ui.RoleSelect):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        super().__init__(placeholder='Selecione todos os cargos autorizados', min_values=1, max_values=10)
    async def callback(self, interaction):
        roles = list(self.values)
        set_cfg(self.guild_id, 'ticket_support_role_ids', json.dumps([role.id for role in roles]))
        # Mantém compatibilidade com versões antigas do bot.
        set_cfg(self.guild_id, 'ticket_support_role_id', roles[0].id)
        mentions = ', '.join(role.mention for role in roles)
        await interaction.response.edit_message(embed=status_embed('✅ Cargos de atendimento salvos', f'Cargos autorizados: {mentions}', success=True), view=TicketSetupView(self.guild_id))

class TicketLogsSelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        super().__init__(placeholder='Selecione o canal de logs', channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
    async def callback(self, interaction):
        set_cfg(self.guild_id, 'ticket_logs_channel_id', self.values[0].id)
        await interaction.response.edit_message(embed=status_embed('✅ Canal de logs salvo', f'Logs serão enviados em {self.values[0].mention}.', success=True), view=TicketSetupView(self.guild_id))

class TicketPublishView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.add_item(TicketPublishChannelSelect(guild_id))

class TicketPublishChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        super().__init__(placeholder='Escolha onde publicar o painel', channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        title = cfg(self.guild_id, 'ticket_panel_title', '🎫 Central de Atendimento')
        description = cfg(self.guild_id, 'ticket_panel_description', 'Precisa de ajuda? Clique no botão abaixo para abrir um ticket privado com a equipe.')
        embed = discord.Embed(title=title[:256], description=description[:4000], color=color_from(DEFAULT_COLOR))
        embed.add_field(name='🔒 Atendimento privado', value='Somente você e a equipe autorizada poderão ver seu ticket.', inline=True)
        embed.add_field(name='⚡ Como funciona', value='Clique em **Abrir ticket** e descreva o que precisa.', inline=True)
        embed.add_field(name='🧾 Antes de abrir', value='Envie detalhes úteis para agilizar o atendimento.', inline=False)
        embed.set_footer(text=f'{SHOP_NAME} • Central de suporte')
        try:
            await channel.send(embed=embed, view=TicketPanelView())
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.edit_message(embed=status_embed('❌ Não foi possível publicar', 'Confira as permissões do bot no canal escolhido.', success=False), view=TicketSetupView(self.guild_id))
        await interaction.response.edit_message(embed=status_embed('✅ Painel publicado', f'O painel de tickets foi publicado em {channel.mention}.', success=True), view=TicketSetupView(self.guild_id))

@bot.tree.command(name='config-ticket', description='Abrir painel interativo de configuração de tickets')
@staff_only()
async def config_ticket(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message(embed=status_embed('ZORDX STORE', 'Use este comando dentro do servidor.'), ephemeral=True)
    category_id = int(cfg(guild.id, 'ticket_category_id', '0') or 0)
    role_id = int(cfg(guild.id, 'ticket_support_role_id', '0') or 0)
    role_ids = ticket_role_ids(guild.id)
    logs_id = int(cfg(guild.id, 'ticket_logs_channel_id', '0') or 0)
    category = guild.get_channel(category_id) if category_id else None
    role = guild.get_role(role_id) if role_id else None
    logs = guild.get_channel(logs_id) if logs_id else None
    evaluation_id = int(cfg(guild.id, 'ticket_evaluation_channel_id', '0') or 0)
    evaluation_channel = guild.get_channel(evaluation_id) if evaluation_id else None
    embed = status_embed('🔧 | Configurações Gerais', 'Ajustes que controlam o funcionamento do sistema de tickets no servidor.\n\n**👥 Atendimento** — cargos, categoria e logs. Cada opção pode ser configurada individualmente.\n**✏️ Personalização** — título, descrição e mensagem de boas-vindas.\n**🔢 Limite de tickets** — quantidade máxima de tickets abertos por pessoa.\n**📨 Publicação** — enviar o painel para o canal escolhido.', success=True)
    role_mentions = ', '.join(guild.get_role(rid).mention for rid in role_ids if guild.get_role(rid)) or (role.mention if role else 'Não configurado')
    embed.add_field(name='📌 Estado atual', value=f"**Categoria:** {category.name if isinstance(category, discord.CategoryChannel) else 'Não configurada'}\n**Cargos de atendimento:** {role_mentions}\n**Canal de logs:** {logs.mention if isinstance(logs, discord.TextChannel) else 'Não configurado'}\n**Canal de avaliação:** {evaluation_channel.mention if isinstance(evaluation_channel, discord.TextChannel) else 'Usa o canal de logs'}\n**Formato do nome:** `{cfg(guild.id, 'ticket_name_format', 'ticket-{assunto}-{usuario}')}`\n**Imagem ao abrir:** {'Configurada' if cfg(guild.id, 'ticket_opening_image', '') else 'Não configurada'}\n**Limite por pessoa:** {cfg(guild.id, 'ticket_open_limit', '1')}", inline=False)
    embed.set_footer(text=f'{SHOP_NAME} • Painel administrativo • Só você pode ver esta mensagem')
    await interaction.response.send_message(embed=embed, view=TicketSetupView(guild.id), ephemeral=True)

@bot.tree.command(name='saldo', description='Consultar seu saldo disponível para saque')
async def saldo(interaction: discord.Interaction):
    with db() as c:
        row = c.execute('SELECT balance FROM zordx_balances WHERE guild_id=? AND user_id=?', (interaction.guild_id, interaction.user.id)).fetchone()
    balance = float(row[0]) if row else 0.0
    e = status_embed('💰 Seu saldo', f'**Saldo registrado:** {money(balance)}\n\nO saldo precisa ser atualizado pelo sistema de vendas da loja para refletir vendas reais.')
    e.add_field(name='Conta', value=interaction.user.mention, inline=True)
    await interaction.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name='sacar', description='Solicitar saque do saldo registrado')
@app_commands.describe(valor='Valor que deseja sacar em reais')
async def sacar(interaction: discord.Interaction, valor: app_commands.Range[float, 0.01, 100000000.0]):
    with db() as c:
        row = c.execute('SELECT balance FROM zordx_balances WHERE guild_id=? AND user_id=?', (interaction.guild_id, interaction.user.id)).fetchone()
    balance = float(row[0]) if row else 0.0
    if float(valor) > balance:
        return await interaction.response.send_message(embed=status_embed('❌ Saldo insuficiente', f'Você solicitou **{money(valor)}**, mas seu saldo registrado é **{money(balance)}**.', success=False), ephemeral=True)
    with db() as c:
        c.execute('INSERT INTO zordx_withdrawals(guild_id,user_id,amount) VALUES(?,?,?)', (interaction.guild_id, interaction.user.id, float(valor)))
        c.execute('UPDATE zordx_balances SET balance=balance-? WHERE guild_id=? AND user_id=?', (float(valor), interaction.guild_id, interaction.user.id))
    e = status_embed('💸 Solicitação de saque registrada', f'**Valor:** {money(valor)}\n**Status:** Pendente de revisão\n\nA equipe da loja precisa revisar e concluir o pagamento.', success=True)
    await interaction.response.send_message(embed=e, ephemeral=True)
    channel_id = int(cfg(interaction.guild_id, 'withdrawal_channel_id', '0') or 0)
    channel = interaction.guild.get_channel(channel_id) if interaction.guild and channel_id else None
    if isinstance(channel, discord.TextChannel):
        log_embed = status_embed('💸 Nova solicitação de saque', f'**Usuário:** {interaction.user.mention}\n**Valor:** {money(valor)}\n**Status:** Pendente de revisão')
        await channel.send(embed=log_embed)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        message = '🔒 Este comando é restrito à equipe do servidor.' if not is_premium(interaction) else '🔒 Você não tem permissão para usar este comando.'
    else:
        log.exception('Erro em comando slash', exc_info=error)
        message = 'Ocorreu um erro ao executar o comando. Confira os registros da hospedagem.'
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=status_embed('ZORDX STORE', message), ephemeral=True)
        else:
            await interaction.response.send_message(ephemeral=True, embed=status_embed('ZORDX STORE', message))
    except discord.HTTPException:
        pass
_commands_synced = False

@bot.event
async def on_ready():
    global _commands_synced
    log.info('Bot conectado como %s (%s)', bot.user, bot.user.id if bot.user else '?')
    if not _commands_synced:
        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
                log.info('Comandos sincronizados no servidor %s', GUILD_ID)
            else:
                await bot.tree.sync()
                log.info('Comandos globais sincronizados')
            _commands_synced = True
        except Exception:
            log.exception('Falha ao sincronizar comandos')

async def main():
    if not TOKEN:
        raise RuntimeError('A variável DISCORD_TOKEN não está configurada na hospedagem.')
    init_db()
    bot.add_view(TicketPanelView())
    bot.add_view(TicketCloseView())
    with db() as c:
        saved_panels = c.execute('SELECT message_id,buttons_json FROM zordx_panels').fetchall()
    for message_id, buttons_json in saved_panels:
        try:
            bot.add_view(PanelActionView(json.loads(buttons_json or '[]')), message_id=message_id)
        except (ValueError, TypeError, discord.DiscordException):
            log.exception('Não foi possível restaurar os botões do painel %s', message_id)
    bot.tree.add_command(add_group)
    bot.tree.add_command(edit_group)
    bot.tree.add_command(remove_group)
    bot.tree.add_command(spin_group)
    bot.tree.add_command(embed_group)
    async with bot:
        await bot.start(TOKEN)
if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
