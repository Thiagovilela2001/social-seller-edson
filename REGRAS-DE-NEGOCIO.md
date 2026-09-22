# REGRAS DE NEGÓCIO — Agente Social Seller (Edson)

**Versão:** 2.3.0 · **Motor:** `plugins/instagram-seller/rules.py` (`RULES_VERSION = "2.3.0"`)
**Base:** Documento Mestre do Agente v2.0 (21/09/2026, 33 pp.) + `agents/docs/05-limites-e-aprovacao-humana.md` + LGPD (Lei 13.709/2018)
**Status:** implementado em código e coberto por testes (`tests/test_regras_de_negocio.py` + `tests/test_lgpd.py`)

---

## 0. Por que este documento existe

O Documento Mestre v2.0 **não tem seção de regras de negócio**. A expressão
"regras de negócio" aparece **uma vez**, de passagem (p.7), como camada que o
OpenClaw não elimina. Em 33 páginas, a palavra "nunca" aparece **duas vezes** — as
duas em "nunca presumir". Não existe nenhuma tabela, lista ou regra dura do tipo
"nunca vender durante uma crise".

Isso não significa que o agente operava sem regras. Significa que ele operava com
regras **que ninguém assinou**: em `rules.py` já rodavam, em código, a matriz A0,
o opt-out permanente, o bloqueio de janela de 24h, o horário de silêncio 21h–8h,
a cota de follow-up e a detecção de injeção de prompt — nenhuma delas autorizada
por escrito pelo cliente.

