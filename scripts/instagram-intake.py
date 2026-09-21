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

# ---------------------------------------------------------------------------
# Proibições específicas por gatilho A0 — as RN-002/003/004 em linguagem de
# instrução. A proibição genérica ("não mencione preço") não cobre o que cada
# cenário exige; e o cenário é justamente onde o agente erra com boa intenção.
# ---------------------------------------------------------------------------
PROIBICOES_POR_FLAG: dict[str, list[str]] = {
    "crise_emocional": [
        "PROIBIDO mencionar produto, preço, link, curso, 'isso passa' ou qualquer próximo passo comercial. Sem exceção, sem A/B.",
        "Use o protocolo de crise aprovado LITERAL, incluindo o CVV 188.",
        "A automação desta conversa está PAUSADA até um humano liberar.",
    ],
    "saude_mental": [
        "PROIBIDO aconselhar, diagnosticar ou indicar tratamento.",
        "Acolher em uma linha e encaminhar. Nenhum próximo passo comercial.",
    ],
    "desespero_financeiro": [
        "PROIBIDO vender, ofertar ou mencionar dinheiro como saída.",
        "Acolher e encaminhar. Exploração de vulnerabilidade financeira é linha vermelha.",
    ],
    "menor_idade": [
        "PROIBIDO ofertar, enviar link ou falar de preço — a conversa é encerrada sem oferta.",
        "PROIBIDO pedir dado pessoal. Encaminhar para humano imediatamente.",
    ],
    "dados_de_terceiro": [
        "NÃO processe, NÃO repita e NÃO registre documento, print ou cadastro de terceiro que tenha aparecido.",
        "NÃO peça o dado novamente. Encaminhe para humano.",
    ],
    "juridico": [
        "PROIBIDO responder por conta própria sobre Procon, processo, advogado ou CDC.",
        "PROIBIDO admitir culpa, prometer acordo ou propor reembolso.",
        "Encaminhar para humano imediatamente.",
    ],
    "pedido_desconto": [
        "PROIBIDO conceder, prometer ou dizer que 'vou ver com o time' sobre desconto.",
        "Desconto é A0 e decisão do Edson. Escalar.",
    ],
    "reclamacao": [
        "ABRIR atendimento e PARAR de vender. Resolver tem prioridade sobre vender.",
        "Não peça comprovante nem dado pessoal por comentário público.",
    ],
    "pedido_humano": [
        "Atender na hora: avisar que um humano vai assumir e pausar a automação.",
    ],
    "hostilidade": [
        "NÃO rebater, ironizar ou entrar em discussão.",
        "Se repetir, encerrar sem anunciar.",
    ],
}


