#!/usr/bin/env python3
"""Hook pre_tool_call — portão de envio do Instagram.

Protocolo (conforme doc do Hermes):
  stdin  -> JSON com hook_event_name, tool_name, tool_input, session_id, cwd, profile, extra
  stdout -> JSON de resposta
            {"action": "block", "message": "..."}  bloqueia
            {}                                     deixa passar

Exit codes:
  0  -> stdout é interpretado
  2  -> bloqueia mesmo sem JSON (compatível com Claude Code / Cursor)

IMPORTANTE: configurado com fail_closed: true no config.yaml.
Se este script não existir, travar, ou cuspir lixo, o envio é BLOQUEADO.
Ver issue #100942 — em gateway non-TTY isso pode não se aplicar; por isso o
guardrail real também vive dentro da tool, em instagram_api.py.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Tools que passam por este portão
GATED = {
    "ig_send_dm",
    "ig_private_reply",
    "ig_reply_comment",
}

KILL_SWITCH = Path(
    os.getenv(
        "IG_KILL_SWITCH",
        str((Path(os.getenv("HERMES_HOME", "")) if os.getenv("HERMES_HOME") else Path.home() / ".hermes") / "ig-kill-switch"),
    )
)

# Motivos A0 que exigem humano — o agente não envia, só escala.
# Em produção isto vem do banco (leads.alerta_sensivel / flags).
#
# ⚠️ TENSÃO CONHECIDA, e é por isso que este hook é a camada GROSSA (desligada
# por padrão): ele bloqueia TODO envio enquanto a flag A0 estiver ativa —
# inclusive a mensagem de acolhimento aprovada, que DEVE sair (RN-012 permite
# acolher, proíbe vender). A camada fina é a do caminho do envio
# (`rules.avaliar_envio` → RN-002/003/004), que distingue as duas coisas.
# Ligue este hook sabendo que ele para o acolhimento também.
A0_FLAGS = {
    "alerta_sensivel",
    "reclamacao",
    "juridico",
    "pedido_desconto",
    "menor_idade",
    "hostilidade",
    # Adicionados com a seção de regras de negócio (RN-002/003/004):
    "crise_emocional",
    "saude_mental",
    "desespero_financeiro",
    "dados_de_terceiro",
}


def block(message: str) -> None:
    print(json.dumps({"action": "block", "message": message}))
    sys.exit(0)


def allow() -> None:
    print(json.dumps({}))
    sys.exit(0)


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # stdin ilegível. Deixamos o fail_closed do host decidir.
        # (Se fail_closed estiver ativo, isto bloqueia; se não, libera.)
        sys.exit(1)

    tool_name = payload.get("tool_name")
    if tool_name not in GATED:
        allow()

    # 1) Kill switch — o botão que o cliente aperta
    if KILL_SWITCH.exists():
        block(
            "Kill switch do Instagram ativo. Nenhum envio permitido "
            "até ser desligado no painel."
        )

    # 2) Flags A0 da sessão — aceita duas posições no payload:
    #    extra.lead_flags (wire protocol do Hermes) ou lead_flags no topo
    #    (payload sintético do `hermes hooks test --payload-file`).
    extra = payload.get("extra") or {}
    flags = set(extra.get("lead_flags") or [])
    flags |= set(payload.get("lead_flags") or [])
    hit = flags & A0_FLAGS
    if hit:
        block(
            "Conversa marcada como A0 ("
            + ", ".join(sorted(hit))
            + "). Exige atendimento humano — o agente não responde por aqui."
        )

    allow()


if __name__ == "__main__":
    main()
