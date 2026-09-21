# Development — para quem mexe no agente

O [`README.md`](README.md) é para quem só vai **usar**. Este é para quem vai **alterar**
personalidade, skills, plugin ou cron.

## O que é este repositório

Uma **Profile Distribution** do Hermes Agent: o agente inteiro empacotado como repositório git.
`hermes profile install <url>` materializa o agente; `hermes profile update <name>` atualiza no
lugar sem tocar em memória, sessão, `.env` ou `auth.json`.

Referência completa do mecanismo (schema do manifesto, exclusões, comportamento do instalador):
`references/profile-distributions.md` na skill `hermes-agent`.

## Estrutura

```
social-seller-edson/
├── distribution.yaml            # manifesto: nome, versão, env vars, distribution_owned
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

## O que viaja e o que não viaja

**Viaja** — declarado em `distribution_owned`:

| Item | O que é |
|---|---|
| `distribution.yaml` | Manifesto |
| `SOUL.md` | Personalidade |
| `config.yaml` | Modelo, toolsets, `plugins.enabled`, rota de webhook |
| `skills/` | As três skills |
| `cron/jobs.json` | Os dois jobs — **pausados** |
| `plugins/instagram-seller/` | As tools de envio + o guardrail |

**NUNCA viaja** (exclusão dura, no instalador): `auth.json` · `.env` · `memories/` ·
`sessions/` · `state.db*` · `logs/` · `workspace/` · `plans/` · `home/` · caches · `local/`.

> A exclusão roda **no instalador** — ela protege quem recebe, não você. Um `.gitignore`
> mal feito vaza segredo seu no histórico do git, e segredo commitado continua lá depois de
> removido. **Confira `git status` antes de todo commit.**

**Não viaja porque não é profile** — provisionado no cliente: App Meta · tokens · Postgres ·
conhecimento do produto · RAG · proxy + TLS · bot do Telegram · chave de inferência · credencial
da Clint.

## Ciclo de desenvolvimento

Instale do diretório local, sem precisar de push:

```bash
hermes profile install ./social-seller-edson --name sse-teste --alias
hermes -p sse-teste plugins doctor "<path>/plugins/instagram-seller" --ci
hermes -p sse-teste cron list
hermes -p sse-teste chat -q "quem é você?"

# depois de editar, teste o update no lugar
hermes profile update sse-teste -y

hermes profile delete sse-teste --yes
hermes profile purge-identity sse-teste     # é um passo separado
```

`plugins doctor` respondendo `registrations: 3 tool(s), 1 hook(s)` é o sinal mais barato de que
o plugin **e** o hook in-process estão de pé.

### Testar o guardrail sem gastar token

O `_gate()` vive no caminho do envio, então dá para provar o bloqueio direto — sem chave de
API e sem tocar na rede:

```bash
HERMES_HOME="<path>/profiles/sse-teste" python - <<'PY'
import sys; sys.path.insert(0, "plugins/instagram-seller")
import instagram_api as api
api.KILL_SWITCH.write_text("blocked")
try:
    api.send_dm("igsid_qualquer", "oi")
    print("PROBLEMA: passou!")
except api.PolicyBlock as e:
    print("BLOQUEADO:", str(e)[:60])
finally:
    api.KILL_SWITCH.unlink(missing_ok=True)
PY
```

### Testar a personalidade sem chave de API

Profiles **não herdam credencial** — um profile novo não tem provider, e `chat -q` para com
"not connected to any AI provider". O substituto sem credencial é rodar o `SOUL.md` pelo
escâner de injeção real:

```python
from agent.prompt_builder import _scan_context_content
out = _scan_context_content(open("SOUL.md").read(), "SOUL.md", user_authored=False)
assert not out.startswith("[BLOCKED:"), out[:200]
```

`SOUL.md` de distribution é lido como **conteúdo de terceiro** — um hit de padrão de injeção
**bloqueia o arquivo inteiro** e o agente sobe sem personalidade, com uma linha de log como
único sinal. Rode isso depois de toda edição do `SOUL.md`.

## Regras que não são óbvias

**`main` é release.** Não existe pinning de ref nesta versão do Hermes: o install faz
`git clone --depth 1` e **segue a branch default**. Se você commitar algo quebrado na `main`,
quem instalar naquele intervalo pega o quebrado. Trabalhe em branch e faça merge só quando
estiver validado.

**`distribution_owned` é allowlist, não decoração.** Declarado, só os caminhos listados são
copiados — e `plugins/` **não** está entre os padrões do Hermes. Se você adicionar um diretório
novo ao payload e esquecer de declará-lo, ele simplesmente não chega, em silêncio.

**`config.yaml` é sobrescrito no update** (está em `distribution_owned`, de propósito). Toda
configuração específica do cliente vai no `.env`, que nunca é tocado. O preço disso: se o
cliente editar o `config.yaml`, a edição se perde no próximo update.

**`cron/jobs.json` também é sobrescrito no update** — é um arquivo, não um diretório, então não
é mesclado como as skills. Jobs que o cliente criou por conta própria somem. Exporte
`hermes -p <name> cron list` antes de atualizar um cliente.

**O cron store é um arquivo só.** Jobs individuais em `cron/<nome>.json` não são lidos por
ninguém. Para gerar o schema certo, crie jobs num profile descartável e leia o `jobs.json` que
o Hermes gravou.

**Financemente: LF, não CRLF.** Um script com shebang e CRLF quebra no Linux com
`bad interpreter`. O `.gitattributes` deste repositório força LF — não remova.

## Publicar

```bash
git status                      # LEIA. Nada de .env, memories/, sessions/
git add -A
git commit -m "v1.0.1 — <o que mudou>"
git tag v1.0.1
git push origin main --tags
```

Repositório **privado**. É código comercial + configuração de cliente.

## Verificação antes de cada release

- [ ] `hermes profile install ./social-seller-edson --name sse-teste -y` funciona
- [ ] `plugins doctor` → `3 tool(s), 1 hook(s)`
- [ ] `cron list` → os jobs presentes e pausados
- [ ] `hermes profile update sse-teste -y` preserva o `.env`
- [ ] `SOUL.md` passa o escâner de injeção
- [ ] `git ls-files` não tem nada de `.env`, `auth.json`, `memories/`, `sessions/`
- [ ] Nenhum CRLF nos `.py` de `deploy/` e `plugins/`
