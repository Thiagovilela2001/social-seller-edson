"""Schemas das tools — é isto que o LLM lê para decidir quando chamar.

As descrições são instrução, não enforcement. Quem impede o envio indevido é
instagram_api.py. O schema só ajuda o modelo a pedir a coisa certa.
"""

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
                    "Marque TRUE quando você está PROCURANDO a pessoa (follow-up, "
                    "reativação, aviso que ela não pediu). Abordagem proativa só sai "
                    "em horário humano (8h–21h BRT) e respeita teto de toques. "
                    "Deixe FALSE quando está respondendo algo que ela acabou de mandar."
                ),
            },
            "acao": {
                "type": "string",
                "description": (
                    "Ação declarada, para a matriz de autonomia. Use, por exemplo: "
                    "informar_preco, enviar_link, responder_duvida_rag, upsell, "
                    "captura_whatsapp, desconto, reembolso. Ações A0 (desconto, "
                    "reclamação, reembolso, crise) e A1 (upsell, alto valor) "
                    "são recusadas sem humano."
                ),
            },
        },
        "required": ["igsid", "text"],
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
            "acao": {
                "type": "string",
                "description": "Ação declarada (matriz de autonomia). Ex.: private_reply_palavra_chave.",
            },
        },
        "required": ["comment_id", "text"],
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
            "acao": {
                "type": "string",
                "description": "Ação declarada (matriz de autonomia). Ex.: reply_comment.",
            },
        },
        "required": ["comment_id", "text"],
    },
}

ALL = [SEND_DM, PRIVATE_REPLY, REPLY_COMMENT]

# Tools que esta operação NUNCA deve disparar sem humano no meio.
# A lista vive aqui para o hook e o guardrail usarem a mesma fonte.
GATED_TOOLS = {"ig_send_dm", "ig_private_reply", "ig_reply_comment"}
