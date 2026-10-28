import os, json, sqlite3, logging
from datetime import timedelta
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
TOKEN=os.getenv("DISCORD_TOKEN","").strip()
GUILD_ID=int(os.getenv("GUILD_ID","0") or 0)
WELCOME_CHANNEL_ID=int(os.getenv("WELCOME_CHANNEL_ID","0") or 0)
TICKET_CATEGORY_ID=int(os.getenv("TICKET_CATEGORY_ID","0") or 0)
TICKET_SUPPORT_ROLE_ID=int(os.getenv("TICKET_SUPPORT_ROLE_ID","0") or 0)
RULES_CHANNEL_ID=int(os.getenv("RULES_CHANNEL_ID","0") or 0)
SHOP_NAME=os.getenv("SHOP_NAME","ZORDX STORE")
DB_PATH=os.getenv("DB_PATH","zordx.sqlite3")
PRODUCTS_PATH=os.getenv("PRODUCTS_PATH","products.json")
intents=discord.Intents.default()
intents.members=True
bot=commands.Bot(command_prefix="!", intents=intents)

def db_init():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("CREATE TABLE IF NOT EXISTS warnings(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,user_id INTEGER,moderator_id INTEGER,reason TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS tickets(channel_id INTEGER PRIMARY KEY,guild_id INTEGER,opener_id INTEGER,status TEXT DEFAULT 'open')")

def embed(title, description):
    return discord.Embed(title=title, description=description, color=discord.Color.red())

def staff(i):
    return isinstance(i.user, discord.Member) and (i.user.guild_permissions.administrator or i.user.guild_permissions.manage_guild or i.user.guild_permissions.manage_messages)

async def staff_check(i):
    return staff(i)

class CloseTicket(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Fechar ticket",style=discord.ButtonStyle.danger,emoji="🔒",custom_id="zordx:close")
    async def close(self,i,b):
        if not i.guild or not isinstance(i.channel,discord.TextChannel):
            return await i.response.send_message("Use no canal do ticket.",ephemeral=True)
        with sqlite3.connect(DB_PATH) as c:
            row=c.execute("SELECT opener_id,status FROM tickets WHERE channel_id=?",(i.channel.id,)).fetchone()
        if not row or row[1]!="open":
            return await i.response.send_message("Ticket já fechado ou não registrado.",ephemeral=True)
        if i.user.id!=row[0] and not staff(i):
            return await i.response.send_message("Só quem abriu ou a equipe pode fechar.",ephemeral=True)
        with sqlite3.connect(DB_PATH) as c: c.execute("UPDATE tickets SET status='closed' WHERE channel_id=?",(i.channel.id,))
        await i.response.send_message("🔒 Ticket fechado.")
        try:
            await i.channel.edit(name=("fechado-"+i.channel.name)[:100])
            await i.channel.set_permissions(i.guild.default_role,view_channel=False)
        except discord.HTTPException: pass

class TicketPanel(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Abrir ticket",style=discord.ButtonStyle.success,emoji="🎫",custom_id="zordx:open")
    async def open(self,i,b):
        g=i.guild
        if not g: return await i.response.send_message("Use dentro do servidor.",ephemeral=True)
        with sqlite3.connect(DB_PATH) as c:
            row=c.execute("SELECT channel_id FROM tickets WHERE guild_id=? AND opener_id=? AND status='open'",(g.id,i.user.id)).fetchone()
        if row and g.get_channel(row[0]):
            return await i.response.send_message(f"Você já tem ticket: {g.get_channel(row[0]).mention}",ephemeral=True)
        cat=g.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        if not isinstance(cat,discord.CategoryChannel): cat=None
        perms={g.default_role:discord.PermissionOverwrite(view_channel=False),i.user:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,attach_files=True)}
        if g.me: perms[g.me]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,manage_channels=True)
        role=g.get_role(TICKET_SUPPORT_ROLE_ID) if TICKET_SUPPORT_ROLE_ID else None
        if role: perms[role]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
        try:
            safe="".join(x.lower() if x.isalnum() else "-" for x in i.user.name).strip("-") or str(i.user.id)
            ch=await g.create_text_channel(("ticket-"+safe)[:90],category=cat,overwrites=perms)
            with sqlite3.connect(DB_PATH) as c: c.execute("INSERT OR REPLACE INTO tickets(channel_id,guild_id,opener_id,status) VALUES(?,?,?,'open')",(ch.id,g.id,i.user.id))
            await ch.send(f"{i.user.mention}",embed=embed("🎫 Suporte ZORDX STORE","Explique como podemos ajudar. Nunca envie senhas ou códigos de autenticação."),view=CloseTicket())
            await i.response.send_message(f"Ticket criado: {ch.mention}",ephemeral=True)
        except discord.Forbidden:
            await i.response.send_message("Falta permissão Gerenciar Canais ao bot.",ephemeral=True)

