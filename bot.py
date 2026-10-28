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
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT channel_id FROM tickets WHERE guild_id=? AND opener_id=? AND status='open'",
            (guild.id, interaction.user.id)
        ).fetchone()
    if row and guild.get_channel(row[0]):
        return await interaction.response.send_message(
            f"Você já tem um ticket aberto: {guild.get_channel(row[0]).mention}", ephemeral=True
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
        channel = await guild.create_text_channel(
            f"ticket-{safe}", category=category, overwrites=perms,
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


@bot.tree.command(name="painel_tickets", description="Publica o painel de tickets.")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def ticket_panel(interaction: discord.Interaction):
    await interaction.channel.send(
        embed=embed("🎫 Suporte — " + SHOP_NAME, "🎫 **Central de atendimento**\n\nSelecione o botão azul abaixo para abrir seu atendimento privado com a equipe da ZORDX STORE.\n\n🔒 Somente você e a equipe poderão ver o canal."),
        view=TicketPanel()
    )
    await interaction.response.send_message("Painel publicado.", ephemeral=True)


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


@bot.tree.command(name="produtos", description="Lista produtos da loja.")
@app_commands.guild_only()
async def products(interaction: discord.Interaction):
    items = get_products()
    if not items:
        return await interaction.response.send_message("Nenhum produto cadastrado.", ephemeral=True)
    e = embed("🛒 Produtos — " + SHOP_NAME, "Consulte o suporte antes de pagar.")
    for product in items[:25]:
        e.add_field(
            name=f"{product.get('name', 'Produto')} — {product.get('price', 'Consulte')}"[:256],
            value=str(product.get("description", "Sem descrição"))[:900],
            inline=False
        )
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="produto", description="Busca um produto pelo nome exato.")
@app_commands.describe(nome="Nome do produto")
@app_commands.guild_only()
async def product(interaction: discord.Interaction, nome: str):
    item = next((p for p in get_products()
                 if str(p.get("name", "")).casefold() == nome.casefold()), None)
    if not item:
        return await interaction.response.send_message(
            "Produto não encontrado. Use /produtos.", ephemeral=True
        )
    e = embed(item.get("name", "Produto"), item.get("description", "Sem descrição"))
    e.add_field(name="Preço", value=str(item.get("price", "Consulte")))
    if item.get("link"):
        e.add_field(name="Link", value=str(item["link"])[:1024], inline=False)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="addproduto", description="Cadastra ou atualiza um produto.")
@app_commands.guild_only()
@app_commands.check(staff_check)
@app_commands.describe(nome="Nome do produto", preco="Preço em reais",
                       descricao="Descrição", estoque="Quantidade inicial", link="Link opcional")
async def addproduto(interaction: discord.Interaction, nome: str, preco: app_commands.Range[float, 0, 100000000],
                     descricao: str, estoque: app_commands.Range[int, 0, 1000000] = 0, link: str = ""):
    nome = nome.strip()[:100]
    if not nome:
        return await interaction.response.send_message("Informe um nome válido.", ephemeral=True)
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT INTO shop_products(name,description,price,stock,link) VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET description=excluded.description,price=excluded.price,"
            "stock=excluded.stock,link=excluded.link",
            (nome, descricao[:1000], float(preco), int(estoque), link[:500])
        )
    await interaction.response.send_message(
        f"✅ Produto **{discord.utils.escape_markdown(nome)}** salvo. Preço: {money(preco)}; estoque: {estoque}.",
        ephemeral=True
    )


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
                "Produto não encontrado no cadastro. Use /addproduto primeiro.", ephemeral=True
            )
        new_stock = int(row[0]) + int(quantidade)
        c.execute("UPDATE shop_products SET stock=? WHERE name=? COLLATE NOCASE",
                  (new_stock, nome.strip()))
    await interaction.response.send_message(
        f"📦 Estoque de **{discord.utils.escape_markdown(nome)}** atualizado: **{new_stock}** unidade(s).",
        ephemeral=True
    )


class EmbedBuilderModal(discord.ui.Modal, title="Criar Embed — ZORDX STORE"):
    titulo = discord.ui.TextInput(label="Título", placeholder="Ex.: Novidades da loja", max_length=256)
    descricao = discord.ui.TextInput(
        label="Descrição", placeholder="Escreva a mensagem do embed", style=discord.TextStyle.paragraph,
        max_length=3500
    )
    cor = discord.ui.TextInput(label="Cor hexadecimal", default="#5865F2", max_length=7, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.cor.value).strip().lstrip("#")
        try:
            if len(raw) != 6:
                raise ValueError
            color = discord.Color(int(raw, 16))
        except ValueError:
            return await interaction.response.send_message(
                "Cor inválida. Use um código hexadecimal, como #5865F2.", ephemeral=True
            )
        card = embed(str(self.titulo.value), str(self.descricao.value), color=color)
        card.set_footer(text=f"{SHOP_NAME} • Embed personalizado")
        await interaction.channel.send(embed=card)
        await interaction.response.send_message("✅ Embed publicado com sucesso!", ephemeral=True)


@bot.tree.command(name="embed", description="Abre o criador interativo de embeds.")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def embed_interativo(interaction: discord.Interaction):
    await interaction.response.send_modal(EmbedBuilderModal())


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
        "Confira os produtos disponíveis na nossa loja.\n\n"
        "📦 Use **/produtos** para ver o catálogo e os estoques.\n"
        "🔎 Use **/produto** para consultar um produto específico.\n\n"
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


@bot.tree.command(name="criar_cupom", description="Cria cupom de desconto.")
@app_commands.guild_only()
@app_commands.check(staff_check)
@app_commands.describe(codigo="Código do cupom", desconto="Desconto percentual de 1 a 90",
                       usos="Quantidade de usos disponíveis")
async def criar_cupom(interaction: discord.Interaction, codigo: str,
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


@bot.tree.command(name="advertencias", description="Consulta advertências de membro.")
@app_commands.describe(membro="Membro")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def warnings(interaction: discord.Interaction, membro: discord.Member):
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT reason,moderator_id,created_at FROM warnings WHERE guild_id=? AND user_id=? "
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
        "`/produtos` e `/produto` — Produtos\n`/addproduto` — Cadastrar produto\n"
        "`/addestoque` — Adicionar estoque\n`/addpainel` — Painel da loja\n"
        "`/embed` — Criador interativo de embeds\n`/embed_criar` — Publicar embed\n`/criar_cupom` — Criar cupom\n"
        "`/painel_tickets`, `/tickets`, `/config tickets` — Tickets e configuração\n"
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


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Configure DISCORD_TOKEN nas variáveis de ambiente.")
    db_init()
    bot.run(TOKEN)
