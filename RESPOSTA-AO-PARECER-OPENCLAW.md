# Resposta ao Parecer OpenClaw — Plano de Adequação

**Base do parecer:** Parecer 1.0, 22/09/2026, revisão do commit `e4f3798`.
**Esta resposta:** commit a publicar, `RULES_VERSION 2.3.0`, plugin `0.5.0`, distribuição `1.4.0`.
**Suíte local:** 205 testes, OK (`python -m unittest discover -s tests`).

## Vocabulário de estado (exigido pelo parecer, seção 10)

O parecer pede que não se tratem estes estados como sinônimos. Uso exatamente a
distinção solicitada, e **nada nesta tabela é "liberado para produção"**:

| Estado | Significado aqui |
|---|---|
| **implementado** | Existe código que faz, no caminho real (não só função isolada). |
| **configurado** | Precisa de credencial/parâmetro do cliente. |
| **testado localmente** | Coberto pela suíte, com rede simulada. |
| **homologado em integração** | Verificado contra Meta/Clint/Bling reais, em conta de teste. **Nada aqui.** |
| **liberado para produção** | Autorização do responsável. **Nada aqui** — os bloqueadores fecham por capacidade. |

---

## Achados F01–F06 — bloqueadores de envio

### F01 · Resposta privada usava a chamada da resposta pública — **CONFIRMADO E CORRIGIDO**

**Concordância total.** As duas funções faziam literalmente a mesma chamada:
`_post(f"{comment_id}/replies", {"message": text})`. `POST /{comment_id}/replies`
cria um `IGComment` — **comentário público**. O conteúdo que o agente tratava como
privado era publicado no post. O nome diferente das funções não mudava o destino.

**Ajuste.** A operação privada passou a ser `POST /{ig-user-id}/messages` com
`recipient.comment_id`, que é o mecanismo de private reply. A pública permanece em
`/{comment_id}/replies`.

**Além do pedido, no mesmo ponto:** o parecer pediu "distinguir recusa confirmada de
resultado desconhecido". A cota era marcada **antes** da chamada, tratando falha de
rede como envio consumado. Agora:

- recusa confirmada da Meta (4xx) → a cota foi consumida, marca e propaga;
- timeout/rede (resultado desconhecido) → **não** marca, abre pendência de
  reconciliação e a próxima tentativa no mesmo alvo é bloqueada (RN-021).

| Item | Onde |
|---|---|
| Código | `plugins/instagram-seller/instagram_api.py` (`send_private_reply`) |
| Regra | RN-021 (`rules.py`) |
| Estado | **implementado**, **testado localmente** |
| Testes | `TestRotaPrivadaVersusPublica` (endpoint e payload), `TestResultadoIncerto` |
| Falta | **homologado em integração** — conta de teste da Meta (critério T02) |

### F02 · Proteções por cliente recebiam identidade vazia — **CONFIRMADO E CORRIGIDO**

**Concordância total**, e o diagnóstico é preciso. Os handlers não passavam `igsid`; o
parâmetro tem valor vazio por padrão e as verificações eram **puladas em silêncio**
(`if igsid:`), não bloqueadas. Ausência de identidade virava ausência de restrição.

**Ajuste.** A identidade deixou de ser responsabilidade do chamador: passou a ser
resolvida **no servidor**, a partir do registro que o intake grava
(`rules.igsid_do_comentario`). Três casos reprovam, e nenhum deles é "seguir com
cautela": comentário sem registro, registro sem autor, e igsid declarado que não é o
dono do comentário (**critério T06**: comentário de terceiro não é alvo).

O ponto único de resolução (`vinculo_resolver`) existe porque "lembrar de passar o
parâmetro" é exatamente o que falhou na primeira revisão.

| Item | Onde |
|---|---|
| Código | `instagram_api.py` (`_resolver_vinculo`, rotas de comentário) |
| Regra | RN-020 (`rules.py`) |
| Estado | **implementado**, **testado localmente** |
| Testes | `TestVinculoDeIdentidade` — inclusive o caso que antes era impossível: o chamador **não** passa igsid e a proteção por pessoa continua valendo |
| Falta | Homologação no gateway real (o parecer observa, com razão, que testar a função interna não prova o handler — por isso os testes novos atravessam `api.send_*`, e o `test_intake.py` atravessa o script) |

### F03 · Token do Bling liberava afirmações sem consulta — **CONFIRMADO E CORRIGIDO**

**Concordância total, e o defeito era meu.** `bling_disponivel()` (token preenchido)
liberava a RN-019 enquanto `consultar_pedido` ainda levantava `NotImplementedError`:
bastava um `export` para retirar a barreira.

**Ajuste.** Entrou a distinção obrigatória como estrutura, não como comentário:

```
bling_configurado   → o token existe no ambiente
bling_consulta      → a leitura existe (hoje False, e é código, não credencial)
status_de_pedido    → configurado E implementado. Só isto libera a RN-019
```

