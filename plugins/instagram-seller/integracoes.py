"""Adaptadores das integrações prometidas no Documento Mestre — hoje INDISPONÍVEIS.

Por que este arquivo existe: a v2 cita Bling **36x**, Clint **45x**, WhatsApp **32x**,
e-mail **32x** e rastreio **19x**. O agente entregue tem zero linha de integração. Sem
uma interface explícita, o caminho natural é o modelo improvisar: inventar um status
de pedido plausível, confirmar uma entrega que não viu, prometer um rastreio que não
existe. Isso é pior do que não ter integração, porque vira afirmação falsa com a
assinatura do cliente.

Estes adaptadores **falham FECHADO**: quem chama recebe `FonteIndisponivel`, e a regra
RN-019 transforma isso em bloqueio de envio. Nada aqui devolve dado inventado.

Quando a credencial existir, é AQUI que a leitura entra — e o resto do agente não
muda, porque a interface já está no lugar e o bloqueio da RN-019 se desfaz sozinho.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class FonteIndisponivel(RuntimeError):
    """A fonte não está configurada.

    Levantada em vez de devolver um dicionário vazio ou um `None` silencioso: um
    retorno vazio seria indistinguível de "não existe pedido", e o agente diria ao
    cliente que o pedido dele não existe.
    """

    def __init__(self, integracao: str, variavel: str, o_que: str = "") -> None:
        detalhe = f" — {o_que}" if o_que else ""
        super().__init__(
            f"{integracao} não configurada: falta {variavel} no .env{detalhe}"
        )
        self.integracao = integracao
        self.variavel = variavel
        self.o_que = o_que


# --------------------------------------------------------------------------
# CAPACIDADES — o que existe de verdade, separado do que está configurado
#
# O parecer OpenClaw (F03) apontou o defeito: credencial preenchida liberava a
# RN-019 enquanto a consulta ainda era `NotImplementedError`. Estas chaves são a
# resposta honesta e são a ÚNICA forma de liberar afirmação transacional. Ligar uma
# capacidade é o passo que falta depois de implementar e homologar a leitura — não
# o `export` de um token.
# --------------------------------------------------------------------------

CAPACIDADES: dict[str, bool] = {
    # Consultas transacionais — nenhuma implementada hoje.
    "bling.consultar_pedido": False,
    "bling.consultar_rastreio": False,
    "bling.evidencia_pagamento": False,
    "clint.vinculo_do_cliente": False,
    "clint.contexto_do_cliente": False,
    # Envios por outros canais — nenhum implementado hoje.
    "whatsapp.enviar": False,
    "email.enviar": False,
    # Moderação executável — preparada no motor, sem ferramenta no plugin.
    "meta.ocultar_comentario": False,
    "meta.excluir_comentario": False,
    "meta.reexibir_comentario": False,
}


def _configurada(variavel: str) -> bool:
    return bool((os.getenv(variavel) or "").strip())


# --------------------------------------------------------------------------
# Disponibilidade — o que decide se a RN-019 bloqueia ou libera
# --------------------------------------------------------------------------

def bling_disponivel() -> bool:
    """Bling (ERP): pedidos, financeiro e rastreio. PDF §11, [T4]."""
    return _configurada("BLING_API_TOKEN")


def clint_disponivel() -> bool:
    """Clint (CRM): histórico de WhatsApp, lead e oportunidade. Somente leitura."""
    return _configurada("CLINT_MCP_URL")


def whatsapp_disponivel() -> bool:
    """Canal de WhatsApp. O Instagram não é canal de follow-up (PDF §06)."""
    return _configurada("WHATSAPP_TOKEN") and _configurada("WHATSAPP_PHONE_ID")


def status_pedido_disponivel() -> bool:
    """Há fonte CONFIÁVEL para afirmar status de pedido, pagamento ou rastreio?

    A distinção obrigatória do parecer OpenClaw (F03):

        credencial preenchida ≠ integração funcionando ≠ cliente autorizado
        ≠ pedido consultado ≠ pagamento confirmado

    Antes desta correção, `bling_disponivel()` (token preenchido) liberava a RN-019
    enquanto `consultar_pedido` ainda levantava `NotImplementedError`. Bastava pôr um
    token no `.env` para **retirar a barreira** e o agente voltar a poder afirmar
    status sem que nenhuma consulta existisse.

    Agora o portão é a CAPACIDADE, não a credencial: `bling_disponivel()` responde
    "está configurado"; `status_pedido_disponivel()` responde "posso afirmar com
    evidência". Sem leitura implementada a capacidade é False, e nenhum token muda
    isso.
    """
    return CAPACIDADES.get("bling.consultar_pedido", False) and bling_disponivel()


def bling_saudavel() -> bool:
    """A integração respondeu de verdade nas últimas verificações?

    Hoje sempre False: não há leitura implementada. Existe separado de
    `bling_disponivel()` porque, quando houver, "configurado mas fora do ar" precisa
    ser um estado próprio — e não pode virar informação positiva nem negativa de
    pagamento.
    """
    return CAPACIDADES.get("bling.consultar_pedido", False)


def CAPACIDADE_IMPLEMENTADA(nome: str) -> bool:  # noqa: N802 - vocabulário do parecer
    return bool(CAPACIDADES.get(nome, False))


def status_integracoes() -> dict[str, bool]:
    """Retrato honesto do que existe, para o briefing e para o handover.

    Separa CONFIGURADO de IMPLEMENTADO de propósito (achado F03 do parecer OpenClaw:
    "credencial preenchida ≠ integração funcionando"). Quem lê isto precisa poder
    distinguir "falta o token" de "o token está lá e a leitura não existe" — são
    pendências diferentes, com responsáveis diferentes.
    """
    return {
        # CONFIGURADO: a credencial está no ambiente.
        "bling_configurado": bling_disponivel(),
        "clint_configurado": clint_disponivel(),
        "whatsapp_configurado": whatsapp_disponivel(),
        # IMPLEMENTADO: existe leitura de verdade. É o que autoriza afirmar algo.
        "bling_consulta": CAPACIDADES.get("bling.consultar_pedido", False),
        # AUTORIZADO A AFIRMAR: configurado E implementado. Só isto libera a RN-019.
        "status_de_pedido": status_pedido_disponivel(),
    }


# --------------------------------------------------------------------------
# Leituras — todas indisponíveis até a credencial existir
# --------------------------------------------------------------------------

def consultar_pedido(igsid: str) -> dict:
    """Pedidos do cliente validado. PDF §11: 'Ler pedidos e dados financeiros
    pertinentes ao cliente validado. Sem baixa, estorno, cancelamento, emissão
    fiscal ou movimentação de estoque.'"""
    if not bling_disponivel():
        raise FonteIndisponivel(
            "Bling", "BLING_API_TOKEN",
            "sem ela não há como afirmar nada sobre pedido, pagamento ou rastreio",
        )
    raise NotImplementedError(
        "Leitura do Bling ainda não implementada. A interface existe para que a "
        "RN-019 libere o agente no dia em que o token estiver no .env — e para que "
        "o bloqueio seja removido por configuração, não por edição de regra."
    )


