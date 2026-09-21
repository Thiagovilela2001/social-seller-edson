# Social Seller — time do Edson Burger

Agente Hermes para o Instagram **@edsonburger**, em três pilares: **moderação** (proteger),
**atendimento** (resolver) e **vendas** (converter) — mais uma camada de inteligência que
minera os WhatsApps da Clint semanalmente.

Este repositório é uma **Profile Distribution** do Hermes Agent: o agente inteiro empacotado
como repositório git. Quem tem acesso instala com **um comando**, atualiza no lugar, e mantém
as próprias memórias, sessões e chaves de API intactas.

> **Isto não é um chatbot.** É um vendedor digital com memória, funil e freio. A diferença está
> em três coisas: ele sabe *onde* a pessoa está no funil, lembra do que já foi dito, e sabe a
> hora de calar a boca e chamar um humano.

## Instalar

```bash
hermes profile install <url-do-repo> --alias
```

O instalador mostra o manifesto, cria o profile, e marca cada variável de ambiente como
`set` ou `needs setting`. Depois:

```bash
# 1. Credenciais — as SUAS
cp "$(hermes profile show social-seller-edson | grep -i path)" ...   # ou veja o caminho impresso
cp <profile>/.env.EXAMPLE <profile>/.env
# preencha com os seus tokens

# 2. Conferir
hermes profile info social-seller-edson

# 3. Habilitar o cron  ← NÃO ESQUEÇA
hermes -p social-seller-edson cron list

# 4. Subir o gateway
hermes -p social-seller-edson gateway start
```

O passo 3 é onde a maioria dos deploys falha em silêncio. Detalhes em [`HANDOVER.md`](HANDOVER.md).

## O que viaja e o que não viaja

**Viaja (domínio do autor):**

| Item | O que é |
|---|---|
| `distribution.yaml` | Manifesto: nome, versão, requisitos de env |
| `SOUL.md` | A personalidade — quem ele é e as linhas que não se movem |
| `config.yaml` | Modelo, toolsets, `plugins.enabled`, a rota de webhook |
| `skills/` | `social-seller-edson` (funil), `moderacao` (A0), `atribuicao` (origem) |
| `cron/jobs.json` | Follow-up da janela e mineração semanal — **pausados na instalação** |
| `plugins/instagram-seller/` | As tools de envio + o guardrail |

**NUNCA viaja** (exclusão dura, no instalador): `auth.json` · `.env` · `memories/` ·
`sessions/` · `state.db*` · `logs/` · `workspace/` · `plans/` · `home/` · `caches`.

**Não viaja porque não é profile** — provisionado no cliente: App Meta · tokens do Instagram ·
Postgres · conhecimento do produto · RAG · proxy de borda + TLS · bot do Telegram · chave de
inferência · credencial da Clint.

## Estrutura

```
social-seller-edson/
├── distribution.yaml            # manifesto
├── SOUL.md                      # personalidade (identidade + não-negociáveis)
├── config.yaml                  # plugin, rota de webhook, MCP
├── skills/
│   ├── social-seller-edson/     # funil, jogadas, few-shots, cadência de follow-up
│   ├── moderacao/               # matriz de moderação, protocolo A0, regras da Meta
│   └── atribuicao/              # media_id como eixo, IGSID como chave
├── cron/jobs.json               # os 2 jobs, instalados pausados
├── plugins/instagram-seller/    # tools ig_* + guardrail de kill switch
└── deploy/                      # infra do cliente: proxy de borda, TLS, hook de shell
```

## Atualizar

```bash
hermes profile update social-seller-edson
```

Puxa a versão nova sem perder memória, sessão, `.env` ou `config.yaml`. Skills e cron que o
cliente criou por conta própria permanecem.

## Desenvolvimento

Instale do diretório local, sem precisar de push:

```bash
hermes profile install ./social-seller-edson --name sse-teste --alias
hermes -p sse-teste plugins doctor plugins/instagram-seller
hermes -p sse-teste chat -q "quem é você?"
hermes profile delete sse-teste --yes
```

**`main` é release.** Não existe pinning de ref nesta versão do Hermes: o install segue a
branch default. Trabalhe em branch e faça merge só quando estiver validado — se você commitar
algo quebrado na `main`, quem instalar naquele intervalo pega o quebrado.

---

Antes de prometer ao cliente que a atualização é um comando, leia [`HANDOVER.md`](HANDOVER.md).
Lá estão as armadilhas que quebram um deploy em silêncio.
