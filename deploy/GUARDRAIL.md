# Guardrail de shell — opcional, e por que ele não vem ligado

Este diretório guarda a infraestrutura do cliente. Nada aqui viaja na distribution: são os
arquivos que você instala na VPS dele.

| Arquivo | O que é |
|---|---|
| `edge_proxy.py` | Proxy de borda sem dependência: responde o GET da Meta e encaminha o POST |
| `Caddyfile` | Versão de produção com TLS automático |
| `nginx.conf` | Alternativa em nginx |
| `ig-send-gate.py` | Hook `pre_tool_call` de bloqueio, em shell |

---

## O proxy de borda é obrigatório

O adapter de webhook do Hermes **só aceita POST**. A Meta faz um **GET** de verificação antes
de aceitar a URL do webhook. Sem o proxy na frente, você não consegue cadastrar o webhook.

Aponte o webhook da Meta para `https://SEU_DOMINIO/webhooks/instagram`. O proxy responde
`hub.challenge` no GET e encaminha o POST intacto para o Hermes.

**Teste antes de cadastrar na Meta:**

```bash
curl -i "http://localhost:8080/webhooks/instagram?hub.mode=subscribe&hub.verify_token=SEU_TOKEN&hub.challenge=12345"
# Esperado: HTTP 200 e corpo exatamente "12345"
```

---

## O hook de shell — leia antes de ligar

O `ig-send-gate.py` **não está declarado no `config.yaml`**, de propósito. Três motivos, todos
verificados:

### 1. Exige consentimento de primeiro uso
Hooks de shell precisam de uma entrada em `<profile>/shell-hooks-allowlist.json`, criada por
consentimento interativo. **Deploy headless não concede sozinho.** Sem a entrada, o hook não
registra e **falha ABERTO** silenciosamente (issue
[#100942](https://github.com/NousResearch/hermes-agent/issues/100942)) — exatamente o oposto do
que se precisa de um guardrail.

### 2. O caminho do comando não é portável
O Hermes resolve o comando do hook com `os.path.expanduser`, e **só isso**. `~` expande para o
`$HOME` do processo — que **não** é o `HERMES_HOME` de um profile. No Windows o Hermes vive em
`%LOCALAPPDATA%\hermes`, não em `~/.hermes`. E não há expansão de `${HOME}` nem de variável de
ambiente no caminho.

Na prática: **o caminho precisa ser absoluto e específico da máquina.** Não existe forma
portável de declarar isso numa distribution, o que é o segundo motivo de ele não viajar.

### 3. O script precisa de bit de execução
`chmod +x` no arquivo, senão o hook não roda.

---

## Se ainda assim quiser ligá-lo

**Passo 1.** Copie o script para um caminho absoluto fora do repositório:

```bash
mkdir -p /opt/social-seller/agent-hooks
cp deploy/ig-send-gate.py /opt/social-seller/agent-hooks/
chmod +x /opt/social-seller/agent-hooks/ig-send-gate.py
```

**Passo 2.** Adicione o bloco em `<profile>/config.yaml`, com o caminho **absoluto**:

```yaml
hooks:
  pre_tool_call:
    - matcher: "^ig_(send_dm|private_reply|reply_comment)$"
      command: "/opt/social-seller/agent-hooks/ig-send-gate.py"
      timeout: 10
      # fail_closed: true é O PONTO INTEIRO deste hook.
      # Sem ele: script ausente, timeout ou lixo no stdout = envio LIBERADO.
      # Com ele: qualquer falha = envio BLOQUEADO.
      fail_closed: true
```

⚠️ Como `config.yaml` está em `distribution_owned`, **o próximo `profile update` apaga esse
bloco.** Se for usar o hook de shell no cliente, tire `config.yaml` de `distribution_owned` — e
aceite que as rotas e o plugin de versões novas não chegarão sozinhos.

**Passo 3.** Aprove o hook e verifique:

```bash
# Aprova todos os hooks de shell sem TTY (equivale a HERMES_ACCEPT_HOOKS=1)
hermes -p social-seller-edson cron --accept-hooks status

# Estado: matcher, timeout, consentimento, bit de execução, mtime
hermes -p social-seller-edson hooks list
hermes -p social-seller-edson hooks doctor
hermes -p social-seller-edson hooks test pre_tool_call
```

O `hooks list` mostra `✓ allowed` ou `✗ not allowlisted`. **Se estiver `✗`, o guardrail não
existe** — e o agente parece protegido sem estar.

---

## As três verificações que importam

| # | Cenário | Esperado |
|---|---|---|
| 3a | Kill switch ligado | mensagem **NÃO** sai |
| 3b | **Hook quebrado** (renomeie o script, tire permissão) | mensagem **NÃO** sai — fail closed |
| 3c | Tudo normal | mensagem sai |

**3b é o teste que importa.** Fail-open é indistinguível de "sem guardrail" até o dia em que
você precisa dele.

Se 3b falhar, o guardrail do hook não serve — e é por isso que a linha de defesa real é o
`_gate()` dentro de `plugins/instagram-seller/instagram_api.py`, no caminho do envio. Ele já
viaja ligado e não depende de consentimento, permissão de arquivo nem caminho absoluto.
