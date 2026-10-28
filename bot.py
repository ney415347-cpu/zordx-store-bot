import os
import json
import sqlite3
import logging
import io
from datetime import timedelta, datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0") or 0)
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0") or 0)
TICKET_SUPPORT_ROLE_ID = int(os.getenv("TICKET_SUPPORT_ROLE_ID", "0") or 0)
RULES_CHANNEL_ID = int(os.getenv("RULES_CHANNEL_ID", "0") or 0)
SHOP_NAME = os.getenv("SHOP_NAME", "ZORDX STORE")
SHOP_COLOR = os.getenv("SHOP_COLOR", "5865F2").lstrip("#")
DB_PATH = os.getenv("DB_PATH", "zordx.sqlite3")
PRODUCTS_PATH = os.getenv("PRODUCTS_PATH", "products.json")

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


def db_init():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS warnings(
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER,
            moderator_id INTEGER, reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets(
            channel_id INTEGER PRIMARY KEY, guild_id INTEGER, opener_id INTEGER,
            status TEXT DEFAULT 'open', ticket_type TEXT DEFAULT 'Atendimento',
            closed_by INTEGER, closed_at TEXT)""")
        # Migrate databases created by older versions without losing existing tickets.
        ticket_columns = {row[1] for row in c.execute("PRAGMA table_info(tickets)").fetchall()}
        for column, declaration in (
            ("ticket_type", "TEXT DEFAULT 'Atendimento'"),
            ("closed_by", "INTEGER"),
            ("closed_at", "TEXT"),
        ):
            if column not in ticket_columns:
                c.execute(f"ALTER TABLE tickets ADD COLUMN {column} {declaration}")
        c.execute("""CREATE TABLE IF NOT EXISTS ticket_reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER,
            opener_id INTEGER, rating INTEGER, comment TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS shop_products(
            name TEXT PRIMARY KEY COLLATE NOCASE, description TEXT, price REAL,
            stock INTEGER DEFAULT 0, link TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS coupons(
            code TEXT PRIMARY KEY COLLATE NOCASE, percent INTEGER, uses_left INTEGER,
            created_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS sales(
            id INTEGER PRIMARY KEY AUTOINCREMENT, product_name TEXT, amount REAL,
            quantity INTEGER, seller_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS shop_config(
            key TEXT PRIMARY KEY, value TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS member_profiles(
            guild_id INTEGER, user_id INTEGER, bio TEXT DEFAULT '', points INTEGER DEFAULT 0,
            PRIMARY KEY(guild_id, user_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS member_ranks(
            guild_id INTEGER, user_id INTEGER, rank TEXT, updated_by INTEGER,
            PRIMARY KEY(guild_id, user_id))""")


def embed(title, description, color=None):
    if color is None:
        try:
            color = discord.Color(int(SHOP_COLOR, 16))
        except ValueError:
            color = discord.Color.from_rgb(88, 101, 242)
    return discord.Embed(
        title=str(title)[:256],
        description=str(description)[:4000],
        color=color
    )


def staff(i):
    return isinstance(i.user, discord.Member) and (
        i.user.guild_permissions.administrator
        or i.user.guild_permissions.manage_guild
        or i.user.guild_permissions.manage_messages
    )


async def staff_check(i):
    return staff(i)


def money(value):
    return f"R$ {float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def get_products():
    # Use the database first; fall back to the existing products.json file.
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT name, description, price, stock, link FROM shop_products ORDER BY name"
        ).fetchall()
    if rows:
        return [
            {"name": r[0], "description": r[1], "price": money(r[2]), "stock": r[3], "link": r[4]}
            for r in rows
        ]
    try:
        with open(PRODUCTS_PATH, encoding="utf-8") as f:
            items = json.load(f)
        return items if isinstance(items, list) else []
    except (OSError, json.JSONDecodeError):
        return []


TICKET_TYPES = [
    ("Ajuda", "🆘"), ("Dúvidas", "❓"),
    ("Suporte de compra", "🛒"), ("Denúncias", "🚨"),
    ("Relatos", "📝"), ("Pagamentos", "💳"),
    ("Entrega de produto", "📦"), ("Parcerias", "🤝"),
]


def config_value(key, default=""):
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT value FROM shop_config WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def save_config(key, value):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT OR REPLACE INTO shop_config(key,value) VALUES(?,?)", (key, str(value)))


async def create_ticket(interaction: discord.Interaction, ticket_type: str):
    guild = interaction.guild
    if not guild:
        return await interaction.response.send_message("Use dentro do servidor.", ephemeral=True)
    schedule_ok, schedule_message = ticket_schedule_allows(guild.id)
    if not schedule_ok:
        return await interaction.response.send_message(f"🕒 {schedule_message}", ephemeral=True)
    with sqlite3.connect(DB_PATH) as c:
        open_rows = c.execute(
            "SELECT channel_id FROM tickets WHERE guild_id=? AND opener_id=? AND status='open'",
            (guild.id, interaction.user.id)
        ).fetchall()
    active_channels = [guild.get_channel(row[0]) for row in open_rows]
    active_channels = [channel for channel in active_channels if channel is not None]
    max_open = int(config_value(f"ticket_limit_{guild.id}", "5") or 5)
    if len(active_channels) >= max_open:
        return await interaction.response.send_message(
            f"Você atingiu o limite de **{max_open} tickets abertos**. Feche um ticket antes de abrir outro.",
            ephemeral=True
        )
    category_id = int(config_value("ticket_category_id", str(TICKET_CATEGORY_ID)) or 0)
    support_role_id = int(config_value("ticket_support_role_id", str(TICKET_SUPPORT_ROLE_ID)) or 0)
    category = guild.get_channel(category_id) if category_id else None
    if not isinstance(category, discord.CategoryChannel):
        category = None
    perms = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        ),
    }
    if guild.me:
        perms[guild.me] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, manage_channels=True
        )
    role = guild.get_role(support_role_id) if support_role_id else None
    if role:
        perms[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )
    try:
        safe = "".join(x.lower() if x.isalnum() else "-" for x in interaction.user.name).strip("-")
        safe = (safe or str(interaction.user.id))[:45]
        template = config_value(f"ticket_channel_name_template_{guild.id}", "ticket-{user}")
        channel_name = template.replace("{user}", safe).replace("{type}", "".join(x.lower() if x.isalnum() else "-" for x in ticket_type).strip("-")).strip("-")
        channel_name = "-".join(part for part in channel_name.split("-") if part)[:95] or f"ticket-{interaction.user.id}"
        channel = await guild.create_text_channel(
            channel_name, category=category, overwrites=perms,
            topic=f"Ticket de {interaction.user} | Categoria: {ticket_type} | ID: {interaction.user.id}"
        )
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "INSERT OR REPLACE INTO tickets(channel_id,guild_id,opener_id,status,ticket_type) "
                "VALUES(?,?,?,'open',?)",
                (channel.id, guild.id, interaction.user.id, ticket_type)
            )
        e = embed(f"🎫 {ticket_type} — {SHOP_NAME}",
                  f"Olá {interaction.user.mention}! Explique como podemos ajudar.\n\n"
                  "🔒 Este canal é privado. Nunca envie senhas ou códigos de autenticação.",
                  color=discord.Color.blue())
        open_image = config_value(f"ticket_open_image_{guild.id}", "").strip()
        if open_image:
            e.set_image(url=open_image)
        await channel.send(content=interaction.user.mention, embed=e, view=CloseTicket())
        log_id = int(config_value("ticket_logs_channel_id", "0") or 0)
        log_channel = guild.get_channel(log_id) if log_id else None
        if isinstance(log_channel, discord.TextChannel):
            await log_channel.send(embed=embed(
                "🟢 Ticket aberto", f"**Categoria:** {ticket_type}\n**Usuário:** {interaction.user.mention}\n**Canal:** {channel.mention}",
                color=discord.Color.blue()))
        await interaction.response.send_message(f"✅ Seu ticket foi criado: {channel.mention}", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "Não consegui criar o ticket. Confira se o bot tem as permissões Gerenciar Canais e Ver Canais.",
            ephemeral=True
        )


class TicketCategoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        for index, (label, emoji) in enumerate(TICKET_TYPES):
            self.add_item(TicketCategoryButton(label, emoji, row=index // 2))


class TicketCategoryButton(discord.ui.Button):
    def __init__(self, label: str, emoji: str, row: int):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.primary, row=row)
        self.ticket_type = label

    async def callback(self, interaction: discord.Interaction):
        await create_ticket(interaction, self.ticket_type)


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Atendimento", style=discord.ButtonStyle.primary,
                       emoji="🎫", custom_id="zordx:open")
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        e = embed("🎫 Central de Atendimento — " + SHOP_NAME,
                  "Selecione uma das categorias abaixo para abrir seu atendimento.",
                  color=discord.Color.blue())
        await interaction.response.send_message(embed=e, view=TicketCategoryView(), ephemeral=True)


class TicketRatingView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int, opener_id: int):
        super().__init__(timeout=86400)
        self.guild_id, self.channel_id, self.opener_id = guild_id, channel_id, opener_id
        for score in range(1, 6):
            self.add_item(TicketRatingButton(score, self))


class TicketRatingButton(discord.ui.Button):
    def __init__(self, score: int, parent_view: TicketRatingView):
        super().__init__(label=f"{score} estrela{'s' if score != 1 else ''}",
                         emoji="⭐", style=discord.ButtonStyle.primary)
        self.score, self.parent_view = score, parent_view

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.opener_id:
            return await interaction.response.send_message("Só quem abriu o ticket pode avaliar.", ephemeral=True)
        with sqlite3.connect(DB_PATH) as c:
            existing = c.execute("SELECT id FROM ticket_reviews WHERE channel_id=? AND opener_id=?",
                                  (self.parent_view.channel_id, self.parent_view.opener_id)).fetchone()
            if existing:
                return await interaction.response.send_message("Você já avaliou este atendimento.", ephemeral=True)
            c.execute("INSERT INTO ticket_reviews(guild_id,channel_id,opener_id,rating) VALUES(?,?,?,?)",
                      (self.parent_view.guild_id, self.parent_view.channel_id,
                       self.parent_view.opener_id, self.score))
        review_channel_id = int(config_value("ticket_reviews_channel_id", "0") or 0)
        guild = bot.get_guild(self.parent_view.guild_id)
        review_channel = guild.get_channel(review_channel_id) if guild and review_channel_id else None
        if isinstance(review_channel, discord.TextChannel):
            await review_channel.send(embed=embed(
                "⭐ Nova avaliação — " + SHOP_NAME,
                f"**Cliente:** {interaction.user.mention}\n**Nota:** {'⭐' * self.score} ({self.score}/5)\n"
                f"**Ticket:** `#{self.parent_view.channel_id}`", color=discord.Color.blue()))
        for child in self.parent_view.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Obrigado pela avaliação! Você deu **{'⭐' * self.score} ({self.score}/5)**.",
            view=self.parent_view
        )


class CloseTicket(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fechar ticket", style=discord.ButtonStyle.danger,
                       emoji="🔒", custom_id="zordx:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Use no canal do ticket.", ephemeral=True)
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute(
                "SELECT opener_id,status,ticket_type FROM tickets WHERE channel_id=?",
                (interaction.channel.id,)
            ).fetchone()
        if not row or row[1] != "open":
            return await interaction.response.send_message("Ticket já fechado ou não registrado.", ephemeral=True)
        if interaction.user.id != row[0] and not staff(interaction):
            return await interaction.response.send_message("Só quem abriu ou a equipe pode fechar.", ephemeral=True)
        channel = interaction.channel
        transcript_lines = [f"Transcrição do ticket #{channel.name}", f"ID do canal: {channel.id}",
                            f"Categoria: {row[2]}", f"Fechado por: {interaction.user} ({interaction.user.id})", ""]
        try:
            async for msg in channel.history(limit=None, oldest_first=True):
                stamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                body = (msg.content or "[sem texto]").replace("\n", " ")
                if msg.attachments:
                    body += " | anexos: " + ", ".join(a.url for a in msg.attachments)
                if msg.embeds:
                    body += f" | embeds: {len(msg.embeds)}"
                transcript_lines.append(f"[{stamp}] {msg.author} ({msg.author.id}): {body}")
            transcript = "\n".join(transcript_lines)
            transcript_file = discord.File(io.BytesIO(transcript.encode("utf-8")),
                                           filename=f"ticket-{channel.id}-transcript.txt")
        except discord.HTTPException:
            transcript_file = None
        with sqlite3.connect(DB_PATH) as c:
            c.execute("UPDATE tickets SET status='closed',closed_by=?,closed_at=CURRENT_TIMESTAMP WHERE channel_id=?",
                      (interaction.user.id, channel.id))
        await interaction.response.send_message("🔒 Ticket fechado. Estou registrando o histórico.")
        log_id = int(config_value("ticket_logs_channel_id", "0") or 0)
        log_channel = interaction.guild.get_channel(log_id) if log_id else None
        if isinstance(log_channel, discord.TextChannel):
            log_embed = embed("🔒 Ticket fechado — " + SHOP_NAME,
                f"**Categoria:** {row[2]}\n**Cliente:** <@{row[0]}> (`{row[0]}`)\n"
                f"**Fechado por:** {interaction.user.mention}\n**Canal original:** `#{channel.name}` (`{channel.id}`)",
                color=discord.Color.blue())
            try:
                if transcript_file:
                    await log_channel.send(embed=log_embed, file=transcript_file)
                else:
                    await log_channel.send(embed=log_embed.set_footer(text="Transcrição indisponível."))
            except discord.HTTPException:
                logging.exception("Não foi possível enviar o log/transcrição do ticket")
        opener = interaction.guild.get_member(row[0])
        if opener and config_value("ticket_reviews_enabled", "1") == "1":
            try:
                await opener.send(embed=embed("⭐ Avalie seu atendimento — " + SHOP_NAME,
                    "Como foi sua experiência? Escolha uma nota de 1 a 5 estrelas.", color=discord.Color.blue()),
                    view=TicketRatingView(interaction.guild.id, channel.id, row[0]))
            except discord.Forbidden:
                pass
        try:
            await channel.edit(name=("fechado-" + channel.name)[:100])
            await channel.set_permissions(interaction.guild.default_role, view_channel=False)
        except discord.HTTPException:
            pass


class ConfigChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, config_key: str, label: str, channel_types):
        super().__init__(placeholder=label, channel_types=channel_types, min_values=1, max_values=1)
        self.config_key = config_key

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        save_config(self.config_key, selected.id)
        await interaction.response.send_message(f"✅ Configuração salva: {selected.mention}", ephemeral=True)


class ConfigRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Escolher cargo da equipe de suporte", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        global TICKET_SUPPORT_ROLE_ID
        role = self.values[0]
        TICKET_SUPPORT_ROLE_ID = role.id
        save_config("ticket_support_role_id", role.id)
        await interaction.response.send_message(f"✅ Equipe de suporte definida: {role.mention}", ephemeral=True)


class TicketConfigView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Categoria dos tickets", emoji="📁", style=discord.ButtonStyle.primary, row=0)
    async def category(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=180)
        view.add_item(ConfigChannelSelect("ticket_category_id", "Selecionar categoria", [discord.ChannelType.category]))
        await interaction.response.send_message("📁 Escolha a categoria onde os tickets serão criados:", view=view, ephemeral=True)

    @discord.ui.button(label="Equipe de suporte", emoji="👥", style=discord.ButtonStyle.primary, row=0)
    async def support(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=180)
        view.add_item(ConfigRoleSelect())
        await interaction.response.send_message("👥 Escolha o cargo da equipe:", view=view, ephemeral=True)

    @discord.ui.button(label="Canal de logs", emoji="📋", style=discord.ButtonStyle.primary, row=1)
    async def logs(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=180)
        view.add_item(ConfigChannelSelect("ticket_logs_channel_id", "Selecionar canal de logs", [discord.ChannelType.text]))
        await interaction.response.send_message("📋 Os fechamentos e transcrições serão enviados aqui:", view=view, ephemeral=True)

    @discord.ui.button(label="Canal de avaliações", emoji="⭐", style=discord.ButtonStyle.primary, row=1)
    async def reviews(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=180)
        view.add_item(ConfigChannelSelect("ticket_reviews_channel_id", "Selecionar canal de avaliações", [discord.ChannelType.text]))
        await interaction.response.send_message("⭐ Escolha onde publicar as avaliações:", view=view, ephemeral=True)

    @discord.ui.button(label="Ativar avaliações", emoji="✅", style=discord.ButtonStyle.success, row=2)
    async def enable_reviews(self, interaction: discord.Interaction, button: discord.ui.Button):
        save_config("ticket_reviews_enabled", "1")
        await interaction.response.send_message("✅ Avaliações ativadas.", ephemeral=True)

    @discord.ui.button(label="Desativar avaliações", emoji="⛔", style=discord.ButtonStyle.secondary, row=2)
    async def disable_reviews(self, interaction: discord.Interaction, button: discord.ui.Button):
        save_config("ticket_reviews_enabled", "0")
        await interaction.response.send_message("Avaliações desativadas.", ephemeral=True)

    @discord.ui.button(label="Publicar painel", emoji="🎫", style=discord.ButtonStyle.primary, row=3)
    async def publish_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.channel.send(embed=embed(
            "🎫 Central de Atendimento — " + SHOP_NAME,
            "Precisa de ajuda? Clique em **Atendimento** para ver as 8 categorias disponíveis.",
            color=discord.Color.blue()), view=TicketPanel())
        await interaction.response.send_message("✅ Painel de atendimento publicado.", ephemeral=True)

    @discord.ui.button(label="Estatísticas", emoji="📊", style=discord.ButtonStyle.primary, row=3)
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        with sqlite3.connect(DB_PATH) as c:
            opened, closed = c.execute("SELECT SUM(status='open'),SUM(status='closed') FROM tickets WHERE guild_id=?",
                                       (interaction.guild_id,)).fetchone()
            reviews, avg = c.execute("SELECT COUNT(*),AVG(rating) FROM ticket_reviews WHERE guild_id=?",
                                     (interaction.guild_id,)).fetchone()
        e = embed("📊 Estatísticas de tickets", f"🟢 Abertos: **{opened or 0}**\n🔒 Fechados: **{closed or 0}**\n"
                  f"⭐ Avaliações: **{reviews or 0}**\n📈 Média: **{f'{avg:.2f}/5' if avg is not None else 'Sem avaliações'}**",
                  color=discord.Color.blue())
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="Limite de tickets", emoji="🧮", style=discord.ButtonStyle.secondary, row=4)
    async def limits(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = config_value("ticket_limit_per_user", "1")
        await interaction.response.send_message(
            f"O limite atual é **{current} ticket(s) aberto(s) por usuário**. No momento, o sistema usa limite 1 para evitar tickets duplicados.",
            ephemeral=True)

    @discord.ui.button(label="Mensagens e segurança", emoji="🔒", style=discord.ButtonStyle.secondary, row=4)
    async def safety(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "🔒 Os tickets são privados por padrão. O cliente e o cargo de suporte configurado podem ver o canal; a equipe autorizada ou quem abriu pode fechá-lo.\n\n"
            "As mensagens de abertura/fechamento podem ser personalizadas em uma próxima etapa.", ephemeral=True)


@bot.event
async def on_ready():
    global SHOP_NAME, SHOP_COLOR, TICKET_CATEGORY_ID, TICKET_SUPPORT_ROLE_ID
    db_init()
    with sqlite3.connect(DB_PATH) as c:
        saved = dict(c.execute("SELECT key,value FROM shop_config").fetchall())
    SHOP_NAME = saved.get("shop_name", SHOP_NAME)
    SHOP_COLOR = saved.get("shop_color", SHOP_COLOR)
    TICKET_CATEGORY_ID = int(saved.get("ticket_category_id", TICKET_CATEGORY_ID) or 0)
    TICKET_SUPPORT_ROLE_ID = int(saved.get("ticket_support_role_id", TICKET_SUPPORT_ROLE_ID) or 0)
    bot.add_view(TicketPanel())
    bot.add_view(CloseTicket())
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
    except Exception:
        logging.exception("Falha ao sincronizar comandos")
    logging.info("Conectado como %s", bot.user)


@bot.event
async def on_member_join(member):
    channel = member.guild.get_channel(WELCOME_CHANNEL_ID) if WELCOME_CHANNEL_ID else None
    if isinstance(channel, discord.TextChannel):
        e = embed(f"👋 Bem-vindo à {SHOP_NAME}!",
                  f"Olá {member.mention}! Leia as regras e procure a equipe se precisar de ajuda.")
        e.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=e)


@bot.event
async def on_member_remove(member):
    channel = member.guild.get_channel(WELCOME_CHANNEL_ID) if WELCOME_CHANNEL_ID else None
    if isinstance(channel, discord.TextChannel):
        await channel.send(embed=embed(
            "🚪 Membro saiu",
            f"**{discord.utils.escape_markdown(member.display_name)}** saiu do servidor."
        ))


@bot.tree.command(name="tickets", description="Mostra o estado dos tickets.")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def tickets(interaction: discord.Interaction):
    with sqlite3.connect(DB_PATH) as c:
        opened = c.execute(
            "SELECT COUNT(*) FROM tickets WHERE guild_id=? AND status='open'",
            (interaction.guild_id,)
        ).fetchone()[0]
        closed = c.execute(
            "SELECT COUNT(*) FROM tickets WHERE guild_id=? AND status='closed'",
            (interaction.guild_id,)
        ).fetchone()[0]
    await interaction.response.send_message(
        embed=embed("🎫 Tickets", f"Abertos: **{opened}**\nFechados: **{closed}**"),
        ephemeral=True
    )


config_group = app_commands.Group(name="config", description="Configura os sistemas da ZORDX STORE")


@config_group.command(name="tickets", description="Abre o painel completo de configuração de tickets.")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def config_tickets_panel(interaction: discord.Interaction):
    e = embed("⚙️ Gerenciamento de Tickets — " + SHOP_NAME,
              "Configure categoria, equipe, logs, transcrições, avaliações, painel e estatísticas.\n\n"
              "Use os botões abaixo para ajustar cada opção.", color=discord.Color.blue())
    e.add_field(name="📋 Logs", value="Registros de abertura e fechamento + transcrição em TXT.", inline=False)
    e.add_field(name="⭐ Avaliações", value="Clientes podem dar notas de 1 a 5 estrelas após o fechamento.", inline=False)
    await interaction.response.send_message(embed=e, view=TicketConfigView(), ephemeral=True)


bot.tree.add_command(config_group)


@bot.tree.command(name="config_tickets", description="Atalho antigo para configurar categoria e equipe.")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def config_tickets_legacy(interaction: discord.Interaction):
    e = embed("⚙️ Gerenciamento de Tickets — " + SHOP_NAME,
              "Use os botões abaixo para configurar o atendimento.", color=discord.Color.blue())
    await interaction.response.send_message(embed=e, view=TicketConfigView(), ephemeral=True)


@bot.tree.command(name="regras", description="Mostra as regras da loja.")
@app_commands.guild_only()
async def rules(interaction: discord.Interaction):
    text = (
        "**1.** Respeite membros e equipe.\n"
        "**2.** Confira produto, preço e condições antes de pagar.\n"
        "**3.** Use apenas os meios de pagamento oficiais.\n"
        "**4.** Nunca compartilhe senhas ou códigos.\n"
        "**5.** Use tickets para pedir suporte.\n"
        "**6.** Golpes e comprovantes falsos são proibidos.\n"
        "**7.** Não faça divulgação sem autorização.\n"
        "**8.** Trocas e reembolsos seguem a política da loja e a legislação aplicável.\n"
        "**9.** Spam, assédio e discriminação não são permitidos.\n"
        "**10.** Violações podem resultar em aviso, timeout, expulsão ou banimento."
    )
    await interaction.response.send_message(embed=embed("📜 Regras — " + SHOP_NAME, text))


@bot.tree.command(name="addestoque", description="Adiciona unidades ao estoque de um produto.")
@app_commands.guild_only()
@app_commands.check(staff_check)
@app_commands.describe(nome="Nome exato do produto", quantidade="Quantidade a adicionar")
async def addestoque(interaction: discord.Interaction, nome: str,
                     quantidade: app_commands.Range[int, 1, 1000000]):
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT stock FROM shop_products WHERE name=? COLLATE NOCASE",
                        (nome.strip(),)).fetchone()
        if not row:
            return await interaction.response.send_message(
                "Produto não encontrado no cadastro. Confira o cadastro existente ou os dados da loja.", ephemeral=True
            )
        new_stock = int(row[0]) + int(quantidade)
        c.execute("UPDATE shop_products SET stock=? WHERE name=? COLLATE NOCASE",
                  (new_stock, nome.strip()))
    await interaction.response.send_message(
        f"📦 Estoque de **{discord.utils.escape_markdown(nome)}** atualizado: **{new_stock}** unidade(s).",
        ephemeral=True
    )


class EmbedFieldModal(discord.ui.Modal, title="Adicionar campo ao Embed"):
    nome = discord.ui.TextInput(label="Nome do campo", max_length=256, required=True)
    valor = discord.ui.TextInput(label="Conteúdo do campo", style=discord.TextStyle.paragraph, max_length=1024, required=True)
    inline = discord.ui.TextInput(label="Em linha? (sim/não)", default="não", max_length=5, required=True)

    def __init__(self, editor):
        super().__init__()
        self.editor = editor

    async def on_submit(self, interaction: discord.Interaction):
        if len(self.editor.data["fields"]) >= 25:
            return await interaction.response.send_message("Um embed aceita no máximo 25 campos.", ephemeral=True)
        inline = str(self.inline.value).strip().lower() in ("sim", "s", "yes", "y", "true", "1")
        self.editor.data["fields"].append({"name": str(self.nome.value), "value": str(self.valor.value), "inline": inline})
        await interaction.response.edit_message(content=f"✅ Campo adicionado ({len(self.editor.data['fields'])}/25).", embed=self.editor.make_embed(), view=self.editor)


class EmbedActionModal(discord.ui.Modal):
    def __init__(self, editor, action):
        self.editor, self.action = editor, action
        definitions = {
            "title": ("Editar título", [("Título", "title", 256, False)]),
            "description": ("Editar descrição", [("Descrição", "description", 4000, True)]),
            "color": ("Alterar cor", [("Cor hexadecimal", "color", 7, False)]),
            "title_url": ("URL do título", [("URL https://...", "title_url", 400, False)]),
            "author": ("Autor do Embed", [("Nome do autor", "author", 256, False), ("Ícone do autor (URL)", "author_icon", 400, False), ("Link do autor (URL)", "author_url", 400, False)]),
            "thumbnail": ("Miniatura", [("URL da miniatura", "thumbnail", 400, False)]),
            "image": ("Imagem principal", [("URL da imagem", "image", 400, False)]),
            "footer": ("Rodapé", [("Texto do rodapé", "footer", 2048, False), ("Ícone do rodapé (URL)", "footer_icon", 400, False)]),
            "field_remove": ("Remover campo", [("Número do campo", "field_index", 2, False)]),
        }
        title, fields = definitions[action]
        super().__init__(title=title)
        self.inputs = {}
        for label, key, max_length, paragraph in fields:
            item = discord.ui.TextInput(label=label, default=str(editor.data.get(key, ""))[:max_length], max_length=max_length,
                required=(key == "field_index"), style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short)
            self.inputs[key] = item
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        updated = {key: str(item.value).strip() for key, item in self.inputs.items()}
        if self.action == "color":
            raw = updated["color"].lstrip("#")
            try:
                if len(raw) != 6: raise ValueError
                int(raw, 16)
            except ValueError:
                return await interaction.response.send_message("Cor inválida. Use um código hexadecimal como #2563EB.", ephemeral=True)
            updated["color"] = "#" + raw.upper()
        if self.action == "field_remove":
            try: index = int(updated["field_index"]) - 1
            except ValueError: index = -1
            if index < 0 or index >= len(self.editor.data["fields"]):
                return await interaction.response.send_message("Número inválido. Veja a lista de campos e tente novamente.", ephemeral=True)
            removed = self.editor.data["fields"].pop(index)
            await interaction.response.edit_message(content=f"🗑️ Campo **{removed['name']}** removido.", embed=self.editor.make_embed(), view=self.editor)
            return
        self.editor.data.update(updated)
        await interaction.response.edit_message(content="💙 Alterações aplicadas. Confira a prévia antes de publicar.", embed=self.editor.make_embed(), view=self.editor)


class EmbedDesignerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)
        self.data = {"title": "", "description": "", "color": "#2563EB", "title_url": "", "author": "",
                     "author_icon": "", "author_url": "", "thumbnail": "", "image": "",
                     "footer": f"{SHOP_NAME} • ZORDX STORE", "footer_icon": "", "timestamp": False, "fields": []}

    def make_embed(self):
        raw = str(self.data.get("color", "#2563EB")).lstrip("#")
        try: color = discord.Color(int(raw, 16))
        except ValueError: color = discord.Color.blue()
        card = discord.Embed(title=self.data.get("title") or None, description=self.data.get("description") or None,
                             color=color, url=self.data.get("title_url") or None,
                             timestamp=datetime.now(timezone.utc) if self.data.get("timestamp") else None)
        if self.data.get("author"):
            card.set_author(name=self.data["author"], icon_url=self.data.get("author_icon") or None,
                            url=self.data.get("author_url") or None)
        if self.data.get("thumbnail"): card.set_thumbnail(url=self.data["thumbnail"])
        if self.data.get("image"): card.set_image(url=self.data["image"])
        if self.data.get("footer") or self.data.get("footer_icon"):
            card.set_footer(text=self.data.get("footer") or "\u200b", icon_url=self.data.get("footer_icon") or None)
        for item in self.data.get("fields", [])[:25]:
            card.add_field(name=item["name"], value=item["value"], inline=item.get("inline", False))
        return card

    async def edit_action(self, interaction, action):
        await interaction.response.send_modal(EmbedActionModal(self, action))

    @discord.ui.button(label="Título", emoji="📝", style=discord.ButtonStyle.primary, row=0)
    async def title_button(self, interaction, button): await self.edit_action(interaction, "title")
    @discord.ui.button(label="Descrição", emoji="📄", style=discord.ButtonStyle.primary, row=0)
    async def description_button(self, interaction, button): await self.edit_action(interaction, "description")
    @discord.ui.button(label="Cor", emoji="🎨", style=discord.ButtonStyle.primary, row=0)
    async def color_button(self, interaction, button): await self.edit_action(interaction, "color")
    @discord.ui.button(label="URL do título", emoji="🔗", style=discord.ButtonStyle.secondary, row=0)
    async def title_url_button(self, interaction, button): await self.edit_action(interaction, "title_url")
    @discord.ui.button(label="Autor", emoji="✍️", style=discord.ButtonStyle.secondary, row=1)
    async def author_button(self, interaction, button): await self.edit_action(interaction, "author")
    @discord.ui.button(label="Miniatura", emoji="🖼️", style=discord.ButtonStyle.secondary, row=1)
    async def thumbnail_button(self, interaction, button): await self.edit_action(interaction, "thumbnail")
    @discord.ui.button(label="Imagem principal", emoji="🌄", style=discord.ButtonStyle.secondary, row=1)
    async def image_button(self, interaction, button): await self.edit_action(interaction, "image")
    @discord.ui.button(label="Rodapé", emoji="🔻", style=discord.ButtonStyle.secondary, row=1)
    async def footer_button(self, interaction, button): await self.edit_action(interaction, "footer")
    @discord.ui.button(label="Adicionar campo", emoji="➕", style=discord.ButtonStyle.primary, row=2)
    async def add_field_button(self, interaction, button): await interaction.response.send_modal(EmbedFieldModal(self))
    @discord.ui.button(label="Remover campo", emoji="➖", style=discord.ButtonStyle.secondary, row=2)
    async def remove_field_button(self, interaction, button):
        if not self.data["fields"]: return await interaction.response.send_message("Este embed ainda não tem campos.", ephemeral=True)
        listing = "\n".join(f"**{i+1}.** {f['name']}" for i, f in enumerate(self.data["fields"]))
        await interaction.response.send_message(f"**Campos atuais**\n{listing}\nDigite o número para remover:", view=EmbedRemoveFieldView(self), ephemeral=True)
    @discord.ui.button(label="Data/hora", emoji="🕒", style=discord.ButtonStyle.secondary, row=2)
    async def timestamp_button(self, interaction, button):
        self.data["timestamp"] = not self.data.get("timestamp", False)
        await interaction.response.edit_message(content=f"🕒 Data/hora: **{'ativada' if self.data['timestamp'] else 'desativada'}**.", embed=self.make_embed(), view=self)
    @discord.ui.button(label="Prévia", emoji="👁️", style=discord.ButtonStyle.success, row=3)
    async def preview_button(self, interaction, button):
        await interaction.response.edit_message(content="👁️ **Prévia do Embed**", embed=self.make_embed(), view=self)
    @discord.ui.button(label="Publicar", emoji="📤", style=discord.ButtonStyle.success, row=3)
    async def publish_button(self, interaction, button):
        if not self.data.get("title") and not self.data.get("description") and not self.data.get("fields"):
            return await interaction.response.send_message("Preencha ao menos título, descrição ou um campo antes de publicar.", ephemeral=True)
        await interaction.response.send_message("Selecione o canal onde deseja publicar o embed:", view=EmbedPublishView(self), ephemeral=True)


class EmbedRemoveFieldView(discord.ui.View):
    def __init__(self, editor): super().__init__(timeout=120); self.editor = editor
    @discord.ui.button(label="Escolher número", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def remove(self, interaction, button): await interaction.response.send_modal(EmbedActionModal(self.editor, "field_remove"))


class EmbedPublishView(discord.ui.View):
    def __init__(self, editor): super().__init__(timeout=120); self.add_item(EmbedPublishChannelSelect(editor))


class EmbedPublishChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, editor): super().__init__(placeholder="Escolha o canal de destino", channel_types=[discord.ChannelType.text], min_values=1, max_values=1); self.editor = editor
    async def callback(self, interaction):
        target = self.values[0]
        try: await target.send(embed=self.editor.make_embed())
        except discord.Forbidden: return await interaction.response.send_message("Não tenho permissão para publicar nesse canal.", ephemeral=True)
        except discord.HTTPException: return await interaction.response.send_message("O Discord recusou o embed. Confira URLs e limites dos campos.", ephemeral=True)
        await interaction.response.send_message(f"✅ Embed publicado em {target.mention}.", ephemeral=True)


@bot.tree.command(name="embed", description="Abre o novo editor interativo de embeds.")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def embed_interativo(interaction: discord.Interaction):
    view = EmbedDesignerView()
    await interaction.response.send_message("💙 **Editor de Embed — ZORDX STORE**\nEdite o conteúdo e confira a prévia antes de publicar.",
                                            embed=view.make_embed(), view=view, ephemeral=True)


@bot.tree.command(name="embed_criar", description="Cria e publica um embed personalizado.")
@app_commands.guild_only()
@app_commands.check(staff_check)
@app_commands.describe(titulo="Título do embed", descricao="Texto do embed",
                       canal="Canal onde publicar (opcional)", cor="Cor hexadecimal, exemplo #ff0000")
async def embed_criar(interaction: discord.Interaction, titulo: str, descricao: str,
                      canal: discord.TextChannel = None, cor: str = "#5865F2"):
    try:
        raw = cor.strip().lstrip("#")
        if len(raw) != 6:
            raise ValueError
        color = discord.Color(int(raw, 16))
    except ValueError:
        return await interaction.response.send_message(
            "Cor inválida. Use formato hexadecimal como #ff0000.", ephemeral=True
        )
    target = canal or interaction.channel
    await target.send(embed=embed(titulo, descricao, color))
    await interaction.response.send_message(f"Embed publicado em {target.mention}.", ephemeral=True)


@bot.tree.command(name="addpainel", description="Publica o painel de produtos da loja.")
@app_commands.guild_only()
@app_commands.check(staff_check)
@app_commands.describe(canal="Canal onde publicar (opcional)")
async def addpainel(interaction: discord.Interaction, canal: discord.TextChannel = None):
    target = canal or interaction.channel
    e = embed(
        f"🛍️ {SHOP_NAME} | Painel de Vendas",
        "Confira as novidades e procure a equipe pelo sistema de tickets quando precisar de atendimento.\n\n"
        "💙 Obrigado por escolher a ZORDX STORE!",
        color=discord.Color.blue()
    )
    e.set_footer(text="ZORDX STORE • Painel de vendas")
    await target.send(embed=e)
    await interaction.response.send_message(f"Painel publicado em {target.mention}.", ephemeral=True)


@bot.tree.command(name="confi_loja", description="Configura nome e apresentação da loja.")
@app_commands.guild_only()
@app_commands.check(staff_check)
@app_commands.describe(nome="Nome da loja", cor="Cor hexadecimal, exemplo #ff0000")
async def confi_loja(interaction: discord.Interaction, nome: str, cor: str = "#5865F2"):
    try:
        raw = cor.strip().lstrip("#")
        if len(raw) != 6:
            raise ValueError
        int(raw, 16)
    except ValueError:
        return await interaction.response.send_message("Cor inválida. Use #ff0000, por exemplo.", ephemeral=True)
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT OR REPLACE INTO shop_config(key,value) VALUES('shop_name',?)", (nome[:100],))
        c.execute("INSERT OR REPLACE INTO shop_config(key,value) VALUES('shop_color',?)", (raw.lower(),))
    global SHOP_NAME, SHOP_COLOR
    SHOP_NAME = nome[:100]
    SHOP_COLOR = raw.lower()
    await interaction.response.send_message(
        f"Configuração salva: **{discord.utils.escape_markdown(nome[:100])}**. "
        "O nome e a cor serão mantidos após reiniciar o bot.",
        ephemeral=True
    )


cupom_group = app_commands.Group(name="cupom", description="Gerencia os cupons da loja")


@cupom_group.command(name="criar", description="Cria um cupom de desconto.")
@app_commands.guild_only()
@app_commands.check(staff_check)
@app_commands.describe(codigo="Código do cupom", desconto="Desconto percentual de 1 a 90",
                       usos="Quantidade de usos disponíveis")
async def cupom_criar(interaction: discord.Interaction, codigo: str,
                      desconto: app_commands.Range[int, 1, 90],
                      usos: app_commands.Range[int, 1, 100000] = 1):
    code = codigo.strip().upper()
    if not code or len(code) > 32 or not code.replace("-", "").replace("_", "").isalnum():
        return await interaction.response.send_message(
            "Código inválido. Use letras, números, hífen ou sublinhado (até 32 caracteres).",
            ephemeral=True
        )
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR REPLACE INTO coupons(code,percent,uses_left,created_by) VALUES(?,?,?,?)",
            (code, int(desconto), int(usos), interaction.user.id)
        )
    await interaction.response.send_message(
        f"🎟️ Cupom **{code}** criado: **{desconto}%** de desconto, **{usos}** uso(s).",
        ephemeral=True
    )


@cupom_group.command(name="listar", description="Lista os cupons cadastrados.")
@app_commands.guild_only()
async def cupom_listar(interaction: discord.Interaction):
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT code,percent,uses_left FROM coupons ORDER BY code LIMIT 25").fetchall()
    if not rows:
        return await interaction.response.send_message("🎟️ Ainda não há cupons cadastrados.", ephemeral=True)
    e = embed("🎟️ Cupons — " + SHOP_NAME, "Cupons cadastrados e usos restantes:", color=discord.Color.blue())
    for code, percent, uses in rows:
        e.add_field(name=f"{code} — {percent}%", value=f"Usos restantes: **{uses}**", inline=True)
    await interaction.response.send_message(embed=e, ephemeral=True)


bot.tree.add_command(cupom_group)


@bot.tree.command(name="nick", description="Altera o apelido de um membro.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_nicknames=True)
@app_commands.describe(membro="Membro", apelido="Novo apelido; deixe vazio para remover")
async def nick(interaction: discord.Interaction, membro: discord.Member, apelido: str = ""):
    if membro.id == interaction.guild.owner_id:
        return await interaction.response.send_message("Não posso alterar o apelido do dono do servidor.", ephemeral=True)
    if interaction.guild.owner_id != interaction.user.id and membro.top_role >= interaction.user.top_role:
        return await interaction.response.send_message("O cargo desse membro é igual ou superior ao seu.", ephemeral=True)
    try:
        await membro.edit(nick=apelido[:32] or None, reason=f"Alterado por {interaction.user}")
        await interaction.response.send_message(
            f"Apelido de {membro.mention} atualizado.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message("O bot não tem permissão ou cargo suficiente.", ephemeral=True)


@bot.tree.command(name="lock", description="Bloqueia ou desbloqueia mensagens no canal.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.describe(acao="Bloquear ou desbloquear mensagens")
@app_commands.choices(acao=[
    app_commands.Choice(name="bloquear", value="bloquear"),
    app_commands.Choice(name="desbloquear", value="desbloquear")
])
async def lock(interaction: discord.Interaction, acao: app_commands.Choice[str]):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("Use em um canal de texto.", ephemeral=True)
    try:
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False if acao.value == "bloquear" else None
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite,
                                      reason=f"Canal {acao.value} por {interaction.user}")
        message = "🔒 Canal bloqueado." if acao.value == "bloquear" else "🔓 Canal desbloqueado."
        await interaction.response.send_message(message)
    except discord.Forbidden:
        await interaction.response.send_message("O bot não tem permissão para editar o canal.", ephemeral=True)


@bot.tree.command(name="limpar", description="Apaga mensagens recentes deste canal.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(quantidade="Quantidade de mensagens (1 a 100)")
async def limpar(interaction: discord.Interaction, quantidade: app_commands.Range[int, 1, 100]):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Use em um canal de texto.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        deleted = await interaction.channel.purge(limit=int(quantidade))
        await interaction.followup.send(f"🧹 Apaguei {len(deleted)} mensagem(ns).", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("O bot precisa da permissão Gerenciar Mensagens.", ephemeral=True)


@bot.tree.command(name="limpar_canal", description="Apaga o histórico recriando o canal.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True, manage_messages=True)
@app_commands.describe(confirmar="Digite CONFIRMAR para recriar este canal")
async def limpar_canal(interaction: discord.Interaction, confirmar: str):
    if confirmar != "CONFIRMAR":
        return await interaction.response.send_message(
            "Ação cancelada. Para confirmar, use exatamente `CONFIRMAR`.", ephemeral=True
        )
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("Use em um canal de texto.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        new_channel = await channel.clone(reason=f"Canal limpo por {interaction.user}")
        await new_channel.edit(position=channel.position)
        await channel.delete(reason=f"Canal limpo por {interaction.user}")
        await new_channel.send(f"🧹 Canal recriado por {interaction.user.mention}. O histórico anterior foi removido.")
        await interaction.followup.send(f"Canal recriado: {new_channel.mention}", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("O bot precisa de permissões para gerenciar canais.", ephemeral=True)


@bot.tree.command(name="perfil", description="Mostra seu perfil ou o de outro membro.")
@app_commands.guild_only()
@app_commands.describe(membro="Membro opcional")
async def perfil(interaction: discord.Interaction, membro: discord.Member = None):
    member = membro or interaction.user
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT bio,points FROM member_profiles WHERE guild_id=? AND user_id=?",
            (interaction.guild_id, member.id)
        ).fetchone()
        rank_row = c.execute(
            "SELECT rank FROM member_ranks WHERE guild_id=? AND user_id=?",
            (interaction.guild_id, member.id)
        ).fetchone()
    bio = row[0] if row else "Ainda sem biografia."
    points = row[1] if row else 0
    rank = rank_row[0] if rank_row else "Sem rank definido"
    e = embed(f"👤 Perfil de {member.display_name}", bio)
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="Rank", value=rank, inline=True)
    e.add_field(name="Pontos", value=str(points), inline=True)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="rankset", description="Define o rank de um membro.")
@app_commands.guild_only()
@app_commands.check(staff_check)
@app_commands.describe(membro="Membro", rank="Nome do rank")
async def rankset(interaction: discord.Interaction, membro: discord.Member, rank: str):
    rank = rank.strip()[:80]
    if not rank:
        return await interaction.response.send_message("Informe um rank.", ephemeral=True)
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR REPLACE INTO member_ranks(guild_id,user_id,rank,updated_by) VALUES(?,?,?,?)",
            (interaction.guild_id, membro.id, rank, interaction.user.id)
        )
    await interaction.response.send_message(
        f"🏅 Rank de {membro.mention} definido como **{discord.utils.escape_markdown(rank)}**.",
        ephemeral=True
    )


@bot.tree.command(name="registrar_venda", description="Registra uma venda para o relatório de rendimentos.")
@app_commands.guild_only()
@app_commands.check(staff_check)
@app_commands.describe(produto="Nome do produto", valor="Valor total recebido em reais",
                       quantidade="Quantidade vendida")
async def registrar_venda(interaction: discord.Interaction, produto: str,
                           valor: app_commands.Range[float, 0.01, 100000000.0],
                           quantidade: app_commands.Range[int, 1, 1000000] = 1):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT INTO sales(product_name,amount,quantity,seller_id) VALUES(?,?,?,?)",
            (produto[:100], float(valor), int(quantidade), interaction.user.id)
        )
        c.execute(
            "UPDATE shop_products SET stock=MAX(0,stock-?) WHERE name=? COLLATE NOCASE",
            (int(quantidade), produto.strip())
        )
    await interaction.response.send_message(
        f"✅ Venda registrada: **{discord.utils.escape_markdown(produto)}**, "
        f"{quantidade} unidade(s), total {money(valor)}.", ephemeral=True
    )


@bot.tree.command(name="rendimentos", description="Mostra os rendimentos registrados da loja.")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def rendimentos(interaction: discord.Interaction):
    with sqlite3.connect(DB_PATH) as c:
        total, sales_count, units = c.execute(
            "SELECT COALESCE(SUM(amount),0),COUNT(*),COALESCE(SUM(quantity),0) FROM sales"
        ).fetchone()
        rows = c.execute(
            "SELECT product_name,SUM(amount),SUM(quantity) FROM sales "
            "GROUP BY product_name ORDER BY SUM(amount) DESC LIMIT 10"
        ).fetchall()
    description = (
        f"**Total registrado:** {money(total)}\n"
        f"**Registros de venda:** {sales_count}\n"
        f"**Unidades vendidas:** {units}\n\n"
        "Esses valores consideram somente vendas registradas com `/registrar_venda`."
    )
    e = embed("📈 Rendimentos — " + SHOP_NAME, description)
    for name, amount, qty in rows:
        e.add_field(name=str(name)[:256], value=f"{money(amount)} • {qty} unidade(s)", inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="aviso", description="Publica um aviso da loja.")
@app_commands.describe(titulo="Título", mensagem="Texto do aviso")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def announce(interaction: discord.Interaction, titulo: str, mensagem: str):
    await interaction.channel.send(embed=embed("📢 " + titulo[:240], mensagem[:4000]))
    await interaction.response.send_message("Aviso publicado.", ephemeral=True)


@bot.tree.command(name="advertir", description="Registra advertência.")
@app_commands.describe(membro="Membro", motivo="Motivo")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def warn(interaction: discord.Interaction, membro: discord.Member, motivo: str):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT INTO warnings(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
            (interaction.guild_id, membro.id, interaction.user.id, motivo[:1000])
        )
        count = c.execute(
            "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?",
            (interaction.guild_id, membro.id)
        ).fetchone()[0]
    await interaction.response.send_message(
        f"⚠️ {membro.mention} advertido. Total: **{count}**. Motivo: {motivo[:1000]}"
    )



@bot.tree.command(
    name="advertencias",
    description="Consulta suas advertências ou as de outro membro."
)
@app_commands.guild_only()
@app_commands.describe(
    membro="Opcional: membro que deseja consultar (somente equipe)"
)
async def warnings(
    interaction: discord.Interaction,
    membro: discord.Member = None
):
    alvo = membro or interaction.user

    # Membros comuns só podem consultar as próprias advertências.
    if not staff(interaction) and alvo.id != interaction.user.id:
        return await interaction.response.send_message(
            "🔒 Você só pode consultar suas próprias advertências.",
            ephemeral=True
        )

    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT reason, moderator_id, created_at FROM warnings "
            "WHERE guild_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT 10",
            (interaction.guild_id, alvo.id)
        ).fetchall()

    if not rows:
        return await interaction.response.send_message(
            f"✅ Nenhuma advertência registrada para {alvo.mention}.",
            ephemeral=True
        )

    texto = "\n".join(
        f"• {data}: {motivo} (moderador <@{mod_id}>)"
        for motivo, mod_id, data in rows
    )

    await interaction.response.send_message(
        embed=embed(f"⚠️ Advertências — {alvo}", texto),
        ephemeral=True
    )
 WHERE guild_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT 10", (interaction.guild_id, membro.id)
        ).fetchall()
    if not rows:
        return await interaction.response.send_message("Sem advertências registradas.", ephemeral=True)
    text = "\n".join(f"• {date}: {reason} (moderador <@{mod_id}>)"
                     for reason, mod_id, date in rows)
    await interaction.response.send_message(
        embed=embed("Advertências — " + str(membro), text), ephemeral=True
    )


@bot.tree.command(name="timeout", description="Aplica timeout temporário.")
@app_commands.describe(membro="Membro", minutos="De 1 a 40320 minutos", motivo="Motivo")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def timeout_member(interaction: discord.Interaction, membro: discord.Member,
                         minutos: app_commands.Range[int, 1, 40320], motivo: str):
    if membro.top_role >= interaction.user.top_role and interaction.guild.owner_id != interaction.user.id:
        return await interaction.response.send_message("Cargo igual ou superior ao seu.", ephemeral=True)
    try:
        await membro.timeout(timedelta(minutes=minutos), reason=motivo[:500])
        await interaction.response.send_message(
            f"🔇 {membro.mention} recebeu timeout de {minutos} minuto(s). Motivo: {motivo}"
        )
    except discord.Forbidden:
        await interaction.response.send_message("Sem permissão para aplicar timeout.", ephemeral=True)


@bot.tree.command(name="expulsar", description="Expulsa um membro.")
@app_commands.describe(membro="Membro", motivo="Motivo")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def kick(interaction: discord.Interaction, membro: discord.Member, motivo: str):
    if membro.id == interaction.user.id or membro.id == interaction.guild.owner_id:
        return await interaction.response.send_message("Não é possível expulsar essa pessoa.", ephemeral=True)
    if membro.top_role >= interaction.user.top_role and interaction.guild.owner_id != interaction.user.id:
        return await interaction.response.send_message("Cargo igual ou superior ao seu.", ephemeral=True)
    try:
        await membro.kick(reason=motivo[:500])
        await interaction.response.send_message(f"👢 {membro} foi expulso. Motivo: {motivo}")
    except discord.Forbidden:
        await interaction.response.send_message("Sem permissão para expulsar.", ephemeral=True)


@bot.tree.command(name="banir", description="Bane um membro.")
@app_commands.describe(membro="Membro", motivo="Motivo")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def ban(interaction: discord.Interaction, membro: discord.Member, motivo: str):
    if membro.id == interaction.user.id or membro.id == interaction.guild.owner_id:
        return await interaction.response.send_message("Não é possível banir essa pessoa.", ephemeral=True)
    if membro.top_role >= interaction.user.top_role and interaction.guild.owner_id != interaction.user.id:
        return await interaction.response.send_message("Cargo igual ou superior ao seu.", ephemeral=True)
    try:
        await membro.ban(reason=motivo[:500])
        await interaction.response.send_message(f"🔨 {membro} foi banido. Motivo: {motivo}")
    except discord.Forbidden:
        await interaction.response.send_message("Sem permissão para banir.", ephemeral=True)


@bot.tree.command(name="ping", description="Ver latência do bot")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! Latência: {round(bot.latency * 1000)} ms")


@bot.tree.command(name="ajuda", description="Ver os comandos do bot")
async def ajuda(interaction: discord.Interaction):
    text = (
        "🤖 **ZORDX STORE BOT**\n"
        "`/ping` — Latência\n`/ajuda` — Ajuda\n`/regras` — Regras\n"
        "`/config_ticket` — Configuração de tickets\n"
        "`/embed` — Editor interativo de embeds; `/embed_criar` — Publicar embed simples\n"
        "`/cupom criar` e `/cupom listar` — Cupons\n"
        "`/ajuda-vendas`, `/anunciar` — Vendas e anúncios\n"
        "`/confi_loja` — Configurar apresentação da loja\n"
        "`/limpar`, `/limpar_canal`, `/lock`, `/nick` — Moderação\n"
        "`/perfil`, `/rankset` — Perfil e rank\n"
        "`/registrar_venda`, `/rendimentos` — Vendas e rendimentos\n"
        "`/aviso`, `/advertir`, `/advertencias`, `/timeout`, `/expulsar`, `/banir` — Equipe"
    )
    await interaction.response.send_message(text, ephemeral=True)


@bot.tree.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logging.error("Erro de comando: %r", error)
    if isinstance(error, app_commands.MissingPermissions):
        message = "Você não tem as permissões necessárias para esse comando."
    elif isinstance(error, app_commands.CheckFailure):
        message = "Comando restrito à equipe ou sem permissão necessária."
    else:
        message = "Não foi possível executar. Confira as permissões do bot e tente novamente."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass



# ═══════════════════════════════════════════════════════════════
# NOVO PAINEL INTERATIVO DE TICKETS — ZORDX STORE (tema azul)
# Configurações de painéis são guardadas em SQLite para persistirem.
# ═══════════════════════════════════════════════════════════════

def ticket_panels_load(guild_id: int):
    raw = config_value(f"ticket_panels_{guild_id}", "[]")
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def ticket_panels_save(guild_id: int, panels):
    save_config(f"ticket_panels_{guild_id}", json.dumps(panels, ensure_ascii=False))


def panel_embed(data):
    raw = str(data.get("color", "2563eb")).lstrip("#")
    try:
        color = discord.Color(int(raw, 16))
    except ValueError:
        color = discord.Color.blue()
    result = discord.Embed(
        title=(data.get("title") or "🎫 Sistema de Tickets")[:256],
        description=(data.get("description") or "Selecione abaixo como podemos ajudar.")[:4000],
        color=color,
        url=(data.get("title_url") or None)
    )
    if data.get("author"):
        result.set_author(name=data["author"][:256], icon_url=data.get("author_icon") or None,
                          url=data.get("author_url") or None)
    if data.get("thumbnail"):
        result.set_thumbnail(url=data["thumbnail"])
    if data.get("image"):
        result.set_image(url=data["image"])
    if data.get("footer") or data.get("footer_icon"):
        result.set_footer(text=(data.get("footer") or "ZORDX STORE")[:2048],
                          icon_url=data.get("footer_icon") or None)
    if data.get("timestamp"):
        result.timestamp = datetime.now(timezone.utc)
    for field in data.get("fields", [])[:25]:
        if field.get("name") and field.get("value"):
            result.add_field(name=field["name"][:256], value=field["value"][:1024], inline=bool(field.get("inline", False)))
    return result


class TicketPanelEditModal(discord.ui.Modal, title="Editor do Painel em Embed"):
    titulo = discord.ui.TextInput(label="Título", max_length=256, required=True)
    descricao = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, max_length=3500, required=True)
    cor = discord.ui.TextInput(label="Cor hexadecimal", default="#2563EB", max_length=7, required=True)
    rodape = discord.ui.TextInput(label="Texto do rodapé (opcional)", max_length=200, required=False)

    def __init__(self, guild_id, panel_index=None):
        super().__init__()
        self.guild_id = guild_id
        self.panel_index = panel_index
        panels = ticket_panels_load(guild_id)
        data = panels[panel_index] if panel_index is not None and panel_index < len(panels) else {}
        self.titulo.default = data.get("title", "🎫 Sistema de Tickets")
        self.descricao.default = data.get("description", "Selecione abaixo como podemos ajudar.")
        self.cor.default = "#" + data.get("color", "2563eb").lstrip("#")
        self.rodape.default = data.get("footer", "ZORDX STORE • Atendimento")

    async def on_submit(self, interaction: discord.Interaction):
        color = str(self.cor.value).strip().lstrip("#")
        try:
            if len(color) != 6:
                raise ValueError
            int(color, 16)
        except ValueError:
            return await interaction.response.send_message("Cor inválida. Use um código como #2563EB.", ephemeral=True)
        panels = ticket_panels_load(self.guild_id)
        data = {"title": str(self.titulo.value), "description": str(self.descricao.value),
                "color": color.lower(), "footer": str(self.rodape.value), "mode": "button",
                "options": [{"label": "Atendimento", "emoji": "🎫"}]}
        if self.panel_index is None:
            if len(panels) >= 5:
                return await interaction.response.send_message("Você já atingiu o limite de 5 painéis.", ephemeral=True)
            panels.append(data)
            self.panel_index = len(panels) - 1
        else:
            old = panels[self.panel_index]
            data.update({k: v for k, v in old.items() if k not in data})
            panels[self.panel_index] = data
        ticket_panels_save(self.guild_id, panels)
        view = TicketPanelEditorView(self.guild_id, self.panel_index)
        await interaction.response.send_message(
            "💙 **Painel salvo!** Use os botões para escolher como os membros abrirão o ticket, ver a prévia ou publicar.",
            embed=panel_embed(panels[self.panel_index]), view=view, ephemeral=True)


class TicketOpenButton(discord.ui.Button):
    def __init__(self, label, emoji="🎫"):
        super().__init__(label=label[:80], emoji=emoji if emoji else None,
                         style=discord.ButtonStyle.primary, custom_id=f"zordx:open:{label[:40]}")
        self.ticket_label = label

    async def callback(self, interaction: discord.Interaction):
        await create_ticket(interaction, self.ticket_label)


class TicketOpenSelect(discord.ui.Select):
    def __init__(self, options):
        choices = [discord.SelectOption(label=x.get("label", "Atendimento")[:100],
                    emoji=x.get("emoji") if x.get("emoji") else None,
                    description=x.get("description", "Abrir um ticket")[:100]) for x in options[:25]]
        super().__init__(placeholder="Selecione o tipo de atendimento...", min_values=1, max_values=1, options=choices)

    async def callback(self, interaction: discord.Interaction):
        await create_ticket(interaction, self.values[0])


class PublishedTicketPanelView(discord.ui.View):
    def __init__(self, data):
        super().__init__(timeout=None)
        options = data.get("options") or [{"label": "Atendimento", "emoji": "🎫"}]
        if data.get("mode") == "select":
            self.add_item(TicketOpenSelect(options))
        else:
            for opt in options[:25]:
                self.add_item(TicketOpenButton(opt.get("label", "Atendimento"), opt.get("emoji", "🎫")))


class TicketPanelOptionsModal(discord.ui.Modal, title="Gerenciar opções do painel"):
    options_text = discord.ui.TextInput(label="Uma opção por linha: nome | emoji | descrição", style=discord.TextStyle.paragraph, max_length=4000, required=True)
    def __init__(self, guild_id, panel_index):
        super().__init__(); self.guild_id, self.panel_index = guild_id, panel_index
        panels = ticket_panels_load(guild_id)
        data = panels[panel_index] if panel_index < len(panels) else {}
        current = data.get("options") or [{"label": "Atendimento", "emoji": "🎫", "description": "Abrir um ticket"}]
        self.options_text.default = "\n".join(f"{x.get('label','Atendimento')} | {x.get('emoji','🎫')} | {x.get('description','Abrir um ticket')}" for x in current)[:4000]
    async def on_submit(self, interaction):
        parsed = []
        for line in str(self.options_text.value).splitlines():
            line = line.strip()
            if not line: continue
            parts = [x.strip() for x in line.split("|", 2)]
            label = parts[0][:80]
            emoji = (parts[1] if len(parts) > 1 else "🎫")[:50]
            description = (parts[2] if len(parts) > 2 else f"Abrir ticket: {label}")[:100]
            if not label: continue
            parsed.append({"label": label, "emoji": emoji, "description": description})
        if not parsed: return await interaction.response.send_message("Informe pelo menos uma opção.", ephemeral=True)
        if len(parsed) > 25: return await interaction.response.send_message("O Discord permite no máximo 25 opções por painel.", ephemeral=True)
        panels = ticket_panels_load(self.guild_id)
        if self.panel_index >= len(panels): return await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
        panels[self.panel_index]["options"] = parsed
        ticket_panels_save(self.guild_id, panels)
        await interaction.response.send_message("✅ Opções salvas. Cada linha foi interpretada como nome | emoji | descrição.", embed=panel_embed(panels[self.panel_index]), view=TicketPanelEditorView(self.guild_id, self.panel_index), ephemeral=True)


class TicketPanelEditorView(discord.ui.View):
    def __init__(self, guild_id, panel_index):
        super().__init__(timeout=600)
        self.guild_id, self.panel_index = guild_id, panel_index

    def current(self):
        panels = ticket_panels_load(self.guild_id)
        return panels[self.panel_index] if self.panel_index < len(panels) else None

    @discord.ui.button(label="Editar Embed", emoji="📝", style=discord.ButtonStyle.primary, row=0)
    async def edit_embed(self, interaction, button):
        await interaction.response.send_modal(TicketPanelEditModal(self.guild_id, self.panel_index))

    @discord.ui.button(label="Botão", emoji="🔘", style=discord.ButtonStyle.primary, row=0)
    async def mode_button(self, interaction, button):
        data = self.current()
        if not data: return await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
        data["mode"] = "button"
        ticket_panels_save(self.guild_id, ticket_panels_load(self.guild_id))
        panels = ticket_panels_load(self.guild_id); panels[self.panel_index] = data; ticket_panels_save(self.guild_id, panels)
        await interaction.response.edit_message(content="Modo definido: **Botões**.", embed=panel_embed(data), view=self)

    @discord.ui.button(label="Menu de seleção", emoji="📋", style=discord.ButtonStyle.primary, row=0)
    async def mode_select(self, interaction, button):
        panels = ticket_panels_load(self.guild_id); data = panels[self.panel_index]
        data["mode"] = "select"; ticket_panels_save(self.guild_id, panels)
        await interaction.response.edit_message(content="Modo definido: **Menu de seleção**. Para publicar, use Enviar Painel.", embed=panel_embed(data), view=self)

    @discord.ui.button(label="Gerenciar opções", emoji="🧩", style=discord.ButtonStyle.primary, row=1)
    async def manage_options(self, interaction, button):
        await interaction.response.send_modal(TicketPanelOptionsModal(self.guild_id, self.panel_index))

    @discord.ui.button(label="Ver prévia", emoji="👁️", style=discord.ButtonStyle.secondary, row=2)
    async def preview(self, interaction, button):
        data = self.current()
        await interaction.response.send_message(embed=panel_embed(data), view=PublishedTicketPanelView(data), ephemeral=True)

    @discord.ui.button(label="Enviar Painel", emoji="📤", style=discord.ButtonStyle.success, row=2)
    async def send_panel(self, interaction, button):
        await interaction.response.send_message("Escolha o canal onde deseja publicar este painel:",
            view=PublishChannelView(self.guild_id, self.panel_index), ephemeral=True)

    @discord.ui.button(label="Voltar", emoji="⬅️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction, button):
        await interaction.response.edit_message(content="**Gerenciar painéis** — selecione uma opção.",
            embed=None, view=TicketPanelsView(self.guild_id))


class PublishPanelSelectView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=180)
        panels = ticket_panels_load(guild_id)
        self.add_item(PublishPanelSelect(guild_id, panels))
        self.guild_id = guild_id


class PublishPanelSelect(discord.ui.Select):
    def __init__(self, guild_id, panels):
        self.guild_id = guild_id
        super().__init__(placeholder="Selecione um painel...", min_values=1, max_values=1,
            options=[discord.SelectOption(label=(p.get("title") or f"Painel {i+1}")[:100], value=str(i)) for i,p in enumerate(panels[:25])])

    async def callback(self, interaction):
        await interaction.response.send_message("Agora escolha o canal para publicar:",
            view=PublishChannelView(self.guild_id, int(self.values[0])), ephemeral=True)


class PublishChannelView(discord.ui.View):
    def __init__(self, guild_id, panel_index):
        super().__init__(timeout=180)
        self.guild_id, self.panel_index = guild_id, panel_index
        self.add_item(PublishChannelSelect(guild_id, panel_index))


class PublishChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id, panel_index):
        super().__init__(placeholder="Selecione o canal de publicação...", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
        self.guild_id, self.panel_index = guild_id, panel_index

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        panels = ticket_panels_load(self.guild_id)
        if self.panel_index >= len(panels):
            return await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
        try:
            await target.send(embed=panel_embed(panels[self.panel_index]), view=PublishedTicketPanelView(panels[self.panel_index]))
        except discord.Forbidden:
            return await interaction.response.send_message("Não tenho permissão para enviar mensagens e embeds nesse canal.", ephemeral=True)
        await interaction.response.send_message(f"✅ Painel publicado em {target.mention}.", ephemeral=True)


class TicketPanelsView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        panels = ticket_panels_load(guild_id)
        if panels:
            self.add_item(TicketPanelSelect(guild_id))

    @discord.ui.button(label="Criar novo (Manual)", emoji="➕", style=discord.ButtonStyle.primary, row=1)
    async def create_manual(self, interaction, button):
        panels = ticket_panels_load(self.guild_id)
        if len(panels) >= 5:
            return await interaction.response.send_message("Limite atingido: você pode ter até 5 painéis.", ephemeral=True)
        await interaction.response.send_modal(TicketPanelEditModal(self.guild_id))

    @discord.ui.button(label="Criar Painel com IA", emoji="✨", style=discord.ButtonStyle.primary, row=1)
    async def create_ai(self, interaction, button):
        panels = ticket_panels_load(self.guild_id)
        if len(panels) >= 5:
            return await interaction.response.send_message("Limite atingido: você pode ter até 5 painéis.", ephemeral=True)
        # Geração automática local de um modelo completo, sem depender de chave externa.
        data = {"title": "🎫 Central de Atendimento", "description": "Olá! Selecione abaixo o tipo de atendimento para falar com nossa equipe.\n\n💙 Responderemos assim que possível. Não envie senhas ou códigos de autenticação.", "color": "2563eb", "footer": f"{SHOP_NAME} • Atendimento", "mode": "button", "options": [{"label": label, "emoji": emoji, "description": f"Abrir ticket: {label}"} for label, emoji in TICKET_TYPES[:5]]}
        panels.append(data)
        ticket_panels_save(self.guild_id, panels)
        idx = len(panels) - 1
        await interaction.response.edit_message(content="✨ **Modelo automático criado e salvo!** Edite o embed e as opções antes de publicar.", embed=panel_embed(data), view=TicketPanelEditorView(self.guild_id, idx))

    @discord.ui.button(label="Excluir Painel", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def delete_panel(self, interaction, button):
        panels = ticket_panels_load(self.guild_id)
        if not panels:
            return await interaction.response.send_message("Não há painéis para excluir.", ephemeral=True)
        await interaction.response.send_message("Escolha qual painel excluir. A exclusão é permanente.", view=DeletePanelView(self.guild_id), ephemeral=True)

    @discord.ui.button(label="Voltar", emoji="⬅️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction, button):
        await interaction.response.edit_message(embed=ticket_home_embed(), view=TicketHomeView(self.guild_id))


class TicketPanelSelect(discord.ui.Select):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        panels = ticket_panels_load(guild_id)
        super().__init__(placeholder="Selecione um Painel para editar...", min_values=1, max_values=1,
            options=[discord.SelectOption(label=(p.get("title") or f"Painel {i+1}")[:100], value=str(i), description=f"Painel {i+1} de {len(panels)}") for i,p in enumerate(panels[:25])])

    async def callback(self, interaction):
        idx = int(self.values[0]); data = ticket_panels_load(self.guild_id)[idx]
        await interaction.response.edit_message(content=f"**Editor do Painel:** {data.get('title', 'Painel')} ", embed=panel_embed(data), view=TicketPanelEditorView(self.guild_id, idx))


class DeletePanelSelect(discord.ui.Select):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        panels = ticket_panels_load(guild_id)
        super().__init__(placeholder="Selecione o painel para excluir...", min_values=1, max_values=1,
            options=[discord.SelectOption(label=(p.get("title") or f"Painel {i+1}")[:100], value=str(i)) for i,p in enumerate(panels[:25])])

    async def callback(self, interaction):
        await interaction.response.send_message("Tem certeza de que deseja excluir este painel?", view=ConfirmDeletePanelView(self.guild_id, int(self.values[0])), ephemeral=True)


class DeletePanelView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=180); self.add_item(DeletePanelSelect(guild_id))


class ConfirmDeletePanelView(discord.ui.View):
    def __init__(self, guild_id, index):
        super().__init__(timeout=90); self.guild_id, self.index = guild_id, index

    @discord.ui.button(label="Confirmar exclusão", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        panels = ticket_panels_load(self.guild_id)
        if self.index < len(panels):
            panels.pop(self.index); ticket_panels_save(self.guild_id, panels)
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(content="Painel excluído.", view=self)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(content="Exclusão cancelada.", view=None)


def ticket_home_embed():
    return embed("🎫 Sistema de Tickets",
        "Bem-vindo ao painel de configuração do sistema de tickets!\n"
        "Aqui você pode criar painéis personalizados, configurar cargos de suporte, canais de logs e muito mais — tudo de forma rápida e organizada.\n\n"
        "**Como começar**\n1. **Painéis de Ticket:** crie a mensagem que os membros verão.\n"
        "2. **Configurações Gerais:** defina cargos, categoria e opções de atendimento.\n"
        "3. **Enviar Painel:** publique o painel pronto em um canal.\n\n**O que deseja configurar?**", color=discord.Color.blue())


class TicketHomeView(discord.ui.View):
    def __init__(self, guild_id): super().__init__(timeout=600); self.guild_id = guild_id

    @discord.ui.button(label="Painéis de Ticket", emoji="🧩", style=discord.ButtonStyle.primary, row=0)
    async def panels(self, interaction, button):
        panels = ticket_panels_load(self.guild_id)
        await interaction.response.edit_message(content=f"**Gerenciar painéis** — Painéis {len(panels)}/5. Criar um painel não o publica automaticamente.", embed=None, view=TicketPanelsView(self.guild_id))

    @discord.ui.button(label="Configurações Gerais", emoji="⚙️", style=discord.ButtonStyle.primary, row=0)
    async def settings(self, interaction, button):
        await interaction.response.edit_message(embed=embed("⚙️ Configurações Gerais",
            "Ajustes que controlam o funcionamento do sistema de tickets.\n\n"
            "🎫 **Atendimento** — cargos, categorias, logs, nome e tipo do canal, imagem, avaliação e limite.\n"
            "🕒 **Atendimento por Horário** — dias e horários de abertura.\n"
            "🤖 **Inteligência Artificial** — respostas automáticas dentro dos tickets.\n\n"
            "Começando agora? Configure primeiro Cargos de Suporte e Categoria dos Tickets.", color=discord.Color.blue()), view=TicketGeneralSettingsView(self.guild_id))

    @discord.ui.button(label="Enviar Painel", emoji="📤", style=discord.ButtonStyle.success, row=1)
    async def publish(self, interaction, button):
        panels = ticket_panels_load(self.guild_id)
        if not panels: return await interaction.response.send_message("Crie um painel primeiro.", ephemeral=True)
        await interaction.response.edit_message(content="📤 **Enviar Painel**\nSelecione o painel que deseja publicar.", embed=None, view=PublishPanelSelectView(self.guild_id))


class TicketScheduleModal(discord.ui.Modal, title="Atendimento por Horário"):
    dias = discord.ui.TextInput(label="Dias (0=seg ... 6=dom; ex.: 0,1,2,3,4)", default="0,1,2,3,4,5,6", max_length=30)
    inicio = discord.ui.TextInput(label="Horário inicial (24h, HH:MM)", default="09:00", max_length=5)
    fim = discord.ui.TextInput(label="Horário final (24h, HH:MM)", default="21:00", max_length=5)
    fuso = discord.ui.TextInput(label="Fuso UTC (ex.: -3 para Brasília)", default="-3", max_length=4)
    habilitado = discord.ui.TextInput(label="Ativar agenda? (sim/não)", default="não", max_length=5)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            days = sorted(set(int(x.strip()) for x in str(self.dias.value).split(",") if x.strip() != ""))
            if not days or any(x < 0 or x > 6 for x in days): raise ValueError
            start = datetime.strptime(str(self.inicio.value).strip(), "%H:%M").strftime("%H:%M")
            end = datetime.strptime(str(self.fim.value).strip(), "%H:%M").strftime("%H:%M")
            offset = float(str(self.fuso.value).strip())
            if not -12 <= offset <= 14: raise ValueError
        except ValueError:
            return await interaction.response.send_message("Dados inválidos. Use dias de 0 a 6, horários HH:MM e fuso UTC entre -12 e +14.", ephemeral=True)
        enabled = str(self.habilitado.value).strip().lower() in ("sim", "s", "yes", "y", "true", "1")
        save_config(f"ticket_schedule_{interaction.guild_id}", json.dumps({"enabled": enabled, "days": days, "start": start, "end": end, "utc_offset": offset}))
        status = "ativada" if enabled else "desativada"
        await interaction.response.send_message(f"✅ Agenda {status}. Dias: {', '.join(str(x) for x in days)}; horário: {start}–{end}; UTC{offset:+g}.", ephemeral=True)


def ticket_schedule_allows(guild_id: int):
    raw = config_value(f"ticket_schedule_{guild_id}", "")
    if not raw:
        return True, ""
    try:
        cfg = json.loads(raw)
        if not cfg.get("enabled", False): return True, ""
        local_now = datetime.now(timezone.utc) + timedelta(hours=float(cfg.get("utc_offset", -3)))
        days = cfg.get("days", list(range(7)))
        start = datetime.strptime(cfg.get("start", "09:00"), "%H:%M").time()
        end = datetime.strptime(cfg.get("end", "21:00"), "%H:%M").time()
        inside_time = start <= local_now.time() <= end if start <= end else (local_now.time() >= start or local_now.time() <= end)
        if local_now.weekday() not in days or not inside_time:
            return False, f"O atendimento está fechado agora. Horário: {start.strftime('%H:%M')}–{end.strftime('%H:%M')} (UTC{float(cfg.get('utc_offset', -3)):+g})."
        return True, ""
    except (ValueError, TypeError, json.JSONDecodeError):
        logging.exception("Configuração de horário de tickets inválida")
        return True, ""


class TicketGeneralSettingsView(discord.ui.View):
    def __init__(self, guild_id): super().__init__(timeout=600); self.guild_id = guild_id

    @discord.ui.button(label="Atendimento", emoji="🎫", style=discord.ButtonStyle.primary, row=0)
    async def service(self, interaction, button):
        e = embed("🎫 | Atendimento", "Defina como os tickets são abertos e atendidos. Cada configuração pode valer para o servidor inteiro (Global) ou apenas para um painel (Por Ticket).\n\n**Editando:** 🌐 Global — todos os painéis\n\n**Equipe**\nCargos de Suporte — quem atende, é mencionado e tem acesso aos tickets\n\n**Canais**\nCategoria dos Tickets\nCanal de Logs\nCanal de Avaliação\n\n**Ticket**\nNome do Canal: `🆘️•ticket•aberto•🆘️`\nTipo de Canal: Canal individual\nImagem ao Abrir\nLimite por Usuário: `5 por usuário`\n\nPrimeiros passos: configure Cargos de Suporte e Categoria dos Tickets.", color=discord.Color.blue())
        await interaction.response.edit_message(embed=e, view=TicketServiceSettingsView(self.guild_id))

    @discord.ui.button(label="Atendimento por Horário", emoji="🕒", style=discord.ButtonStyle.primary, row=0)
    async def hours(self, interaction, button):
        await interaction.response.send_modal(TicketScheduleModal())

    @discord.ui.button(label="Inteligência Artificial", emoji="🤖", style=discord.ButtonStyle.primary, row=1)
    async def ai(self, interaction, button):
        await interaction.response.send_message("A tela está prevista, mas respostas de IA exigem conectar um provedor de IA. Nenhuma IA externa está conectada agora.", ephemeral=True)

    @discord.ui.button(label="Voltar", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button): await interaction.response.edit_message(embed=ticket_home_embed(), view=TicketHomeView(self.guild_id))


class TicketBehaviorModal(discord.ui.Modal, title="Nome do canal e imagem de abertura"):
    nome = discord.ui.TextInput(label="Modelo do nome (use {user} e {type})", default="ticket-{user}", max_length=90, required=True)
    imagem = discord.ui.TextInput(label="URL da imagem ao abrir (opcional)", max_length=400, required=False)
    async def on_submit(self, interaction):
        template = str(self.nome.value).strip()
        if not template or not any(token in template for token in ("{user}", "{type}")):
            return await interaction.response.send_message("O nome precisa conter {user} ou {type} para diferenciar os tickets.", ephemeral=True)
        image_url = str(self.imagem.value).strip()
        if image_url and not image_url.lower().startswith(("https://", "http://")):
            return await interaction.response.send_message("A URL da imagem precisa começar com https:// ou http://.", ephemeral=True)
        save_config(f"ticket_channel_name_template_{interaction.guild_id}", template)
        save_config(f"ticket_open_image_{interaction.guild_id}", image_url)
        await interaction.response.send_message("✅ Modelo do canal e imagem de abertura salvos.", ephemeral=True)


class TicketServiceSettingsView(discord.ui.View):
    def __init__(self, guild_id): super().__init__(timeout=600); self.guild_id = guild_id
    @discord.ui.button(label="Cargos de Suporte", emoji="👥", style=discord.ButtonStyle.primary, row=0)
    async def roles(self, interaction, button):
        view = discord.ui.View(timeout=180); view.add_item(ConfigRoleSelect())
        await interaction.response.send_message("Selecione o cargo de suporte. Para vários cargos, repita a configuração.", view=view, ephemeral=True)
    @discord.ui.button(label="Categoria dos Tickets", emoji="📁", style=discord.ButtonStyle.primary, row=0)
    async def category(self, interaction, button):
        view = discord.ui.View(timeout=180); view.add_item(ConfigChannelSelect("ticket_category_id", "Selecionar categoria", [discord.ChannelType.category]))
        await interaction.response.send_message("Selecione a categoria onde os canais serão criados:", view=view, ephemeral=True)
    @discord.ui.button(label="Canal de Logs", emoji="📋", style=discord.ButtonStyle.primary, row=1)
    async def logs(self, interaction, button):
        view = discord.ui.View(timeout=180); view.add_item(ConfigChannelSelect("ticket_logs_channel_id", "Selecionar canal de logs", [discord.ChannelType.text]))
        await interaction.response.send_message("Selecione o canal de logs:", view=view, ephemeral=True)
    @discord.ui.button(label="Canal de Avaliação", emoji="⭐", style=discord.ButtonStyle.primary, row=1)
    async def reviews(self, interaction, button):
        view = discord.ui.View(timeout=180); view.add_item(ConfigChannelSelect("ticket_reviews_channel_id", "Selecionar canal de avaliação", [discord.ChannelType.text]))
        await interaction.response.send_message("Selecione o canal de avaliações:", view=view, ephemeral=True)
    @discord.ui.button(label="Nome e imagem de abertura", emoji="🖼️", style=discord.ButtonStyle.primary, row=2)
    async def behavior(self, interaction, button):
        await interaction.response.send_modal(TicketBehaviorModal())
    @discord.ui.button(label="Limite por Usuário", emoji="🔢", style=discord.ButtonStyle.primary, row=3)
    async def limit(self, interaction, button):
        await interaction.response.send_modal(TicketLimitModal())
    @discord.ui.button(label="Voltar", emoji="⬅️", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction, button): await interaction.response.edit_message(embed=embed("⚙️ Configurações Gerais", "Escolha uma seção para configurar.", color=discord.Color.blue()), view=TicketGeneralSettingsView(self.guild_id))


class TicketLimitModal(discord.ui.Modal, title="Limite de Tickets por Usuário"):
    limite = discord.ui.TextInput(label="Quantidade máxima de tickets abertos", default="5", max_length=2)
    async def on_submit(self, interaction):
        try: value = int(str(self.limite.value)); assert 1 <= value <= 20
        except (ValueError, AssertionError): return await interaction.response.send_message("Informe um número de 1 a 20.", ephemeral=True)
        save_config(f"ticket_limit_{interaction.guild_id}", value)
        await interaction.response.send_message(f"✅ Limite salvo: **{value} ticket(s) aberto(s) por usuário**. A regra já está ativa para as próximas tentativas de abertura.", ephemeral=True)


@bot.tree.command(name="config_ticket", description="Abre o painel interativo de configuração de tickets.")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def config_ticket(interaction: discord.Interaction):
    await interaction.response.send_message(embed=ticket_home_embed(), view=TicketHomeView(interaction.guild_id), ephemeral=True)


@bot.tree.command(name="ajuda-vendas", description="Explica os comandos de vendas da ZORDX STORE.")
@app_commands.guild_only()
async def ajuda_vendas(interaction: discord.Interaction):
    text = ("💙 **Ajuda de Vendas — ZORDX STORE**\n\n"
            "`/config_ticket` — configurar o sistema de tickets.\n"
            "`/cupom criar` — criar cupom (equipe).\n`/cupom listar` — listar cupons.\n"
            "`/anunciar` — publicar um anúncio personalizado (equipe).\n\n"
            "Nunca envie senhas ou códigos de autenticação. Confirme preço e condições antes de comprar.")
    await interaction.response.send_message(embed=embed("📘 Ajuda de Vendas", text, color=discord.Color.blue()), ephemeral=True)


@bot.tree.command(name="anunciar", description="Publica um anúncio em um canal escolhido.")
@app_commands.guild_only()
@app_commands.check(staff_check)
@app_commands.describe(titulo="Título do anúncio", mensagem="Texto do anúncio", canal="Canal de destino (opcional)")
async def anunciar(interaction: discord.Interaction, titulo: str, mensagem: str, canal: discord.TextChannel = None):
    target = canal or interaction.channel
    card = embed("📢 " + titulo[:240], mensagem[:3500], color=discord.Color.blue())
    card.set_footer(text=f"{SHOP_NAME} • Anúncio")
    try:
        await target.send(embed=card)
    except discord.Forbidden:
        return await interaction.response.send_message("Não tenho permissão para publicar nesse canal.", ephemeral=True)
    await interaction.response.send_message(f"✅ Anúncio publicado em {target.mention}.", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Configure DISCORD_TOKEN nas variáveis de ambiente.")
    db_init()
    bot.run(TOKEN)
