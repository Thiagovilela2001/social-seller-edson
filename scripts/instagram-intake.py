#!/usr/bin/env python3
"""Intake determinístico do Social Seller — roda ANTES do LLM.

Contrato do Hermes para script de rota de webhook (`gateway/platforms/webhook_filters.py`):
  - invocado como  [python, <este arquivo>]   com cwd = <HERMES_HOME>/scripts/
  - payload JSON bruto chega no STDIN
  - STDOUT vira o payload do agente:
        JSON objeto  -> SUBSTITUI o payload
        "[SILENT]"   -> descarta o evento
        saída vazia ou exit != 0 -> descarta o evento
  - limites/secretas: env saneado, timeout de segundos

CONSEQUÊNCIA DE DESENHO — leia antes de "consertar":
    Este script FALHA FECHADO. Se ele quebrar, o evento é DESCARTADO, não repassado
    cru para o modelo. É de propósito: sem o motor de regras não se sabe se a pessoa
    está em crise ou pediu para não ser contatada, e responder sem saber é o pior
    resultado possível (PDF §04: "as regras autorizam").
    Para o evento não se perder, toda falha é gravada com o payload original em
    <HERMES_HOME>/logs/instagram-intake-falhas.jsonl — replay é possível.

O que este script NUNCA faz: enviar mensagem. Ele só decide e enriquece.
A autorização de envio mora em `instagram_api.py` (última barreira antes da rede).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# O diretório do plugin fica ao lado deste script: <HERMES_HOME>/scripts/ e
# <HERMES_HOME>/plugins/instagram-seller/. Resolver por __file__ (não por cwd nem
# por HERMES_HOME) para funcionar igual no repo e no profile instalado.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "instagram-seller"
sys.path.insert(0, str(_PLUGIN_DIR))

import rules  # noqa: E402  (precisa vir depois do sys.path)

# Campos de webhook da Meta que sabemos tratar. O resto é registrado e descartado
# de propósito — melhor não processar do que processar errado.
CAMPOS_TRATADOS = {"comments", "live_comments", "messages"}

DIRETIVAS_POR_ACAO: dict[str, tuple[list[str], list[str]]] = {
    "responder": ([], []),
    "responder_com_cautela": (
        ["Responder normalmente, uma vez, sem insistir."],
        [
            "NÃO rebater, ironizar ou entrar em discussão.",
            "NÃO pedir desculpas repetidas nem justificar demais.",
            "Se repetir ofensa, encerrar o atendimento sem anunciar.",
        ],
    ),
    "escalar": (
        [
            "Escalar para humano (fila de A0).",
            "Usar SOMENTE o texto aprovado do protocolo para este flag.",
            "Registrar a interação e marcar automacao_pausada no lead.",
        ],
        [
            "NÃO mencionar preço, oferta, link ou próxima etapa de venda.",
            "NÃO dar conselho de saúde, jurídico ou financeiro.",
            "NÃO pedir dados pessoais (documento, telefone, endereço, comprovante).",
            "NÃO rebater hostilidade.",
        ],
    ),
    "encerrar": (
        [
            "Agradecer em UMA linha e encerrar.",
            "Registrar opt-out imediato e permanente.",
        ],
        [
            "NÃO vender, ofertar, nem apresentar próxima etapa.",
            "NÃO perguntar o motivo, NÃO tentar reverter.",
            "NÃO enviar follow-up futuro algum.",
        ],
    ),
}


def _log_falha(raw: str, exc: Exception) -> Path:
    """Grava a falha COM o payload original, para não perder o evento."""
    destino = rules.hermes_home() / "logs" / "instagram-intake-falhas.jsonl"
    destino.parent.mkdir(parents=True, exist_ok=True)
    registro = {
        "at": datetime.now(rules.tz_brt()).isoformat(),
        "erro": f"{type(exc).__name__}: {exc}",
        "traceback": traceback.format_exc()[-2000:],
        "rules_version": rules.RULES_VERSION,
        "payload_bruto": raw[:8000],
    }
    try:
        with destino.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
    except OSError:
        pass  # não há o que fazer se nem o log abre; o descarte já é seguro
    return destino


def _normalizar(payload: dict) -> list[dict]:
    """Achata o webhook da Meta em eventos simples.

    Trata os dois formatos que a conta recebe:
      - comentário:  entry[].changes[]  com value.field in {comments, live_comments}
      - Direct:      entry[].messaging[].message
    """
    eventos: list[dict] = []
    if (payload.get("object") or "").lower() not in {"", "instagram"}:
        return eventos

    for entry in payload.get("entry") or []:
        conta_id = str(entry.get("id") or "")

        # --- comentários (feed e live) ---
        for change in entry.get("changes") or []:
            campo = str(change.get("field") or "")
            valor = change.get("value") or {}
            if campo not in {"comments", "live_comments"}:
                eventos.append({"tipo_evento": "nao_tratado", "campo": campo})
                continue
            autor = valor.get("from") or {}
            autor_id = str(autor.get("id") or "")
            if autor_id and autor_id == conta_id:
                eventos.append({"tipo_evento": "proprio_comentario", "campo": campo})
                continue
            eventos.append(
                {
                    "tipo_evento": "comentario",
                    "campo": campo,
                    "igsid": autor_id,
                    "username": autor.get("username") or "",
                    "text": valor.get("text") or "",
                    "comment_id": str(valor.get("id") or ""),
                    "parent_id": str(valor.get("parent_id") or ""),
                    "media_id": str((valor.get("media") or {}).get("id") or ""),
                    "media_produto": (valor.get("media") or {}).get("media_product_type") or "",
                    "timestamp": valor.get("timestamp") or entry.get("time"),
                    "conta_id": conta_id,
                }
            )

        # --- Direct (mensagens) ---
        for msg in entry.get("messaging") or []:
            mensagem = msg.get("message") or {}
            if mensagem.get("is_echo"):
                eventos.append({"tipo_evento": "echo", "campo": "messages"})
                continue
            if mensagem.get("is_deleted"):
                eventos.append({"tipo_evento": "apagada", "campo": "messages"})
                continue
            eventos.append(
                {
                    "tipo_evento": "direct",
                    "campo": "messages",
                    "igsid": str((msg.get("sender") or {}).get("id") or ""),
                    "text": mensagem.get("text") or "",
                    "message_id": str(mensagem.get("mid") or ""),
                    "anexo": mensagem.get("attachments") or [],
                    "timestamp": msg.get("timestamp"),
                    "conta_id": conta_id,
                }
            )
    return eventos


def _briefing(evento: dict, decisao: rules.Decision) -> str:
    """Resumo em português do que chegou e do que foi decidido — o agente lê isto
    primeiro, para não depender de interpretar JSON aninhado."""
    canal = "comentário público" if evento["tipo_evento"] == "comentario" else "Direct"
    linhas = [
        f"[{canal}] @{evento.get('username') or evento.get('igsid') or 'desconhecido'}",
        f"Texto: {decisao.text or '(sem texto)'}",
        f"Regra aplicada: {decisao.motivo} (v{decisao.rules_version})",
        f"Ação determinada: {decisao.acao}",
    ]
    if decisao.flags:
        linhas.append(
            "A0: " + "; ".join(f"{f.flag}/{f.severity}" for f in decisao.flags)
        )
    if decisao.detalhe:
        linhas.append(f"Orientacao: {decisao.detalhe}")
    if decisao.avisos:
        linhas.append("Avisos: " + ", ".join(decisao.avisos))
    return "\n".join(linhas)


def main() -> int:
    raw = sys.stdin.read() or "{}"
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload do webhook nao e um objeto JSON")
    except Exception as exc:  # payload ilegível: descartar, registrando
        _log_falha(raw, exc)
        print("[SILENT]")
        return 0

    try:
        eventos = _normalizar(payload)
    except Exception as exc:
        _log_falha(raw, exc)
        print("[SILENT]")
        return 0

    if not eventos:
        print("[SILENT]")
        return 0

    # Só UM evento passa por execução. Lote ambíguo é descartado e registrado:
    # responder dois webhooks num turno embaralha a janela de 24h.
    if len(eventos) > 1:
        trancados = [e for e in eventos if e["tipo_evento"] in {"comentario", "direct"}]
        if len(trancados) != 1:
            _log_falha(raw, ValueError(f"lote com {len(eventos)} eventos; esperado 1"))
            print("[SILENT]")
            return 0
        eventos = trancados

    evento = eventos[0]

    if evento["tipo_evento"] not in {"comentario", "direct"}:
        # Eco, comentário próprio, apagada, campo não tratado: nada a fazer.
        print("[SILENT]")
        return 0

    directivas, proibicoes = [], []
    try:
        decisao = rules.evaluate_intake(evento)
        if not decisao.permitir_agente:
            # Duplicata ou opt-out já registrado: descarta sem custo de LLM.
            print("[SILENT]")
            return 0

        directivas, proibicoes = DIRETIVAS_POR_ACAO.get(decisao.acao, ([], []))

        interaction_id = f"ig:{evento['tipo_evento']}:{decisao.event_id}"
        decisao.interaction_id = interaction_id
        rules.record_interaction(
            interaction_id=interaction_id,
            igsid=evento.get("igsid") or "",
            channel=evento["tipo_evento"],
            media_id=evento.get("media_id") or "",
            comment_id=evento.get("comment_id") or "",
            message_id=evento.get("message_id") or "",
            risk=(decisao.severity or ""),
            recommended_action=decisao.acao,
            attribution_method="organico_publicacao" if evento.get("media_id") else "",
        )
        rules.mark_processed(decisao.event_id, evento["tipo_evento"])
    except Exception as exc:
        _log_falha(raw, exc)
        print("[SILENT]")
        return 0

    agora = rules.now_brt()
    saida = {
        "briefing": _briefing(evento, decisao),
        "diretiva": decisao.acao,
        "decisao": decisao.to_dict(),
        "diretivas_obrigatorias": directivas,
        "proibicoes": proibicoes,
        "evento": {
            "plataforma": "instagram",
            "tipo_evento": evento["tipo_evento"],
            "igsid": evento.get("igsid") or "",
            "username": evento.get("username") or "",
            "texto": decisao.text,
            "comment_id": evento.get("comment_id") or "",
            "parent_id": evento.get("parent_id") or "",
            "message_id": evento.get("message_id") or "",
            "media_id": evento.get("media_id") or "",
            "conta_id": evento.get("conta_id") or "",
            "recebido_em_brt": agora.isoformat(),
            "recebido_em_utc": agora.astimezone(timezone.utc).isoformat(),
        },
        "auditoria": {
            "event_id": decisao.event_id,
            "interaction_id": interaction_id,
            "rules_version": decisao.rules_version,
            "script": "instagram-intake.py",
            "script_version": "1.0.0",
        },
    }

    out = json.dumps(saida, ensure_ascii=False)
    # Guarda contra stdout gigante: o payload original fica só para auditoria.
    if len(out) > 60_000:
        out = json.dumps(
            {k: v for k, v in saida.items() if k != "payload_meta"}, ensure_ascii=False
        )[:60_000]
    print(out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as fatal:  # última barreira: nunca deixar passar cru
        try:
            _log_falha("<nao lido>", fatal)
        finally:
            print("[SILENT]")
        sys.exit(0)