Um registro de `CAPACIDADES` no `integracoes.py` é a **única** forma de liberar
afirmação transacional. `consultar_pedido` com token inválido, timeout, pedido de
outra pessoa ou dado ausente **não** vira informação positiva nem negativa.

| Item | Onde |
|---|---|
| Código | `integracoes.py` (`CAPACIDADES`, `status_integracoes`), `rules.py` (RN-019) |
| Estado | **implementado**, **testado localmente** |
| Testes | `TestAdaptadoresFailClosed`, `TestRN019StatusSemFonte` (token sozinho **não** libera; a capacidade libera) |
| Falta | Critério T05 completo (token inválido, timeout, pedido de terceiro com retorno real) exige a integração existir |

### F04 · Ação e proatividade autodeclaradas e opcionais — **CONFIRMADO E CORRIGIDO (com uma ressalva declarada)**

**Concordância no principal:** a aplicação não pode delegar ao próprio pedido do
agente a decisão de quais permissões verificar.

**Ajuste:**
1. `acao` é **obrigatória** nos três schemas, com **vocabulário fechado** (enum vindo
   de `NIVEL_AUTONOMIA`, fonte única). Rótulo inventado e omissão são recusados.
2. No caminho das ferramentas, `exige_acao=True`: omitir bloqueia. Antes, omitir
   desligava a matriz.
3. `proativo` é obrigatório no schema (omitir é erro de ferramenta, não `False`
   silencioso).

**Ressalva declarada, não escondida.** O parecer pede que o tipo de execução venha de
"contexto confiável". Eu derivei o que é derivável sem introduzir dependência de
relógio: para o Instagram, a barreira dura contra uma abordagem proativa
mal declarada já é a **janela de 24h** — declarar `proativo=false` **não compra** uma
janela nova. O que continua autodeclarado é a classificação de uma abordagem que está
*dentro* da janela (a pessoa escreveu há pouco), onde o efeito de errar é menor.
**Derivar proatividade do relógio tornaria a suíte dependente da hora do dia** —
trocar um erro de autorização por um teste que passa de manhã e falha à noite não é
melhoria. Fica registrado como ponto aberto para a adaptação ao OpenClaw, onde o
enfileiramento por tarefa programada fornece o contexto que aqui não existe.

| Item | Onde |
|---|---|
| Código | `schemas.py` (required + enum), `__init__.py` (`_acao_obrigatoria`, `_proatividade_obrigatoria`), `rules.py` (RN-009) |
| Estado | **implementado**, **testado localmente** |
| Testes | `TestAcaoObrigatoria`, e os três schemas em `test_regras_de_negocio` |
| Falta | Critério T04 no caminho de rotina programada (pertence à etapa 5) |

### F05 · Lotes, marcação antecipada e recuperação — **PARCIALMENTE CORRIGIDO**

**Concordância.** O `[SILENT]` em lote descarta interações válidas, a marcação
antecipada impede nova tentativa, e arquivo de falhas não comprova recuperação.

**Feito:** a metade de "não sei se saiu" — a distinção entre recusa confirmada e
resultado desconhecido, com pendência rastreável e retentativa bloqueada (RN-021,
tabela `reconciliacao`).

**Não feito, e digo por quê:** separar o lote em eventos individuais, persistir fila
recuperável e reconciliar automaticamente exige um **executor** com estado de
processamento (`recebido → validado → enfileirado → processando → ação confirmada →
concluído`). No Hermes isso é o intake + o hook; no OpenClaw é a camada de recepção
(adaptadores/fila/n8n) que o próprio parecer descreve na arquitetura-alvo. Implementar
isso agora no runtime que está sendo **substituído** seria construir a peça no lugar
errado. Estado: **não implementado** — pertence à etapa 2 na arquitetura OpenClaw.

### F06 · Agrupamento e isolamento — **NÃO TRATADO (depende da arquitetura-alvo)**

**Concordância**, inclusive no limite da conclusão: não houve mistura comprovada, há
inconsistência de contrato a verificar. O ponto que **não** pode ser transportado sem
validação é `entry.0.messaging.0.sender.id` como chave de agrupamento, junto com o
script que roda antes do agrupamento.

**Ação:** não transportei nada; e o isolamento por conta/canal/cliente/caso é uma
propriedade da camada de recepção do OpenClaw. Estado: **não implementado**.

---

## Governança e documentação — F07 a F10

