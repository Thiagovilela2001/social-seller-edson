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


class ResultadoIncerto(Exception):
    """O envio foi disparado e NÃO se sabe se chegou (parecer OpenClaw, F05).

    Distinta de `PolicyBlock` (recusado antes da rede) e de `RuntimeError` (recusa
    confirmada da Meta). Existe porque "não sei" é um estado próprio: repetir pode
    duplicar a resposta ao cliente, e não repetir pode deixá-lo sem resposta. Quem
    recebe isto NÃO repete — confere e resolve a pendência.
    """


def _recusa_confirmada(mensagem: str) -> bool:
    """A Meta RECUSOU a chamada (4xx)? Distingue recusa de resultado desconhecido."""
    return "Graph API 4" in mensagem


# --------------------------------------------------------------------------
# Autorização — o único caminho para um envio
# --------------------------------------------------------------------------

def _check_kill_switch() -> None:
    alvo = rules.kill_switch_path()
    if alvo.exists():
        raise PolicyBlock(
            f"[{rules.RN_PARADA_EMERGENCIA}] Kill switch ativo. Envio bloqueado. "
            f"Desligue removendo {alvo}."
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
        f"[{rules.RN_OPT_OUT}] Opt-out registrado. Nenhum envio — nem despedida, "
        "que já foi enviada. Este contato está encerrado de forma permanente."
    )


def _check_window(igsid: str) -> None:
    restante = rules.window_remaining(igsid)
    if restante <= 0:
        raise PolicyBlock(
            "Janela de 24h expirada (ou o usuário nunca falou com a conta). "
            "O Instagram não permite envio. Use WhatsApp/e-mail com consentimento."
        )


def _check_private_reply_available(comment_id: str) -> None:
    # RN-021 — resultado desconhecido trava nova tentativa NO ESCURO. Sem isto,
    # "não sei se saiu" vira "mando de novo" e o cliente recebe duas vezes.
    if rules.reconciliacao_pendente(comment_id=comment_id):
        raise PolicyBlock(
            f"[{rules.RN_RECONCILIACAO}] Private reply com resultado DESCONHECIDO "
            "para este comentário. Não repito no escuro: alguém confere o que "
            "aconteceu e resolve a pendência antes de qualquer novo envio."
        )
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
    texto: str = "",
    exige_janela: bool = True,
    permite_despedida: bool = True,
    canal: str = "instagram",
    proativo: bool = False,
    acao: str = "",
    exige_identidade: bool = False,
) -> None:
    """Todas as regras que antecedem um envio, em ordem estável.

    A ordem é fixa porque o MOTIVO do bloqueio é a informação mais útil para quem
    está operando: kill switch e opt-out são mais graves que janela expirada.

    A partir de rules 2.0.0, as regras de negócio (REGRAS-DE-NEGOCIO.md) entram
    por `rules.avaliar_envio()` — uma única implementação para todo o sistema.
    Cada bloqueio carrega o identificador `RN-nnn` da regra que o produziu, para
    que o log de auditoria responda "qual regra aprovada impediu isto".
    """
    _check_kill_switch()

    # RN-020 (achado F02 do parecer OpenClaw): aqui estava o buraco. As verificações
    # abaixo começam com `if igsid:` — quando o chamador não passava identidade, elas
    # eram PULADAS, não bloqueadas. Ausência de identidade virava ausência de
    # restrição. Rota que exige vínculo agora RECUSA sem ele.
    if exige_identidade and not igsid:
        raise PolicyBlock(str(rules.VinculoInsuficiente("comentario_sem_vinculo")))
    if igsid:
        _check_opt_out(igsid, permite_despedida=permite_despedida)
    if comment_id:
        _check_private_reply_available(comment_id)
    if igsid and exige_janela:
        _check_window(igsid)

    bloqueios = rules.avaliar_envio(
        texto=texto,
        igsid=igsid,
        canal=canal,
        proativo=proativo,
        acao=acao,
        # RN-009/F04: no caminho das ferramentas, omitir `acao` bloqueia. Quem
        # chama esta função é uma ferramenta registrada — não uma decisão de negócio.
        exige_acao=True,
    )
    if bloqueios:
        # Reporta TODOS os bloqueios: um envio que viola duas regras precisa
        # aparecer como dois problemas, não como o primeiro deles.
        raise PolicyBlock(" | ".join(str(b) for b in bloqueios))


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


def _resolver_vinculo(comment_id: str, igsid: str = "") -> str:
    """Identidade confiável para agir num comentário (RN-020), ou `PolicyBlock`.

    Existe para que a recusa por falta de vínculo tenha o MESMO tipo das outras
    recusas do gate: quem chama já trata `PolicyBlock` como `blocked_by_policy`. Sem
    esta conversão, `VinculoInsuficiente` cairia no `except Exception` genérico do
    handler e sairia como erro de ferramenta — difícil de distinguir de um bug.
    """
    try:
        return rules.vinculo_resolver(comment_id, igsid)
    except rules.VinculoInsuficiente as e:
        raise PolicyBlock(str(e)) from None


