# Handover — o que quebra um deploy em silêncio

Checklist de entrega. Cada item aqui existe por um comportamento verificado do Hermes, não por
precaução genérica. **Nada disso é opcional.**

---

## 1. Checklist de instalação

Rode na máquina do cliente, em ordem. Cada passo tem critério de conclusão.

| # | Comando | Conclusão esperada |
|---|---|---|
| 1 | `hermes profile install <url> --alias` | Manifesto impresso; cada env var marcada `set` ou `needs setting` |
| 2 | `cp <profile>/.env.EXAMPLE <profile>/.env` e preencher | As 10 variáveis do manifesto preenchidas |
| 3 | `hermes profile info social-seller-edson` | Versão e source registrados |
| 4 | `hermes -p social-seller-edson plugins doctor plugins/instagram-seller` | Aprovado, sem imports legados |
| 5 | `hermes -p social-seller-edson plugins list` | `instagram-seller` com status `enabled` |
| 6 | `hermes -p social-seller-edson chat -q "quem é você?"` | **Responde como "time do Edson"**, não como assistente genérico |
| 7 | `hermes -p social-seller-edson cron list` | Os 3 jobs listados como **paused** |
| 8 | `hermes -p social-seller-edson cron resume sse-followup-janela` | Job passa a `scheduled` |
| 9 | `hermes -p social-seller-edson gateway start` | Gateway no ar; `curl localhost:8644/health` → `{"status":"ok"}` |
| 10 | `hermes -p social-seller-edson tools list` | `ig_send_dm`, `ig_private_reply`, `ig_reply_comment` visíveis |

**O passo 6 é o único que prova que a personalidade carregou.** Se ele responder como
assistente genérico, o `SOUL.md` não entrou — e você entregou um agente sem alma.

**O passo 7/8 é onde o deploy morre em silêncio.** Cron de distribution **não é agendado
automaticamente**, por segurança. O instalador instala e para. Se ninguém retomar, o agente
atende bem e **o follow-up nunca roda** — falha silenciosa, o tipo pior. Os jobs já vêm
**pausados de propósito**, para que a falta de habilitação seja visível em `cron list` em vez
de invisível.

---

## 2. O kill switch

Arquivo-flag dentro do `HERMES_HOME` do profile. Presente = **nenhum envio é permitido**.

```bash
# Descobrir o home do profile
hermes -p social-seller-edson config env-path      # mostra o caminho do .env; o home é o pai

# LIGAR (bloquear tudo)
touch "<HERMES_HOME>/ig-kill-switch"

# DESLIGAR
rm "<HERMES_HOME>/ig-kill-switch"
```