def consultar_rastreio(pedido_id: str) -> dict:
    if not bling_disponivel():
        raise FonteIndisponivel("Bling", "BLING_API_TOKEN", "rastreio vem do Bling")
    raise NotImplementedError("Consulta de rastreio ainda não implementada.")


def vinculo_do_cliente(igsid: str) -> dict:
    """Vínculo autorizado entre conta social e cliente. PDF §12.

    Sem isto, a RN-004 (dados de terceiro) é barreira de linguagem, não de dados —
    não há como confirmar que quem fala é o titular.
    """
    if not (bling_disponivel() or clint_disponivel()):
        raise FonteIndisponivel(
            "Clint/Bling", "CLINT_MCP_URL ou BLING_API_TOKEN",
            "sem vínculo não há como validar que quem fala é o titular",
        )
    raise NotImplementedError("Validação de vínculo ainda não implementada.")


def enviar_whatsapp(igsid: str, texto: str) -> dict:
    """Botão de WhatsApp. Exige consentimento registrado (PDF §16) e canal próprio."""
    if not whatsapp_disponivel():
        raise FonteIndisponivel(
            "WhatsApp", "WHATSAPP_TOKEN e WHATSAPP_PHONE_ID",
            "o Instagram não é canal de follow-up",
        )
    raise NotImplementedError("Envio por WhatsApp ainda não implementado.")
