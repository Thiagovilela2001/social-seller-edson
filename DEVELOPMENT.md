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
├── cron/jobs.json               # os 3 jobs, instalados pausados
├── REGRAS-DE-NEGOCIO.md         # RN-001..RN-013 — as regras que o motor impõe (viaja)
├── plugins/instagram-seller/
│   ├── rules.py                 # MOTOR DE REGRAS determinístico + estado (SQLite)
│   ├── instagram_api.py         # gate de envio (última barreira antes da rede)
│   ├── integracoes.py           # Bling/Clint/WhatsApp — FAIL-CLOSED (RN-019)
│   └── __init__.py              # registro das 3 tools + 1 hook
├── scripts/
│   ├── instagram-intake.py      # intake da rota: roda ANTES do LLM, falha fechado
│   └── fila-humana-sla.py       # vigia da fila humana (RN-012): quem passou do prazo
├── tests/                       # 205 testes — NÃO viaja (artefato de desenvolvimento)
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
| `cron/jobs.json` | Os três jobs — **pausados** |
| `REGRAS-DE-NEGOCIO.md` | As regras de negócio que o motor impõe (acompanha `RULES_VERSION`) |
| `plugins/instagram-seller/` | Motor de regras + as tools de envio + o guardrail |
| `scripts/instagram-intake.py` | O intake da rota de webhook |
| `scripts/fila-humana-sla.py` | O vigia de SLA da fila humana (RN-012) |

**Não viaja:** `tests/`, `README.md`, `HANDOVER.md`, `deploy/`, `.gitignore` — artefatos de
desenvolvimento e documentação. Só chega ao cliente o que está em `distribution_owned`.

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

## Motor de regras e testes

Três camadas, independentes de propósito. A regra que importa está repetida em duas delas — mas
com **uma implementação só**, em `rules.py`, para não divergirem com o tempo.

| Camada | Quando roda | O que decide |
|---|---|---|
| `scripts/instagram-intake.py` | antes do LLM | dedupe, opt-out, A0, sanitização, janela |
| `plugins/.../instagram_api.py` | depois do LLM, antes da rede | kill switch, opt-out, janela, cota, texto vazio |
| prompt do agente | durante a resposta | tom, jogada, o que dizer |

Nenhuma camada confia na anterior. Se a de cima estiver desligada, com bug ou contornada por
prompt injection, a de baixo ainda barra. **Nunca mova uma regra para o prompt** — é o que o
PDF §06 proíbe ("confiança não substitui uma regra").

### Rodar os testes

```bash
python -m unittest discover -s tests        # 205 testes, ~10s, sem rede e sem chave de API
```

A mesma suíte roda no GitHub Actions a cada push e PR
(`.github/workflows/testes.yml`) — o parecer OpenClaw registrou "0 execuções" no
Actions, e número em documentação não é homologação enquanto só roda na máquina de
quem escreveu.

| Arquivo | Cobre |
|---|---|
| `tests/test_rules.py` | gatilhos A0, dois níveis, opt-out, janela, dedupe, pré-condições de follow-up, §21 |
| `tests/test_intake.py` | o contrato real do webhook: subprocess, payload no stdin, `[SILENT]`, lotes, falhas |
| `tests/test_gate.py` | kill switch, janela, cota de private reply, opt-out, `executed_action` |
| `tests/test_regras_de_negocio.py` | RN-001..RN-013 no caminho do envio + o **carimbo** de cada bloqueio |
| `tests/test_lgpd.py` | RN-014..RN-018: retenção, expurgo, direito do titular, relatório mascarado |

O teste do carimbo (`TestCarimboDasRegras`) não testa uma regra: protege o contrato do
`REGRAS-DE-NEGOCIO.md`. Se um bloqueio novo sair sem `[RN-nnn]`, o documento passa a mentir sobre o
comportamento do agente — e é ele que o cliente assina.

`test_intake.py` chama o script como o gateway chama — por subprocess, não por import. Testar por
import não provaria a fronteira, que é exatamente onde quebra.

### As duas políticas de erro são deliberadas e opostas

**Para A0, falso positivo é barato.** Escalar sem necessidade custa minutos de atenção; deixar
passar uma crise custa a marca. Por isso os padrões são de alta revocação.