No Windows, o `HERMES_HOME` de um profile fica em
`%LOCALAPPDATA%\hermes\profiles\social-seller-edson\` — **não** em `~/.hermes`.

Você pode apontar outro caminho com a variável `IG_KILL_SWITCH` no `.env`.

**Teste antes de entregar:** ligue a flag e peça ao agente para enviar uma DM. O envio tem que
ser recusado com a mensagem de kill switch. Se sair mensagem, o guardrail não está de pé.

---

## 3. As armadilhas, uma por uma

### 3.1 🔴 `.gitignore` depois do primeiro commit
A exclusão dura do Hermes roda **no instalador** — ela protege o cliente, **não** você. O
`.gitignore` deste repositório foi criado **antes** de qualquer commit, mas se você rodar o
profile uma vez para testar, o diretório passa a conter `.env`, `auth.json`, `memories/` e
`sessions/`. **Confira `git status` a cada commit.** Segredo commitado fica no histórico do git
mesmo depois de removido.

### 3.2 🟠 Não existe pinning de versão
O install faz `git clone --depth 1` e **segue a branch default**. Você não consegue garantir que
o cliente instalou exatamente a v1.0.0. Trate `main` como release, e registre a versão instalada
de cada cliente com `hermes profile info`.

### 3.3 🟠 Cron não é auto-agendado
Ver §1, passos 7 e 8. Vale repetir: é a falha mais silenciosa do pacote.

### 3.4 🟠 `cron/jobs.json` é sobrescrito em `profile update`
O cron store do Hermes é **um único arquivo**, `<profile>/cron/jobs.json`. Como `cron/` está em
`distribution_owned`, o update **substitui o arquivo inteiro** — inclusive jobs que o cliente
tenha criado por conta própria.

**Mitigação:** antes de rodar `update` num cliente, exporte
`hermes -p social-seller-edson cron list`, e depois do update confira se os jobs dele continuam
lá e recrie o que faltar. Se o cliente for criar muitos jobs próprios, considere tirar `cron/`
de `distribution_owned` e passar a instalar os jobs por instrução.

### 3.5 🟠 `SOUL.md` e skills ficam ativos no primeiro chat
Não há etapa de aprovação. A personalidade entra em vigor imediatamente. Teste a distribution
em um profile separado antes de entregar, e revise o `SOUL.md` como código que vai rodar em
produção — porque é.

Além disso: um `SOUL.md` de distribution é escaneado como **conteúdo de terceiro**. Se ele casar
com um padrão de injeção de prompt, o Hermes **bloqueia o arquivo inteiro** e o agente sobe sem
personalidade — falhando em silêncio outra vez. Se você editar o `SOUL.md`, rode o passo 6 do
checklist depois.

### 3.6 🟡 `config.yaml` é preservado por padrão em update
`config.yaml` **está** em `distribution_owned` deste manifesto, então é sobrescrito por padrão.
É deliberado: uma versão nova que adiciona plugin ou rota não chegaria ao cliente se o config
dele fosse preservado. Em troca, **qualquer ajuste que o cliente faça no `config.yaml` se perde
no update.** Toda configuração específica do cliente — token, URL, preço, senha — vai no
**`.env`**, que nunca é tocado.

### 3.7 🔴 O hook de shell exige consentimento e não vem ligado
O `config.yaml` deste repositório **não** declara `hooks:`. Motivo: hooks de shell exigem uma
entrada em `<profile>/shell-hooks-allowlist.json`, criada por consentimento interativo no
primeiro uso. **Deploy headless não concede sozinho — o hook não registra e falha ABERTO**
(issue [#100942](https://github.com/NousResearch/hermes-agent/issues/100942)). Um guardrail que
pode não registrar não é um guardrail.

O que protege de verdade, e já viaja ligado:
1. O **hook in-process do plugin** (`plugins/instagram-seller/__init__.py`) — sem allowlist, sem
   bit de execução.
2. E principalmente o **`_gate()` dentro de `instagram_api.py`**, no caminho do envio. Código no
   caminho do envio não tem como "não registrar".

Se quiser o hook de shell como defesa extra, veja [`deploy/GUARDRAIL.md`](deploy/GUARDRAIL.md).

### 3.8 🟠 O proxy de borda é obrigatório para o webhook
O adapter de webhook do Hermes **só aceita POST**. A Meta faz um **GET** de verificação antes de
aceitar a URL. Sem o proxy na frente, você não consegue nem cadastrar o webhook. Ver `deploy/`.

### 3.9 ✅ O intake determinístico existe e roda antes do agente
`config.yaml` declara `script: instagram-intake.py` na rota. O script está em `scripts/` e
**viaja** na distribution (está em `distribution_owned`).

Ele resolve, sem gastar um token de LLM: dedupe (a Meta reentrega webhook), opt-out permanente,
janela de 24h, os oito gatilhos A0, sanitização, "conteúdo não é comando" (§21) e a gravação da
interação na base. O resultado é um payload com `diretiva` e `proibicoes` que o agente **obedece,
não redecide**.

**Ele falha FECHADO.** Se quebrar, o evento é descartado — não repassado cru ao modelo. Sem o
motor de regras não se sabe se a pessoa está em crise. O payload original vai para
`logs/instagram-intake-falhas.jsonl`, então nada se perde: dá para reprocessar.

⚠️ **Por isso, monitore esse arquivo.** Um drain nele significa que eventos estão sendo
descartados em silêncio do ponto de vista de quem opera:

```bash
tail -f <perfil>/logs/instagram-intake-falhas.jsonl
```

Se ele crescer, o webhook está recebendo algo que o intake não entende. É falha de configuração
ou formato novo da Meta — as duas merecem conserto rápido.

### 3.10 🟠 O `executed_action` ainda depende de o envio casar com a interação
O gate grava `executed_action` depois do envio confirmado, casando por `comment_id`/`message_id`.
Quando não casa, o envio sai normalmente e fica um WARNING no log — mas a trilha de auditoria
fica com buraco. Vale acompanhar no modo copiloto. Ver PDF §13.

---

## 4. Pendências com o cliente antes do go-live

> As seis primeiras viraram tabela em `REGRAS-DE-NEGOCIO.md` § *Pendências*, com o que cada
> resposta muda no comportamento do agente. Nenhuma é técnica.

- [ ] **Divulgar que é automação?** — o mecanismo está pronto e desligado por padrão
      (`IG_DIVULGAR_AUTOMACAO=false`, RN-008). Muda a voz do agente na cara do cliente.
- [ ] **Política de garantia / devolução / frete por escrito** — sem ela o agente não afirma
      nada disso (RN-011), nem o que é direito legal do consumidor. Fail-closed de propósito.
- [ ] Confirmar **horário (8h–21h) e teto (3 toques/14 dias)** da abordagem proativa (RN-006/007)
- [ ] Definir **quem é o plantão** que recebe o alerta da fila humana e confere o SLA
- [ ] Aprovação explícita da identidade **"time do Edson"**
- [ ] Lista oficial de produtos, preços, garantias e links
- [ ] "O que eu nunca falaria para um cliente" — as linhas vermelhas dele
- [ ] **Credenciais das integrações** (`BLING_API_TOKEN`, `CLINT_MCP_URL`,
      `WHATSAPP_TOKEN`) — é o que separa a RN-019 de virar recurso. Regra, interfaces
      e testes já estão no lugar; falta o acesso.
- [ ] Aprovação da matriz de moderação (critérios e autonomia por ação)
- [ ] **Declarar finalidade, base legal e encarregado (DPO)** — sem encarregado não há
      quem responda ao titular no prazo da LGPD (RN-014..RN-018)
- [ ] **Validar com o jurídico os prazos de retenção** da tabela da RN-015
- [ ] **Ligar o CRM ao direito do titular** — a RN-017 cobre o banco do agente; dado em
      Bling/Clint/planilha fica fora do alcance dela
- [ ] Revisão jurídica: LGPD, termos, política de privacidade, alegações
- [ ] **App Meta no nome do cliente**, com Business Verification e App Review aprovados
- [ ] Validação em conta de teste: janela de 24h, cota única de private reply, e a restrição de
      anexo/botão em private reply para quem não segue a conta
- [ ] Rota de WhatsApp com consentimento (o Instagram não é canal de follow-up)

## 5. Modo copiloto antes de autopiloto

Duas semanas com o agente **sugerindo** e um humano aprovando cada envio.

Isso não é cautela excessiva: gera um dataset rotulado de graça, calibra os limiares de
autonomia que hoje são só recomendação, e evita queimar a marca de um perfil com 1,1M de
seguidores em uma semana de erro.

Revise 30 conversas aleatórias por semana. É o único jeito real de pegar deriva de
personalidade. Se o tom degradar, geralmente é prompt longo demais, few-shot ruim, ou modelo
trocado — e trocar de modelo muda a personalidade mesmo com o mesmo prompt. Revalide sempre.
