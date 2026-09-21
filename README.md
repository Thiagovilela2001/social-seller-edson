# Social Seller — time do Edson Burger

Agente de Instagram que atende, modera e vende no perfil **@edsonburger** — moderação,
atendimento e vendas em um só lugar, com memória do lead e freio para chamar humano na hora
certa.

Depois de instalado, ele roda sozinho: responde comentários e Directs, e acorda todo dia às 9h
para trabalhar os leads que esfriaram.

---

## Antes de começar

Você precisa de três coisas:

**1. Hermes instalado.**

```bash
hermes --version
```

Se o comando não existir, instale primeiro:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

**2. Acesso a este repositório.** Ele é **privado** — peça para adicionarem você como
colaborador (no GitHub: *Settings → Collaborators*). Sem isso, o download falha pedindo senha.

**3. As credenciais na mão.** Ele não funciona sem elas, e você consegue todas aqui:

| Credencial | Onde conseguir |
|---|---|
| `IG_ACCESS_TOKEN` | App Meta → Instagram → token de longa duração da conta profissional |
| `IG_USER_ID` | App Meta → ID da conta profissional do Instagram |
| `META_APP_SECRET` | App Meta → Configurações → Básico → Chave secreta do app |
| `META_VERIFY_TOKEN` | **Você inventa.** Qualquer frase longa |
| `WEBHOOK_SECRET` | **Você inventa.** Outra frase longa |
| `TELEGRAM_BOT_TOKEN` | Telegram → fale com o **@BotFather** → `/newbot` |
| `DATABASE_URL` | Endereço do Postgres da operação (`postgresql://usuario:senha@host:5432/banco`) |
| `OPENROUTER_API_KEY` | openrouter.ai → Keys. **Defina um teto de gasto.** |
| `CLINT_MCP_URL` | Opcional. Só necessário para a mineração semanal. |

> As duas frases que você inventa, guarde — ninguém fornece elas. O `META_VERIFY_TOKEN` você
> vai usar de novo no painel da Meta, no último passo.

---

## Instalar

### 1. Instale o agente

```bash
hermes profile install github.com/Thiagovilela2001/social-seller-edson --alias
```

Ele mostra o que vai instalar e uma lista de credenciais, cada uma marcada `needs setting`.
Isso é normal — você preenche no passo seguinte.

### 2. Preencha as credenciais

Descubra onde ficou a pasta do agente:

```bash
hermes profile show social-seller-edson
```

O campo **`Path:`** é ela. Copie o modelo de credenciais e edite:

```bash
cp "<Path>/.env.EXAMPLE" "<Path>/.env"
```

Abra o `.env` num editor de texto, preencha cada linha com o valor da tabela acima e salve.

### 3. Confira

```bash
hermes profile info social-seller-edson
hermes -p social-seller-edson plugins list --plain --no-bundled
```

Na segunda linha, o esperado é:

```
enabled      user     0.1.0    instagram-seller
```

Se aparecer `disabled`, o agente não consegue falar com o Instagram — não siga adiante.

### 4. Ligue as tarefas automáticas ⚠️

**Não pule este passo.** As tarefas vêm desligadas de propósito, e **nada acontece até você
ligar.** Sem isso, o agente atende bem e o follow-up nunca roda.

```bash
hermes -p social-seller-edson cron list
hermes -p social-seller-edson cron resume sse-followup-janela
hermes -p social-seller-edson cron resume sse-mineracao-semanal
```

Confira que os dois saíram de `paused` e passaram a `active`, já com um `Next run` preenchido:

```
sse-followup-janela [active]
  Next run:  2026-09-22T09:00:00-03:00
sse-mineracao-semanal [active]
  Next run:  2026-09-28T08:00:00-03:00
```

> Se você ainda não preencheu `CLINT_MCP_URL`, deixe `sse-mineracao-semanal` pausada — sem essa
> credencial ela não tem o que ler.

### 5. Ligue o agente

```bash
hermes -p social-seller-edson gateway start
curl http://localhost:8644/health
```

O `curl` deve responder `{"status": "ok", "platform": "webhook"}`.

**Último passo, no painel da Meta:** cadastre o webhook apontando para
`https://SEU-DOMINIO/webhooks/instagram`, usando o mesmo `META_VERIFY_TOKEN` que você inventou.
Sem isso, o Instagram não avisa o agente quando alguém comenta ou manda Direct.

---

## Como saber que funcionou

```bash
hermes -p social-seller-edson chat -q "quem é você?"
```

A resposta começa com algo como *"Sou do time do Edson"* — **não** com "sou um assistente
virtual". Se ele responder como assistente genérico, a personalidade não carregou: pare e
avise quem te entregou este agente.

Depois, comente algo no perfil de teste e veja se chegou:

```bash
hermes -p social-seller-edson logs --follow
```

---

## Desligar tudo, na hora

Existe um botão de pânico que bloqueia **todo** envio no Instagram. Para usá-lo, descubra a
pasta do agente:

```bash
hermes profile show social-seller-edson
```

E crie um arquivo vazio chamado `ig-kill-switch` dentro daquele `Path:`.

- **Linux / macOS:** `touch "<Path>/ig-kill-switch"`
- **Windows (PowerShell):** `New-Item -ItemType File "<Path>\ig-kill-switch"`
- **Windows (cmd):** `type nul > "<Path>\ig-kill-switch"`

A partir daí nenhuma mensagem sai. O agente continua lendo e registrando, mas não envia nada.
Para voltar ao normal, apague o arquivo.

> **O Instagram não deixa apagar nem editar mensagem já enviada.** Na dúvida, ligue o kill
> switch e pergunte — é mais barato que uma resposta errada no perfil de alguém com 1,1 milhão
> de seguidores.

---

## Atualizar

Quando sair uma versão nova:

```bash
hermes profile update social-seller-edson
```

Suas credenciais, memórias e conversas **não são tocadas**. Só o agente em si é atualizado.

---

## Se algo der errado

| O que você vê | O que fazer |
|---|---|
| `403` ou pedido de senha no install | Você não tem acesso ao repositório. Peça para adicionar você como colaborador. |
| `disabled` em vez de `enabled` no passo 3 | Rode `hermes -p social-seller-edson plugins doctor "<Path>/plugins/instagram-seller" --ci` |
| O agente responde como assistente genérico | A personalidade não carregou. Avise quem te entregou. |
| Nada é enviado no Instagram | Provavelmente a janela de 24h (o Instagram só permite responder quem falou primeiro) ou o kill switch ligado. |
| O follow-up não roda | Você esqueceu o passo 4. Rode `hermes -p social-seller-edson cron list`. |
| Quer entender tudo que pode dar errado | Leia [`HANDOVER.md`](HANDOVER.md) — as armadilhas, uma por uma. |

---

## Antes de usar em produção

- [ ] Kill switch testado — ligou, e o envio foi recusado
- [ ] Passo 4 feito — as tarefas automáticas ligadas
- [ ] Lista oficial de produtos, preços e links cadastrada
- [ ] Um humano definido para receber as escaladas no Telegram
- [ ] **Duas semanas em modo copiloto**: o agente sugere, um humano aprova cada envio

O modo copiloto não é excesso de zelo. É o que calibra os limites de autonomia com dados reais
— e o que evita queimar a marca do cliente em uma semana de erro.

---

*Quem for **mexer** no agente — mudar personalidade, skills ou plugin — comece por
[`DEVELOPMENT.md`](DEVELOPMENT.md).*
