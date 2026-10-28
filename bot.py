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
    async def add_botao(self, interaction: discord.Interaction, button: discord.ui.Button
