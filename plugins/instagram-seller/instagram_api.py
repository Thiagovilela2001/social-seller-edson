"""Camada de acesso à Meta Graph API + última barreira antes da rede.

PRINCÍPIO CENTRAL DESTE ARQUIVO:
    Toda regra que decide "pode enviar?" é imposta AQUI, em código, no caminho do
    envio. Nunca no prompt, e nunca APENAS em hook.

Motivo: hook pre_tool_call de bloqueio falha ABERTO silenciosamente sob gateway
non-TTY (hermes-agent #100942). Um guardrail que pode não registrar não é um
guardrail. Código no caminho do envio não tem como "não registrar".

RELACIONAMENTO COM scripts/instagram-intake.py — defesa em profundidade:
    o intake decide ANTES do LLM (dedupe, opt-out, A0, sanitização);
    este arquivo decide DEPOIS do LLM, imediatamente antes da chamada de rede.
    Camadas independentes: a primeira pode estar desligada, com bug ou contornada
    por prompt injection, e esta ainda barra. A regra que importa está duplicada
    de propósito — mas com UMA implementação, em `rules.py`, para as duas não
    divergirem com o tempo.

Não há caminho de envio que não passe por `_autorizar()`.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# O loader de plugins NÃO põe o diretório do plugin no sys.path — só em
# module.__path__. Então o import relativo é o caminho real em produção, e o
# absoluto serve ao harness de teste (spike/run_spike.py) e aos testes.
try:
    from . import rules
except ImportError:  # pragma: no cover - somente fora do contexto de pacote
    import rules  # type: ignore[no-redef]

_log = logging.getLogger(__name__)

# Versão da Graph API. Fixe e revise periodicamente — a Meta descontinua versões.
GRAPH_VERSION = os.getenv("IG_GRAPH_VERSION", "v21.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Reexportados para o resto do plugin. A implementação vive em `rules.py`.
WINDOW_SECONDS = rules.DM_WINDOW_SECONDS
PRIVATE_REPLY_MAX_AGE = rules.PRIVATE_REPLY_MAX_AGE


def _hermes_home() -> Path:
    """HERMES_HOME do profile. Mantido por compatibilidade com o `__init__`."""
    return rules.hermes_home()


class PolicyBlock(Exception):
    """Envio recusado por política. Levantada SEMPRE antes de tocar na rede."""


# --------------------------------------------------------------------------
# Autorização — o único caminho para um envio
# --------------------------------------------------------------------------

def _check_kill_switch() -> None:
    alvo = rules.kill_switch_path()
    if alvo.exists():
        raise PolicyBlock(
            f"Kill switch ativo. Envio bloqueado. Desligue removendo {alvo}."
        )


def _check_opt_out(igsid: str, *, permite_despedida: bool = True) -> None:
    """Opt-out é permanente — com UMA exceção: a despedida privada.

    Se bloqueasse tudo, a despedida em uma linha que a política manda enviar nunca
    sairia. Se não limitasse, o opt-out não valeria nada. Então: exatamente uma
    mensagem depois do pedido de parada, e silêncio para sempre.

    `permite_despedida=False` nos canais PÚBLICOS: responder em público quem pediu
    para ser deixado em paz é pior do que não responder — repete a exposição na
    frente de todo mundo em vez de encerrar em privado.
    """
    if not igsid or not rules.is_opted_out(igsid):
        return
    if permite_despedida and rules.pode_enviar_despedida(igsid):
        return
    raise PolicyBlock(
        "Opt-out registrado. Nenhum envio — nem despedida, que já foi enviada. "
        "Este contato está encerrado de forma permanente."
    )


def _check_window(igsid: str) -> None:
    restante = rules.window_remaining(igsid)
    if restante <= 0:
        raise PolicyBlock(
            "Janela de 24h expirada (ou o usuário nunca falou com a conta). "
            "O Instagram não permite envio. Use WhatsApp/e-mail com consentimento."
        )


def _check_private_reply_available(comment_id: str) -> None:
    idade = rules.private_reply_age(comment_id)
    if idade is None:
        return
    raise PolicyBlock(
        "Private reply já consumida para este comentário "
        f"(enviada há {int(idade)}s). São 1 por comentário — não existe retry. "
        "De agora em diante, só DM ou resposta pública."
    )


def _autorizar(
    igsid: str = "",
    *,
    comment_id: str = "",
    exige_janela: bool = True,
    permite_despedida: bool = True,
) -> None:
    """Todas as regras que antecedem um envio, em ordem estável.

    A ordem é fixa porque o MOTIVO do bloqueio é a informação mais útil para quem
    está operando: kill switch e opt-out são mais graves que janela expirada.
    """
    _check_kill_switch()
    if igsid:
        _check_opt_out(igsid, permite_despedida=permite_despedida)
    if comment_id:
        _check_private_reply_available(comment_id)
    if igsid and exige_janela:
        _check_window(igsid)


def _registrar_envio(
    *, igsid: str = "", comment_id: str = "", message_id: str = "", acao: str
) -> None:
    """Grava `executed_action` DEPOIS do envio confirmado (PDF §13).

    Falha aqui não derruba o envio já feito — o envio é o que importa para o
    cliente. Mas a falha fica no log, porque perder a trilha de auditoria é o
    outro lado do problema.
    """
    try:
        if igsid and rules.pode_enviar_despedida(igsid):
            rules.marcar_despedida_enviada(igsid)
        marcadas = rules.mark_executed(
            comment_id=comment_id, message_id=message_id, igsid=igsid, executed=acao
        )
        if not marcadas:
            _log.warning(
                "Envio '%s' sem interação registrada (igsid=%s comment=%s msg=%s)",
                acao, igsid, comment_id, message_id,
            )
    except Exception as exc:  # noqa: BLE001 — auditoria nunca cancela o envio
        _log.warning("Falha ao registrar execução '%s': %s", acao, exc)


# --------------------------------------------------------------------------
# Graph API
# --------------------------------------------------------------------------

def _post(path: str, payload: dict) -> dict:
    token = os.environ["IG_ACCESS_TOKEN"]
    url = f"{GRAPH_BASE}/{path}?access_token={urllib.parse.quote(token)}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        # Não vaze o token na mensagem de erro.
        raise RuntimeError(f"Graph API {e.code} em /{path}: {detail}") from None


def send_dm(igsid: str, text: str) -> dict:
    """DM dentro da janela de 24h."""
    if not text or not text.strip():
        raise PolicyBlock("Mensagem vazia.")
    _autorizar(igsid, exige_janela=True)

    ig_user_id = os.environ["IG_USER_ID"]
    resultado = _post(
        f"{ig_user_id}/messages", {"recipient": {"id": igsid}, "message": {"text": text}}
    )
    _registrar_envio(igsid=igsid, acao="ig_send_dm")
    return resultado


def send_private_reply(comment_id: str, text: str, igsid: str = "") -> dict:
    """Resposta privada a um comentário. UMA vez por comentário, até 7 dias.

    ATENÇÃO: envie SEMPRE texto puro. Anexos/botões são recusados para quem não
    segue a conta — e a chamada que falha AINDA consome a private reply (docs/01 §3.3).
    """
    if not text or not text.strip():
        raise PolicyBlock("Mensagem vazia.")
    # A private reply é permitida justamente fora da janela de 24h (até 7 dias),
    # então NÃO exige janela — mas exige a cota e o kill switch.
    _autorizar(igsid, comment_id=comment_id, exige_janela=False)

    # Marca ANTES da chamada: uma falha de rede também queima a private reply, e um
    # retry cego geraria erro em cima de erro.
    rules.mark_private_reply_sent(comment_id)

    resultado = _post(f"{comment_id}/replies", {"message": text})
    _registrar_envio(igsid=igsid, comment_id=comment_id, acao="ig_private_reply")
    return resultado


def reply_comment_public(comment_id: str, text: str, igsid: str = "") -> dict:
    """Resposta pública a um comentário. Visível para todo mundo.

    Não exige janela (é público), mas exige kill switch e respeita opt-out — e aqui
    a despedida NÃO é permitida: quem pediu para ser deixado em paz não é engajado
    em público nem uma vez.
    """
    if not text or not text.strip():
        raise PolicyBlock("Mensagem vazia.")
    _autorizar(igsid, exige_janela=False, permite_despedida=False)

    resultado = _post(f"{comment_id}/replies", {"message": text})
    _registrar_envio(igsid=igsid, comment_id=comment_id, acao="ig_reply_comment")
    return resultado
