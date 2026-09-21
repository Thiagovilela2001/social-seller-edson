#!/usr/bin/env python3
"""Vigia da fila humana — RN-012 (espera máxima do humano).

Responde uma pergunta só: **quais casos passaram do prazo combinado e ninguém
pegou?** Um SLA que ninguém cobra não é SLA, é enfeite — e o cliente descobre que
o prazo era decorativo no dia em que um caso de crise dorme a noite inteira.

Uso:
    python scripts/fila-humanas-sla.py        # (nome real: fila-humana-sla.py)
    python scripts/fila-humana-sla.py --json  # saída só JSON, para o agente

Saída em texto pensada para o agente ler e repassar ao time no Telegram.
Sai com código 0 sempre que conseguir ler o banco: "sem atraso" é um resultado
válido, não um erro. Exit != 0 apenas quando o próprio vigia falhou — e aí o
agente precisa saber que NÃO sabe se há atraso.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# O diretório do plugin fica ao lado deste script: <HERMES_HOME>/scripts/ e
# <HERMES_HOME>/plugins/instagram-seller/. Resolver por __file__, não por cwd.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "instagram-seller"
sys.path.insert(0, str(_PLUGIN_DIR))

import rules  # noqa: E402  (precisa vir depois do sys.path)


def montar_relatorio(*, registrar_alerta: bool = True) -> dict:
    agora = rules.now_brt()
    fora = rules.casos_fora_do_sla()
    resumo = rules.fila_resumo()

    if registrar_alerta:
        for caso in fora:
            rules.registrar_alerta_caso(int(caso["id"]))

    return {
        "gerado_em_brt": agora.isoformat(),
        "rules_version": rules.RULES_VERSION,
        "regra": rules.RN_ESPERA_HUMANA,
        "fila": resumo,
        "atrasados": [
            {
                "caso": int(caso["id"]),
                # RN-018 — mascarado, não cru: o relatório vai para o Telegram.
                "igsid": rules.mascarar_id(caso.get("igsid") or ""),
                "prioridade": caso.get("prioridade") or "",
                "motivo": caso.get("motivo") or "",
                "sla_minutos": rules.sla_minutos(caso.get("prioridade") or ""),
                "atraso_minutos": caso.get("atraso_minutos"),
                "alertas_anteriores": int(caso.get("alertas") or 0),
                "aberto_em_brt": rules.to_brt(caso["aberto_em"]).isoformat(),
            }
            for caso in fora
        ],
    }


def texto(relatorio: dict) -> str:
    fila = relatorio["fila"]
    atrasados = relatorio["atrasados"]

    linhas = [
        f"Fila humana — {relatorio['gerado_em_brt']}",
        f"abertos: {fila['abertos']} | assumidos: {fila['assumidos']} | "
        f"resolvidos: {fila['resolvidos']}",
    ]

    if not atrasados:
        linhas.append("Nenhum caso passou do prazo. Fila em dia.")
        return "\n".join(linhas)

    linhas.append("")
    linhas.append(f"{len(atrasados)} caso(s) PASSARAM DO PRAZO (RN-012):")
    for caso in atrasados:
        reincidente = (
            f" · já cobrado {caso['alertas_anteriores']}x"
            if caso["alertas_anteriores"]
            else ""
        )
        # RN-018 — o igsid NÃO sai no relatório: é identificador de pessoa e isto
        # aqui circula em app de mensagem. O número do caso é interno, não
        # identifica ninguém, e é por ele que o time abre o atendimento.
        linhas.append(
            f"  #{caso['caso']} [{caso['prioridade']}] {caso['motivo']} — "
            f"atraso de {caso['atraso_minutos']}min "
            f"(prazo era {caso['sla_minutos']}min){reincidente}"
        )
    linhas.append("")
    linhas.append(
        "Ação: alguém precisa ASSUMIR estes casos. Enquanto ninguém assume, o "
        "agente segue podendo acolher — e não pode vender (§17 do documento)."
    )
    return "\n".join(linhas)


def main() -> int:
    parser = argparse.ArgumentParser(description="Vigia de SLA da fila humana")
    parser.add_argument("--json", action="store_true", help="saída apenas em JSON")
    parser.add_argument(
        "--sem-registrar",
        action="store_true",
        help="não incrementa o contador de cobranças dos casos",
    )
    args = parser.parse_args()

    try:
        relatorio = montar_relatorio(registrar_alerta=not args.sem_registrar)
    except Exception as exc:  # noqa: BLE001 — o agente PRECISA saber que não sabe
        print(
            f"ERRO: não foi possível ler a fila humana ({type(exc).__name__}: {exc}). "
            "NÃO afirme que a fila está em dia.",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(relatorio, ensure_ascii=False))
    else:
        print(texto(relatorio))
        print()
        print(json.dumps(relatorio, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
