"""Plugin Hermes — Instagram Social Seller.

Registra três tools de envio e um hook pre_tool_call de bloqueio.

SOBRE O HOOK: ele é defesa em profundidade, NÃO a linha de defesa principal.
Ver spike/README.md — a issue #100942 relata que hooks de bloqueio falham
ABERTO sob gateway non-TTY. Por isso toda regra real vive em instagram_api.py,
no caminho do envio. O hook só antecipa o erro com mensagem melhor.
"""

from __future__ import annotations

import json
import os

# Import relativo e o padrao do Hermes (plugin carregado como pacote).
# O fallback absoluto serve ao harness de teste (spike/run_spike.py), que
# carrega este arquivo fora do contexto de pacote.
try:
    from . import schemas
    from .instagram_api import (
        PolicyBlock,
        _hermes_home,
        reply_comment_public,
        send_dm,
        send_private_reply,
    )
except ImportError:  # pragma: no cover - somente no harness
    import schemas  # type: ignore[no-redef]
    from instagram_api import (  # type: ignore[no-redef]
        PolicyBlock,
        _hermes_home,
        reply_comment_public,
        send_dm,
        send_private_reply,
    )

# --------------------------------------------------------------------------
# Códigos de motivo — usados no log de auditoria e no painel
# --------------------------------------------------------------------------

BLOCKED_BY_POLICY = "blocked_by_policy"
TOOL_ERROR = "tool_error"


def _ok(payload: dict) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _err(kind: str, message: str, **extra) -> str:
    return json.dumps(
        {"success": False, "error": kind, "message": message, **extra},
        ensure_ascii=False,
    )


def register(ctx):
    # ----------------------------------------------------------------
    # Tools
    # ----------------------------------------------------------------

    def handle_send_dm(params, **kwargs):
        del kwargs
        # HARD LIMIT: o agente manda 1 ou 2 mensagens, nunca 3+.
        text = (params.get("text") or "").strip()
        if len(text) > 900:
            return _err(
                BLOCKED_BY_POLICY,
                "Mensagem longa demais para DM. Quebre em até 2 mensagens de 3 linhas.",
            )
        try:
            result = send_dm(
                params["igsid"],
                text,
                # `proativo` liga as RN-006/RN-007 (horário humano + teto de
                # toques). Sem isso, uma abordagem de follow-up não teria hora
                # nem cota — e ninguém perceberia até o primeiro unfollow.
                proativo=bool(params.get("proativo")),
                # `acao` é o vocabulário de NIVEL_AUTONOMIA (RN-009).
                acao=(params.get("acao") or "").strip(),
            )
        except PolicyBlock as e:
            return _err(BLOCKED_BY_POLICY, str(e))
        except KeyError as e:
            return _err(TOOL_ERROR, f"Parâmetro obrigatório ausente: {e}")
        except Exception as e:  # noqa: BLE001 — devolver erro é melhor que quebrar o turno
            return _err(TOOL_ERROR, str(e))
        return _ok({"message_id": result.get("message_id"), "canal": "ig_dm"})

    def handle_private_reply(params, **kwargs):
        del kwargs
        text = (params.get("text") or "").strip()

        # Trava anti-prejuízo: anexo/botão em private reply para não-seguidor
        # pode ser recusado E ainda consumir a cota única. Bloqueamos na origem.
        lowered = text.lower()
        if "http://" in lowered or "https://" in lowered:
            return _err(
                BLOCKED_BY_POLICY,
                "Private reply vai SEMPRE em texto puro — sem link. "
                "O link vai na DM depois que a pessoa responder.",
            )

        try:
            result = send_private_reply(
                params["comment_id"], text, acao=(params.get("acao") or "").strip()
            )
        except PolicyBlock as e:
            return _err(BLOCKED_BY_POLICY, str(e))
        except KeyError as e:
            return _err(TOOL_ERROR, f"Parâmetro obrigatório ausente: {e}")
        except Exception as e:  # noqa: BLE001
            return _err(TOOL_ERROR, str(e))
        return _ok(
            {
                "message_id": result.get("message_id"),
                "canal": "ig_private_reply",
                "aviso": "Cota única deste comentário consumida.",
            }
        )

    def handle_reply_comment(params, **kwargs):
        del kwargs
        try:
            result = reply_comment_public(
                params["comment_id"],
                (params.get("text") or "").strip(),
                acao=(params.get("acao") or "").strip(),
            )
        except PolicyBlock as e:
            return _err(BLOCKED_BY_POLICY, str(e))
        except KeyError as e:
            return _err(TOOL_ERROR, f"Parâmetro obrigatório ausente: {e}")
        except Exception as e:  # noqa: BLE001
            return _err(TOOL_ERROR, str(e))
        return _ok({"message_id": result.get("id"), "canal": "ig_comment_public"})

    ctx.register_tool(
        name="ig_send_dm",
        toolset="instagram",
        schema=schemas.SEND_DM,
        handler=handle_send_dm,
    )
    ctx.register_tool(
        name="ig_private_reply",
        toolset="instagram",
        schema=schemas.PRIVATE_REPLY,
        handler=handle_private_reply,
    )
    ctx.register_tool(
        name="ig_reply_comment",
        toolset="instagram",
        schema=schemas.REPLY_COMMENT,
        handler=handle_reply_comment,
    )

    # ----------------------------------------------------------------
    # Hook pre_tool_call — defesa em profundidade
    # ----------------------------------------------------------------

    def on_pre_tool_call(tool_name=None, args=None, **kwargs):
        """Bloqueia envio quando o kill switch está ligado.

        Retorna None para deixar passar. Retorna a diretiva de bloqueio para parar.

        NOTA: casa com a forma canônica documentada para shell hooks
        ({"action": "block", "message": ...}). Se o manager de plugins Python
        usar outra forma, ajuste aqui — teste em spike/3.
        """
        if tool_name not in schemas.GATED_TOOLS:
            return None

        kill_switch = os.getenv("IG_KILL_SWITCH") or str(_hermes_home() / "ig-kill-switch")
        if os.path.exists(kill_switch):
            return {
                "action": "block",
                "message": (
                    "Kill switch do Instagram está ativo. "
                    "Nenhum envio é permitido até ser desligado no painel."
                ),
            }
        return None

    ctx.register_hook("pre_tool_call", on_pre_tool_call)