O risco disso é específico: no dia em que um lead reclamar ("por que o robô me
bloqueou?"), a resposta "está no código" não sustenta nada. **Este documento
transforma o comportamento real do agente em regra auditável e assinável.**

As regras abaixo seguem o formato que o laudo de lacunas definiu: identificador
numerado, texto, escopo, nível de autonomia, exceção e consequência de violação.

Todo bloqueio de envio causado por uma regra de negócio volta carimbado com o
`RN-*` que o causou (`[RN-010] alegação proibida`). Os bloqueios que vêm da própria
Meta — janela de 24h, private reply única por comentário — voltam com o nome da
política, não com um `RN-*`: são limite de plataforma, não regra de negócio.

### Legenda de imposição

| Camada | Significado | Pode ser burlada pelo modelo? |
|---|---|---|
| **CÓDIGO** | `rules.py` barra antes do envio (`instagram_api._autorizar`) | Não |
| **PROMPT** | instrução no system prompt / SKILL.md | Sim, se a instrução for fraca |
| **CASCO** | `scripts/instagram-intake.py` decide antes do LLM | Não (roda antes) |
| **GATE** | `deploy/ig-send-gate.py` (hook de shell, desligado por padrão) | Não |

---

## RN-001 — Parada de emergência (kill switch)

- **Texto:** existe um interruptor que para 100% dos envios do agente, imediatamente,
  sem precisar de deploy, restart ou aprovação técnica.
- **Escopo:** todos os contatos, todos os canais, resposta e abordagem.
- **Autonomia:** não se aplica — é controle do humano, não ação do agente.
- **Exceção:** nenhuma. É a única regra sem exceção.
- **Violação:** envio que sai com o interruptor ligado. O intake declara
  `estado.kill_switch_ativo` e todo envio levanta `PolicyBlock`.
- **Imposição:** CÓDIGO + CASCO + GATE.

> **Ligar:** `touch <HERMES_HOME>/ig-kill-switch` · **Desligar:** `rm`.
> Vale a pena testar isto em produção, com o agente rodando, antes de precisar.

---

## RN-002 — Menor de idade: nenhuma oferta, nenhum link, nenhum preço

- **Texto:** identificada a menoridade, o agente **não** apresenta preço, valor,
  link, oferta, condição de pagamento nem convite a compra. Em nenhum canal.
- **Escopo:** o contato inteiro, enquanto a flag `menor_idade` estiver ativa.
- **Autonomia:** A0 — humano sempre. Não se automatiza em nenhuma fase de maturidade.
- **Exceção:** encerrar a conversa e encaminhar para humano continua permitido
  (bloquear isso deixaria o agente mudo).
- **Violação:** bloco `[RN-002]`; o envio não sai; caso humano aberto com SLA de 15 min.
- **Imposição:** CÓDIGO (`FLAGS_QUE_BLOQUEIAM_COMERCIAL`) + PROMPT.

---

## RN-003 — Crise emocional e vulnerabilidade nunca viram venda

- **Texto:** diante de sofrimento emocional, menção a autoagressão, crise de
  ansiedade, desespero financeiro ou questão de saúde mental, o agente **acolhe e
  passa para humano**. Não vende, não oferece, não promete alívio, não usa a dor
  como argumento de urgência, não diz "isso passa".
- **Escopo:** o contato inteiro. Vale para `crise_emocional`, `saude_mental` e
  `desespero_financeiro`.
- **Autonomia:** A0.
- **Exceção:** a mensagem de acolhimento aprovada (sem preço, sem link) **não é
  bloqueada pelo gate**. Mas atenção ao caminho real, porque os dois níveis
  divergem de propósito: no intake, uma flag de crise P0 **pausa a automação**
  (`pause_automation`), então o agente devolve `[SILENT]` — quem fala é o humano,
  e o caso P0 entra na fila com prazo de 15 minutos (`[SILENT]` nunca significa
  "ninguém viu": o flag, o caso e o relógio ficam registrados).
  Ou seja: **hoje o agente não envia o acolhimento sozinho.** O gate permite; o
  intake silencia. É a Pendência 7 — decisão do Edson, não do código.
- **Violação:** bloco `[RN-003]`, escalonamento imediato com prazo de 15 minutos.
- **Imposição:** CÓDIGO + CASCO (proibições específicas por gatilho) + PROMPT.

> **Protocolo de crise:** a mensagem de acolhimento e a referência ao **CVV 188**
> vivem em `skills/moderacao/SKILL.md`. Revisar o texto com o Edson antes do
> go-live: é a mensagem mais importante que este agente pode enviar, e a única
> que não pode errar o tom.

---

## RN-004 — Dados de terceiro: não processar, não repetir, não armazenar

- **Texto:** se a pessoa enviar ou oferecer documento, CPF, comprovante ou cadastro
  **de outra pessoa** (cônjuge, pai, mãe, filho, amigo), o agente não processa, não
  repete o dado, não pede de novo e não armazena. Encaminha para humano.
- **Escopo:** qualquer dado pessoal que não seja do próprio titular da conversa.
- **Autonomia:** A0.
- **Exceção:** se o próprio titular mandar o **próprio** comprovante para tratar do
  **próprio** pedido, é operação normal e não dispara.
- **Violação:** bloco `[RN-004]`; caso P0; conteúdo comercial suspenso.
- **Imposição:** CÓDIGO (detecção + bloqueio) + CASCO.

> **Limite honesto desta regra:** ela detecta por linguagem ("documento do meu
> marido", "comprei no nome do meu pai"). Não há verificação de identidade nem
> controle de vínculo do cliente — isso exigiria o Bling/Clint, que o PDF promete
> (p.11) e **cujo código não existe** no agente entregue. Enquanto essa integração
> não existir, RN-004 é uma barreira de linguagem, não uma barreira de dados.
> Registrado como pendência P0 do laudo.

---

## RN-005 — Opt-out é da pessoa, não do canal

- **Texto:** pedido de parar ("não tenho interesse", "para de me mandar", "me tira
  da lista") encerra **toda** comunicação com aquela pessoa, em todos os canais —
  Instagram, WhatsApp, e-mail. Vale para sempre.
- **Escopo:** pessoa, permanentemente, cross-canal.
- **Autonomia:** A3 (o agente aplica sozinho e reporta). Aplicar opt-out é sempre
  permitido; nunca requer aprovação.
- **Exceção:** nenhuma. Um pedido de parar não é negociável, não se pergunta o
  motivo e não se tenta reverter.
- **Violação:** bloco `[RN-005]`. Reabordagem de quem pediu para sair é a falta
  mais grave da lista depois de RN-003.
- **Imposição:** CÓDIGO (`pode_contatar_por`, checado antes de qualquer envio).

> **Despedida única:** o agente agradece em uma linha e encerra. Não é violação da
> regra — é o encerramento educado que o próprio PDF prevê (p.16). A regra proíbe
> reabrir a conversa, não proíbe se despedir.

---

## RN-006 — Horário: abordagem proativa respeita, resposta não

- **Texto:** o agente só **procura** a pessoa entre **8h e 21h (BRT)**. Fora dessa
  janela, nenhuma abordagem proativa sai — nem follow-up, nem reativação, nem aviso.
- **Escopo:** envios proativos (`proativo: true`). **Não** se aplica a responder
  quem acabou de escrever.
- **Autonomia:** A2 — o agente decide a abordagem, o horário é imposto.
- **Exceção:** resposta a mensagem recebida sai a qualquer hora, inclusive 3h da
  manhã. Quem escreveu às 3h está acordado e esperando; bloquear ali seria abandono,
  não proteção.
- **Violação:** bloco `[RN-006]`; a tentativa **não consome cota** de follow-up.
- **Imposição:** CÓDIGO (`in_quiet_hours`, 21h–8h) + PROMPT.

> **Pendência do cliente:** 8h–21h é o que o código já usava. Confirmar com o Edson
> se é o horário dele e se fim de semana é igual.

---

## RN-007 — Teto de frequência

- **Texto:** nenhuma pessoa recebe abordagem proativa mais de **3 vezes em 14 dias**,
  respeitando intervalo mínimo de **20 horas** entre abordagens.
- **Escopo:** abordagem proativa por pessoa, somando todos os motivos.
- **Autonomia:** A2.
- **Exceção:** resposta a contato espontâneo da pessoa não conta para o teto — é
  conversa, não prospecção.
- **Violação:** bloco `[RN-007]`. Persistir depois do teto transforma o agente em
  spam e queima a conta do cliente.
- **Imposição:** CÓDIGO (`pode_tocar_proativo`, `registrar_toque_proativo`).

> **Princípio de projeto:** o toque só é contado **depois** de o envio ser
> confirmado. Uma tentativa recusada pelo horário errado não queima a cota — senão
> o agente perderia o toque por ter tentado na hora errada.

---

## RN-008 — Divulgação de automação

- **Texto:** o agente não se passa por pessoa. Quando a divulgação é devida, responde
  a verdade em uma linha: *"Sou o assistente automático do time do Edson — se
  preferir, chamo uma pessoa do time pra continuar com você."*
- **Escopo:** conforme o **modo** configurado em `IG_DIVULGAR_AUTOMACAO`:

| Modo | Comportamento |
|---|---|
| `nunca` | não se anuncia em hipótese alguma — inclusive se perguntarem |
| **`sob_pergunta`** *(padrão)* | responde a verdade quando **a pessoa pergunta**; não se anuncia sozinho |
| `sempre` | anuncia na primeira interação relevante de toda conversa |

- **Autonomia:** A1 (decisão de marca, não de execução). O modo é configuração — não é
  escolha do modelo em tempo de conversa.
- **Exceção:** a persona pública do agente na bio do perfil.
- **Violação:** publicidade enganosa. É o risco jurídico mais barato de resolver e o
  mais caro de ignorar.

> ✅ **DECISÃO TOMADA — modo `sob_pergunta`.** Ficou definido que o agente **não se
> anuncia sozinho** e **não mente se perguntarem**. É o meio-termo entre a persona que
> quebra no primeiro "oi" e a mentira por omissão: quem pergunta "você é um robô?"
> recebe a verdade, no meio do funil, sem aviso de robô no primeiro contato.
>
> Vale registrar o que o modo `nunca` significaria: não se anunciar **nem quando
> perguntam diretamente** é mentira por omissão. Continua disponível por configuração,
> e continua sendo decisão de risco do dono — não do código.
>
> Dois detalhes que o código trava: o valor `true` no `.env` de quem já instalou
> equivale a `sempre` e **não muda de sentido em silêncio**; e valor desconhecido (erro
> de digitação) **cai no padrão**, nunca em `nunca` — errar uma variável não pode
> silenciar a divulgação na direção mais arriscada.

---

## RN-009 — Matriz de autonomia (A0 → A3)

- **Texto:** cada ação do agente tem um nível de autonomia, e o nível é imposto em
  código — não confiado ao bom senso do modelo.

| Nível | Significado | Ações |
|---|---|---|
| **A3** | Age e reporta | agradecer, encerrar, aplicar opt-out, responder elogio |
| **A2** | Envia, humano audita | dúvida da base, private reply, diagnóstico, informar preço, enviar link, quebra de objeção, follow-up em janela, onboarding pós-compra |
| **A1** | Humano aprova antes | upsell, lead de alto valor, pergunta fora da base, imprensa/parceria, produto novo, mudança de tom |
| **A0** | Humano sempre | desconto, negociação de preço, reclamação, reembolso, jurídico, crise emocional, desespero financeiro, hostilidade, menor de idade, dúvida sensível |

- **Escopo:** toda ação que resulte em envio ou alteração de estado do lead.
- **Autonomia:** é a própria regra das autonomias.
- **Exceção:** ação com nome desconhecido **não** é bloqueada (bloquear por nome
  quebraria o agente a cada ação nova). O buraco é fechado por outro lado — a flag
  ativa do lead barra o conteúdo comercial independentemente do nome da ação.
- **Violação:** bloco `[RN-009]` com o nível no motivo (`'desconto' é A0`).
- **Imposição:** CÓDIGO (`pode_executar`) + PROMPT.

> **Aprovação A1 é registrada e expira.** Aprovar "upsell" para um lead não libera
> upsell para sempre nem para todos: a aprovação é por ação e por lead, com validade.

---

## RN-010 — Alegações proibidas (guardrail de saída)

- **Texto:** o agente não promete resultado, prazo, renda, cura ou transformação;
  não fabrica escassez nem urgência.

| Categoria | Exemplo que é bloqueado |
|---|---|
| `promessa_de_resultado` | "você vai faturar 6 dígitos", "resultado garantido" |
| `promessa_de_prazo` | "em 30 dias você já está vendendo" |
| `promessa_de_renda` | "vira renda garantida todo mês" |
| `escassez_falsa` | "são as últimas vagas" |
| `urgencia_fabricada` | "só hoje" |
| `linguagem_de_cura_ou_saude` | "o método cura a depressão" |
| `garantia_de_transformacao` | "isso vai mudar sua vida" |

- **Escopo:** **todo** texto de saída — DM, private reply e comentário público.
  Comentário público tem o mesmo filtro: é onde a promessa faz mais estrago.
- **Autonomia:** A0 sobre o texto. Não existe "só essa vez".
- **Exceção:** escassez e urgência **reais** podem ser liberadas por aprovação
  registrada (`escassez_falsa`, `urgencia_fabricada` são as únicas liberáveis).
  Sem aprovação viva, continuam bloqueadas — "é verdade" não é critério, é
  alegação de quem escreveu.
- **Violação:** bloco `[RN-010]`. **Reescrever a mesma mensagem para escapar do
  padrão é fraude de guardrail** e escala para humano — o prompt diz isso
  explicitamente ao agente.
- **Imposição:** CÓDIGO (`detect_alegacao_proibida`).

> **Calibração medida:** o teste `test_falso_positivo_em_conversa_legitima` fixa
> 7 frases normais de funil ("o livro custa R$ 97", "quer que eu te mande o link?")
> que **precisam** passar. Falso positivo aqui não é bug cosmético: é venda perdida
> no meio da conversa.

---

## RN-011 — Garantia, devolução e frete só com fonte aprovada

- **Texto:** o agente não afirma garantia, prazo de devolução, política de troca ou
  frete grátis de cabeça. Só repete o que estiver na política aprovada pelo cliente.
- **Escopo:** qualquer afirmação sobre direito do consumidor.
- **Autonomia:** A1.
- **Exceção:** com `IG_POLITICA_CONSUMIDOR` preenchida, o agente pode afirmar
  **exatamente** o que está escrito ali — nada além.
- **Violação:** bloco `[RN-011]`. Prometer o que o cliente não oferece cria
  passivo de CDC que o agente não tem como honrar.
- **Imposição:** CÓDIGO (`detect_afirmacao_consumidor_sem_fonte`, compara com o
  texto aprovado).

> **Fail-closed deliberado:** sem política cadastrada, "você tem 7 dias para
> devolver" (que é direito legal) **também** é bloqueado. É intencional: não se
> afirma direito do consumidor em nome do cliente sem que ele tenha dito que
> oferece. Preencher a variável resolve — é uma linha.

---

## RN-012 — Espera humana: SLA, e o que o agente faz enquanto espera

- **Texto:** quando o agente escala para humano, o caso entra numa fila com **prazo
  de resposta**. Enquanto o caso está aberto, o agente pode **acolher** e não pode
  **vender**. Quando um humano **assume**, o agente fica em silêncio total.

| Prioridade | Prazo | O que entra aqui |
|---|---|---|
| **P0** | 15 min | crise emocional, menor, jurídico, dados de terceiro |
| **P1** | 60 min | reclamação, reembolso, pedido de humano, desconto |
| **P2** | 4 h | hostilidade, dúvida fora da base |
| **P3** | 24 h | dúvida comum, ajuste de tom |

- **Escopo:** todo caso escalado.
- **Autonomia:** a regra é do humano: o agente **não** decide o prazo, e o prazo é
  medido e cobrado.
- **Exceção:** caso aberto e **não assumido** ainda deixa o agente falar (é a janela
  exata em que ele envia o acolhimento aprovado). Caso não resolvido **não** libera
  a venda automaticamente.
- **Violação:** envio com `humano_no_comando` ativo → bloco `[RN-012]`. Caso fora do
  SLA → aparece em `fila_humana.fora_do_sla` e no relatório do vigia. Resolver o
  problema tem prioridade sobre vender (§17 do documento).
- **Imposição:** CÓDIGO (`abrir_caso_humano`, `assumir_caso_humano`,
  `resolver_caso_humano`, `humano_no_comando`) + vigia (`scripts/fila-humana-sla.py`).

> **Um SLA que ninguém cobra é enfeite.** Por isso existe o vigia: ele lista os
> casos que passaram do prazo, ordenados por gravidade (crise de 5 min antes de
> dúvida de 30 min) e marca quantas vezes o caso já foi cobrado. Caso reincidente é
> caso sem dono — e isso precisa aparecer, não sumir na fila.

---

## RN-013 — Moderação destrutiva exige autorização com nome e critério

- **Texto:** o agente não oculta, exclui ou bane comentário por conta própria —
  mesmo quando o comentário é golpe, ofensa ou reclamação pública.
- **Escopo:** ações destrutivas em comentários (`ocultar`, `excluir`, `banir`).
- **Autonomia:** A0.
- **Exceção:** **reexibir/restaurar é sempre permitido, sem aprovação** — corrigir
  um erro de moderação não pode depender de aprovação, senão comentário removido por
  engano fica removido para sempre.
- **Violação:** `moderacao_autorizada()` retorna falso; a ação não executa.
  Autorizar sem `autorizado_por` ou sem `justificativa` levanta erro — "sem nome e
  sem motivo" é o que produz censura sem dono.
- **Imposição:** CÓDIGO + PROMPT (+ `skills/moderacao`).

---

## RN-014 a RN-018 — Proteção de dados (LGPD)

Estas cinco não estavam no Documento Mestre em nenhuma forma: `LGPD`, `CDC`,
`consentimento`, `base legal`, `retenção`, `titular` e `DPO` têm **zero ocorrências**
nas 33 páginas. As RN-001..013 diziam o que o agente pode **fazer**; nenhuma dizia o
que ele pode **guardar**, por quanto tempo, nem o que fazer quando o titular pedir
para apagar.

O que existia, medido no banco antes desta seção: o texto da mensagem enviada
(`followups.mensagem`), a pergunta literal da pessoa (`lacunas.pergunta`) e — o pior —
o briefing com o **texto da crise** dentro da fila humana (`fila_humana.resumo`), que
ainda era impresso numa mensagem de Telegram. Dado sensível, ligado a identificador,
sem prazo, saindo do perímetro.

---

## RN-014 — Minimização: não guardar o que não precisa

- **Texto:** o agente guarda o mínimo necessário para atender. Texto de mensagem não
  entra em tabela de operação; documento, telefone, e-mail e arroba são removidos
  antes de qualquer gravação.
- **Escopo:** toda escrita no banco de estado.
- **Autonomia:** A3 — é comportamento do sistema, não decisão por conversa.
- **Exceção:** o texto integral continua acessível **na conversa do Instagram**, onde
  ele já está e de onde veio. O que não existe é uma segunda cópia dentro do agente.
- **Violação:** o resumo do caso humano conter o texto da pessoa.
- **Imposição:** CÓDIGO (`resumo_para_fila`, `redigir`).

> A pergunta "como o time sabe o que aconteceu?" fica respondida pelo próprio resumo:
> `crise_emocional · severidade P0 · flags crise_emocional`. Isso diz o que fazer. O
> texto da pessoa não acrescenta ação — só risco.

---

## RN-015 — Retenção com prazo, e expurgo que roda

- **Texto:** cada tabela tem prazo de guarda declarado, e o expurgo **executa** — no
  máximo uma vez por dia, sem depender de alguém lembrar de rodar script.
- **Escopo:** todo o banco de estado.
- **Autonomia:** A3.
- **Exceção:** duas tabelas não expiram, e as duas por decisão deliberada — ver abaixo.
- **Violação:** dado passado do prazo continuar no banco; ou o expurgo falhar em
  silêncio (o estado do intake passa a mostrar `expurgo: falhou`).
- **Imposição:** CÓDIGO (`RETENCAO_DIAS`, `expurgar_expirados`, `expurgar_se_preciso`).

| Tabela | Prazo | Por quê esse número |
|---|---|---|
| `lacunas` (pergunta literal) | 30 dias | Serve à mineração semanal; 4 ciclos bastam |
| `processed` (id de evento) | 7 dias | Só descarta reentrega da Meta |
| `inbound` (quem falou 24h) | 30 dias | É lista de identificadores |
| `followups` (texto enviado) | 90 dias | Cobre auditoria de tom e plantão |
| `fila_humana` | 90 dias | **Só casos resolvidos** |
| `lead_flags` (marca sensível) | 90 dias | Marca durante o atendimento, não para sempre |
| `private_replies`, `toques_proativos`, `interactions`, `lead_stage` | 180 dias | Histórico operacional do funil |
| `aprovacoes`, `moderacao_aprovacoes`, `exclusoes` | 365 dias | Prova de autorização humana — auditoria |
| `opt_outs` · `bloqueios_permanentes` | **não expira** | Ver abaixo |

> **As duas que não expiram, e por quê.** `opt_outs` é o registro da recusa — a
> própria base para não contatar de novo. Apagar a recusa transformaria opt-out em
> consentimento. `bloqueios_permanentes` guarda só um hash, sem dado pessoal, e é o
> que sobra depois de um pedido de exclusão.
>
> **Caso aberto nunca é expurgado.** Apagar um caso sem resolução não protegeria o
> titular — esconderia o atendimento que ninguém fez.

---

## RN-016 — Dado sensível: guarda a marca, não o conteúdo

- **Texto:** saúde, emoção, menoridade e questão jurídica entram como **marca**, com
  prazo curto e sem texto. A marca existe para o agente não vender; não para perfilar.
- **Escopo:** `lead_flags` e qualquer classificação de risco.
- **Autonomia:** A3.
- **Exceção:** nenhuma.
- **Violação:** a tabela de flags ganhar coluna de conteúdo, ou a marca sobreviver
  indefinidamente. Marcar alguém como "em crise" dois anos depois é exatamente o que
  a LGPD proíbe.
- **Imposição:** CÓDIGO (schema sem coluna de texto + prazo de 90 dias).

---

## RN-017 — Direito do titular: acesso e eliminação

- **Texto:** qualquer pessoa pode pedir o que existe sobre ela e pedir para apagar. Os
  dois são operações do agente, não promessa no contrato.
- **Escopo:** todos os dados ligados ao igsid.
- **Autonomia:** A0 — atendido por humano, nunca decidido pelo modelo.
- **Exceção:** nenhuma.
- **Violação:** `exportar_titular` omitir tabela sem declarar; ou o apagamento deixar
  o igsid para trás.
- **Imposição:** CÓDIGO (`exportar_titular`, `apagar_titular`).

Duas decisões que definem se isso é eliminação ou teatro:

1. **O bloqueio de contato sobrevive ao apagamento** — em `bloqueios_permanentes`, só
   como hash. Se o apagamento levasse a recusa junto, a pessoa que pediu para ser
   esquecida seria abordada na campanha seguinte: o apagamento viraria a **causa** da
   violação. Por isso `is_opted_out` consulta as duas tabelas.
2. **A prova do atendimento não guarda o igsid** — só o hash, a data e a contagem do
   que foi apagado. Guardar quem pediu o apagamento é manter o dado que se apagou.

> **Limite honesto, e ele fica escrito aqui:** `private_replies`, `processed` e
> `moderacao_aprovacoes` são chaveados por comentário/evento, não por igsid — não há
> como ligá-los à pessoa por essa via. Quem responder ao titular precisa saber o que
> **não** entrou na exportação. Extensão disso exige a integração com o CRM (Pendência 9).

---

## RN-018 — Relatório sem dado pessoal na saída

- **Texto:** relatório que circula fora do banco (Telegram, e-mail, tela compartilhada)
  não leva identificador de pessoa. O vigia identifica o caso pelo **número interno**,
  que não identifica ninguém, e mostra o igsid só como hash curto.
- **Escopo:** toda saída de relatório — vigia de SLA, alertas, resumos.
- **Autonomia:** A3.
- **Exceção:** consulta técnica direta ao banco, dentro do perímetro do cliente.
- **Violação:** igsid em mensagem de Telegram, como acontecia com o vigia antes desta
  regra.
- **Imposição:** CÓDIGO (`mascarar_id`) + script do vigia.

---

## RN-019 — Status de pedido, pagamento e rastreio só com fonte consultada

- **Texto:** o agente não afirma, não estima e não projeta prazo, status de pagamento,
  de entrega ou de rastreio. Sem fonte consultada, a resposta é "não consigo ver isso
  daqui" e o encaminhamento para o time.
- **Escopo:** qualquer saída do agente, em qualquer canal. Vale para afirmação
  ("seu pedido já foi enviado"), para estimativa ("provavelmente já saiu") e para
  **promessa de verificar** ("vou checar seu pedido") — prometer conferir o que não se
  pode conferir é a mesma mentira com uma etapa a mais.
- **Autonomia:** A0 sobre a afirmação (não há nível em que ela passe sem fonte);
  A2 para dizer que não consegue ver e encaminhar.
- **Exceção:** nenhuma, **enquanto não houver integração configurada**. É a única regra
  deste documento que se desfaz por configuração: no dia em que `BLING_API_TOKEN`
  existir, `status_pedido_disponivel()` devolve `true` e o bloqueio cai sozinho — sem
  editar regra e sem tocar em teste.
- **Violação:** afirmação falsa sobre pedido com a assinatura do cliente. É pior do que
  não ter a integração: sem integração o cliente ouve "não consigo ver aqui"; com o
  improviso, ele ouve uma data que ninguém vai cumprir.
- **Imposição:** CÓDIGO (`rules.detect_afirmacao_status_sem_fonte` no caminho do envio,
  com `status_pedido_disponivel()` como chave) + PROMPT (`diretiva_sem_integracao`).

### Por que esta regra nasceu de um buraco de cobertura

O Documento Mestre cita **Bling 36x**, **Clint 45x**, **WhatsApp 32x**, e-mail **32x**,
rastreio **19x** e "pedido" **34x**. O agente entregue tem **zero** linha de integração
com qualquer um deles.

Sem a RN-019 o buraco não era "falta uma feature" — era o modelo preenchendo o vazio
sozinho, com um status plausível. Os adaptadores ficam em
`plugins/instagram-seller/integracoes.py` e **falham fechado**: quem chama recebe
`FonteIndisponivel` e nunca um dicionário vazio. Vazio seria pior do que o erro, porque
é indistinguível de "não existe pedido" — e o agente diria ao cliente que o pedido dele
não existe.

**Pendência 10** é o que falta para esta regra virar recurso.

## RN-020 — Vínculo de identidade antes da ação

- **Texto:** nenhuma ação que dependa de quem é a pessoa acontece sem vínculo
  confiável entre a ação e o titular. O autor é resolvido **no servidor**, a partir do
  registro do evento — nunca confiado a quem chamou a ferramenta.
- **Escopo:** todas as rotas de comentário (resposta privada e resposta pública) e
  qualquer ação futura de outro canal.
- **Autonomia:** A0 — não existe nível em que uma ação identificada rode sem vínculo.
- **Exceção:** nenhuma. Ausência de vínculo **bloqueia**; não é "seguir com cautela".
- **Violação:** o motor conhece a restrição (opt-out, flag sensível, humano no
  comando) e a ferramenta chega sem a quem aplicá-la. Na prática: a pessoa que pediu
  para não ser contatada volta a ser contatada.
- **Imposição:** CÓDIGO (`rules.vinculo_resolver` no caminho do envio,
  `instagram_api._resolver_vinculo`) + PROMPT.
- **Origem:** achado **F02** do Parecer OpenClaw 1.0. Antes, `igsid` era parâmetro com
  valor vazio por padrão e as verificações eram puladas em silêncio (`if igsid:`).

Três motivos reprovam o vínculo, e cada um sai **carimbado** no log com sua chave:

| Motivo | Quando |
|---|---|
| `comentario_sem_vinculo` | Comentário sem registro de autor, ou registro sem autor |
| `alvo_de_outra_pessoa` | O igsid declarado não é o dono do comentário (**critério T06**: comentário de terceiro não é alvo) |

## RN-021 — Efeito externo com resultado desconhecido

- **Texto:** "não sei se saiu" é um estado próprio. O agente não repete o envio e
  ninguém assume que saiu ou que não saiu sem conferir.
- **Escopo:** todo envio que atravessa a rede (DM, private reply).
- **Autonomia:** A0 para repetir; o desfecho é resolvido por humano.
- **Exceção:** nenhuma.
- **Violação:** repetir no escuro duplica a resposta ao cliente; não repetir sem
  registro deixa o cliente sem resposta e ninguém sabe que ficou.
- **Imposição:** CÓDIGO (`rules.abrir_reconciliacao`, `reconciliacao_pendente`,
  `instagram_api.ResultadoIncerto`).
- **Origem:** achados **F01** e **F05** do Parecer OpenClaw 1.0. Antes, a cota da
  private reply era marcada **antes** da chamada: uma falha de rede era tratada como
  envio consumado.

Desfechos possíveis de uma chamada:

| Desfecho | O que a regra faz |
|---|---|
| Envio confirmado | Marca a cota e registra a execução |
| **Recusa confirmada** da Meta (4xx) | A cota foi consumida de fato: marca e propaga o erro |
| **Resultado desconhecido** (timeout/rede) | **Não** marca, abre pendência rastreável, e a próxima tentativa no mesmo alvo é bloqueada até alguém resolver |

---

## Pendências — o que o código não pode decidir

Nenhum destes itens é técnico. Todos mudam o que o agente faz na cara do cliente.

| # | Pendência | Por que precisa do Edson |
|---|---|---|
| 1 | ~~**Divulgar que é automação?** (RN-008)~~ **DECIDIDO: modo `sob_pergunta`** | Não se anuncia sozinho; diz a verdade se perguntarem. O modo `nunca` (não dizer nem quando perguntam) continua disponível — e seria mentira por omissão. |
| 2 | **"O que eu nunca falaria para um cliente?"** | É a matéria-prima das linhas vermelhas que só o dono sabe. Uma pergunta, resposta dele, e o motor fica calibrado com a voz real. |
| 3 | **Política de garantia / devolução / frete** (RN-011) | Hoje o agente não pode afirmar nada. Uma linha escrita libera. |
| 4 | **Horário e teto** (RN-006/RN-007) | 8h–21h e 3 toques/14 dias é o que o código já usava. Confirmar. |
| 5 | **Quem é o plantão da fila** (RN-012) | SLA sem gente do outro lado é número decorativo. |
| 6 | **Durante uma crise, o agente fala ou cala?** | Hoje: cala e alerta o humano em ≤15 min. A alternativa é ele enviar o acolhimento aprovado (com o CVV 188) na hora e depois passar para o humano. É decisão de risco do dono, não de código — e é a diferença entre "ninguém respondeu por 15 minutos" e "alguém respondeu na hora". |
| 7 | **Declarar finalidade, base legal e encarregado (DPO)** | As RN-014..018 impõem o que é imponível em código. O *porquê legal* de guardar o dado é declaração do cliente — e sem encarregado não há quem responda ao titular no prazo que a LGPD dá. |
| 8 | **Validar os prazos de retenção com o jurídico** | Os números da tabela da RN-015 são defensáveis, não sagrados (90 dias para marca sensível, 365 para auditoria). Quem assina, assume. |
| 9 | **Ligar o CRM ao direito do titular** | A RN-017 cobre o banco do agente. O mesmo dado em Bling, Clint ou planilha fica fora do alcance dela — e o titular não distingue "sistema" de "empresa". |
| 10 | **Credenciais das integrações** (`BLING_API_TOKEN`, `CLINT_MCP_URL`, `WHATSAPP_TOKEN`) | É o único item que separa a RN-019 de virar recurso: a regra, as interfaces e os testes já estão no lugar, faltando o acesso. Com o token no `.env`, o bloqueio cai sozinho — sem editar regra. |
| 11 | **Assinar este documento** | É o objetivo dele: o comportamento real do agente deixando de ser regra de código sem dono. |

---

## Changelog

| Versão | O que mudou |
|---|---|
| **2.3.0** | **Parecer OpenClaw 1.0.** Entram a **RN-020** (vínculo de identidade antes da ação) e a **RN-021** (efeito externo com resultado desconhecido). Corrige os bloqueadores: **F01** (a resposta privada usava a chamada da pública e publicava como comentário), **F02** (identidade vazia nas rotas de comentário pulava as verificações), **F03** (token preenchido liberava afirmação de status sem consulta) e **F04** (ação e proatividade autodeclaradas e opcionais). |
| **2.2.0** | Entra a **RN-019** (status de pedido/pagamento/rastreio só com fonte consultada) e os adaptadores fail-closed em `integracoes.py`. A **RN-008** passa a ter três modos (`nunca` \| `sob_pergunta` \| `sempre`), com `sob_pergunta` como padrão decidido — o agente não mente se perguntarem e não se anuncia sozinho. |
| **2.1.0** | Entra a seção de **PROTEÇÃO DE DADOS** (RN-014..RN-018): prazo de guarda por tabela, expurgo que roda, minimização do texto, direito de acesso e eliminação, e relatório sem dado pessoal. Corrige o vazamento do texto da crise na fila humana. |
| **2.0.0** | Entra a seção REGRAS DE NEGÓCIO (RN-001..RN-013), com o identificador carimbado em todo bloqueio de envio. |
| 1.0.0 | Regras da Meta (janela de 24h, private reply única, kill switch) e a matriz A0 herdada do `docs/05`. |

---

## Como verificar que estas regras estão de pé

```bash
python -m unittest discover -s tests          # 205 testes (71 originais + 134 destas regras)
python -m unittest tests.test_regras_de_negocio -v   # RN-001..RN-013 e RN-019
python -m unittest tests.test_lgpd -v                # RN-014..RN-018 (proteção de dados)
python -m unittest tests.test_intake -v              # a LIGAÇÃO: o briefing que chega ao agente
python scripts/fila-humana-sla.py             # fila humana e casos fora do SLA
```

Cada regra acima tem pelo menos um teste que a **prova** — não que a descreve. Onde
dá, o teste atravessa o caminho real do envio (`instagram_api.send_dm`), porque é lá
que a regra vale, não no prompt.
