"""Camada de acesso à Meta Graph API + regras de política.

PRINCÍPIO CENTRAL DESTE ARQUIVO:
As regras da Meta (janela de 24h, private reply única, kill switch) são
impostas AQUI, em código, no caminho do envio. Nunca no prompt, e nunca
APENAS em hook.

Motivo: a issue #100942 do hermes-agent relata que hooks pre_tool_call de
bloqueio falham ABERTO silenciosamente sob gateway non-TTY. Um guardrail que
pode não registrar não é um guardrail. Código no caminho do envio não tem
como "não registrar".
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

# Versão da Graph API. Fixe e revise periodicamente — a Meta descontinua versões.
GRAPH_VERSION = os.getenv("IG_GRAPH_VERSION", "v21.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Janela padrão de mensageria do Instagram.
WINDOW_SECONDS = 24 * 60 * 60

# Private reply: 1 por comentário, até 7 dias.
PRIVATE_REPLY_MAX_AGE = 7 * 24 * 60 * 60

# Estado do spike. Em produção isto é Postgres (ver docs/01 §5).


def _hermes_home() -> Path:
    """HERMES_HOME do processo, com fallback ~/.hermes.

    Path.home() nao e a mesma coisa: no Windows o Hermes vive em AppData.
    """
    env = os.getenv("HERMES_HOME")
    return Path(env) if env else Path.home() / ".hermes"


STATE_DB = Path(
    os.getenv("IG_STATE_DB", str(_hermes_home() / "instagram-seller.db"))
)

# Arquivo-flag do kill switch. O cliente liga/desliga pelo painel.
KILL_SWITCH = Path(
    os.getenv("IG_KILL_SWITCH", str(_hermes_home() / "ig-kill-switch"))
)


class PolicyBlock(Exception):
    """Envio recusado por política. Levantada SEMPRE antes de tocar na rede."""


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------

@contextmanager
def _db():
    """Conexão com o estado local, sempre fechada ao sair.

    NÃO use `with sqlite3.connect(...) as conn`: o context manager do sqlite
    faz commit/rollback mas NÃO fecha a conexão. A conexão vazada mantém o
    arquivo travado — no Windows isso impede até apagar o diretório.
    """
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DB)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbound (
                igsid       TEXT PRIMARY KEY,
                last_seen   REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS private_replies (
                comment_id  TEXT PRIMARY KEY,
                sent_at     REAL NOT NULL
            )
            """
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_inbound(igsid: str, ts: float | None = None) -> None:
    """Chamado pelo script da rota a cada mensagem RECEBIDA do usuário.

    É o que abre (e reabre) a janela de 24h. Sem isso, ig_send_dm sempre falha.
    """
    ts = ts or time.time()
    with _db() as conn:
        conn.execute(
            "INSERT INTO inbound (igsid, last_seen) VALUES (?, ?) "
            "ON CONFLICT(igsid) DO UPDATE SET last_seen = excluded.last_seen",
            (igsid, ts),
        )


def window_remaining(igsid: str) -> float:
    """Segundos restantes da janela de 24h. Negativo = expirada."""
    with _db() as conn:
        row = conn.execute(
            "SELECT last_seen FROM inbound WHERE igsid = ?", (igsid,)
        ).fetchone()
    if not row:
        return -1.0
    return (row[0] + WINDOW_SECONDS) - time.time()


# --------------------------------------------------------------------------
# Guardrails — executados ANTES de qualquer chamada de rede
# --------------------------------------------------------------------------

def _check_kill_switch() -> None:
    if KILL_SWITCH.exists():
        raise PolicyBlock(
            "Kill switch ativo. Envio bloqueado. "
            f"Desligue removendo {KILL_SWITCH}."
        )


def _check_window(igsid: str) -> None:
    remaining = window_remaining(igsid)
    if remaining <= 0:
        raise PolicyBlock(
            "Janela de 24h expirada (ou usuário nunca falou com a conta). "
            "O Instagram não permite envio. Use WhatsApp/e-mail com consentimento."
        )


def _check_private_reply_available(comment_id: str) -> None:
    with _db() as conn:
        row = conn.execute(
            "SELECT sent_at FROM private_replies WHERE comment_id = ?",
            (comment_id,),
        ).fetchone()
    if not row:
        return
    age = time.time() - row[0]
    raise PolicyBlock(
        "Private reply já consumida para este comentário "
        f"(enviada há {int(age)}s). São 1 por comentário — não existe retry. "
        "De agora em diante, só DM ou resposta pública."
    )


def _mark_private_reply_sent(comment_id: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO private_replies (comment_id, sent_at) VALUES (?, ?)",
            (comment_id, time.time()),
        )


# --------------------------------------------------------------------------
# Graph API
# --------------------------------------------------------------------------

def _post(path: str, payload: dict) -> dict:
    token = os.environ["IG_ACCESS_TOKEN"]
    url = f"{GRAPH_BASE}/{path}?access_token={urllib.parse.quote(token)}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
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
    _check_kill_switch()
    _check_window(igsid)

    if not text or not text.strip():
        raise PolicyBlock("Mensagem vazia.")

    ig_user_id = os.environ["IG_USER_ID"]
    return _post(
        f"{ig_user_id}/messages",
        {"recipient": {"id": igsid}, "message": {"text": text}},
    )


def send_private_reply(comment_id: str, text: str) -> dict:
    """Resposta privada a um comentário. UMA vez por comentário, até 7 dias.

    ATENÇÃO: envie SEMPRE texto puro. Há relato de que anexos/botões são
    recusados para quem não segue a conta — e a chamada que falha AINDA
    consome a private reply. Ver docs/01 §3.3.
    """
    _check_kill_switch()
    _check_private_reply_available(comment_id)

    if not text or not text.strip():
        raise PolicyBlock("Mensagem vazia.")

    # Marca ANTES da chamada: uma falha de rede também queima a private reply,
    # e um retry cego geraria erro em cima de erro.
    _mark_private_reply_sent(comment_id)

    return _post(f"{comment_id}/replies", {"message": text})


def reply_comment_public(comment_id: str, text: str) -> dict:
    """Resposta pública a um comentário. Visível para todo mundo."""
    _check_kill_switch()
    if not text or not text.strip():
        raise PolicyBlock("Mensagem vazia.")
    return _post(f"{comment_id}/replies", {"message": text})
