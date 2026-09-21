---
name: atribuicao
description: "Use ao registrar origem: ligar interação à publicação."
version: 1.0.0
author: "Gian — [SUA EMPRESA], Hermes Agent"
license: Commercial
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [atribuicao, media-id, igsid, lead, metricas, organico]
    related_skills: [social-seller-edson, moderacao]
---

# Atribuição — de qual publicação veio o lead

Registro de origem e venda. A operação é **orgânica**, então o eixo principal é a
**publicação**, não o anúncio. A parte de conversa está na skill `social-seller-edson`.

## When to Use

- Chegou interação e você precisa gravá-la com a origem.
- Um lead avançou no funil ou comprou, e isso precisa ser ligado a uma publicação.
- Você está prestes a preencher um campo de origem que não veio de dado observado.

**Não use para:** decidir o que responder (skill `social-seller-edson`).

## Procedure

1. **Identifique a pessoa pelo IGSID.** O `@username` é atributo mutável — muda, e quando muda
   o histórico se rompe. `media_id` e IGSID são as chaves estáveis.
2. **Grave a interação uma única vez**, com referência à publicação (`media_id`, tipo, permalink,
   data). Se a publicação estiver ligada a mais de um anúncio, as relações com anúncio vão em
   **tabela separada** — nunca duplique a linha da interação.
3. **Preencha os vínculos só com o que existe.** O que a integração não forneceu entra como
   `desconhecido` ou `não atribuível`.
4. **Preserve a evidência:** origem observada, método de ligação e o dado que sustenta a ligação.
5. **Ao fechar uma venda, tente ligar a publicação de origem** — e se o caso for ambíguo,
   **deixe sem atribuição.**

Critério de conclusão: toda linha gravada tem `media_id` (ou `não atribuível` explícito),
IGSID, e nenhum campo preenchido por dedução.

## Quick Reference — o que preservar

| Objeto | O que preservar | Uso |
|---|---|---|
| **Publicação / mídia** | `media_id` original, tipo (Reel, post, carrossel, story), permalink, data | **Eixo principal de origem.** Agrupar comentários e comparar desempenho |
| **Comentário / mensagem** | ID único, data, canal, texto, referência à conversa | Evitar duplicidade, manter encadeamento |
| **Pessoa** | IGSID (chave estável), `@username` como atributo mutável | Nunca usar username como chave única |
| **Oportunidade / pedido** | Referência comercial, checkout, evidência de pagamento | Distinguir interesse, tentativa e compra confirmada |
| **Campanha e anúncio** | `campaign_id`, `adset_id`, `ad_id`, `creative_id`, quando existirem | **Secundário.** Usar quando houver tráfego pago; nunca presumir |

### Os três links, sem inventar URL

- **Link da publicação** — caminho público, quando existir
- **Link do comentário** — acesso à interação, quando disponível
- **Link do anúncio** — apenas quando a integração fornecer

**Ausência de link ou de `ad_id` aparece como `desconhecido` ou `não atribuível`.** Nunca
preencher por dedução.

### A pergunta que a atribuição responde

> Qual publicação gera **lead qualificado**, não qual gera **engajamento**.

É a métrica mais valiosa do projeto — e a que quase ninguém mede. Um Reel com 500k views pode
gerar zero lead quente; um post sóbrio pode gerar vinte.

### Etapas do resultado — não confunda

1. Mensagem preparada
2. Tentativa de envio
3. **Envio confirmado**
4. Resposta recebida
5. Falha

Não registre "enviei no Direct" antes do passo 3.

## Pitfalls

- **Atribuir por proximidade de horário ou nome parecido é proibido.** Sem `ad_id`, a tentação
  de "adivinhar" qual Reel gerou a venda é muito maior do que no pago. **Não adivinhe.** Casos
  ambíguos permanecem **sem atribuição** — e isso é o resultado correto, não uma lacuna a
  preencher.
- **Uma publicação ligada a vários anúncios não multiplica números.** Se o total de comentários
  ou vendas subir por causa de um vínculo, o registro está errado.
- **`@username` como chave quebra o histórico.** Use IGSID.
- **Distinguir interesse, tentativa e compra confirmada.** São três coisas; somar as três é
  inflar o painel.

## A validar

Os IDs `effective_instagram_media_id`, `object_story_id` e `effective_object_story_id` foram
citados como referências técnicas na conversa original. **Disponibilidade e encadeamento
precisam ser verificados na versão de API utilizada.** Não presuma que existem nem que se
ligam entre si.

## Verification

- Todo campo de origem tem dado observado por trás, ou diz `desconhecido` / `não atribuível`?
- Nenhuma interação foi duplicada por causa de vínculo com anúncio?
- A chave usada foi o IGSID, não o `@username`?
- O painel distingue interesse, tentativa de envio e compra confirmada?