def send_dm(igsid: str, text: str, *, proativo: bool = False, acao: str = "") -> dict:
    """DM dentro da janela de 24h.

    `proativo=True` marca a ABORDAGEM (follow-up, reativação, aviso) — é o que
    liga as RN-006/RN-007: horário humano e teto de toques. Responder quem
    acabou de escrever é `proativo=False` e não tem restrição de horário.

    `acao` é a ação declarada pelo agente (vocabulário de `NIVEL_AUTONOMIA`).
    Ação A0 sem humano é bloqueada; A1 sem aprovação registrada também.
    """
    if not text or not text.strip():
        raise PolicyBlock("Mensagem vazia.")
    _autorizar(igsid, exige_janela=True, texto=text, proativo=proativo, acao=acao)

    ig_user_id = os.environ["IG_USER_ID"]
    try:
        resultado = _post(
            f"{ig_user_id}/messages", {"recipient": {"id": igsid}, "message": {"text": text}}
        )
    except RuntimeError as e:
        # Recusa confirmada (4xx) propaga como erro conhecido. Timeout/rede é
        # resultado DESCONHECIDO: abre pendência e proíbe repetir no escuro (RN-021).
        if _recusa_confirmada(str(e)):
            raise
        rules.abrir_reconciliacao(
            acao="ig_send_dm", canal="instagram", igsid=igsid, detalhe=str(e)
        )
        raise ResultadoIncerto(str(e)) from None
    if proativo:
        # Só DEPOIS do envio confirmado: tentativa bloqueada não consome cota.
        rules.registrar_toque_proativo(igsid, tipo="followup", canal="instagram")
    _registrar_envio(igsid=igsid, acao="ig_send_dm")
    return resultado


def send_private_reply(comment_id: str, text: str, igsid: str = "", *, acao: str = "") -> dict:
    """Resposta privada a um comentário. UMA vez por comentário, até 7 dias.

    ATENÇÃO: envie SEMPRE texto puro. Anexos/botões são recusados para quem não
    segue a conta — e a chamada que falha AINDA consome a private reply (docs/01 §3.3).
    """
    if not text or not text.strip():
        raise PolicyBlock("Mensagem vazia.")

    # RN-001 primeiro: a parada de emergência não espera resolução de vínculo. A ordem
    # importa para o operador — "kill switch ligado" é a informação útil, não
    # "comentário sem autor registrado".
    _check_kill_switch()

    # RN-020 — a identidade é resolvida NO SERVIDOR, a partir do comentário. O igsid
    # recebido é conferido contra o dono real: comentário de terceiro não é alvo.
    igsid = _resolver_vinculo(comment_id, igsid)

    # A private reply é permitida justamente fora da janela de 24h (até 7 dias),
    # então NÃO exige janela — mas exige a cota, o vínculo e o kill switch.
    _autorizar(
        igsid,
        comment_id=comment_id,
        exige_janela=False,
        texto=text,
        acao=acao,
        exige_identidade=True,
    )

    ig_user_id = os.environ["IG_USER_ID"]
    # F01 do parecer OpenClaw: a operação privada é `POST /{ig-user-id}/messages` com
    # `recipient.comment_id`. `POST /{comment_id}/replies` cria um COMENTÁRIO PÚBLICO
    # (IGComment) — e era exatamente a mesma chamada das duas funções, ou seja: o que
    # o agente tratava como privado ia para o comentário, à vista de todos. Dar nomes
    # diferentes às funções não mudava o destino da chamada.
    try:
        resultado = _post(
            f"{ig_user_id}/messages",
            {"recipient": {"comment_id": comment_id}, "message": {"text": text}},
        )
    except RuntimeError as e:
        if not _recusa_confirmada(str(e)):
            # Não sei se saiu: NÃO marca a cota (marcar seria afirmar que saiu) e abre
            # a pendência que trava nova tentativa no escuro.
            rules.abrir_reconciliacao(
                acao="ig_private_reply",
                comment_id=comment_id,
                igsid=igsid,
                detalhe=str(e),
            )
            raise ResultadoIncerto(str(e)) from None
        # Recusa CONFIRMADA da Meta: a cota foi consumida de verdade. Só agora marca.
        rules.mark_private_reply_sent(comment_id)
        raise

    rules.mark_private_reply_sent(comment_id)
    _registrar_envio(igsid=igsid, comment_id=comment_id, acao="ig_private_reply")
    return resultado


def reply_comment_public(comment_id: str, text: str, igsid: str = "", *, acao: str = "") -> dict:
    """Resposta pública a um comentário. Visível para todo mundo.

    Não exige janela (é público), mas exige kill switch e respeita opt-out — e aqui
    a despedida NÃO é permitida: quem pediu para ser deixado em paz não é engajado
    em público nem uma vez.

    `texto` passa pelo guardrail de saída (RN-010/RN-011): comentário público é
    onde uma promessa de resultado faz mais estrago.
    """
    if not text or not text.strip():
        raise PolicyBlock("Mensagem vazia.")

    # RN-001 primeiro — mesma razão da private reply: o motivo do bloqueio é a
    # informação mais útil para quem opera.
    _check_kill_switch()

    # RN-020 — o vínculo vale também no público: quem pediu para ser deixado em paz
    # não é engajado em comentário, e comentário de terceiro não é alvo.
    #
    # `comment_id` NÃO entra em `_autorizar` de propósito: ali ele dispara a checagem
    # de cota da private reply, que não tem nada a ver com resposta pública.
    igsid = _resolver_vinculo(comment_id, igsid)
    _autorizar(
        igsid,
        exige_janela=False,
        permite_despedida=False,
        texto=text,
        acao=acao,
        exige_identidade=True,
    )

    resultado = _post(f"{comment_id}/replies", {"message": text})
    _registrar_envio(igsid=igsid, comment_id=comment_id, acao="ig_reply_comment")
    return resultado