| Achado | Situação nesta entrega | Ação |
|---|---|---|
| **F07** Calibrar regras sem retirar proteção (`processo` → jurídico, `psicóloga` → saúde mental) | **Concordo.** O caso é real e é o risco oposto ao meu: falso positivo em frase comum de compra é venda perdida. | **Pendente** — precisa da rodada de testes positivos/negativos por regra com contexto. Entra como próximo item, com os dois exemplos citados como caso de teste. |
| **F08** Contradição de identidade (`SOUL.md` proíbe dizer que é IA; intake informa quando devido) | **Concordo, e é contradição real.** | **Pendente** — `SOUL.md` precisa de uma única política coerente com os três modos de `IG_DIVULGAR_AUTOMACAO` (`nunca` \| `sob_pergunta` \| `sempre`). O modo decidido é `sob_pergunta`. |
| **F09** Minimização cobrir alertas e logs (briefing com identificador e texto indo ao Telegram) | **Concordo.** A minimização anterior cobriu o banco, não o caminho de alerta. | **Aberto** — enviar ao time o mínimo, com acesso restrito ao caso completo. |
| **F10** Publicidade e documentação do repositório | **Concordo.** Consulta anterior: público; o README descrevia privado. | **Decidido pelo responsável nesta entrega:** o repositório é **público**, e a documentação passa a dizer isso. Material restrito não entra. |
| **Testes declarados ≠ homologação** (0 execuções no Actions) | **Concordo plenamente** — e o número em documentação não era verificável por terceiros. | **Feito:** `.github/workflows/testes.yml` roda a suíte a cada push/PR. A suíte é stdlib pura: roda sem rede e sem credencial. |

---

## Arquitetura (seção 08) — o que eu **não** fiz, e por quê

O parecer define: **o Social Seller opera no OpenClaw**; o Hermes fica para o agente
técnico. Isso é decisão do responsável e não é minha para contestar — mas tem uma
consequência prática que preciso declarar com clareza:

**Este repositório é uma distribuição de profile Hermes.** Adaptar manifesto,
registro de plugins, rota de webhook, acesso a estado por `HERMES_HOME` e mecanismo de
cron exige a **versão de OpenClaw instalada**, que eu não tenho. O próprio parecer
avisa: "a configuração-alvo deve ser validada contra a versão de OpenClaw instalada;
não basta renomear arquivos". Eu não vou inventar esse contrato.

O que sobrevive, e é a maior parte do valor já produzido — o parecer reconhece isso em
"Reaproveitamento esperado":

- `rules.py` (motor único determinístico), `integracoes.py` (capacidades),
  `scripts/instagram-intake.py`, `scripts/fila-humana-sla.py`;
- a suíte de testes (205), que o parecer pede para usar como ponto de partida;
- `REGRAS-DE-NEGOCIO.md` (RN-001..RN-021), que é política do cliente, não de runtime.

Como o parecer diz que **não é obrigatório reescrever Python**, a forma de preservar
isso é expor o motor como serviço/adaptador com contrato claro para o OpenClaw.
Essa é a etapa 1 ("alinhar") — e depende de acesso ao ambiente OpenClaw.

---

## Critérios de aceite (seção 09) — cobertura local

| ID | Coberto localmente? | Onde |
|---|---|---|
| T02 público × privado | **Sim** (endpoint e payload) | `TestRotaPrivadaVersusPublica` |
| T03 opt-out e controle humano nas rotas | **Sim** | `TestVinculoDeIdentidade`, `TestOptOutNoGate` |
| T04 autorização incompleta | **Parcial** (omissão e rótulo: sim; contexto de tarefa programada: não) | `TestAcaoObrigatoria` |
| T05 token sem consulta | **Parcial** (token sozinho não libera; falhas reais dependem da integração) | `TestRN019StatusSemFonte` |
| T16 regressão de regras | **Parcial** — a suíte tem ambos os lados; falta a rodada com os dois exemplos do F07 | `test_regras_de_negocio`, `test_intake` |
| T01, T06–T15, T17 | **Não.** Dependem de runtime OpenClaw, contas autorizadas e integrações | — |

Os critérios T02–T05 são de **código**, e por isso avançaram. Os demais são de
**ambiente**, e nenhum deles se resolve editando este repositório.

---

## Ordem proposta para continuar

1. **F07** — calibrar padrões com testes positivos/negativos (é código, é barato, e o
   falso positivo custa venda).
2. **F08** — unificar a política de identidade no `SOUL.md` com o modo `sob_pergunta`.
3. **F09** — minimização no caminho de alerta (Telegram) e revisão de logs.
4. **F10/README** — documentar visibilidade pública.
5. **Etapa 1 do parecer (alinhar/OpenClaw)** — mapa `Documento Mestre → componente →
   status → evidência` e contrato do motor para o OpenClaw. **Precisa de acesso ao
   ambiente OpenClaw instalado.**
6. **F05/F06** — fila recuperável e isolamento, já na camada de recepção do OpenClaw.

## O que continua fora do meu alcance (dependência externa)

- Credenciais e contas: Bling, Clint, WhatsApp (Evolution ou nativo), e-mail, Meta Ads.
- Ambiente OpenClaw instalado e sua versão.
- Contas de teste autorizadas para homologação (Meta, e-commerce).
- Google Sheets (5 abas), RAG existente (inventário e conexão), Facebook/Messenger.
- Revisão jurídica, encarregado de dados (DPO) e assinatura do `REGRAS-DE-NEGOCIO.md`.