def _proibicoes_por_flag(decisao: rules.Decision) -> list[str]:
    """Proibições específicas dos gatilhos que dispararam, sem repetir linha."""
    vistas: list[str] = []
    for hit in decisao.flags:
        for linha in PROIBICOES_POR_FLAG.get(hit.flag, []):
            if linha not in vistas:
                vistas.append(linha)
    return vistas


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
        # Proibições específicas do gatilho (RN-002/003/004): a lista genérica não
        # cobre o que cada cenário exige.
        proibicoes = list(proibicoes) + _proibicoes_por_flag(decisao)

        # RN-019 — proibição PERMANENTE enquanto não houver integração (é o estado
        # atual): impede o agente de improvisar status de pedido antes mesmo de o
        # gate barrar. O gate é a barreira dura; isto é a instrução, para o bloqueio
        # nem acontecer.
        proibicoes = list(proibicoes) + [rules.diretiva_sem_integracao()]

        # RN-008 — divulgação devida? Depende do MODO e, no modo padrão
        # (`sob_pergunta`), de a pessoa ter perguntado. Não é escolha do modelo.
        #
        # ATENÇÃO À CHAVE: o evento normalizado usa `text` (linhas 194/218) e o motor
        # devolve o mesmo conteúdo em `decisao.text`. Ler `evento["texto"]` devolvia
        # string vazia e transformava esta regra em código morto — o teste unitário
        # passava e o caminho real não disparava nunca. Por isso o texto sai do
        # `decisao`, que é a fonte que o motor já validou.
        texto_da_pessoa = decisao.text or evento.get("text") or ""
        perguntou_automacao = rules.pessoa_perguntou_se_e_automacao(texto_da_pessoa)
        if rules.divulgar_agora(texto_da_pessoa):
            directivas = list(directivas) + [rules.diretiva_divulgacao()]

        igsid = evento.get("igsid") or ""
        avisos_expurgo = ""

        # Persiste as flags A0 do lead. Sem isto, a flag morre no fim do turno e
        # o agente pode mandar preço para alguém em crise no turno seguinte.
        for hit in decisao.flags:
            rules.registrar_flag(igsid, hit.flag, hit.severity)

        # RN-012 — escalada abre caso com PRAZO. "Prazos a definir" é o mesmo que
        # não ter prazo: sem `prazo_sla` gravado não existe atraso a detectar.
        #
        # RN-014 — o resumo do caso NÃO leva o texto da pessoa. Era aqui que a
        # mensagem de crise ia parar dentro da fila e, de lá, numa mensagem de
        # Telegram: dado sensível, ligado a identificador, saindo do perímetro. O
        # caso diz o que aconteceu (motivo, severidade, flags); não repete o que a
        # pessoa escreveu. Quem precisa do texto abre a conversa no Instagram.
        if decisao.acao == "escalar" and igsid:
            rules.abrir_caso_humano(
                igsid=igsid,
                prioridade=rules.pior_severidade(decisao.flags),
                motivo=decisao.motivo,
                resumo=rules.resumo_para_fila(decisao),
            )

        # RN-015 — expurgo do que passou do prazo de guarda, no máximo uma vez por
        # dia. Roda aqui porque o intake é o único ponto que executa sempre; e a
        # falha NÃO é silenciosa: se não rodar, o horário não é atualizado e a
        # próxima execução tenta de novo.
        try:
            rules.expurgar_se_preciso()
        except Exception as exc:  # noqa: BLE001 — expurgo nunca pode derrubar o atendimento
            avisos_expurgo = f"expurgo falhou: {type(exc).__name__}: {exc}"

        interaction_id = f"ig:{evento['tipo_evento']}:{decisao.event_id}"
        decisao.interaction_id = interaction_id
        rules.record_interaction(
            interaction_id=interaction_id,
            igsid=igsid,
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

    # RN-001 — parada de emergência. O intake NÃO descarta o evento (o agente
    # segue lendo e registrando), mas declara que nenhum envio vai sair. Sem isto
    # o agente gasta o turno tentando enviar e recebendo recusa sem entender.
    kill_switch = rules.kill_switch_path().exists()
    if kill_switch:
        directivas = list(directivas) + [
            "PARADA DE EMERGÊNCIA ATIVA: NENHUM envio é permitido. "
            "Registre o evento, explique a limitação se houver canal humano, e não tente enviar."
        ]
        proibicoes = list(proibicoes) + [
            "PROIBIDO chamar ig_send_dm, ig_private_reply ou ig_reply_comment: "
            "o kill switch está ligado e o envio será recusado."
        ]

    igsid_evento = evento.get("igsid") or ""
    saida = {
        "briefing": _briefing(evento, decisao),
        "diretiva": decisao.acao,
        "decisao": decisao.to_dict(),
        "diretivas_obrigatorias": directivas,
        "proibicoes": proibicoes,
        "estado": {
            "kill_switch_ativo": kill_switch,
            "flags_ativas": rules.flags_ativas(igsid_evento),
            "humano_no_comando": rules.humano_no_comando(igsid_evento),
            "fila_humana": rules.fila_resumo(),
            "rules_version": decisao.rules_version,
            # RN-015 — se o expurgo falhou, isto aparece. Retenção que não roda e
            # não avisa é o pior estado: o dado continua lá e ninguém sabe.
            "expurgo": avisos_expurgo or "ok",
            # RN-019 — o agente precisa saber (e o operador também) que não há fonte
            # de pedido. `false` aqui é o motivo de ele não poder afirmar status.
            "integracoes": rules.integracoes_status(),
            # RN-008 — modo vigente e se a divulgação é devida nesta mensagem.
            "modo_divulgacao": rules.modo_divulgacao(),
            "divulgar_automacao": rules.divulgar_agora(texto_da_pessoa),
            "perguntou_sobre_automacao": perguntou_automacao,
        },
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