@bot.event
async def on_ready():
    db_init()
    bot.add_view(TicketPanel()); bot.add_view(CloseTicket())
    try:
        if GUILD_ID:
            guild=discord.Object(id=GUILD_ID); bot.tree.copy_global_to(guild=guild); await bot.tree.sync(guild=guild)
        else: await bot.tree.sync()
    except Exception: logging.exception("Falha ao sincronizar comandos")
    logging.info("Conectado como %s",bot.user)

@bot.event
async def on_member_join(m):
    ch=m.guild.get_channel(WELCOME_CHANNEL_ID) if WELCOME_CHANNEL_ID else None
    if isinstance(ch,discord.TextChannel):
        e=embed(f"👋 Bem-vindo à {SHOP_NAME}!",f"Olá {m.mention}! Leia as regras e procure a equipe se precisar de ajuda.")
        e.set_thumbnail(url=m.display_avatar.url); await ch.send(embed=e)

@bot.event
async def on_member_remove(m):
    ch=m.guild.get_channel(WELCOME_CHANNEL_ID) if WELCOME_CHANNEL_ID else None
    if isinstance(ch,discord.TextChannel): await ch.send(embed=embed("🚪 Membro saiu",f"**{discord.utils.escape_markdown(m.display_name)}** saiu do servidor."))

@bot.tree.command(name="painel_tickets",description="Publica o painel de tickets.")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def ticket_panel(i:discord.Interaction):
    await i.channel.send(embed=embed("🎫 Suporte — "+SHOP_NAME,"Clique abaixo para abrir um ticket privado."),view=TicketPanel())
    await i.response.send_message("Painel publicado.",ephemeral=True)

@bot.tree.command(name="regras",description="Mostra as regras da loja.")
@app_commands.guild_only()
async def rules(i:discord.Interaction):
    txt="**1.** Respeite membros e equipe.\n**2.** Confira produto, preço e condições antes de pagar.\n**3.** Use apenas os meios de pagamento oficiais.\n**4.** Nunca compartilhe senhas ou códigos.\n**5.** Use tickets para pedir suporte.\n**6.** Golpes e comprovantes falsos são proibidos.\n**7.** Não faça divulgação sem autorização.\n**8.** Trocas e reembolsos seguem a política da loja e a legislação aplicável.\n**9.** Spam, assédio e discriminação não são permitidos.\n**10.** Violações podem resultar em aviso, timeout, expulsão ou banimento."
    await i.response.send_message(embed=embed("📜 Regras — "+SHOP_NAME,txt))

def get_products():
    try:
        with open(PRODUCTS_PATH,encoding="utf-8") as f: return json.load(f)
    except (OSError,json.JSONDecodeError): return []

@bot.tree.command(name="produtos",description="Lista produtos da loja.")
@app_commands.guild_only()
async def products(i:discord.Interaction):
    items=get_products()
    if not items: return await i.response.send_message("Nenhum produto cadastrado.",ephemeral=True)
    e=embed("🛒 Produtos — "+SHOP_NAME,"Consulte o suporte antes de pagar.")
    for p in items[:25]: e.add_field(name=f"{p.get('name','Produto')} — {p.get('price','Consulte')}",value=str(p.get("description","Sem descrição"))[:900],inline=False)
    await i.response.send_message(embed=e)

@bot.tree.command(name="produto",description="Busca um produto pelo nome exato.")
@app_commands.describe(nome="Nome do produto")
@app_commands.guild_only()
async def product(i:discord.Interaction,nome:str):
    p=next((x for x in get_products() if str(x.get("name","")).casefold()==nome.casefold()),None)
    if not p: return await i.response.send_message("Produto não encontrado. Use /produtos.",ephemeral=True)
    e=embed(p.get("name","Produto"),p.get("description","Sem descrição"))
    e.add_field(name="Preço",value=p.get("price","Consulte"))
    if p.get("link"): e.add_field(name="Link",value=str(p["link"])[:1024],inline=False)
    await i.response.send_message(embed=e)

@bot.tree.command(name="aviso",description="Publica um aviso da loja.")
@app_commands.describe(titulo="Título",mensagem="Texto do aviso")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def announce(i:discord.Interaction,titulo:str,mensagem:str):
    await i.channel.send(embed=embed("📢 "+titulo[:240],mensagem[:4000]))
    await i.response.send_message("Aviso publicado.",ephemeral=True)

