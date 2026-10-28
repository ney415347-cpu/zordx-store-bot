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

# ═══════════════════════════════════════════════════════════════
#                    🎨 CONFIGURAÇÕES
# ═══════════════════════════════════════════════════════════════
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

# Cores adicionais para os painéis
COR_AZUL = 0x3498DB
COR_ESCURO = 0x2A2D34
COR_DESTAQUE = 0x00A8FF
COR_SUCESSO = 0x22C55E
COR_PERIGO = 0xEF4444
COR_AVISO = 0xF1C40F

# ═══════════════════════════════════════════════════════════════
#                    🗄️ BANCO DE DADOS
# ═══════════════════════════════════════════════════════════════
def db_init():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS warnings(
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER,
            moderator_id INTEGER, reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets(
            channel_id INTEGER PRIMARY KEY, guild_id INTEGER, opener_id INTEGER,
            status TEXT DEFAULT 'open', ticket_type TEXT DEFAULT 'Atendimento',
            closed_by INTEGER, closed_at TEXT)""")
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


# ═══════════════════════════════════════════════════════════════
#                    🎫 EMBEDS DO SISTEMA DE TICKETS
# ═══════════════════════════════════════════════════════════════
def embed_ticket_config(atendente="ninguém", estado="Aberto", descanso="normal"):
    return discord.Embed(
        title="⚙️ Configurações do Ticket",
        description=(
            "Tudo que a equipe precisa para gerenciar este atendimento, em um só lugar.\n\n"
            "━━━━━━ **Situação atual** ━━━━━━\n"
            f"👥 **Atendente** › {atendente}\n"
            f"📁 **Estado** › {estado}\n"
            f"☁️ **Descanso** › {descanso}\n\n"
            
            "━━━━━━ **Atendimento** ━━━━━━\n"
            "☁️ **Horário de Descanso** — Pausa de 50 min; volta quando o usuário fala.\n"
            "📢 **Chamar Usuário** — Envia aviso privado ao dono do ticket.\n"
            "✈️ **Transferir Ticket** — Passa o atendimento para outro atendente.\n\n"
            
            "━━━━━━ **Gerenciar** ━━━━━━\n"
            "✏️ **Renomear Ticket** — Muda o nome do canal.\n"
            "📁 **Arquivar Ticket** — Esconde do usuário.\n"
            "⏱️ **Fechamento Automático** — Fecha após um tempo sem atividade.\n\n"
            
            "━━━━━━ **Participantes** ━━━━━━\n"
            "➕ **Adicionar Membro** — Dá acesso a mais alguém.\n"
            "🗑️ **Remover Membro** — Retira acesso.\n"
            "📡 **Criar Call** — Sala de voz privada vinculada ao ticket."
        ),
        color=COR_DESTAQUE
    )


# ═══════════════════════════════════════════════════════════════
#                    📋 MENU DE AÇÕES DO TICKET
# ═══════════════════════════════════════════════════════════════
class MenuAcoesTicket(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="Escolha uma ação...",
        options=[
            discord.SelectOption(label="☁️ Horário de Descanso", value="descanso", description="Pausa o atendimento por até 50 minutos"),
            discord.SelectOption(label="📢 Chamar Usuário", value="chamar", description="Envia um aviso privado ao dono do ticket"),
            discord.SelectOption(label="✈️ Transferir Ticket", value="transferir", description="Passa o atendimento para outro atendente"),
            discord.SelectOption(label="✏️ Renomear Ticket", value="renomear", description="Muda o nome deste ticket"),
            discord.SelectOption(label="📁 Arquivar Ticket", value="arquivar", description="O usuário deixa de ver o ticket"),
            discord.SelectOption(label="⏱️ Fechamento Automático", value="fechamento_auto", description="Fecha o ticket automaticamente após um tempo"),
            discord.SelectOption(label="➕ Adicionar Membro", value="adicionar", description="Dá acesso a mais um participante"),
            discord.SelectOption(label="🗑️ Remover Membro", value="remover", description="Retira acesso de um participante"),
            discord.SelectOption(label="📡 Criar Call", value="criar_call", description="Cria uma sala de voz privada aqui"),
        ]
    )
    async def acao_selecionada(self, interaction: discord.Interaction, select: discord.ui.Select):
        acao = select.values[0]
        msgs = {
            "descanso": "☁️ **Modo descanso ativado!**\nO atendimento será pausado. Voltará quando o usuário falar novamente.",
            "chamar": "📢 **Aviso enviado!**\nO usuário recebeu uma mensagem pedindo para retornar ao atendimento.",
            "transferir": "✈️ **Mencione o atendente** para quem deseja transferir este ticket.",
            "renomear": "✏️ **Digite o novo nome** para este ticket:",
            "arquivar": "📁 **Ticket arquivado!**\nMovido para a categoria de arquivados.",
            "fechamento_auto": "⏱️ **Fechamento automático configurado.**\nSerá fechado em 24h sem resposta.",
            "adicionar": "➕ **Mencione o usuário** para adicionar ao ticket:",
            "remover": "🗑️ **Mencione o usuário** para remover do ticket:",
            "criar_call": "📡 **Sala de voz criada!**\nSala privada criada para este atendimento."
        }
        await interaction.response.send_message(msgs.get(acao, "❌ Ação não reconhecida"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════
#                    🎨 EMBEDS DO CRIADOR DE EMBEDS
# ═══════════════════════════════════════════════════════════════
def embed_painel_embeds():
    return discord.Embed(
        title="Painel de Criação de Embed",
        description=(
            "⭐ Você também pode criar Containers com botões interativos, "
            "templates e muito mais! Use agora `/container criar`."
        ),
        color=COR_AZUL
    )

def embed_selecionar_embed():
    emb = discord.Embed(title="Selecione um Embed para editar", color=COR_ESCURO)
    emb.add_field(name="Embed 1", value="Título do Embed", inline=False)
    return emb

def embed_cores_disponiveis():
    emb = discord.Embed(title="Painel de Cores", description="Selecione uma cor", color=COR_AZUL)
    emb.add_field(name="🔵 Azul Clássico", value="#3498DB", inline=True)
    emb.add_field(name="🔴 Vermelho Escuro", value="#992D22", inline=True)
    emb.add_field(name="🔵 Azul Escuro", value="#206694", inline=True)
    emb.add_field(name="🟣 Roxo Clássico", value="#9B59B6", inline=True)
    emb.add_field(name="🔴 Vermelho Clássico", value="#E74C3C", inline=True)
    emb.add_field(name="🔴 Vermelho Discord", value="#ED4245", inline=True)
    emb.add_field(name="+ Nova Cor", value="Salve uma cor com nome + código HEX", inline=False)
    return emb

def embed_form_titulo():
    emb = discord.Embed(
        title="Altere o título do seu Embed.",
        description="⚠️ Este formulário será enviado. Não compartilhe senhas ou informações confidenciais.",
        color=COR_AVISO
    )
    emb.add_field(name="Título", value="Título do Embed", inline=False)
    emb.add_field(name="URL do Título (opcional)", value="De forma opcional, ao clicar no título o usuário será levado...", inline=False)
    return emb

def embed_form_descricao():
    emb = discord.Embed(
        title="Altere a descrição do seu Embed.",
        description="⚠️ Este formulário será enviado. Não compartilhe senhas ou informações confidenciais.",
        color=COR_AVISO
    )
    emb.add_field(name="Descrição", value="⭐ Você também pode criar Containers com botões interativos, templates e muito mais! Use agora `/container criar`.", inline=False)
    return emb

def embed_form_imagem():
    emb = discord.Embed(
        title="Altere as imagens do seu Embed",
        description="⚠️ Este formulário será enviado. Não compartilhe senhas ou informações confidenciais.",
        color=COR_AVISO
    )
    emb.add_field(name="Imagem", value="URL para Imagem, que ficará grande no final", inline=False)
    emb.add_field(name="Thumbnail", value="URL para Thumbnail, que ficará pequena no topo direito", inline=False)
    return emb

def embed_form_botao_link():
    emb = discord.Embed(
        title="Crie seu Botão de Link",
        description="⚠️ Este formulário será enviado. Não compartilhe senhas ou informações confidenciais.",
        color=COR_AVISO
    )
    emb.add_field(name="Texto do botão *", value="Clique aqui para visitar meu site", inline=False)
    emb.add_field(name="URL do botão *", value="https://discord.gg/fS9qXbKz7Y", inline=False)
    emb.add_field(name="Emoji do botão (opcional)", value="😎", inline=False)
    return emb


# ═══════════════════════════════════════════════════════════════
#                    🔘 BOTÕES DO CRIADOR DE EMBEDS
# ═══════════════════════════════════════════════════════════════
class BotoesCriarEmbed(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Adicionar Embed", style=discord.ButtonStyle.green, emoji="➕", row=0)
    async def add_embed(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=embed_selecionar_embed(), ephemeral=True)

    @discord.ui.button(label="Gerar Embed com IA", style=discord.ButtonStyle.blurple, emoji="⚡", row=0)
    async def gerar_ia(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⚡ Gerando embed com IA...", ephemeral=True)

    @discord.ui.button(label="Adicionar Botão", style=discord.ButtonStyle.gray, emoji="🔗", row=1)
    
    async def add_botao(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CriarBotaoModal())


class CriarBotaoModal(discord.ui.Modal, title="Criar botão de link"):
    texto = discord.ui.TextInput(label="Texto do botão", max_length=80)
    url = discord.ui.TextInput(
        label="URL do botão",
        placeholder="https://exemplo.com",
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        link = str(self.url.value).strip()
        if not link.startswith(("https://", "http://")):
            await interaction.response.send_message(
                "❌ A URL deve começar com https:// ou http://.",
                ephemeral=True
            )
            return

        view = discord.ui.View(timeout=300)
        view.add_item(discord.ui.Button(label=str(self.texto.value), url=link))
        await interaction.response.send_message(
            "✅ Botão criado!",
            view=view,
            ephemeral=True
        )


class ModalEmbed(discord.ui.Modal, title="Criar Embed"):
    titulo = discord.ui.TextInput(label="Título", max_length=256)
    descricao = discord.ui.TextInput(
        label="Descrição",
        style=discord.TextStyle.paragraph,
        max_length=3500
    )
    cor = discord.ui.TextInput(
        label="Cor hexadecimal",
        placeholder="3498DB",
        required=False,
        max_length=6
    )

    async def on_submit(self, interaction: discord.Interaction):
        codigo = str(self.cor.value).strip().lstrip("#")
        try:
            cor = discord.Color(int(codigo, 16)) if codigo else discord.Color(COR_AZUL)
        except ValueError:
            await interaction.response.send_message(
                "❌ Código de cor inválido. Exemplo: 3498DB.",
                ephemeral=True
            )
            return

        if codigo and len(codigo) not in (3, 6):
            await interaction.response.send_message(
                "❌ Use uma cor hexadecimal válida.",
                ephemeral=True
            )
            return

        emb = discord.Embed(
            title=str(self.titulo.value),
            description=str(self.descricao.value),
            color=cor
        )
        await interaction.response.send_message(
            "Prévia do seu Embed:",
            embed=emb,
            ephemeral=True
        )


# ═══════════════════════════════════════════════════════════════
#                    🎫 PAINEL DE TICKETS
# ═══════════════════════════════════════════════════════════════

class FecharTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Fechar ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="zordx:fechar_ticket"
    )
    async def fechar(self, interaction: discord.Interaction, button: discord.ui.Button):
        canal = interaction.channel
        if not isinstance(canal, discord.TextChannel):
            await interaction.response.send_message("Canal inválido.", ephemeral=True)
            return

        with sqlite3.connect(DB_PATH) as c:
            registro = c.execute(
                "SELECT opener_id, status FROM tickets WHERE channel_id=?",
                (canal.id,)
            ).fetchone()

        if not registro:
            await interaction.response.send_message(
                "Este canal não está registrado como ticket.",
                ephemeral=True
            )
            return

        opener_id, status = registro
        membro = interaction.user
        autorizado = (
            membro.id == opener_id
            or (
                isinstance(membro, discord.Member)
                and (
                    membro.guild_permissions.manage_channels
                    or membro.guild_permissions.administrator
                    or any(
                        cargo.id == TICKET_SUPPORT_ROLE_ID
                        for cargo in membro.roles
                    ) if TICKET_SUPPORT_ROLE_ID else
                    membro.guild_permissions.manage_channels
                    or membro.guild_permissions.administrator
                )
            )
        )

        if not autorizado:
            await interaction.response.send_message(
                "❌ Você não tem permissão para fechar este ticket.",
                ephemeral=True
            )
            return

        if status == "closed":
            await interaction.response.send_message(
                "Este ticket já está fechado.",
                ephemeral=True
            )
            return

        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "UPDATE tickets SET status='closed', closed_by=?, closed_at=? "
                "WHERE channel_id=?",
                (
                    interaction.user.id,
                    datetime.now(timezone.utc).isoformat(),
                    canal.id
                )
            )

        await canal.set_permissions(
            interaction.guild.default_role,
            view_channel=False
        )
        await interaction.response.send_message(
            f"🔒 Ticket fechado por {interaction.user.mention}."
        )


class TipoTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        opcoes = [
            discord.SelectOption(label=nome, emoji=emoji, value=nome)
            for nome, emoji in TICKET_TYPES
        ]
        seletor = discord.ui.Select(
            placeholder="Selecione o motivo do atendimento",
            options=opcoes
        )
        seletor.callback = self.selecionar
        self.add_item(seletor)

    async def selecionar(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Use este painel dentro de um servidor.",
                ephemeral=True
            )
            return

        tipo = interaction.data["values"][0]

        with sqlite3.connect(DB_PATH) as c:
            registro = c.execute(
                "SELECT channel_id FROM tickets "
                "WHERE guild_id=? AND opener_id=? AND status='open'",
                (guild.id, interaction.user.id)
            ).fetchone()

        if registro:
            canal_existente = guild.get_channel(registro[0])
            if canal_existente:
                await interaction.response.send_message(
                    f"Você já possui um ticket aberto: {canal_existente.mention}",
                    ephemeral=True
                )
                return

        categoria = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        if not isinstance(categoria, discord.CategoryChannel):
            categoria = None

        permissoes = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )
        }

        if guild.me:
            permissoes[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True
            )

        cargo = guild.get_role(TICKET_SUPPORT_ROLE_ID) if TICKET_SUPPORT_ROLE_ID else None
        if cargo:
            permissoes[cargo] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )

        nome = f"ticket-{interaction.user.name}".lower().replace(" ", "-")[:90]

        try:
            canal = await guild.create_text_channel(
                name=nome,
                category=categoria,
                overwrites=permissoes
            )
            with sqlite3.connect(DB_PATH) as c:
                c.execute(
                    "INSERT INTO tickets(channel_id,guild_id,opener_id,status,ticket_type) "
                    "VALUES(?,?,?,'open',?)",
                    (canal.id, guild.id, interaction.user.id, tipo)
                )

            await canal.send(
                content=interaction.user.mention,
                embed=embed(
                    f"🎫 Ticket — {tipo}",
                    "Explique sua solicitação. Nossa equipe responderá assim que possível.",
                    COR_DESTAQUE
                ),
                view=FecharTicketView()
            )
            await interaction.response.send_message(
                f"✅ Ticket criado: {canal.mention}",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Não tenho permissão para criar canais.",
                ephemeral=True
            )


# ═══════════════════════════════════════════════════════════════
#                    🛍️ PRODUTOS E CUPONS
# ═══════════════════════════════════════════════════════════════

produto_group = app_commands.Group(
    name="produto",
    description="Gerenciar os produtos da loja"
)


@produto_group.command(name="adicionar", description="Cadastrar um produto")
@app_commands.check(staff_check)
async def produto_adicionar(
    interaction: discord.Interaction,
    nome: str,
    preco: float,
    estoque: int,
    descricao: str,
    link: str = ""
):
    if preco < 0 or estoque < 0:
        await interaction.response.send_message(
            "Preço e estoque não podem ser negativos.",
            ephemeral=True
        )
        return

    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR REPLACE INTO shop_products"
            "(name,description,price,stock,link) VALUES(?,?,?,?,?)",
            (nome, descricao, preco, estoque, link)
        )

    await interaction.response.send_message(
        f"✅ Produto **{nome}** cadastrado por {money(preco)}.",
        ephemeral=True
    )


@produto_group.command(name="listar", description="Listar produtos da loja")
async def produto_listar(interaction: discord.Interaction):
    produtos = get_products()
    if not produtos:
        await interaction.response.send_message(
            "Nenhum produto cadastrado.",
            ephemeral=True
        )
        return

    emb = embed("🛍️ ZORDX STORE — Produtos", "Confira nosso catálogo.")
    for p in produtos[:25]:
        descricao = (
            f"Preço: {p.get('price', 'Consultar')}\n"
            f"Estoque: {p.get('stock', 'Consultar')}\n"
            f"{p.get('description', '')}"
        )
        link = p.get("link", "")
        if isinstance(link, str) and link.startswith(("https://", "http://")):
            descricao += f"\n[Ver produto]({link})"
        emb.add_field(
            name=str(p.get("name", "Produto"))[:256],
            value=descricao[:1024],
            inline=False
        )

    await interaction.response.send_message(embed=emb)


@produto_group.command(name="remover", description="Remover produto")
@app_commands.check(staff_check)
async def produto_remover(interaction: discord.Interaction, nome: str):
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            "DELETE FROM shop_products WHERE name=? COLLATE NOCASE",
            (nome,)
        )

    await interaction.response.send_message(
        "✅ Produto removido." if cur.rowcount else "Produto não encontrado.",
        ephemeral=True
    )


cupom_group = app_commands.Group(
    name="cupom",
    description="Gerenciar cupons de desconto"
)


@cupom_group.command(name="criar", description="Criar cupom")
@app_commands.check(staff_check)
async def cupom_criar(
    interaction: discord.Interaction,
    codigo: str,
    percentual: app_commands.Range[int, 1, 100],
    usos: app_commands.Range[int, 1, 10000]
):
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "INSERT INTO coupons(code,percent,uses_left,created_by) VALUES(?,?,?,?)",
                (codigo.upper().strip(), percentual, usos, interaction.user.id)
            )
    except sqlite3.IntegrityError:
        await interaction.response.send_message(
            "Já existe um cupom com esse código.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"✅ Cupom **{codigo.upper()}** criado: {percentual}% de desconto, "
        f"{usos} uso(s).",
        ephemeral=True
    )


@cupom_group.command(name="listar", description="Listar cupons")
@app_commands.check(staff_check)
async def cupom_listar(interaction: discord.Interaction):
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT code,percent,uses_left FROM coupons ORDER BY code"
        ).fetchall()

    if not rows:
        await interaction.response.send_message(
            "Nenhum cupom cadastrado.",
            ephemeral=True
        )
        return

    emb = embed("🎟️ Cupons da loja", "Cupons cadastrados.")
    for codigo, percentual, usos in rows[:25]:
        emb.add_field(
            name=codigo,
            value=f"Desconto: {percentual}%\nUsos restantes: {usos}",
            inline=True
        )

    await interaction.response.send_message(embed=emb, ephemeral=True)


# ═══════════════════════════════════════════════════════════════
#                    🛡️ MODERAÇÃO
# ═══════════════════════════════════════════════════════════════

moderacao_group = app_commands.Group(
    name="moderacao",
    description="Comandos de moderação"
)


@moderacao_group.command(name="advertir", description="Advertir membro")
@app_commands.check(staff_check)
async def moderacao_advertir(
    interaction: discord.Interaction,
    membro: discord.Member,
    motivo: str
):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT INTO warnings(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
            (interaction.guild_id, membro.id, interaction.user.id, motivo)
        )

    await interaction.response.send_message(
        f"⚠️ {membro.mention} recebeu uma advertência.\nMotivo: {motivo}"
    )


@moderacao_group.command(name="avisos", description="Consultar advertências")
@app_commands.check(staff_check)
async def moderacao_avisos(
    interaction: discord.Interaction,
    membro: discord.Member
):
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT reason,moderator_id,created_at FROM warnings "
            "WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
            (interaction.guild_id, membro.id)
        ).fetchall()

    if not rows:
        await interaction.response.send_message(
            "Este membro não possui advertências.",
            ephemeral=True
        )
        return

    emb = embed("⚠️ Advertências", f"Histórico de {membro.mention}")
    for motivo, moderador, data in rows:
        emb.add_field(
            name=f"Moderador: {moderador} • {data}",
            value=motivo or "Sem motivo",
            inline=False
        )

    await interaction.response.send_message(embed=emb, ephemeral=True)


@moderacao_group.command(name="limpar", description="Apagar mensagens")
@app_commands.check(staff_check)
async def moderacao_limpar(
    interaction: discord.Interaction,
    quantidade: app_commands.Range[int, 1, 100]
):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "Use este comando em um canal de texto.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    apagadas = await interaction.channel.purge(limit=quantidade)
    await interaction.followup.send(
        f"🧹 {len(apagadas)} mensagem(ns) apagada(s).",
        ephemeral=True
    )


@moderacao_group.command(name="timeout", description="Aplicar timeout")
@app_commands.check(staff_check)
async def moderacao_timeout(
    interaction: discord.Interaction,
    membro: discord.Member,
    minutos: app_commands.Range[int, 1, 40320],
    motivo: str = "Não informado"
):
    try:
        await membro.timeout(timedelta(minutes=minutos), reason=motivo)
        await interaction.response.send_message(
            f"⏱️ Timeout aplicado a {membro.mention} por {minutos} minuto(s)."
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "Não tenho permissão para aplicar timeout.",
            ephemeral=True
        )


@moderacao_group.command(name="expulsar", description="Expulsar membro")
@app_commands.check(staff_check)
async def moderacao_expulsar(
    interaction: discord.Interaction,
    membro: discord.Member,
    motivo: str = "Não informado"
):
    try:
        await membro.kick(reason=motivo)
        await interaction.response.send_message(
            f"👢 {membro} foi expulso. Motivo: {motivo}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "Não tenho permissão para expulsar esse membro.",
            ephemeral=True
        )


@moderacao_group.command(name="banir", description="Banir membro")
@app_commands.check(staff_check)
async def moderacao_banir(
    interaction: discord.Interaction,
    membro: discord.Member,
    motivo: str = "Não informado"
):
    try:
        await membro.ban(reason=motivo)
        await interaction.response.send_message(
            f"🔨 {membro} foi banido. Motivo: {motivo}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "Não tenho permissão para banir esse membro.",
            ephemeral=True
        )


# ═══════════════════════════════════════════════════════════════
#                    📣 COMANDOS GERAIS
# ═══════════════════════════════════════════════════════════════

@bot.tree.command(name="ping", description="Verificar latência do bot")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🏓 Pong! Latência: {round(bot.latency * 1000)} ms."
    )


@bot.tree.command(name="ajuda", description="Ver comandos da ZORDX STORE BOT")
async def ajuda(interaction: discord.Interaction):
    emb = embed(
        "🤖 ZORDX STORE BOT — Ajuda",
        "Comandos disponíveis:\n\n"
        "🎫 `/ticket painel` — publicar painel de tickets\n"
        "🎨 `/embed criar` — abrir criador de embeds\n"
        "🛍️ `/produto adicionar` — cadastrar produto\n"
        "🛍️ `/produto listar` — listar produtos\n"
        "🛍️ `/produto remover` — remover produto\n"
        "🎟️ `/cupom criar` — criar cupom\n"
        "🎟️ `/cupom listar` — listar cupons\n"
        "🛡️ `/moderacao advertir`\n"
        "🛡️ `/moderacao avisos`\n"
        "🛡️ `/moderacao limpar`\n"
        "🛡️ `/moderacao timeout`\n"
        "🛡️ `/moderacao expulsar`\n"
        "🛡️ `/moderacao banir`\n"
        "🏓 `/ping`"
    )
    await interaction.response.send_message(embed=emb, ephemeral=True)


@bot.tree.command(name="embed", description="Abrir painel de criação de embeds")
@app_commands.describe(acao="Escolha a ação")
@app_commands.choices(acao=[
    app_commands.Choice(name="Criar", value="criar"),
    app_commands.Choice(name="Cores", value="cores")
])
async def embed_comando(
    interaction: discord.Interaction,
    acao: app_commands.Choice[str]
):
    if acao.value == "criar":
        await interaction.response.send_modal(ModalEmbed())
    else:
        await interaction.response.send_message(
            embed=embed_cores_disponiveis(),
            ephemeral=True
        )


ticket_group = app_commands.Group(name="ticket", description="Sistema de tickets")


@ticket_group.command(name="painel", description="Publicar painel de tickets")
@app_commands.check(staff_check)
async def ticket_painel(interaction: discord.Interaction):
    emb = embed(
        "🎫 Atendimento — ZORDX STORE",
        "Clique no menu abaixo e selecione o motivo do atendimento.\n"
        "Um canal privado será criado para você.",
        COR_DESTAQUE
    )
    await interaction.channel.send(embed=emb, view=TipoTicketView())
    await interaction.response.send_message(
        "✅ Painel de tickets publicado.",
        ephemeral=True
    )


@bot.tree.command(name="anunciar", description="Enviar anúncio no canal atual")
@app_commands.check(staff_check)
@app_commands.describe(titulo="Título do anúncio", mensagem="Conteúdo do anúncio")
async def anunciar(
    interaction: discord.Interaction,
    titulo: str,
    mensagem: str
):
    await interaction.channel.send(
        embed=embed(f"📢 {titulo}", mensagem, COR_DESTAQUE)
    )
    await interaction.response.send_message(
        "✅ Anúncio publicado.",
        ephemeral=True
    )


# ═══════════════════════════════════════════════════════════════
#                    ⚙️ INICIALIZAÇÃO
# ═══════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    logging.info("Bot conectado como %s (%s)", bot.user, bot.user.id)

    if not getattr(bot, "_views_registradas", False):
        bot.add_view(FecharTicketView())
        bot._views_registradas = True

    if not getattr(bot, "_comandos_sincronizados", False):
        try:
            bot.tree.add_command(produto_group)
            bot.tree.add_command(cupom_group)
            bot.tree.add_command(moderacao_group)
            bot.tree.add_command(ticket_group)

            if GUILD_ID:
                guild_obj = discord.Object(id=GUILD_ID)
                bot.tree.copy_global_to(guild=guild_obj)
                sincronizados = await bot.tree.sync(guild=guild_obj)
            else:
                sincronizados = await bot.tree.sync()

            logging.info("%s comandos sincronizados.", len(sincronizados))
            bot._comandos_sincronizados = True
        except Exception:
            logging.exception("Erro ao sincronizar comandos")


@bot.event
async def on_member_join(member: discord.Member):
    if not WELCOME_CHANNEL_ID:
        return

    canal = member.guild.get_channel(WELCOME_CHANNEL_ID)
    if canal:
        await canal.send(
            embed=embed(
                f"👋 Bem-vindo à {SHOP_NAME}!",
                f"Olá, {member.mention}! Seja bem-vindo ao servidor.",
                COR_DESTAQUE
            )
        )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    logging.error("Erro de comando: %s", error)

    if isinstance(error, app_commands.CheckFailure):
        mensagem = "❌ Você não tem permissão para usar este comando."
    else:
        mensagem = "❌ Ocorreu um erro ao executar o comando. Confira os logs."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(mensagem, ephemeral=True)
        else:
            await interaction.response.send_message(mensagem, ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    db_init()

    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN não foi configurado nas variáveis de ambiente."
        )

    bot.run(TOKEN)

