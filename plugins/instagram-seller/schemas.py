"""Schemas das tools — é isto que o LLM lê para decidir quando chamar.

As descrições são instrução, não enforcement. Quem impede o envio indevido é
instagram_api.py. O schema só ajuda o modelo a pedir a coisa certa.
"""

import importlib


def _vocabulario_de_acoes() -> list[str]:
    """Vocabulário FECHADO de ações (RN-009), lido do motor — fonte única.

    Achado F04 do parecer OpenClaw: `acao` era texto livre E opcional, então o modelo
    podia inventar um rótulo ou simplesmente omitir o campo para escapar da matriz de
    autonomia. Aqui a lista vem de `NIVEL_AUTONOMIA`; se o motor não carregar a lista
    fica vazia e o modelo não recebe enum — mas o motor continua bloqueando ação
    desconhecida no caminho do envio, que é onde a decisão de fato acontece.
    """
    for nome in (f"{__package__}.rules" if __package__ else "", "rules"):
        if not nome:
            continue
        try:
            return sorted(importlib.import_module(nome).NIVEL_AUTONOMIA)
        except Exception:  # noqa: BLE001
            continue
    return []


ACOES = _vocabulario_de_acoes()


def _acao_schema(descricao: str) -> dict:
    """Campo `acao` — obrigatório e restrito ao vocabulário do motor."""
    campo: dict = {"type": "string", "description": descricao}
    if ACOES:
        campo["enum"] = ACOES
    return campo


SEND_DM = {
    "name": "ig_send_dm",
    "description": (
        "Envia uma mensagem direta (DM) no Instagram para um usuário. "
        "Só funciona se o usuário falou com a conta nas últimas 24 horas — "
        "fora dessa janela o Instagram não permite envio e a tool recusa. "
        "Use para responder dúvidas, dar continuidade à conversa e enviar links. "
        "Uma ideia por mensagem. Não mande textão."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "igsid": {
                "type": "string",
                "description": "Instagram-Scoped ID do destinatário (não é o @username).",
            },
            "text": {
                "type": "string",
                "description": "Texto da mensagem. Máximo 3 linhas.",
            },
            "proativo": {
                "type": "boolean",
                "description": (
                    "OBRIGATÓRIO. TRUE quando você está PROCURANDO a pessoa (follow-up, "
                    "reativação, aviso que ela não pediu). Abordagem proativa só sai "
                    "em horário humano (8h–21h BRT) e respeita teto de toques. "
                    "FALSE quando está respondendo algo que ela acabou de mandar. "
                    "Não omitir: omitir é erro de ferramenta, não autorização."
                ),
            },
            "acao": _acao_schema(
                "OBRIGATÓRIO. Ação declarada, do vocabulário fechado (matriz de "
                "autonomia RN-009). Ação fora da lista é recusada, e omitir a ação "
                "também — omissão não é autorização. Ações A0 (desconto, reclamação, "
                "reembolso, crise) e A1 (upsell, alto valor) são recusadas sem humano."
            ),
        },
        "required": ["igsid", "text", "proativo", "acao"],
    },
}

PRIVATE_REPLY = {
    "name": "ig_private_reply",
    "description": (
        "Envia uma mensagem privada para quem comentou em um post. "
        "ATENÇÃO: só existe UMA por comentário e não há segunda chance — "
        "se falhar, aquele comentário não pode mais receber private reply. "
        "Envie SEMPRE texto puro: anexos e botões podem ser recusados para "
        "quem não segue a conta, e a tentativa que falha ainda consome a cota. "
        "Use para abrir conversa a partir de um comentário de interesse."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "comment_id": {
                "type": "string",
                "description": "ID do comentário a responder.",
            },
            "text": {
                "type": "string",
                "description": "Texto puro. Sem link, sem anexo, sem botão.",
            },
            "acao": _acao_schema(
                "OBRIGATÓRIO. Ação declarada (matriz de autonomia). Ex.: "
                "private_reply_palavra_chave. Omitir é erro de ferramenta."
            ),
        },
        "required": ["comment_id", "text", "acao"],
    },
}

REPLY_COMMENT = {
    "name": "ig_reply_comment",
    "description": (
        "Responde um comentário PUBLICAMENTE, visível para todos. "
        "Use com parcimônia: serve para prova social (mostrar que a conta "
        "responde) e para puxar a conversa para a DM. "
        "Nunca use para vender no comentário público. "
        "Nunca use para discutir, rebater crítica ou expor o usuário."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "comment_id": {
                "type": "string",
                "description": "ID do comentário a responder publicamente.",
            },
            "text": {
                "type": "string",
                "description": "Resposta curta e leve. Máximo 2 linhas.",
            },
            "acao": _acao_schema(
                "OBRIGATÓRIO. Ação declarada (matriz de autonomia). Ex.: "
                "reply_comment. Omitir é erro de ferramenta."
            ),
        },
        "required": ["comment_id", "text", "acao"],
    },
}

ALL = [SEND_DM, PRIVATE_REPLY, REPLY_COMMENT]

# Tools que esta operação NUNCA deve disparar sem humano no meio.
# A lista vive aqui para o hook e o guardrail usarem a mesma fonte.
GATED_TOOLS = {"ig_send_dm", "ig_private_reply", "ig_reply_comment"}