@bot.tree.command(name="advertir",description="Registra advertência.")
@app_commands.describe(membro="Membro",motivo="Motivo")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def warn(i:discord.Interaction,membro:discord.Member,motivo:str):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT INTO warnings(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",(i.guild_id,membro.id,i.user.id,motivo[:1000]))
        n=c.execute("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?",(i.guild_id,membro.id)).fetchone()[0]
    await i.response.send_message(f"⚠️ {membro.mention} advertido. Total: **{n}**. Motivo: {motivo[:1000]}")

@bot.tree.command(name="advertencias",description="Consulta advertências de membro.")
@app_commands.describe(membro="Membro")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def warnings(i:discord.Interaction,membro:discord.Member):
    with sqlite3.connect(DB_PATH) as c:
        rows=c.execute("SELECT reason,moderator_id,created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",(i.guild_id,membro.id)).fetchall()
    if not rows: return await i.response.send_message("Sem advertências registradas.",ephemeral=True)
    await i.response.send_message(embed=embed("Advertências — "+str(membro),"\n".join(f"• {d}: {r} (moderador <@{mid}>)" for r,mid,d in rows)[:4000]),ephemeral=True)

@bot.tree.command(name="timeout",description="Aplica timeout temporário.")
@app_commands.describe(membro="Membro",minutos="De 1 a 40320 minutos",motivo="Motivo")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def timeout_member(i:discord.Interaction,membro:discord.Member,minutos:app_commands.Range[int,1,40320],motivo:str):
    if membro.top_role>=i.user.top_role and i.guild.owner_id!=i.user.id:
        return await i.response.send_message("Cargo igual ou superior ao seu.",ephemeral=True)
    try:
        await membro.timeout(timedelta(minutes=minutos),reason=motivo[:500])
        await i.response.send_message(f"🔇 {membro.mention} recebeu timeout de {minutos} minuto(s). Motivo: {motivo}")
    except discord.Forbidden: await i.response.send_message("Sem permissão para aplicar timeout.",ephemeral=True)

@bot.tree.command(name="expulsar",description="Expulsa um membro.")
@app_commands.describe(membro="Membro",motivo="Motivo")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def kick(i:discord.Interaction,membro:discord.Member,motivo:str):
    if membro.id==i.user.id or membro.id==i.guild.owner_id: return await i.response.send_message("Não é possível expulsar essa pessoa.",ephemeral=True)
    if membro.top_role>=i.user.top_role and i.guild.owner_id!=i.user.id: return await i.response.send_message("Cargo igual ou superior ao seu.",ephemeral=True)
    try:
        await membro.kick(reason=motivo[:500]); await i.response.send_message(f"👢 {membro} foi expulso. Motivo: {motivo}")
    except discord.Forbidden: await i.response.send_message("Sem permissão para expulsar.",ephemeral=True)

@bot.tree.command(name="banir",description="Bane um membro.")
@app_commands.describe(membro="Membro",motivo="Motivo")
@app_commands.guild_only()
@app_commands.check(staff_check)
async def ban(i:discord.Interaction,membro:discord.Member,motivo:str):
    if membro.id==i.user.id or membro.id==i.guild.owner_id: return await i.response.send_message("Não é possível banir essa pessoa.",ephemeral=True)
    if membro.top_role>=i.user.top_role and i.guild.owner_id!=i.user.id: return await i.response.send_message("Cargo igual ou superior ao seu.",ephemeral=True)
    try:
        await membro.ban(reason=motivo[:500]); await i.response.send_message(f"🔨 {membro} foi banido. Motivo: {motivo}")
    except discord.Forbidden: await i.response.send_message("Sem permissão para banir.",ephemeral=True)

@bot.tree.error @bot.tree.command(name="ping", description="Ver latência do bot")
async def ping(interaction: discord.Interaction):
    ms = round(bot.latency * 1000)
    await interaction.response.send_message(
        f"🏓 Pong! Latência: {ms} ms"
    )


@bot.tree.command(name="ajuda", description="Ver os comandos do bot")
async def ajuda(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🤖 **ZORDX STORE BOT**\n"
        "`/ping` — Latência\n"
        "`/ajuda` — Lista de comandos\n"
        "`/produtos` — Ver produtos\n"
        "`/painel_tickets` — Painel de tickets",
        ephemeral=True
    )
async def command_error(i,error):
    logging.error("Erro de comando: %r",error)
    msg="Não foi possível executar. Confira as permissões e tente novamente."
    if isinstance(error,app_commands.CheckFailure): msg="Comando restrito à equipe."
    try:
        if i.response.is_done(): await i.followup.send(msg,ephemeral=True)
        else: await i.response.send_message(msg,ephemeral=True)
    except discord.HTTPException: pass

if __name__=="__main__":
    if not TOKEN: raise SystemExit("Configure DISCORD_TOKEN nas variáveis de ambiente.")
    db_init()
    bot.run(TOKEN)
