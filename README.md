# ZORDX STORE — Bot Discord (Python)

## Funções
Boas-vindas e saída, tickets privados, regras, lista de produtos e moderação por comandos slash.

## 1. Criar o bot
1. Acesse https://discord.com/developers/applications e crie uma aplicação.
2. Na seção Bot, crie o bot e copie o token. **Não compartilhe o token.**
3. Em Bot > Privileged Gateway Intents, ative **Server Members Intent**.
4. Em OAuth2 > URL Generator, marque `bot` e `applications.commands`.
5. Permissões recomendadas: View Channels, Send Messages, Embed Links, Read Message History, Manage Channels, Kick Members, Ban Members e Moderate Members. Gere o link e adicione o bot ao servidor.

## 2. IDs do servidor
Ative Modo desenvolvedor no Discord (Configurações > Avançado). Copie os IDs do servidor, canal de boas-vindas, categoria de tickets, cargo de suporte e canal de regras. Configure as variáveis correspondentes. Use `0` para campos opcionais.

## 3. Produtos
Edite `products.json` com seus produtos, preços, descrições e links reais.

## 4. Hospedar na nuvem (Render)
1. Envie os arquivos para um repositório privado no GitHub.
2. No Render, crie um **Background Worker** conectado ao repositório.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Configure as variáveis de ambiente do arquivo `.env.example`, principalmente `DISCORD_TOKEN`, `GUILD_ID`, `WELCOME_CHANNEL_ID`, `TICKET_CATEGORY_ID` e `TICKET_SUPPORT_ROLE_ID`.
6. Faça deploy e confira os logs.

O arquivo `render.yaml` é um ponto de partida. Confira as opções atuais do serviço de hospedagem. Sem disco persistente, o banco SQLite pode ser perdido em recriações do serviço.

## 5. Usar
- `/regras`
- `/produtos`
- `/produto nome:<nome exato>`
- `/painel_tickets` (equipe)
- `/aviso titulo:<título> mensagem:<texto>` (equipe)
- `/advertir membro:<membro> motivo:<motivo>` (equipe)
- `/advertencias membro:<membro>` (equipe)
- `/timeout membro:<membro> minutos:<1–40320> motivo:<motivo>` (equipe)
- `/expulsar membro:<membro> motivo:<motivo>` (equipe)
- `/banir membro:<membro> motivo:<motivo>` (equipe)

O bot precisa das permissões correspondentes, e o cargo dele deve ficar acima dos membros que ele irá moderar.