**Mas alerta que sempre toca deixa de ser alerta** — e fila de emergência com ruído é risco de
segurança, não de eficiência. Daí os dois níveis em `A0Rule`:

- `patterns` — **decisivos**: casar um já escala. `quero morrer`, `vou me matar`, `suicidio`.
- `corroborativos` — **ambíguos sozinhos**: só escalam quando DOIS casam. `nao aguento mais` é
  hipérbole do dia a dia ("não aguento mais esse calor") e sozinho não pode virar P0.

E `severity` define o custo: **P0/P1** vão para a fila humana; **P2** (hostilidade) é rótulo —
o agente responde com a instrução de não rebater, sem acordar ninguém.

Ao adicionar um gatilho, decida conscientemente em qual dos dois níveis ele entra, e escreva o
teste dos dois lados: que ele dispara no caso grave **e** que ele não dispara no fluxo normal.
O teste `test_pergunta_de_preco_limpa_nao_dispara_nada` é o mais importante do repositório.

### Testar o gate à mão, no profile instalado

O gate vive no caminho do envio, então dá para provar o bloqueio sem chave de API e sem tocar na
rede — e, importante, **carregando o plugin como o loader carrega**, porque é isso que prova que
o import relativo de `rules` resolve:

```python
import importlib.util, os, sys
from pathlib import Path

home = Path(os.environ["PROFILE"])
pdir = home / "plugins" / "instagram-seller"
spec = importlib.util.spec_from_file_location(
    "instagram_seller", pdir / "__init__.py", submodule_search_locations=[str(pdir)]
)
mod = importlib.util.module_from_spec(spec)
mod.__package__, mod.__path__ = "instagram_seller", [str(pdir)]
sys.modules["instagram_seller"] = mod
spec.loader.exec_module(mod)

rules = sys.modules["instagram_seller.rules"]
api = sys.modules["instagram_seller.instagram_api"]
api._post = lambda p, pl: {"id": "ok"}          # nenhuma rede

rules.record_inbound("u1")
api.send_dm("u1", "oi")                          # passa

rules.kill_switch_path().write_text("")
try:
    api.send_dm("u1", "oi")
    print("PROBLEMA: kill switch não bloqueou")
except api.PolicyBlock as e:
    print("BLOQUEADO:", str(e)[:60])
```

`rules.kill_switch_path()` resolve para o `HERMES_HOME` do profile. Usar `Path.home()` daria o
lugar errado no Windows — foi um bug real do spike original.

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

**O script da rota precisa estar em `scripts/` e declarado.** O Hermes resolve `script:` só
dentro de `<HERMES_HOME>/scripts/` (bloqueia path traversal) e **falha fechado**: script que não
resolve vira evento descartado em silêncio. Um diretório novo no payload sem entrada em
`distribution_owned` produz exatamente isso — o agente fica mudo e nada no `hermes` avisa.
Depois de qualquer mexida em `scripts/` ou na rota, instale e **rode o intake de verdade**:

```bash
cd "<perfil>/scripts"
echo '{"object":"instagram","entry":[{"id":"1","changes":[{"field":"comments",
"value":{"from":{"id":"55"},"media":{"id":"m1"},"id":"c1","text":"quanto custa?"}}]}]}' \
  | HERMES_HOME="<perfil>" python instagram-intake.py
```

Espera-se um JSON com `diretiva: responder`. Se vier `[SILENT]`, o intake não entendeu o payload
— cheque `logs/instagram-intake-falhas.jsonl`.

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

- [ ] `python -m unittest discover -s tests` → **205 testes, OK**
- [ ] `hermes profile install ./social-seller-edson --name sse-teste -y` funciona
- [ ] `plugins doctor` → `3 tool(s), 1 hook(s)`
- [ ] `cron list` → os jobs presentes e pausados
- [ ] `scripts/instagram-intake.py` chegou no profile e responde a um payload de teste
- [ ] `hermes profile update sse-teste -y` preserva o `.env`
- [ ] `SOUL.md` passa o escâner de injeção
- [ ] `git ls-files` não tem nada de `.env`, `auth.json`, `memories/`, `sessions/`
- [ ] Nenhum CRLF nos `.py` de `deploy/`, `plugins/` e `scripts/`
