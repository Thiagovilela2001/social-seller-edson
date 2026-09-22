"""Motor de regras determinístico do Social Seller.

PRINCÍPIO CENTRAL (PDF §04, §06, §14):
    "A IA interpreta e propõe. As regras autorizam. As integrações executam."
    "Confiança não substitui uma regra."
    "Regras críticas não podem depender apenas de recuperação de documentos."

Nenhuma função deste arquivo consulta um modelo. Tudo é tabela, texto normalizado e
aritmética. O modelo recebe o RESULTADO; ele não decide o resultado.

POLÍTICA DE ERRO — deliberada e assimétrica:
    Para A0, **falso positivo é barato e falso negativo é catastrófico.**
    Escalar um humano sem necessidade custa alguns minutos de atenção. Deixar passar
    uma crise emocional, uma ameaça jurídica ou um menor de idade custa a marca.
    Por isso os padrões são de ALTA REVOCAÇÃO: na dúvida, escala.

    O viés oposto vale para promoção de estágio (ver `social-seller-edson`): na dúvida,
    NÃO promove. Os dois vieses não se contradizem — ambos escolhem o lado seguro.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# Versão das regras. Vai carimbada em TODA decisão (PDF §13: "explicar com qual
# informação, regra e versão a resposta foi produzida"). Suba ao mudar qualquer tabela.
#
# 1.x -> 2.0.0: entrou a seção REGRAS DE NEGÓCIO (REGRAS-DE-NEGOCIO.md), com os
# identificadores RN-* carimbados em cada bloqueio de envio.
# 2.2.0 -> 2.3.0: parecer OpenClaw. Entram a RN-020 (vínculo de identidade antes da
#   ação) e a RN-021 (efeito externo com resultado desconhecido), e são corrigidos os
#   achados F01 (privada usava a chamada da pública), F02 (identidade vazia nas rotas
#   de comentário), F03 (token liberava afirmação sem consulta) e F04 (ação/proatividade
#   autodeclaradas e opcionais).
# 2.1.0 -> 2.2.0: entrou a RN-019 (status de pedido/pagamento/rastreio só com fonte
# consultada) e a RN-008 passou a ter três modos de divulgação (nunca | sob_pergunta
# | sempre), com `sob_pergunta` como padrão.
# 2.0.0 -> 2.1.0: entrou a seção de PROTEÇÃO DE DADOS (RN-014..RN-018) — prazo de
# guarda por tabela, expurgo automático, minimização do texto e direito do titular.
RULES_VERSION = "2.3.0"

# ---------- janelas e limites (PDF §05 e docs/05 §2) ----------
DM_WINDOW_SECONDS = 24 * 60 * 60
PRIVATE_REPLY_MAX_AGE = 7 * 24 * 60 * 60
FOLLOWUP_QUIET_START = 21  # 21h BRT
FOLLOWUP_QUIET_END = 8  # 8h BRT
TZ_BRT = "America/Sao_Paulo"

# Limites de entrada. Texto maior que isso é truncado e marcado.
MAX_TEXT_CHARS = 4000

# Cota de toques de follow-up por estágio (prompts/follow-up.md)
FOLLOWUP_MAX_TOQUES = {
    "lead_quente": 3,
    "interessado": 2,
    "curioso": 1,
    "cliente": 3,
}

_SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# Só P0/P1 consomem humano. P2 é rótulo para a resposta (o agente se comporta
# diferente), não fila de emergência — senão a fila perde o valor.
_SEVERIDADES_ESCALAVEIS = ("P0", "P1")


# ---------------------------------------------------------------------------
# Caminhos
# ---------------------------------------------------------------------------

def hermes_home() -> Path:
    """HERMES_HOME do profile.

    Resolve pelo próprio arquivo quando a variável não está no ambiente — o script
    de rota roda como subprocesso com env saneado, e `Path.home()` NÃO é o
    HERMES_HOME (no Windows o Hermes vive em AppData, não em ~/.hermes).

    Layout: <home>/plugins/instagram-seller/rules.py -> parents[2] == <home>
    """
    env = os.getenv("HERMES_HOME")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]


def state_db_path() -> Path:
    return Path(os.getenv("IG_STATE_DB") or (hermes_home() / "instagram-seller.db"))


def kill_switch_path() -> Path:
    return Path(os.getenv("IG_KILL_SWITCH") or (hermes_home() / "ig-kill-switch"))


# ---------------------------------------------------------------------------
# Fuso horário
# ---------------------------------------------------------------------------

def tz_brt():
    """America/Sao_Paulo, com fallback de offset fixo.

    O Brasil não tem mais horário de verão desde 2019, então -03:00 é estável. O
    fallback existe porque `zoneinfo` depende de tzdata, que pode faltar no host.
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(TZ_BRT)
    except Exception:  # pragma: no cover - depende do host
        return timezone(timedelta(hours=-3))


def now_brt() -> datetime:
    return datetime.now(tz_brt())


def to_brt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz_brt())


# ---------------------------------------------------------------------------
# Normalização de texto
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Minúsculas, sem acento, sem pontuação, espaço colapsado.

    Necessário porque ninguém digita acento no Instagram: "nao aguento mais"
    precisa casar com "não aguento mais".
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    stripped = stripped.lower()
    # Mantém letras, dígitos e espaço. Troca o resto por espaço para não colar palavras.
    stripped = re.sub(r"[^a-z0-9\s]", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


# ---------------------------------------------------------------------------
# Tabela de gatilhos A0
#
# `patterns` são expressões regulares aplicadas ao texto JÁ NORMALIZADO.
# `forte=True` significa: esse termo sozinho já escala. O resto é corroborativo.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class A0Rule:
    """Um gatilho A0.

    `patterns` são decisivos: casar um já escala.
    `corroborativos` são ambíguos sozinhos ("não aguento mais" é hipérbole do dia a
    dia) e só escalam quando DOIS deles casam, ou quando algum padrão decisivo
    também casou. Existe para não transformar a fila de A0 em ruído: alerta que
    sempre dispara deixa de ser alerta.
    """

    flag: str
    severity: str
    human_label: str
    patterns: tuple[str, ...]
    corroborativos: tuple[str, ...] = ()
    pause_automation: bool = True


A0_RULES: tuple[A0Rule, ...] = (
    A0Rule(
        flag="crise_emocional",
        severity="P0",
        human_label="CRISE EMOCIONAL — prioridade máxima, automação pausada",
        patterns=(
            r"\bnao quero mais viver\b",
            r"\bquero morrer\b",
            r"\bqueria morrer\b",
            r"\bvou me matar\b",
            r"\bme matar\b",
            r"\btirar minha vida\b",
            r"\bacabar com a minha vida\b",
            r"\bsuicidio\b",
            r"\bsuicidar\b",
            r"\bme matando\b",
            r"\bdesistir de viver\b",
            r"\bautomutilacao\b",
            r"\bme cortar\b",
            r"\bme machucar\b",
            r"\bnao vale a pena viver\b",
            # Decisivos por serem quase inequívocos em DM de venda — e porque o
            # custo do falso positivo aqui é uma pessoa do time olhar a conversa,
            # contra o custo de não olhar.
            r"\bquero desaparecer\b",
            r"\bmelhor nao acordar\b",
            r"\bnunca mais acordar\b",
            r"\bnao quero mais estar aqui\b",
            # "não vejo saída" sozinho é ambíguo (problema, dívida, prazo); com
            # "da minha vida" deixa de ser.
            r"\bnao vejo saida (pra|para) (a )?minha vida\b",
        ),
        corroborativos=(
            # Ambíguos sozinhos — hipérbole comum. Precisam de companhia.
            r"\bnao aguento mais\b",
            r"\bnao vejo sentido\b",
            r"\bacabar com tudo\b",
            # Frases de crise que aparecem em texto real e NÃO estavam na lista:
            # "não aguento mais, penso em desistir de tudo" só tinha um sinal e
            # passava como conversa normal. Com duas, escala.
            r"\bdesistir de tudo\b",
            r"\bdesisti de tudo\b",
            r"\bnao vejo saida\b",
            r"\bnao tenho mais forca\b",
            r"\bcansad[oa] de viver\b",
            r"\bnao faz sentido continuar\b",
            r"\bquero sumir\b",
        ),
    ),
    A0Rule(
        flag="saude_mental",
        severity="P0",
        human_label="SAÚDE MENTAL — escalar, nunca aconselhar",
        patterns=(
            r"\bdepressao\b",
            r"\bdeprimid[oa]\b",
            r"\bcrise de panico\b",
            r"\bansiedade\b",
            r"\bbipolar\b",
            r"\bpsiquiatra\b",
            r"\bpsicolog[oa]\b",
            r"\bmedicacao\b",
            r"\bantidepressivo\b",
            r"\btomo remedio\b",
            r"\bdiagnostico\b",
        ),
    ),
    A0Rule(
        flag="menor_idade",
        severity="P0",
        human_label="MENOR DE IDADE — encerrar sem oferta",
        patterns=(
            r"\bsou menor\b",
            r"\bmenor de idade\b",
            r"\btenho 1[0-7] anos\b",
            r"\btenho [0-9] anos\b",
            r"\bfaco 1[0-7] (anos )?(em|no)\b",
            r"\bestou no (primeiro|segundo|terceiro|1o|2o|3o) ano\b",
        ),
    ),
    A0Rule(
        flag="juridico",
        severity="P0",
        human_label="JURÍDICO — escalar imediatamente, não responder por conta",
        patterns=(
            r"\bprocon\b",
            r"\bprocess[oa]r?\b",
            r"\badvogad[oa]\b",
            r"\bjustica\b",
            r"\bjudicial\b",
            r"\bcdc\b",
            r"\bcodigo de defesa do consumidor\b",
            r"\bvou denunciar\b",
            r"\bnao vou deixar barato\b",
            r"\bacao judicial\b",
            r"\bpequenas causas\b",
        ),
    ),
    A0Rule(
        flag="desespero_financeiro",
        severity="P0",
        human_label="DESESPERO FINANCEIRO — acolher, jamais vender",
        patterns=(
            r"\bperdi tudo\b",
            r"\bestou endividad[oa]\b",
            r"\btodo endividad[oa]\b",
            r"\bdivida com agiota\b",
            r"\bagiota\b",
            r"\bestou falid[oa]\b",
            r"\bnao tenho dinheiro nem\b",
            r"\bsem saida\b",
            r"\bnao consigo pagar\b",
        ),
    ),
    A0Rule(
        flag="hostilidade",
        # P2 e sem pausa: xingamento NÃO precisa de humano, precisa que o agente não
        # rebata. Escalar isso encheria a fila de A0 sem ganho de segurança.
        severity="P2",
        human_label="HOSTILIDADE — não rebater, responder uma vez ou encerrar",
        pause_automation=False,
        patterns=(
            r"\bidiota\b",
            r"\bimbecil\b",
            r"\botario\b",
            r"\bretardad[oa]\b",
            r"\bfilho da puta\b",
            r"\bvai se fuder\b",
            r"\bvai se foder\b",
            r"\bvsf\b",
            r"\bvai tomar no\b",
            r"\barrombad[oa]\b",
            r"\bpilantra\b",
            r"\bgolpista\b",
            r"\bcharlatao\b",
            r"\bdesgraca\b",
            r"\bmerda\b",
            r"\bbosta\b",
        ),
    ),
    A0Rule(
        flag="pedido_desconto",
        severity="P1",
        human_label="PEDIDO DE DESCONTO — nunca negociar, escalar",
        patterns=(
            r"\bdesconto\b",
            r"\bdescontinho\b",
            r"\bcupom\b",
            r"\bmais barato\b",
            r"\bbaixar o preco\b",
            r"\babaixa(r)? (o|esse) preco\b",
            r"\bparcelar sem juros\b",
            r"\bnegociar\b",
            r"\bcondicao especial\b",
            r"\bprecinho\b",
            r"\bfaz por quanto\b",
            r"\bquanto fica a vista\b",
            r"\btem como abaixar\b",
            r"\bfaz um precinho\b",
        ),
    ),
    A0Rule(
        flag="reclamacao",
        severity="P1",
        human_label="RECLAMAÇÃO — abrir atendimento, parar de vender",
        # Escopo deliberadamente restrito a sinais de COMPRA/PRODUTO. "não funciona"
        # sozinho é a crítica legítima ao tema (docs/00 §07 caso 2) e NÃO entra aqui.
        patterns=(
            r"\bnao recebi\b",
            r"\bnao chegou\b",
            r"\bnao foi entregue\b",
            r"\bnada chegou\b",
            r"\bproduto errado\b",
            r"\bcobranca indevida\b",
            r"\bcobrado duas vezes\b",
            r"\bcobraram duas vezes\b",
            r"\bnao consigo acessar\b",
            r"\bnao consigo baixar\b",
            r"\bnunca recebi o acesso\b",
            r"\bpaguei e nao\b",
            r"\bcomprei e nao\b",
            r"\bquero reembolso\b",
            r"\bquero devolucao\b",
            r"\bestorno\b",
            r"\bcancelar (a )?(minha )?compra\b",
            r"\bcancelamento\b",
            r"\bchargeback\b",
            r"\berro na cobranca\b",
        ),
    ),
    A0Rule(
        flag="pedido_humano",
        severity="P1",
        human_label="PEDIU HUMANO — atender na hora",
        patterns=(
            r"\bfalar com (uma )?pessoa\b",
            r"\bfalar com alguem\b",
            r"\bfalar com human[oa]\b",
            r"\batendente humano\b",
            r"\batendimento humano\b",
            r"\bquero falar com o edson\b",
            r"\bquero falar com o time\b",
            r"\bme passa (pra|para) alguem\b",
            r"\btem alguem ai\b",
            r"\bsou humano\b",
        ),
    ),
    A0Rule(
        flag="dados_de_terceiro",
        severity="P0",
        human_label="DADOS DE TERCEIRO — não processar, não armazenar, encaminhar",
        # docs/05 §3.3: conteúdo sensível de terceiro é A0. O agente não recebe
        # documento, print ou cadastro de quem não é o titular da conversa — nem
        # para "ajudar". Processar vira tratamento de dado sem base legal.
        patterns=(
            r"\bdocumento de outra pessoa\b",
            r"\bdados de outra pessoa\b",
            r"\bfoto de outra pessoa\b",
            # "do meu" / "da minha": as duas contrações, porque o agente recebe
            # tanto "documento do meu marido" quanto "comprovante da minha mae".
            r"\bcpf d[ao] (meu|minha|meus|minhas)\b",
            r"\bdocumento d[ao] (meu|minha|meus|minhas)\b",
            r"\bcomprovante d[ao] (meu|minha|meus|minhas)\b",
            r"\bconta d[ao] (meu|minha) (marido|esposa|pai|mae|filho|filha|irmao|irma|amigo|amiga)\b",
            r"\bno nome d[ao] (meu|minha)\b",
            r"\bcomprei no nome d[ao]\b",
            r"\bestou mandando o (documento|cpf|rg|comprovante) d[ao]\b",
        ),
    ),
)

# Opt-out: não é escalada, é PARADA PERMANENTE. Tratado fora de A0_RULES.
OPT_OUT_PATTERNS: tuple[str, ...] = (
    r"\bpara de me mandar\b",
    r"\bpare de me mandar\b",
    r"\bparar de me mandar\b",
    r"\bnao me manda mais\b",
    r"\bnao quero mais receber\b",
    r"\bnao quero receber\b",
    r"\bme tira\b",
    r"\bme remove\b",
    r"\bdescadastrar\b",
    r"\bdescadastra\b",
    r"\bnao me chame\b",
    r"\bnao me procure\b",
    r"\bnao tenho interesse\b",
    r"\bnao quero mais nada\b",
    r"\bbloqueado\b",
    r"\bvou bloquear\b",
    r"\bdeixa de ser\b",
    r"\bsai do meu\b",
)

# "Conteúdo não é comando" (PDF §21). Mensagem de cliente, anexo ou texto
# recuperado NÃO pode alterar instruções, liberar ações nem aprovar políticas.
INJECTION_PATTERNS: tuple[str, ...] = (
    r"\bignor[ea]\s+(as\s+)?(instrucoes|regras|orientacoes)\b",
    r"\bignore (all )?(previous|prior) (instructions|rules)\b",
    r"\besquec[ea]\s+(as\s+)?(instrucoes|regras)\b",
    r"\bvoce (agora )?e um\b",
    r"\byou are now\b",
    r"\bsystem prompt\b",
    r"\bprompt do sistema\b",
    r"\bdesconsider[ea]\b",
    r"\bacting as\b",
    r"\baja como\b",
    r"\bmande (o|um) desconto\b",
    r"\bd[iy]ga que (o )?preco\b",
    r"\baprov[ea] (a )?(politica|as regras)\b",
    r"\bexecute (o )?(codigo|comando)\b",
    r"\boverride\b",
    r"\bjailbreak\b",
    r"\[\s*system\s*\]",
    r"<\s*system\s*>",
    r"\bassistant\s*:",
)


@dataclass
class FlagHit:
    flag: str
    severity: str
    human_label: str
    matched: list[str] = field(default_factory=list)
    pause_automation: bool = True

    def to_dict(self) -> dict:
        return {
            "flag": self.flag,
            "severity": self.severity,
            "rotulo": self.human_label,
            "casou": self.matched,
            "pausa_automacao": self.pause_automation,
        }


def detect_a0(text: str) -> list[FlagHit]:
    """Gatilhos A0 presentes no texto.

    Regra dos dois níveis: um padrão DECISIVO casa sozinho. Um CORROBORATIVO só
    conta acompanhado — com outro corroborativo ou com um decisivo. Ver A0Rule.
    """
    normalized = normalize(text)
    hits: list[FlagHit] = []
    for rule in A0_RULES:
        matched = [p for p in rule.patterns if re.search(p, normalized)]
        ambiguos = [p for p in rule.corroborativos if re.search(p, normalized)]
        if not matched and len(ambiguos) < 2:
            continue
        hits.append(
            FlagHit(
                flag=rule.flag,
                severity=rule.severity,
                human_label=rule.human_label,
                matched=matched + ambiguos,
                pause_automation=rule.pause_automation,
            )
        )
    hits.sort(key=lambda h: _SEVERITY_ORDER.get(h.severity, 99))
    return hits


def detect_opt_out(text: str) -> bool:
    normalized = normalize(text)
    return any(re.search(p, normalized) for p in OPT_OUT_PATTERNS)


def detect_injection(text: str) -> list[str]:
    """Trechos com cara de tentativa de manipular instruções. NÃO bloqueia —
    marca o conteúdo como dado, para o agente tratar como texto de cliente."""
    normalized = normalize(text)
    return [p for p in INJECTION_PATTERNS if re.search(p, normalized)]


# ---------------------------------------------------------------------------
# Sanitização
# ---------------------------------------------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(text: Optional[str]) -> tuple[str, list[str]]:
    """Limpa o texto de entrada. Devolve (texto, avisos)."""
    avisos: list[str] = []
    if not text:
        return "", avisos
    cleaned = _CONTROL_RE.sub("", str(text))
    if len(cleaned) > MAX_TEXT_CHARS:
        cleaned = cleaned[:MAX_TEXT_CHARS]
        avisos.append(f"texto_truncado_em_{MAX_TEXT_CHARS}")
    if cleaned != str(text):
        avisos.append("caracteres_de_controle_removidos")
    return cleaned.strip(), avisos


# ---------------------------------------------------------------------------
# Estado (SQLite). O destino do MVP é Postgres (docs/01 §5); esta é a
# implementação local que funciona sem a infra do cliente.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound (
    igsid       TEXT PRIMARY KEY,
    last_seen   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS private_replies (
    comment_id  TEXT PRIMARY KEY,
    sent_at     REAL NOT NULL
);

-- Efeito externo com resultado DESCONHECIDO (parecer OpenClaw, F05).
--
-- Existe porque "falhou" e "não sei se saiu" são estados diferentes. Um timeout
-- depois do envio não prova que a mensagem saiu nem que não saiu — e as duas
-- suposições erradas custam caro: repetir duplica a resposta ao cliente; não
-- repetir deixa o cliente sem resposta. Sem esta tabela, a única saída era
-- escolher uma das duas no escuro.
CREATE TABLE IF NOT EXISTS reconciliacao (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id    TEXT,
    igsid         TEXT,
    acao          TEXT,
    canal         TEXT,
    estado        TEXT NOT NULL,   -- incerto | enviado | nao_enviado
    detalhe       TEXT,
    criado_em     REAL NOT NULL,
    resolvido_em  REAL,
    resolvido_por TEXT
);
CREATE TABLE IF NOT EXISTS opt_outs (
    igsid       TEXT PRIMARY KEY,
    at          REAL NOT NULL,
    motivo      TEXT,
    despedida_enviada INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS processed (
    event_id    TEXT PRIMARY KEY,
    at          REAL NOT NULL,
    kind        TEXT
);
CREATE TABLE IF NOT EXISTS interactions (
    interaction_id      TEXT PRIMARY KEY,
    igsid               TEXT,
    platform            TEXT DEFAULT 'instagram',
    channel             TEXT,
    media_id            TEXT,
    comment_id          TEXT,
    message_id          TEXT,
    received_at         REAL NOT NULL,
    category            TEXT,
    subcategory         TEXT,
    risk                TEXT,
    classification_status TEXT,
    recommended_action  TEXT,
    approved_action     TEXT,
    executed_action     TEXT,
    status              TEXT DEFAULT 'recebida',
    lead_stage          TEXT,
    product_interest    TEXT,
    attribution_method  TEXT,
    source_document_ids TEXT,
    rules_version       TEXT,
    rag_version         TEXT,
    updated_at          REAL
);
CREATE TABLE IF NOT EXISTS followups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    igsid           TEXT NOT NULL,
    tipo            TEXT,
    due_at          REAL,
    enviado_em      REAL,
    canal           TEXT DEFAULT 'instagram',
    mensagem        TEXT,
    gerado_por      TEXT,
    aprovado_por    TEXT,
    resposta_em     REAL
);
CREATE TABLE IF NOT EXISTS lacunas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pergunta    TEXT NOT NULL,
    igsid       TEXT,
    at          REAL NOT NULL,
    origem      TEXT
);
CREATE TABLE IF NOT EXISTS lead_stage (
    igsid       TEXT PRIMARY KEY,
    stage       TEXT NOT NULL,
    score       INTEGER DEFAULT 0,
    updated_at  REAL
);

-- =====================================================================
-- REGRAS DE NEGÓCIO (REGRAS-DE-NEGOCIO.md) — estado das RN-*
-- =====================================================================

-- RN-002/003/004/... — flag ativa por lead. É o que permite barrar envio
-- comercial DEPOIS do LLM, quando o modelo já decidiu (ou foi convencido) a
-- mandar preço/link para alguém em crise. Esquecer a flag é fácil; consultar
-- uma tabela no caminho do envio, não.
CREATE TABLE IF NOT EXISTS lead_flags (
    igsid       TEXT NOT NULL,
    flag        TEXT NOT NULL,
    severity    TEXT,
    at          REAL NOT NULL,
    ativo       INTEGER DEFAULT 1,
    PRIMARY KEY (igsid, flag)
);

-- RN-007 — todo toque PROATIVO (follow-up, reativação, aviso) fica registrado.
-- Sem este registro a cota global é um número no prompt, não uma regra.
CREATE TABLE IF NOT EXISTS toques_proativos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    igsid       TEXT NOT NULL,
    tipo        TEXT,
    canal       TEXT DEFAULT 'instagram',
    at          REAL NOT NULL
);

-- RN-009 — aprovação humana para ação de nível A1. A1 sem aprovação viva é
-- bloqueada; é o que impede "só desta vez" virar autônomo.
CREATE TABLE IF NOT EXISTS aprovacoes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    acao        TEXT NOT NULL,
    igsid       TEXT DEFAULT '',
    aprovado_por TEXT,
    justificativa TEXT,
    at          REAL NOT NULL,
    expira_em   REAL,
    usado_em    REAL
);

-- RN-012 — fila humana com SLA. A v2 do PDF dizia "prazos ainda precisam ser
-- definidos"; sem prazo registrado não existe atraso a detectar.
CREATE TABLE IF NOT EXISTS fila_humana (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    igsid       TEXT,
    prioridade  TEXT,
    motivo      TEXT,
    resumo      TEXT,
    aberto_em   REAL NOT NULL,
    prazo_sla   REAL,
    assumido_por TEXT,
    assumido_em REAL,
    resolvido_em REAL,
    resolucao   TEXT,
    alertas     INTEGER DEFAULT 0
);

-- RN-013 — livro-razão da moderação destrutiva. Sem autorização registrada,
-- ocultar/excluir não acontece; e toda ação tem reversão rastreável.
CREATE TABLE IF NOT EXISTS moderacao_aprovacoes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id  TEXT NOT NULL,
    acao        TEXT NOT NULL,
    autorizado_por TEXT,
    justificativa TEXT,
    at          REAL NOT NULL,
    executado_em REAL,
    revertido_em REAL,
    revertido_por TEXT
);

-- =====================================================================
-- PROTEÇÃO DE DADOS (RN-014..RN-018) — LGPD
-- =====================================================================

-- Mantém o relógio da última manutenção. Sem isto, o expurgo rodaria a cada
-- evento (caro) ou nunca rodaria (pior): prazo que ninguém executa é enfeite.
CREATE TABLE IF NOT EXISTS manutencao (
    chave       TEXT PRIMARY KEY,
    valor       REAL,
    atualizado_em REAL
);

-- Bloqueio permanente SEM identidade: guarda só o hash do igsid. É o que permite
-- honrar "nunca mais me contate" depois de o titular exercer o direito de
-- exclusão, sem continuar guardando quem ele é. Sem isto, o expurgo apagaria a
-- recusa junto com o dado — e a próxima campanha voltaria a falar com quem pediu
-- para ser esquecido, que é a violação mais grave possível aqui.
CREATE TABLE IF NOT EXISTS bloqueios_permanentes (
    igsid_hash  TEXT PRIMARY KEY,
    at          REAL NOT NULL,
    motivo      TEXT
);

-- Prova de atendimento ao direito do titular, sem dado pessoal: só o hash, a data
-- e as contagens do que foi apagado. Guardar o igsid aqui anularia o expurgo.
CREATE TABLE IF NOT EXISTS exclusoes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    igsid_hash  TEXT NOT NULL,
    at          REAL NOT NULL,
    motivo      TEXT,
    contagens   TEXT
);
"""


def _migrar(conn: sqlite3.Connection) -> None:
    """Adiciona colunas que faltam em bancos já existentes.

    `CREATE TABLE IF NOT EXISTS` NÃO altera tabela existente: sem isto, um banco
    criado na versão anterior continua sem a coluna e as consultas quebram.
    """
    esperadas = {"opt_outs": {"despedida_enviada": "INTEGER DEFAULT 0"}}
    for tabela, colunas in esperadas.items():
        existentes = {row["name"] for row in conn.execute(f"PRAGMA table_info({tabela})")}
        for nome, tipo in colunas.items():
            if nome not in existentes:
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")


@contextmanager
def db(path: Optional[Path] = None):
    """Conexão com o estado, sempre FECHADA ao sair.

    Não use `with sqlite3.connect(...)`: o context manager do sqlite faz
    commit/rollback mas NÃO fecha. A conexão vazada trava o arquivo — no Windows
    isso impede até apagar o diretório.
    """
    target = Path(path or state_db_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _migrar(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- janela de 24h ----------

def record_inbound(igsid: str, ts: Optional[float] = None) -> None:
    """Registra que o usuário falou com a conta. É o que ABRE a janela de 24h."""
    ts = ts if ts is not None else time.time()
    with db() as conn:
        conn.execute(
            "INSERT INTO inbound (igsid, last_seen) VALUES (?, ?) "
            "ON CONFLICT(igsid) DO UPDATE SET last_seen = excluded.last_seen",
            (igsid, ts),
        )


def window_remaining(igsid: str, now: Optional[float] = None) -> float:
    """Segundos restantes da janela de 24h. Negativo = expirada."""
    with db() as conn:
        row = conn.execute(
            "SELECT last_seen FROM inbound WHERE igsid = ?", (igsid,)
        ).fetchone()
    if not row:
        return -1.0
    now = now if now is not None else time.time()
    return (row["last_seen"] + DM_WINDOW_SECONDS) - now


def window_is_open(igsid: str, now: Optional[float] = None) -> bool:
    return window_remaining(igsid, now) > 0


# ---------- opt-out ----------

def is_opted_out(igsid: str) -> bool:
    """Recusa registrada — inclui o bloqueio permanente por hash (RN-017).

    O bloqueio por hash é o que sobra depois de um pedido de exclusão: o igsid já
    não está mais guardado, mas a recusa continua honrada. Consultar só a tabela
    `opt_outs` aqui reintroduziria o contato com quem pediu para ser esquecido —
    exatamente o que o expurgo deveria tornar impossível.
    """
    if not igsid:
        return False
    with db() as conn:
        if conn.execute("SELECT 1 FROM opt_outs WHERE igsid = ?", (igsid,)).fetchone():
            return True
    return bloqueio_permanente_ativo(igsid)


def add_opt_out(igsid: str, motivo: str = "") -> None:
    """Opt-out é imediato e PERMANENTE (SOUL.md linha 5). Não existe desfazer."""
    with db() as conn:
        conn.execute(
            "INSERT INTO opt_outs (igsid, at, motivo) VALUES (?, ?, ?) "
            "ON CONFLICT(igsid) DO UPDATE SET at = excluded.at, motivo = excluded.motivo",
            (igsid, time.time(), motivo),
        )


def pode_enviar_despedida(igsid: str) -> bool:
    """Autoriza UMA única mensagem depois do pedido de parada.

    A política pede despedida em uma linha e silêncio permanente depois. Se o
    gate bloqueasse todo envio, nem a despedida sairia; se não limitasse, o
    opt-out não valeria nada. Então: exatamente uma, e nunca mais.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT despedida_enviada FROM opt_outs WHERE igsid = ?", (igsid,)
        ).fetchone()
    return bool(row) and not row["despedida_enviada"]


def marcar_despedida_enviada(igsid: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE opt_outs SET despedida_enviada = 1 WHERE igsid = ?", (igsid,)
        )


# ---------- dedupe ----------

def event_id_for(event: dict) -> str:
    """ID estável do evento. A Meta REENTREGA webhooks — sem isso, comentário
    repetido vira resposta repetida."""
    for key in ("event_id", "id", "message_id", "comment_id"):
        value = event.get(key)
        if value:
            return f"{key}:{value}"
    # Fallback: hash do conteúdo relevante. Estável entre reentregas do mesmo evento.
    basis = json.dumps(
        {
            k: event.get(k)
            for k in ("igsid", "comment_id", "message_id", "media_id", "text", "timestamp")
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return "hash:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def is_duplicate(event_id: str) -> bool:
    with db() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM processed WHERE event_id = ?", (event_id,)
            ).fetchone()
            is not None
        )


def mark_processed(event_id: str, kind: str = "") -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed (event_id, at, kind) VALUES (?, ?, ?)",
            (event_id, time.time(), kind),
        )


# ---------- cota de private reply ----------

def private_reply_age(comment_id: str) -> Optional[float]:
    with db() as conn:
        row = conn.execute(
            "SELECT sent_at FROM private_replies WHERE comment_id = ?", (comment_id,)
        ).fetchone()
    return None if not row else time.time() - row["sent_at"]


def mark_private_reply_sent(comment_id: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO private_replies (comment_id, sent_at) VALUES (?, ?)",
            (comment_id, time.time()),
        )


# ---------- lacunas de conhecimento (PDF §20) ----------

def register_gap(pergunta: str, igsid: str = "", origem: str = "") -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO lacunas (pergunta, igsid, at, origem) VALUES (?, ?, ?, ?)",
            (pergunta[:1000], igsid, time.time(), origem),
        )


# ---------------------------------------------------------------------------
# Pré-condições de follow-up (prompts/follow-up.md + docs/05)
#
# O prompt original é explícito: "Isso é código determinístico. Nunca deixe o
# modelo decidir se deve ou não mandar follow-up."
# ---------------------------------------------------------------------------

@dataclass
class PreconditionResult:
    ok: bool
    motivo: str
    detalhe: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "motivo": self.motivo, "detalhe": self.detalhe}


def in_quiet_hours(now: Optional[datetime] = None) -> bool:
    """Quiet hours: 21h–8h BRT. Fora dessa faixa não se manda follow-up."""
    moment = now or now_brt()
    return moment.hour >= FOLLOWUP_QUIET_START or moment.hour < FOLLOWUP_QUIET_END


def followup_preconditions(
    lead: dict,
    *,
    now: Optional[float] = None,
    lead_responded_since_last_touch: bool = False,
    bought_since_schedule: bool = False,
    human_takeover: bool = False,
    ticket_exige_humano: bool = False,
) -> PreconditionResult:
    """Checa TODAS as pré-condições. Qualquer uma verdadeira cancela o toque.

    A ordem importa para o motivo reportado: as causas mais graves e permanentes
    primeiro, as operacionais depois.
    """
    igsid = lead.get("igsid") or ""
    stage = lead.get("estagio") or lead.get("stage") or ""

    if lead.get("alerta_sensivel"):
        return PreconditionResult(False, "alerta_sensivel", "Conversa marcada como sensível.")
    if lead.get("opt_out") or (igsid and is_opted_out(igsid)):
        return PreconditionResult(False, "opt_out", "Opt-out é permanente. Nunca mais contatar.")
    if stage == "cliente" and not lead.get("permite_upsell"):
        return PreconditionResult(False, "ja_e_cliente", "Cliente: só onboarding/checagem, não cadência de venda.")
    if bought_since_schedule:
        return PreconditionResult(False, "comprou_desde_o_agendamento", "Objetivo do follow-up já foi alcançado.")
    if human_takeover:
        return PreconditionResult(False, "takeover_humano", "Humano no comando. O agente não concorre.")
    if lead_responded_since_last_touch:
        return PreconditionResult(False, "lead_respondeu", "Ele voltou sozinho. A cadência se reinicia, o toque antigo não sai.")
    if ticket_exige_humano:
        return PreconditionResult(False, "acima_da_alcada", "Ticket acima da alçada definida.")

    limite = FOLLOWUP_MAX_TOQUES.get(stage)
    toques = int(lead.get("toques_enviados") or 0)
    if limite is not None and toques >= limite:
        return PreconditionResult(
            False, "limite_de_toques", f"Já são {toques} toques no estágio '{stage}' (limite {limite})."
        )

    if in_quiet_hours(to_brt(now) if now else None):
        return PreconditionResult(False, "quiet_hours", "Fora da janela 8h–21h BRT.")

    canal = (lead.get("canal") or "instagram").lower()
    if canal == "instagram" and igsid and not window_is_open(igsid, now):
        return PreconditionResult(
            False,
            "fora_da_janela_24h",
            "Instagram não permite envio fora da janela de 24h. Usar WhatsApp/e-mail com consentimento.",
        )

    return PreconditionResult(True, "ok")


# ---------------------------------------------------------------------------
# Decisão de intake — a entrada única
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    permitir_agente: bool
    acao: str
    severity: Optional[str]
    flags: list[FlagHit]
    motivo: str
    detalhe: str = ""
    opt_out: bool = False
    pausar_automacao: bool = False
    text: str = ""
    avisos: list[str] = field(default_factory=list)
    injecao: list[str] = field(default_factory=list)
    rules_version: str = RULES_VERSION
    event_id: str = ""
    interaction_id: str = ""

    def to_dict(self) -> dict:
        return {
            "permitir_agente": self.permitir_agente,
            "acao": self.acao,
            "severidade": self.severity,
            "flags": [f.to_dict() for f in self.flags],
            "motivo": self.motivo,
            "detalhe": self.detalhe,
            "opt_out": self.opt_out,
            "pausar_automacao": self.pausar_automacao,
            "avisos": self.avisos,
            "injecao_detectada": self.injecao,
            "rules_version": self.rules_version,
            "event_id": self.event_id,
            "interaction_id": self.interaction_id,
        }


def evaluate_intake(event: dict, *, agora: Optional[float] = None) -> Decision:
    """Decide o que fazer com UM evento, antes de gastar LLM.

    Ordem das verificações (a mais irreversível primeiro):
      1. duplicata       -> descarta
      2. opt-out         -> encerra e para de contatar
      3. A0              -> escalada, automação pausada
      4. janela          -> abre/renova a janela de 24h
      5. normal          -> segue para o agente
    """
    text, avisos = sanitize_text(event.get("text") or event.get("message") or "")
    igsid = str(event.get("igsid") or event.get("sender_id") or "")
    comment_id = str(event.get("comment_id") or "")
    eid = event_id_for({**event, "text": text})

    injecao = detect_injection(text)
    if injecao:
        avisos.append("conteudo_com_cara_de_instrucao")

    # 1. duplicata (a Meta reentrega)
    if is_duplicate(eid):
        return Decision(
            permitir_agente=False,
            acao="descartar",
            severity=None,
            flags=[],
            motivo="evento_duplicado",
            detalhe="Mesmo event_id já processado. Não duplicar linha, lead, resposta nem moderação.",
            text=text,
            avisos=avisos,
            injecao=injecao,
            event_id=eid,
        )

    # 2. opt-out — imediato e permanente
    if igsid and is_opted_out(igsid):
        return Decision(
            permitir_agente=False,
            acao="opt_out_previo",
            severity=None,
            flags=[],
            motivo="opt_out_registrado",
            detalhe="Já pediu para não ser contatado. Nenhum envio. Nem despedida.",
            opt_out=True,
            text=text,
            avisos=avisos,
            injecao=injecao,
            event_id=eid,
        )

    if detect_opt_out(text):
        if igsid:
            add_opt_out(igsid, motivo="detectado_no_intake")
        return Decision(
            permitir_agente=True,
            acao="encerrar",
            severity=None,
            flags=[],
            motivo="pedido_de_parada",
            detalhe="Agradecer em 1 linha, registrar opt-out e encerrar. Cadência cancelada.",
            opt_out=True,
            text=text,
            avisos=avisos,
            injecao=injecao,
            event_id=eid,
        )

    # 3. A0 — escalada. Falso positivo é barato; falso negativo não.
    #    Mas só P0/P1 consomem humano; P2 (hostilidade) é rótulo e segue respondendo.
    hits = detect_a0(text)
    escalaveis = [h for h in hits if h.severity in _SEVERIDADES_ESCALAVEIS]
    if escalaveis:
        worst = escalaveis[0]
        if igsid:
            register_gap(
                f"A0 {worst.flag}", igsid=igsid, origem=f"intake rules {RULES_VERSION}"
            )
        return Decision(
            permitir_agente=True,
            acao="escalar",
            severity=worst.severity,
            flags=hits,
            motivo=worst.flag,
            detalhe=worst.human_label,
            pausar_automacao=any(h.pause_automation for h in hits),
            text=text,
            avisos=avisos,
            injecao=injecao,
            event_id=eid,
        )

    # 4. janela de 24h — mensagem recebida ABRE a janela
    if igsid:
        record_inbound(igsid, agora)

    # 5. normal. Se sobrou algum P2 (hostilidade), ele viaja como rótulo: o agente
    #    responde, mas com a instrução de não rebater.
    pior = hits[0] if hits else None
    return Decision(
        permitir_agente=True,
        acao="responder_com_cautela" if pior else "responder",
        severity=pior.severity if pior else None,
        flags=hits,
        motivo=pior.flag if pior else "ok",
        detalhe=pior.human_label if pior else "",
        text=text,
        avisos=avisos,
        injecao=injecao,
        event_id=eid,
    )


def record_interaction(
    *,
    interaction_id: str,
    igsid: str = "",
    channel: str = "",
    media_id: str = "",
    comment_id: str = "",
    message_id: str = "",
    category: str = "",
    subcategory: str = "",
    risk: str = "",
    recommended_action: str = "",
    lead_stage: str = "",
    product_interest: str = "",
    attribution_method: str = "",
    status: str = "recebida",
) -> None:
    """Grava a interação UMA vez (PDF §11).

    `recommended_action` é o que o agente propôs. `approved_action` e
    `executed_action` ficam nulos até existirem — ação sugerida NÃO é ação
    executada (PDF §13).
    """
    with db() as conn:
        conn.execute(
            """
            INSERT INTO interactions (
                interaction_id, igsid, platform, channel, media_id, comment_id, message_id,
                received_at, category, subcategory, risk, classification_status,
                recommended_action, status, lead_stage, product_interest,
                attribution_method, rules_version, updated_at
            ) VALUES (?, ?, 'instagram', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(interaction_id) DO NOTHING
            """,
            (
                interaction_id, igsid, channel, media_id, comment_id, message_id,
                time.time(), category, subcategory, risk,
                "provisoria" if not category else "definitiva",
                recommended_action, status, lead_stage, product_interest,
                attribution_method or "nao_atribuivel", RULES_VERSION, time.time(),
            ),
        )


def set_executed_action(interaction_id: str, executed: str, status: str) -> None:
    """Só deve ser chamada DEPOIS de um envio confirmado pela plataforma."""
    with db() as conn:
        conn.execute(
            "UPDATE interactions SET executed_action = ?, status = ?, updated_at = ? "
            "WHERE interaction_id = ?",
            (executed, status, time.time(), interaction_id),
        )


def mark_executed(
    *,
    interaction_id: str = "",
    comment_id: str = "",
    message_id: str = "",
    igsid: str = "",
    executed: str = "",
    status: str = "enviada",
) -> int:
    """Marca como EXECUTADA a interação que corresponde ao envio confirmado.

    Existe para `executed_action` não ficar eternamente nulo (PDF §13: sugerir,
    aprovar e executar são coisas diferentes). Casa pelo identificador mais
    específico disponível, do mais preciso ao mais vago. Devolve quantas linhas
    foram marcadas — zero é informação: significa que o envio não tem interação
    registrada, e isso vale aparecer no log.
    """
    if interaction_id:
        where, args = "interaction_id = ?", (interaction_id,)
    elif comment_id:
        where, args = "comment_id = ? AND executed_action IS NULL", (comment_id,)
    elif message_id:
        where, args = "message_id = ? AND executed_action IS NULL", (message_id,)
    elif igsid:
        where, args = (
            "igsid = ? AND executed_action IS NULL AND status != 'descartada'",
            (igsid,),
        )
    else:
        return 0
    with db() as conn:
        cur = conn.execute(
            f"UPDATE interactions SET executed_action = ?, status = ?, updated_at = ? "
            f"WHERE {where}",
            (executed, status, time.time(), *args),
        )
        return cur.rowcount


def stats(path: Optional[Path] = None) -> dict[str, int]:
    """Contagens para o painel. Denominadores explícitos (PDF §22)."""
    with db(path) as conn:
        def count(table: str) -> int:
            return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])

        return {
            "processados": count("processed"),
            "interacoes": count("interactions"),
            "opt_outs": count("opt_outs"),
            "followups": count("followups"),
            "lacunas": count("lacunas"),
        }


# ===========================================================================
# REGRAS DE NEGÓCIO — implementação das RN-* de REGRAS-DE-NEGOCIO.md
#
# Este bloco é a versão EXECUTÁVEL da seção de regras de negócio. Cada bloqueio
# carrega o identificador `RN-nnn` da regra que o produziu, porque a pergunta que
# se faz depois de um incidente não é "por que bloqueou?" — é "qual regra
# aprovada autorizava (ou impedia) isto?".
#
# Divisão de responsabilidade:
#   RN-001 vive em `_check_kill_switch()` + hook `pre_tool_call` (instagram_api.py),
#          porque depende do caminho do arquivo de parada.
#   RN-002..RN-013 vivem AQUI e são compostas por `avaliar_envio()`, chamada no
#          caminho do envio. Nenhuma delas depende do modelo lembrar de nada.
# ===========================================================================

RN_PARADA_EMERGENCIA = "RN-001"
RN_MENOR_IDADE = "RN-002"
RN_CRISE_EMOCIONAL = "RN-003"
RN_DADOS_DE_TERCEIRO = "RN-004"
RN_OPT_OUT = "RN-005"
RN_JANELA_HORARIO = "RN-006"
RN_TETO_FREQUENCIA = "RN-007"
RN_DIVULGACAO_AUTOMACAO = "RN-008"
RN_MATRIZ_AUTONOMIA = "RN-009"
RN_ALEGACOES_PROIBIDAS = "RN-010"
RN_CONSUMIDOR = "RN-011"
RN_ESPERA_HUMANA = "RN-012"
RN_MODERACAO_DESTRUTIVA = "RN-013"

SEVERIDADE_POR_FLAG = {
    "crise_emocional": "P0",
    "saude_mental": "P0",
    "menor_idade": "P0",
    "juridico": "P0",
    "desespero_financeiro": "P0",
    "dados_de_terceiro": "P0",
    "reclamacao": "P1",
    "pedido_desconto": "P1",
    "pedido_humano": "P1",
    "hostilidade": "P2",
}

# Flag ativa que PROÍBE conteúdo comercial (preço, link, oferta) para aquele
# lead — não importa o que o modelo decidiu depois. É a implementação em código
# da linha vermelha "crise emocional nunca vira oportunidade de venda" e da regra
# da v2 §17: "o agente não continua vendendo durante uma reclamação não resolvida".
FLAGS_QUE_BLOQUEIAM_COMERCIAL = {
    "crise_emocional": RN_CRISE_EMOCIONAL,
    "saude_mental": RN_CRISE_EMOCIONAL,
    "desespero_financeiro": RN_CRISE_EMOCIONAL,
    "menor_idade": RN_MENOR_IDADE,
    "dados_de_terceiro": RN_DADOS_DE_TERCEIRO,
    "juridico": RN_ESPERA_HUMANA,
    "reclamacao": RN_ESPERA_HUMANA,
}

# ---------------------------------------------------------------------------
# RN-002 / RN-003 / RN-004 — flag ativa por lead
# ---------------------------------------------------------------------------

def registrar_flag(igsid: str, flag: str, severity: str = "") -> None:
    """Grava a flag ativa de um lead. É o que sobrevive ao próximo evento.

    A0 já escala a conversa no intake; o que faltava era a flag sobreviver para
    BLOQUEAR o envio comercial depois, quando o modelo — ou uma tentativa de
    manipulação — decidisse mandar preço para quem está em crise.
    """
    if not igsid or not flag:
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO lead_flags (igsid, flag, severity, at, ativo) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(igsid, flag) DO UPDATE SET severity = excluded.severity, "
            "at = excluded.at, ativo = 1",
            (igsid, flag, severity or SEVERIDADE_POR_FLAG.get(flag, ""), time.time()),
        )


def limpar_flag(igsid: str, flag: str = "") -> None:
    """Desativa flag(s). Usado quando o humano resolve o caso (RN-012)."""
    with db() as conn:
        if flag:
            conn.execute(
                "UPDATE lead_flags SET ativo = 0 WHERE igsid = ? AND flag = ?", (igsid, flag)
            )
        else:
            conn.execute("UPDATE lead_flags SET ativo = 0 WHERE igsid = ?", (igsid,))


def flags_ativas(igsid: str) -> dict[str, str]:
    """{flag: severidade} das flags ativas do lead."""
    if not igsid:
        return {}
    with db() as conn:
        linhas = conn.execute(
            "SELECT flag, severity FROM lead_flags WHERE igsid = ? AND ativo = 1", (igsid,)
        ).fetchall()
    return {row["flag"]: (row["severity"] or "") for row in linhas}


# ---------------------------------------------------------------------------
# Detecção de conteúdo comercial e de link no TEXTO DE SAÍDA (RN-002/003/004)
# ---------------------------------------------------------------------------

_URL_RE = re.compile(
    r"(https?://|www\.|\b[a-z0-9][a-z0-9-]*\.(com|com\.br|net|org|link|shop|store|app|io|me)\b)",
    re.IGNORECASE,
)
_VALOR_RE = re.compile(
    r"(r\$\s*\d|\b\d{2,}\s*reais\b|\br\$\s*\d+[,.]?\d*|\bpor apenas\b|\bparcelamos\b)",
    re.IGNORECASE,
)


def contem_link(texto: str) -> bool:
    return bool(_URL_RE.search(texto or ""))


def contem_valor(texto: str) -> bool:
    """Preço em texto. 'R$ 97', '97 reais', 'por apenas'.

    Deliberadamente conservador: número solto NÃO conta como preço, senão
    "3 pilares" ou "segunda às 19h" bloqueariam conversa legítima.
    """
    return bool(_VALOR_RE.search(texto or ""))


# ---------------------------------------------------------------------------
# RN-010 — alegações proibidas (guardrail de SAÍDA)
#
# docs/05 §4: "A lista de alegações proibidas precisa estar em dois lugares: no
# prompt (prevenção) e no guardrail de saída (bloqueio). Prevenção falha;
# bloqueio não pode falhar."
#
# Padrões aplicados ao texto NORMALIZADO (sem acento, minúsculo).
# Conservadores de propósito: o custo do falso positivo aqui é uma venda perdida
# por bloqueio indevido, e o custo do falso negativo é uma reclamação no Procon.
# ---------------------------------------------------------------------------

ALEGACOES_PROIBIDAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "promessa_de_resultado",
        (
            r"\bvai faturar\b",
            r"\bvai ficar rico\b",
            r"\bresultado garantido\b",
            r"\bgarantido que voce\b",
            r"\bfunciona (100|cem por cento)\b",
            r"\b100 por cento garantido\b",
            r"\bcerteza absoluta\b",
            r"\bvoce vai ganhar\b",
            r"\bvai dar certo com certeza\b",
            r"\bgaranto o resultado\b",
        ),
    ),
    (
        "promessa_de_prazo",
        (
            r"\bresultado em \d+\b",
            r"\bem \d+ dias (voce|vai|sua)\b",
            r"\bdentro de \d+ (dias|semanas|meses)\b.{0,25}\b(vai|resultado|funciona|muda)\b",
            r"\bem \d+ (dias|semanas) ja (esta|estara|vai)\b",
        ),
    ),
    (
        "promessa_de_renda",
        (
            r"\brenda garantida\b",
            r"\bganhar dinheiro facil\b",
            r"\bdinheiro facil\b",
            r"\bfaturar \d+",
            r"\bganhar \d+ mil\b",
            r"\bviver de renda\b",
        ),
    ),
    (
        "escassez_falsa",
        (
            r"\bultimas vagas\b",
            r"\bultimas unidades\b",
            r"\bso restam \d+\b",
            r"\bacaba (hoje|essa semana)\b",
            r"\bencerra (hoje|amanha)\b",
            r"\bultima chance\b",
        ),
    ),
    (
        "urgencia_fabricada",
        (
            r"\bso hoje\b",
            r"\bsomente hoje\b",
            r"\bcorre que\b",
            r"\bultimas horas\b",
            r"\bpor tempo limitadissimo\b",
        ),
    ),
    (
        "linguagem_de_cura_ou_saude",
        (
            r"\bcura (a |o |sua |seu )?(depressao|ansiedade|doenca|transtorno|panico)\b",
            r"\btrata (a |o |sua |seu )?(depressao|ansiedade|doenca|transtorno)\b",
            r"\bsubstitui (o )?(remedio|medicacao|terapia)\b",
            r"\bresolve sua (ansiedade|depressao)\b",
        ),
    ),
    (
        "garantia_de_transformacao",
        (
            r"\bvai mudar sua vida\b",
            r"\btransforma(r)? sua vida\b",
            r"\bsua vida muda em \d+\b",
            r"\bmuda sua vida em \d+\b",
        ),
    ),
)

# Frases que o cliente pode liberar explicitamente (escassez/urgência reais e
# aprovadas). Sem aprovação viva na tabela, continuam bloqueadas: "é verdade
# mesmo" não é verificável em tempo de envio, e o custo do erro é assimétrico.
_ALEGACOES_LIBERAVEIS = {"escassez_falsa", "urgencia_fabricada"}


def detect_alegacao_proibida(texto: str) -> list[str]:
    """Categorias de alegação proibida presentes no texto de saída."""
    normalizado = normalize(texto)
    achados: list[str] = []
    for categoria, padroes in ALEGACOES_PROIBIDAS:
        if any(re.search(p, normalizado) for p in padroes):
            achados.append(categoria)
    return achados


# ---------------------------------------------------------------------------
# RN-011 — prazo, garantia e devolução só com fonte aprovada
#
# CDC art. 49 dá 7 dias de arrependimento — esse número é lei, não promessa.
# Qualquer OUTRO prazo, garantia ou condição é política comercial do cliente e
# precisa existir em IG_POLITICA_CONSUMIDOR (.env). Inventar é publicidade
# enganosa, e o agente inventa com boa intenção.
# ---------------------------------------------------------------------------

PADROES_CONSUMIDOR: tuple[str, ...] = (
    r"\bgarantia de \d+\s*(dias|meses|anos)\b",
    r"\b\d+\s*dias de garantia\b",
    r"\b\d+\s*dias para (devolver|trocar|reembolsar)\b",
    r"\b(devolucao|reembolso|troca) (em|no prazo de) \d+\b",
    r"\bgarantia incondicional\b",
    r"\bfrete gratis\b",
    r"\bdevolucao gratis\b",
)


def detect_afirmacao_consumidor_sem_fonte(texto: str) -> list[str]:
    """Alegações de garantia/devolução/frete que exigem política aprovada."""
    normalizado = normalize(texto)
    politica = normalize(os.getenv("IG_POLITICA_CONSUMIDOR", ""))
    achados: list[str] = []
    for padrao in PADROES_CONSUMIDOR:
        for m in re.finditer(padrao, normalizado):
            trecho = m.group(0)
            if politica and trecho in politica:
                continue  # está escrito na política aprovada: pode afirmar
            achados.append(trecho)
    return achados


# ---------------------------------------------------------------------------
# RN-008 — divulgação de automação (MECANISMO, desligado por padrão)
#
# Conflito real e ainda não resolvido pelo cliente:
#   SOUL.md diz "nunca digo que sou uma inteligência artificial";
#   docs/05 §2.2 regra 5 diz "não simular ser humano" (política da Meta) e o CDC
#   exige identificação de comunicação comercial automatizada.
#
# Não cabe a este código decidir a persona do cliente. Então: o mecanismo existe
# e é ligável por .env. Enquanto IG_DIVULGAR_AUTOMACAO não estiver ligada, o
# comportamento é o atual — e a RN-008 fica registrada como ABERTA.
# ---------------------------------------------------------------------------

TEXTO_DIVULGACAO = (
    "Sou o assistente automático do time do Edson — se preferir, chamo uma pessoa "
    "do time pra continuar com você 👊"
)

# Três modos, porque "divulgar ou não" tem uma resposta do meio — e foi a escolhida:
#
#   nunca        -> a persona não se anuncia em hipótese alguma
#   sob_pergunta -> diz a verdade quando perguntam, sem se anunciar sozinho
#   sempre       -> anuncia na primeira interação relevante de toda conversa
#
# O padrão é `sob_pergunta`: não mente para quem pergunta, e não quebra a persona no
# meio do funil. Modo `nunca` é uma decisão de risco, não de conforto — está
# registrado como Pendência 1 no REGRAS-DE-NEGOCIO.md.
MODOS_DIVULGACAO = ("nunca", "sob_pergunta", "sempre")
DIVULGACAO_PADRAO = "sob_pergunta"

# Escrito SEM acento: o `normalize()` roda antes, então "você" chega como "voce".
PERGUNTAS_SOBRE_AUTOMACAO = (
    r"\bvoce e (um |uma )?(robo|bot|ia|inteligencia artificial)\b",
    r"\bisso (e|eh) (um |uma )?(robo|bot|automatico|automacao|ia)\b",
    r"\b(e|eh) (um |uma )?(robo|bot|humano|pessoa) (mesmo|de verdade|real)\b",
    r"\bvoce (e|eh) (humano|humana|real|de verdade|uma pessoa|pessoa)\b",
    r"\b(estou|to) falando com (um |uma )?(robo|bot|humano|pessoa|maquina)\b",
    r"\bfalo com (um |uma )?(robo|bot|humano|pessoa|maquina|atendente)\b",
    r"\b(e|eh|isso e) atendimento (automatico|humano|robotico)\b",
    r"\b(e|eh) (um |uma )?assistente virtual\b",
    r"\bquem (esta|ta) (me )?(respondendo|falando|atendendo)\b",
    r"\bnao (e|eh) (uma )?(pessoa|humano) de verdade\b",
    r"\bvoce (e|eh) (mesmo )?(um )?(robo|bot)\b",
)


def modo_divulgacao() -> str:
    """Modo configurado. Valor irreconhecível cai no PADRÃO, não em `nunca`.

    Errar de digitação na variável não pode silenciar a divulgação: isso seria
    decidir marca por acidente, e na direção mais arriscada.
    """
    bruto = (os.getenv("IG_DIVULGAR_AUTOMACAO", "") or "").strip().lower()
    if not bruto:
        return DIVULGACAO_PADRAO
    if bruto in {"1", "true", "sim", "on", "sempre"}:
        return "sempre"
    if bruto in {"0", "false", "nao", "off", "nunca"}:
        return "nunca"
    if bruto in MODOS_DIVULGACAO:
        return bruto
    return DIVULGACAO_PADRAO


def divulgacao_obrigatoria() -> bool:
    """Só o modo `sempre` anuncia proativamente. Ver `divulgar_agora`."""
    return modo_divulgacao() == "sempre"


def pessoa_perguntou_se_e_automacao(texto: str) -> bool:
    normalizado = normalize(texto or "")
    return any(re.search(p, normalizado) for p in PERGUNTAS_SOBRE_AUTOMACAO)


def divulgar_agora(texto: str = "") -> bool:
    """A divulgação é devida NESTA mensagem?

    Separar "anunciar sozinho" de "responder quando perguntam" é o ponto: o modo
    `sob_pergunta` só dispara com pergunta direta, e é ele que evita a mentira por
    omissão sem transformar todo primeiro contato num aviso de robô.
    """
    modo = modo_divulgacao()
    if modo == "sempre":
        return True
    if modo == "sob_pergunta":
        return pessoa_perguntou_se_e_automacao(texto)
    return False


def diretiva_divulgacao() -> str:
    """Instrução obrigatória para o agente quando a divulgação é devida (RN-008)."""
    return (
        f"DIVULGAÇÃO OBRIGATÓRIA (RN-008): a pessoa perguntou se é automação. "
        f"Responda a verdade, em uma linha, sem rodeio e sem se desculpar. "
        f"Use exatamente este sentido: \"{TEXTO_DIVULGACAO}\""
    )


# ---------------------------------------------------------------------------
# RN-009 — matriz de autonomia (docs/05 §5.2)
# ---------------------------------------------------------------------------

NIVEL_AUTONOMIA: dict[str, str] = {
    # A3 — o agente age e reporta
    "responder_elogio": "A3",
    "agradecer": "A3",
    "encerrar": "A3",
    "aplicar_opt_out": "A3",
    # A2 — o agente envia, humano audita
    "responder_duvida_rag": "A2",
    "private_reply_palavra_chave": "A2",
    "pergunta_diagnostico": "A2",
    "informar_preco": "A2",
    "enviar_link": "A2",
    "followup_janela": "A2",
    "captura_whatsapp": "A2",
    "quebrar_objecao": "A2",
    "onboarding_pos_compra": "A2",
    "reply_comment": "A2",
    # A1 — humano aprova antes
    "upsell": "A1",
    "lead_alto_valor": "A1",
    "pergunta_fora_da_base": "A1",
    "imprensa_parceria": "A1",
    "novo_produto": "A1",
    "mudanca_prompt_tom": "A1",
    # A0 — humano sempre. Não se automatiza em nenhuma fase de maturidade.
    "desconto": "A0",
    "negociacao_preco": "A0",
    "reclamacao": "A0",
    "reembolso": "A0",
    "juridico": "A0",
    "crise_emocional": "A0",
    "desespero_financeiro": "A0",
    "hostilidade": "A0",
    "menor_idade": "A0",
    "duvida_personalizada_sensivel": "A0",
    "conteudo_sensivel": "A0",
}

A0_NUNCA_AUTOMATICO = True


def nivel_da_acao(acao: str) -> str:
    """Nível de autonomia da ação. Ação desconhecida é tratada como A2.

    Decisão deliberada: bloquear por nome de ação desconhecido quebraria o
    agente a cada ação nova. O buraco fica fechado por outro lado — a flag ativa
    do lead (RN-002/003/004) barra o conteúdo comercial independentemente do
    nome que o modelo der à ação.
    """
    return NIVEL_AUTONOMIA.get(acao or "", "A2")


def registrar_aprovacao(
    acao: str, *, igsid: str = "", aprovado_por: str = "", justificativa: str = "",
    validade_minutos: int = 240,
) -> int:
    """Registra aprovação humana para uma ação A1 (ou libera escassez real)."""
    agora = time.time()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO aprovacoes (acao, igsid, aprovado_por, justificativa, at, expira_em) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (acao, igsid, aprovado_por, justificativa, agora,
             agora + validade_minutos * 60),
        )
        return int(cur.lastrowid)


def tem_aprovacao(acao: str, igsid: str = "", *, now: Optional[float] = None) -> bool:
    """Existe aprovação viva (não expirada) para esta ação?"""
    momento = now if now is not None else time.time()
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM aprovacoes WHERE acao = ? AND (igsid = ? OR igsid = '') "
            "AND (expira_em IS NULL OR expira_em > ?) LIMIT 1",
            (acao, igsid or "", momento),
        ).fetchone()
    return row is not None


def pode_executar(acao: str, igsid: str = "") -> tuple[bool, str, str]:
    """(pode, regra, motivo) para a ação declarada pelo agente."""
    if not acao:
        return True, "", ""
    nivel = nivel_da_acao(acao)
    if nivel == "A0":
        return (
            False,
            RN_MATRIZ_AUTONOMIA,
            f"'{acao}' é A0: humano sempre. A0 não se automatiza em nenhuma fase.",
        )
    if nivel == "A1" and not tem_aprovacao(acao, igsid):
        return (
            False,
            RN_MATRIZ_AUTONOMIA,
            f"'{acao}' é A1: precisa de aprovação humana registrada e vigente.",
        )
    return True, "", ""


# ---------------------------------------------------------------------------
# RN-005 — opt-out é da PESSOA, não do canal
#
# A falha clássica: a pessoa pede para parar no Direct e o agente procura no
# WhatsApp "porque o canal é outro". Opt-out vale para todos os canais.
# ---------------------------------------------------------------------------

def pode_contatar_por(igsid: str, canal: str = "") -> PreconditionResult:
    if not igsid:
        return PreconditionResult(True, "ok")
    if is_opted_out(igsid):
        return PreconditionResult(
            False,
            "opt_out",
            f"Opt-out registrado. Vale para TODOS os canais, inclusive '{canal or 'desconhecido'}'.",
        )
    if (flags_ativas(igsid).get("crise_emocional")
            or flags_ativas(igsid).get("saude_mental")
            or flags_ativas(igsid).get("desespero_financeiro")):
        return PreconditionResult(
            False,
            "alerta_sensivel",
            "Conversa marcada como sensível. Nenhuma abordagem proativa.",
        )
    return PreconditionResult(True, "ok")


# ---------------------------------------------------------------------------
# RN-006 — janela e horário: só a abordagem PROATIVA respeita quiet hours
#
# Responder quem escreveu às 2h é conversa. Procurar alguém às 2h é assédio.
# A distinção é `proativo=True`, declarada na chamada da tool.
# ---------------------------------------------------------------------------

# RN-007 — teto global de toques proativos, além da cota por estágio.
PROATIVO_INTERVALO_MINIMO_H = 20
PROATIVO_MAX_JANELA = 3
PROATIVO_JANELA_DIAS = 14


def registrar_toque_proativo(
    igsid: str, tipo: str = "followup", canal: str = "instagram", at: Optional[float] = None
) -> None:
    if not igsid:
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO toques_proativos (igsid, tipo, canal, at) VALUES (?, ?, ?, ?)",
            (igsid, tipo, canal, at if at is not None else time.time()),
        )


def toques_proativos_na_janela(
    igsid: str, dias: int = PROATIVO_JANELA_DIAS, *, now: Optional[float] = None
) -> int:
    momento = now if now is not None else time.time()
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM toques_proativos WHERE igsid = ? AND at > ?",
            (igsid, momento - dias * 86400),
        ).fetchone()
    return int(row["n"])


def ultimo_toque_proativo(igsid: str) -> Optional[float]:
    with db() as conn:
        row = conn.execute(
            "SELECT MAX(at) AS t FROM toques_proativos WHERE igsid = ?", (igsid,)
        ).fetchone()
    return None if not row or row["t"] is None else float(row["t"])


def pode_tocar_proativo(
    igsid: str, *, canal: str = "instagram", now: Optional[float] = None
) -> PreconditionResult:
    """Todas as pré-condições de uma abordagem proativa, em ordem de gravidade."""
    momento = now if now is not None else time.time()

    contato = pode_contatar_por(igsid, canal)
    if not contato.ok:
        return contato

    if igsid and humano_no_comando(igsid):
        return PreconditionResult(
            False, "takeover_humano", "Humano no comando daquele caso. O agente não concorre."
        )

    if in_quiet_hours(to_brt(momento)):
        return PreconditionResult(
            False,
            "quiet_hours",
            f"Fora da janela {FOLLOWUP_QUIET_END}h–{FOLLOWUP_QUIET_START}h BRT. "
            "Abordagem proativa só em horário humano.",
        )

    ultimo = ultimo_toque_proativo(igsid)
    if ultimo is not None and (momento - ultimo) < PROATIVO_INTERVALO_MINIMO_H * 3600:
        horas = (momento - ultimo) / 3600
        return PreconditionResult(
            False,
            "toque_recente",
            f"Último toque proativo há {horas:.1f}h. "
            f"Intervalo mínimo é {PROATIVO_INTERVALO_MINIMO_H}h.",
        )

    total = toques_proativos_na_janela(igsid, PROATIVO_JANELA_DIAS, now=momento)
    if total >= PROATIVO_MAX_JANELA:
        return PreconditionResult(
            False,
            "teto_de_toques",
            f"{total} toques proativos em {PROATIVO_JANELA_DIAS} dias "
            f"(teto {PROATIVO_MAX_JANELA}). Insistir aqui é a definição de spam.",
        )

    return PreconditionResult(True, "ok")


# ---------------------------------------------------------------------------
# RN-012 — fila humana com SLA
#
# A v2 do documento dizia: "Responsáveis, prioridades e prazos internos ainda
# precisam ser definidos." Prazo não definido é prazo que ninguém descumpre —
# então o SLA fica aqui, em dados, com o momento em que o caso estourou.
#
# Consequência de projeto: com o caso ASSUMIDO, o agente silencia (RN-012).
# Caso aberto e ainda não assumido NÃO bloqueia — é justamente a janela em que
# o agente envia a mensagem de acolhimento aprovada.
# ---------------------------------------------------------------------------

SLA_MINUTOS: dict[str, int] = {"P0": 15, "P1": 60, "P2": 240, "P3": 1440}


def sla_minutos(prioridade: str) -> int:
    return SLA_MINUTOS.get((prioridade or "").upper(), SLA_MINUTOS["P3"])


def abrir_caso_humano(
    *, igsid: str = "", prioridade: str = "P3", motivo: str = "", resumo: str = "",
    now: Optional[float] = None,
) -> int:
    """Abre (ou reaproveita) o caso na fila humana, com prazo de SLA."""
    momento = now if now is not None else time.time()
    aberto = caso_aberto(igsid) if igsid else None
    if aberto:
        # Já existe caso vivo: rebaixa o prazo se a prioridade subiu, e não abre
        # uma segunda linha — fila duplicada é fila que ninguém lê.
        if _SEVERITY_ORDER.get(prioridade, 9) < _SEVERITY_ORDER.get(
            aberto["prioridade"] or "P3", 9
        ):
            with db() as conn:
                conn.execute(
                    "UPDATE fila_humana SET prioridade = ?, motivo = ?, prazo_sla = ? WHERE id = ?",
                    (prioridade, motivo, momento + sla_minutos(prioridade) * 60, aberto["id"]),
                )
        return int(aberto["id"])
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO fila_humana (igsid, prioridade, motivo, resumo, aberto_em, prazo_sla) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (igsid, prioridade, motivo, resumo[:2000], momento,
             momento + sla_minutos(prioridade) * 60),
        )
        return int(cur.lastrowid)


def caso_aberto(igsid: str) -> Optional[dict]:
    """Caso humano vivo (não resolvido) daquele contato."""
    if not igsid:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM fila_humana WHERE igsid = ? AND resolvido_em IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (igsid,),
        ).fetchone()
    return dict(row) if row else None


def humano_no_comando(igsid: str) -> bool:
    """True quando uma pessoa assumiu o caso. O agente não concorre com ela."""
    caso = caso_aberto(igsid)
    return bool(caso and caso.get("assumido_por"))


def assumir_caso_humano(caso_id: int, por: str = "", now: Optional[float] = None) -> None:
    momento = now if now is not None else time.time()
    with db() as conn:
        conn.execute(
            "UPDATE fila_humana SET assumido_por = ?, assumido_em = ? WHERE id = ? AND resolvido_em IS NULL",
            (por or "time", momento, caso_id),
        )


def resolver_caso_humano(
    caso_id: int, *, resolucao: str = "", por: str = "", libera_flags: bool = True
) -> None:
    """Fecha o caso. Por padrão limpa as flags do lead: o humano resolveu, o
    agente volta a poder trabalhar normalmente (RN-002/003 deixam de valer)."""
    with db() as conn:
        row = conn.execute("SELECT igsid FROM fila_humana WHERE id = ?", (caso_id,)).fetchone()
        conn.execute(
            "UPDATE fila_humana SET resolvido_em = ?, resolucao = ? WHERE id = ?",
            (time.time(), resolucao[:2000], caso_id),
        )
    if libera_flags and row:
        limpar_flag(row["igsid"])


def casos_fora_do_sla(now: Optional[float] = None) -> list[dict]:
    """Casos abertos e não assumidos cujo prazo estourou. É a métrica que prova
    que o SLA existe — sem isto, o prazo é decorativo.

    Ordem: **gravidade primeiro, atraso depois**. Numa fila de plantão, a crise
    de 5 minutos com prazo de 15 vem antes da dúvida de 30 minutos com prazo de
    60 — ordenar só por atraso colocaria a coisa menos urgente no topo da lista
    que o time lê.
    """
    momento = now if now is not None else time.time()
    with db() as conn:
        linhas = conn.execute(
            "SELECT * FROM fila_humana WHERE resolvido_em IS NULL AND assumido_por IS NULL "
            "AND prazo_sla IS NOT NULL AND prazo_sla < ? "
            "ORDER BY CASE prioridade WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 "
            "WHEN 'P2' THEN 2 ELSE 3 END ASC, prazo_sla ASC",
            (momento,),
        ).fetchall()
    fora = []
    for row in linhas:
        caso = dict(row)
        caso["atraso_minutos"] = int((momento - caso["prazo_sla"]) / 60)
        fora.append(caso)
    return fora


def registrar_alerta_caso(caso_id: int) -> None:
    """Conta quantas vezes este caso já foi cobrado. Caso reincidente é caso
    sem dono — e isso precisa aparecer no relatório, não sumir na fila."""
    with db() as conn:
        conn.execute("UPDATE fila_humana SET alertas = alertas + 1 WHERE id = ?", (caso_id,))


def fila_resumo(now: Optional[float] = None) -> dict:
    momento = now if now is not None else time.time()
    with db() as conn:
        aberto = conn.execute(
            "SELECT COUNT(*) AS n FROM fila_humana WHERE resolvido_em IS NULL"
        ).fetchone()["n"]
        assumido = conn.execute(
            "SELECT COUNT(*) AS n FROM fila_humana WHERE resolvido_em IS NULL AND assumido_por IS NOT NULL"
        ).fetchone()["n"]
        resolvido = conn.execute(
            "SELECT COUNT(*) AS n FROM fila_humana WHERE resolvido_em IS NOT NULL"
        ).fetchone()["n"]
    return {
        "abertos": int(aberto),
        "assumidos": int(assumido),
        "resolvidos": int(resolvido),
        "fora_do_sla": len(casos_fora_do_sla(momento)),
    }


# ---------------------------------------------------------------------------
# RN-013 — moderação destrutiva exige autorização registrada
#
# docs/05 e a v2 do PDF §14 dizem a mesma coisa: "o agente não recebe autorização
# geral para apagar comentários" e "exclusão exige critério objetivo e
# autorização". Aqui isso vira livro-razão: sem linha de autorização com nome,
# não há ocultação nem exclusão — e toda execução tem reversão rastreável.
#
# NÃO existe hoje tool destrutiva no plugin (só responder). Ou seja: a regra
# está armada para o dia em que existir, e a ausência da tool já é fail-closed.
# ---------------------------------------------------------------------------

MODERACAO_DESTRUTIVA = {"ocultar", "ocultacao", "excluir", "exclusao", "banir"}
MODERACAO_REVERSIVEL = {"reexibir", "reexibicao", "restaurar"}


def autorizar_moderacao(
    comment_id: str, acao: str, *, autorizado_por: str = "", justificativa: str = "",
) -> int:
    if acao not in MODERACAO_DESTRUTIVA:
        raise ValueError(
            f"'{acao}' não é ação destrutiva — não precisa de autorização registrada."
        )
    if not autorizado_por:
        raise ValueError("Autorização de moderação destrutiva exige QUEM autorizou.")
    if not justificativa.strip():
        raise ValueError("Autorização de moderação destrutiva exige critério objetivo.")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO moderacao_aprovacoes (comment_id, acao, autorizado_por, justificativa, at) "
            "VALUES (?, ?, ?, ?, ?)",
            (comment_id, acao, autorizado_por, justificativa[:1000], time.time()),
        )
        return int(cur.lastrowid)


def moderacao_autorizada(comment_id: str, acao: str) -> bool:
    if acao in MODERACAO_REVERSIVEL:
        return True  # reexibir corrige um erro: não se pede autorização para consertar
    if acao not in MODERACAO_DESTRUTIVA:
        return False
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM moderacao_aprovacoes WHERE comment_id = ? AND acao = ? "
            "AND revertido_em IS NULL LIMIT 1",
            (comment_id, acao),
        ).fetchone()
    return row is not None


def marcar_moderacao_executada(comment_id: str, acao: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "UPDATE moderacao_aprovacoes SET executado_em = ? "
            "WHERE comment_id = ? AND acao = ? AND executado_em IS NULL",
            (time.time(), comment_id, acao),
        )
        return cur.rowcount


def reverter_moderacao(comment_id: str, acao: str, *, revertido_por: str = "") -> int:
    """Reexibição: registra a reversão. Erro de moderação corrigido é dado de
    qualidade do agente, não vexame a esconder."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE moderacao_aprovacoes SET revertido_em = ?, revertido_por = ? "
            "WHERE comment_id = ? AND acao = ? AND revertido_em IS NULL",
            (time.time(), revertido_por, comment_id, acao),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# avaliar_envio — a composição de TODAS as RN-* de saída
#
# Ponto único de decisão, chamado por instagram_api._autorizar() no caminho do
# envio. Devolve a lista de bloqueios (vazia = pode enviar). Cada bloqueio traz
# a regra que o produziu, porque auditoria sem número de regra não é auditoria.
# ---------------------------------------------------------------------------

@dataclass
class Bloqueio:
    regra: str
    motivo: str
    detalhe: str = ""

    def to_dict(self) -> dict:
        return {"regra": self.regra, "motivo": self.motivo, "detalhe": self.detalhe}

    def __str__(self) -> str:
        return f"[{self.regra}] {self.motivo}" + (f" — {self.detalhe}" if self.detalhe else "")


def avaliar_envio(
    *,
    texto: str = "",
    igsid: str = "",
    canal: str = "instagram",
    proativo: bool = False,
    acao: str = "",
    exige_acao: bool = False,
    agora: Optional[float] = None,
) -> list[Bloqueio]:
    """Todas as regras de negócio que antecedem um envio. Ordem = gravidade.

    Deliberadamente NÃO inclui a RN-001 (kill switch): ela vive em
    `_check_kill_switch()` e no hook, e duplicá-la aqui criaria duas verdades
    sobre a mesma parada de emergência.

    `exige_acao=True` é o modo do caminho das FERRAMENTAS (achado F04 do parecer
    OpenClaw): ali a omissão da ação bloqueia, porque "não declarou" não pode valer
    como "não tem restrição".
    """
    bloqueios: list[Bloqueio] = []
    texto = texto or ""

    # RN-005 — opt-out é da pessoa, vale em todo canal (aqui, os não-Instagram;
    # no Instagram quem barra é _check_opt_out, com a exceção da despedida).
    if igsid and canal != "instagram" and is_opted_out(igsid):
        bloqueios.append(
            Bloqueio(
                RN_OPT_OUT,
                f"Opt-out registrado para este contato. Vale também para '{canal}'.",
                "Opt-out é permanente e não se transfere de canal.",
            )
        )

    # RN-002/003/004 — lead com flag sensível ativa não recebe conteúdo comercial.
    # Não importa o que o modelo decidiu: preço e link não saem.
    if igsid and (contem_link(texto) or contem_valor(texto)):
        ativas = flags_ativas(igsid)
        for flag, regra in FLAGS_QUE_BLOQUEIAM_COMERCIAL.items():
            if flag in ativas:
                bloqueios.append(
                    Bloqueio(
                        regra,
                        f"Lead com '{flag}' ativo não recebe preço nem link.",
                        "Acolher e encaminhar. Conteúdo comercial aqui é a linha que não se move.",
                    )
                )
                break

    # RN-012 — humano assumiu: o agente silencia naquela conversa.
    if igsid and humano_no_comando(igsid):
        caso = caso_aberto(igsid) or {}
        bloqueios.append(
            Bloqueio(
                RN_ESPERA_HUMANA,
                f"Atendimento humano no comando (caso {caso.get('id')}).",
                "Respostas automáticas concorrentes estão suspensas por política.",
            )
        )

    # RN-006 / RN-007 — só a abordagem PROATIVA tem horário e teto.
    if proativo and igsid:
        liberacao = pode_tocar_proativo(igsid, canal=canal, now=agora)
        if not liberacao.ok:
            regra = (
                RN_JANELA_HORARIO
                if liberacao.motivo in {"quiet_hours"}
                else RN_TETO_FREQUENCIA
            )
            bloqueios.append(Bloqueio(regra, liberacao.motivo, liberacao.detalhe))

    # RN-009 — matriz de autonomia da ação declarada.
    #
    # Achado F04 do parecer OpenClaw: `acao` era opcional e a matriz só rodava quando
    # ele existia — então OMITIR o campo desligava a verificação. A decisão de quais
    # permissões se aplicam não pode sair do próprio pedido que está sendo autorizado.
    # Agora: vocabulário fechado (ação desconhecida bloqueia) e, no caminho das
    # ferramentas, omissão também bloqueia.
    if acao:
        if acao not in NIVEL_AUTONOMIA:
            bloqueios.append(
                Bloqueio(
                    RN_MATRIZ_AUTONOMIA,
                    f"Ação fora do vocabulário fechado: '{acao}'.",
                    "Ação sem nível de autonomia definido não é autorizada por "
                    "omissão nem por renomeação. Use uma do vocabulário.",
                )
            )
        else:
            pode, regra, motivo = pode_executar(acao, igsid)
            if not pode:
                bloqueios.append(Bloqueio(regra, motivo))
    elif exige_acao:
        bloqueios.append(
            Bloqueio(
                RN_MATRIZ_AUTONOMIA,
                "Ação não declarada (omissão não é autorização).",
                "Reenvie declarando uma `acao` do vocabulário fechado.",
            )
        )

    # RN-010 — alegações proibidas no texto de saída.
    alegacoes = detect_alegacao_proibida(texto)
    if alegacoes:
        ainda_bloqueadas = [
            categoria
            for categoria in alegacoes
            if not (
                categoria in _ALEGACOES_LIBERAVEIS
                and tem_aprovacao(f"alegacao_{categoria}", igsid)
            )
        ]
        if ainda_bloqueadas:
            bloqueios.append(
                Bloqueio(
                    RN_ALEGACOES_PROIBIDAS,
                    "Alegação proibida no texto: " + ", ".join(ainda_bloqueadas),
                    "Reescreva sem promessa de resultado, prazo, renda, cura, "
                    "escassez ou urgência inventada.",
                )
            )

    # RN-011 — prazo, garantia e devolução exigem política aprovada.
    consumidor = detect_afirmacao_consumidor_sem_fonte(texto)
    if consumidor:
        bloqueios.append(
            Bloqueio(
                RN_CONSUMIDOR,
                "Afirmação de garantia/devolução/frete sem fonte aprovada: "
                + ", ".join(sorted(set(consumidor))),
                "Preencha IG_POLITICA_CONSUMIDOR no .env ou não afirme a condição.",
            )
        )

    # RN-019 — status de pedido, pagamento e rastreio exigem FONTE CONSULTADA.
    #
    # O Documento Mestre cita Bling 36x, Clint 45x e rastreio 19x; o agente não tem
    # nenhuma dessas integrações. Sem esta regra, o caminho natural é o modelo
    # improvisar um status plausível — afirmação falsa com a assinatura do cliente.
    # Enquanto não houver fonte, a resposta honesta é "não consigo ver aqui" e
    # escalar. No dia em que o token existir, o bloqueio se desfaz sozinho.
    if not status_pedido_disponivel():
        sem_fonte = detect_afirmacao_status_sem_fonte(texto)
        if sem_fonte:
            bloqueios.append(
                Bloqueio(
                    RN_STATUS_SEM_FONTE,
                    "Status de pedido/pagamento/rastreio sem fonte: "
                    + ", ".join(sorted(set(sem_fonte))),
                    "Não há integração com Bling/Clint configurada. Diga que você "
                    "não consegue consultar e escale para humano — não estime, não "
                    "suponha, não prometa verificar.",
                )
            )

    return bloqueios


def pior_severidade(flags) -> str:
    """A severidade mais grave entre hits de A0 — usada para o SLA da fila humana.

    Aceita objetos FlagHit (usa `.severity`) ou strings de severidade. Não existe
    severidade conhecida: devolve P3, o menor impacto — nunca P0, senão todo caso
    sem classificação viraria alarme de 15 minutos e a fila perderia o valor.
    """
    severidades = [getattr(f, "severity", f) for f in (flags or [])]
    validas = [s for s in severidades if s in _SEVERITY_ORDER]
    if not validas:
        return "P3"
    return min(validas, key=lambda s: _SEVERITY_ORDER[s])


# ===========================================================================
# RN-014 a RN-018 — PROTEÇÃO DE DADOS (LGPD)
#
# Por que este bloco existe: as RN-001..013 diziam o que o agente pode FAZER.
# Nenhuma dizia o que ele pode GUARDAR, por quanto tempo, nem o que fazer quando
# o titular pedir para apagar. O banco de estado acumulava então: o texto da
# mensagem enviada (followups.mensagem), a pergunta literal da pessoa
# (lacunas.pergunta) e — o pior — o briefing com o texto da crise dentro da fila
# humana (fila_humana.resumo), que ainda era impresso numa mensagem de Telegram.
# Dado sensível ligado a identificador, sem prazo, saindo do perímetro.
#
# o que estas regras NÃO fazem: não substituem a revisão jurídica nem definem a
# base legal — isso é declarado pelo cliente (ver Pendências do
# REGRAS-DE-NEGOCIO.md). Elas impõem o que é imponível em código.
# ===========================================================================

RN_MINIMIZACAO = "RN-014"
RN_RETENCAO = "RN-015"
RN_DADO_SENSIVEL = "RN-016"
RN_DIREITO_TITULAR = "RN-017"
RN_RELATORIO_SEM_DADO = "RN-018"

# Prazo de guarda por tabela, em dias. Cada número tem de aguentar a pergunta
# "por que não 1 dia?" e "por que não 1 ano?" — prazo inventado é prazo que
# ninguém defende na hora em que o cliente perguntar.
RETENCAO_DIAS: dict[str, int] = {
    # Conteúdo literal da pessoa. Curto: serve para a mineração semanal, não para
    # histórico. 30 dias = 4 ciclos de mineração.
    "lacunas": 30,
    # Ids de evento: existem só para descartar reentrega da Meta. Não são dado de
    # pessoa nenhuma — 7 dias bastam.
    "processed": 7,
    # Quem falou com a conta nas últimas 24h. É lista de identificadores: some.
    "inbound": 30,
    # Texto que o agente enviou. 90 dias cobre a auditoria de tom e o plantão.
    "followups": 90,
    # Só casos RESOLVIDOS (caso aberto é obrigação pendente, não histórico).
    "fila_humana": 90,
    # MARCA sensível (crise, saúde, menor). 90 dias: marca a pessoa pelo tempo do
    # atendimento e não para sempre. Marca eterna é o que a LGPD proíbe.
    "lead_flags": 90,
    "private_replies": 180,
    "toques_proativos": 180,
    "interactions": 180,
    # Estágio no funil — é dado sobre a pessoa (o que ela quis comprar). Mesmo
    # prazo da interação. Este campo ficou sem prazo na primeira versão deste
    # bloco e o teste `test_toda_tabela_de_estado_tem_prazo_definido` pegou.
    "lead_stage": 180,
    # Prova de autorização humana — auditoria. Fica mais.
    "aprovacoes": 365,
    "moderacao_aprovacoes": 365,
    "exclusoes": 365,
    # Reconciliação de efeito incerto: auditoria de o que saiu ou não saiu. 365 pelo
    # mesmo motivo da prova de aprovação — é o registro que responde "foi enviado?".
    "reconciliacao": 365,
}

# Tabelas que NÃO expiram, e são só estas duas. A decisão é deliberada:
# - `opt_outs`: o registro da recusa é a própria base para não contatar de novo.
#   Apagar a recusa é transformar opt-out em consentimento — o oposto do que a
#   pessoa pediu.
# - `bloqueios_permanentes`: o bloqueio pós-exclusão, guardado só como hash. Não
#   tem dado pessoal, e apagá-lo faria o expurgo virar a causa de uma reabordagem.
RETENCAO_PERMANENTE = frozenset({"opt_outs", "bloqueios_permanentes"})

_CHAVE_EXPURGO = "ultimo_expurgo"


def mascarar_id(igsid: str, *, tamanho: int = 6) -> str:
    """Hash curto do identificador, para relatório e Telegram (RN-018).

    O igsid é dado pessoal (identificador de conta). Relatório de plantão que
    circula em app de mensagem não é lugar para ele. O número do CASO é interno e
    não identifica ninguém — é por ele que o time abre o atendimento.
    """
    if not igsid:
        return "-"
    return "…" + hashlib.sha256(igsid.encode("utf-8")).hexdigest()[:tamanho]


def redigir(texto: str, *, limite: int = 160) -> str:
    """Tira do texto o que identifica ou é sensível, antes de guardar (RN-014).

    Não é anonimização — é minimização. O texto continua reconhecível como
    assunto; deixa de carregar documento, telefone, e-mail ou arroba.
    """
    limpo = re.sub(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", "[cpf]", texto or "")
    limpo = re.sub(r"\b\d{2}\s?\d{4,5}-?\d{4}\b", "[telefone]", limpo)
    limpo = re.sub(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b", "[email]", limpo)
    limpo = re.sub(r"@[\w.]{2,}", "[arroba]", limpo)
    limpo = re.sub(r"\b\d{11,}\b", "[numero]", limpo)
    limpo = re.sub(r"\s+", " ", limpo).strip()
    return limpo[:limite]


def resumo_para_fila(decisao) -> str:
    """Resumo do caso humano SEM o texto da pessoa (RN-014).

    O caso precisa dizer O QUE aconteceu (motivo, severidade, flags) para o time
    agir; não precisa repetir O QUE a pessoa escreveu. Era exatamente aí que o
    texto da crise ia parar no Telegram.
    """
    flags = ",".join(sorted(getattr(h, "flag", "") for h in (decisao.flags or []))) or "-"
    return redigir(
        f"{getattr(decisao, 'motivo', '') or '-'} · severidade "
        f"{getattr(decisao, 'severity', '-') or '-'} · flags {flags}"
    )


def expurgar_expirados(now: Optional[float] = None) -> dict[str, int]:
    """Apaga o que passou do prazo de guarda (RN-015). Idempotente.

    Duas regras que não são óbvias e por isso estão em código:

    1. `fila_humana` só expurga caso RESOLVIDO. Caso aberto é obrigação pendente:
       apagá-lo não seria proteger o titular, seria esconder um atendimento que
       ninguém fez.
    2. `opt_outs` não é tocada. A recusa é o motivo de não voltar a falar com a
       pessoa — ver RETENCAO_PERMANENTE.
    """
    momento = now if now is not None else time.time()
    apagados: dict[str, int] = {}

    def corte(tabela: str) -> float:
        return momento - RETENCAO_DIAS[tabela] * 86400

    consultas = (
        ("lacunas", "DELETE FROM lacunas WHERE at < ?", lambda: (corte("lacunas"),)),
        ("processed", "DELETE FROM processed WHERE at < ?", lambda: (corte("processed"),)),
        ("inbound", "DELETE FROM inbound WHERE last_seen < ?", lambda: (corte("inbound"),)),
        ("followups", "DELETE FROM followups WHERE COALESCE(enviado_em, due_at, 0) < ?",
         lambda: (corte("followups"),)),
        ("fila_humana",
         "DELETE FROM fila_humana WHERE resolvido_em IS NOT NULL AND resolvido_em < ?",
         lambda: (corte("fila_humana"),)),
        ("lead_flags", "DELETE FROM lead_flags WHERE at < ?", lambda: (corte("lead_flags"),)),
        ("private_replies", "DELETE FROM private_replies WHERE sent_at < ?",
         lambda: (corte("private_replies"),)),
        ("toques_proativos", "DELETE FROM toques_proativos WHERE at < ?",
         lambda: (corte("toques_proativos"),)),
        ("interactions", "DELETE FROM interactions WHERE received_at < ?",
         lambda: (corte("interactions"),)),
        ("lead_stage", "DELETE FROM lead_stage WHERE COALESCE(updated_at, 0) < ?",
         lambda: (corte("lead_stage"),)),
        ("aprovacoes", "DELETE FROM aprovacoes WHERE at < ?", lambda: (corte("aprovacoes"),)),
        ("moderacao_aprovacoes", "DELETE FROM moderacao_aprovacoes WHERE at < ?",
         lambda: (corte("moderacao_aprovacoes"),)),
        ("exclusoes", "DELETE FROM exclusoes WHERE at < ?", lambda: (corte("exclusoes"),)),
    )

    with db() as conn:
        for tabela, sql, argumentos in consultas:
            cursor = conn.execute(sql, argumentos())
            apagados[tabela] = cursor.rowcount if cursor.rowcount > 0 else 0
    return apagados


def expurgar_se_preciso(*, intervalo_horas: int = 24, now: Optional[float] = None) -> dict[str, int]:
    """Roda o expurgo no máximo uma vez por intervalo. Marca no banco.

    Existe para o expurgo não depender de alguém lembrar de rodar um script, e
    não rodar a cada evento recebido.
    """
    momento = now if now is not None else time.time()
    with db() as conn:
        linha = conn.execute(
            "SELECT valor FROM manutencao WHERE chave = ?", (_CHAVE_EXPURGO,)
        ).fetchone()

    if linha and linha["valor"] and (momento - linha["valor"]) < intervalo_horas * 3600:
        return {}

    apagados = expurgar_expirados(now=momento)
    with db() as conn:
        conn.execute(
            "INSERT INTO manutencao (chave, valor, atualizado_em) VALUES (?,?,?) "
            "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor, "
            "atualizado_em=excluded.atualizado_em",
            (_CHAVE_EXPURGO, momento, momento),
        )
    return apagados


def expurgar_marcar_falha(now: Optional[float] = None) -> None:
    """Não marca o expurgo como feito quando ele falhou.

    Se a marca fosse gravada mesmo com exceção, a falha silenciosa ficaria 24h no
    lugar — e retenção que não roda é o pior estado possível: o dado continua lá e
    o relatório diz que foi tratado.
    """
    momento = now if now is not None else time.time()
    with db() as conn:
        conn.execute(
            "DELETE FROM manutencao WHERE chave = ? AND valor IS NULL", (_CHAVE_EXPURGO,)
        )


_TABELAS_COM_IGSID = (
    ("lead_flags", "igsid"),
    ("lead_stage", "igsid"),
    ("interactions", "igsid"),
    ("followups", "igsid"),
    ("lacunas", "igsid"),
    ("toques_proativos", "igsid"),
    ("aprovacoes", "igsid"),
    ("fila_humana", "igsid"),
    ("inbound", "igsid"),
    ("opt_outs", "igsid"),
    ("reconciliacao", "igsid"),
)


def exportar_titular(igsid: str) -> dict:
    """Direito de acesso (LGPD art. 18, II) — tudo o que existe sobre a pessoa.

    Limite honesto: `private_replies`, `processed` e `moderacao_aprovacoes` são
    chaveados por comentário/evento, não por igsid — não há como ligá-los à pessoa
    por esta via. Isso está dito aqui porque quem responde ao titular precisa saber
    o que NÃO foi incluído.
    """
    dados: dict[str, list[dict]] = {}
    with db() as conn:
        for tabela, coluna in _TABELAS_COM_IGSID:
            linhas = conn.execute(
                f"SELECT * FROM {tabela} WHERE {coluna} = ?", (igsid,)  # noqa: S608
            ).fetchall()
            dados[tabela] = [dict(linha) for linha in linhas]

    return {
        "igsid": igsid,
        "gerado_em_brt": now_brt().isoformat(),
        "rules_version": RULES_VERSION,
        "tabelas": dados,
        "nao_incluido": ["private_replies", "processed", "moderacao_aprovacoes"],
    }


def apagar_titular(igsid: str, *, motivo: str = "") -> dict:
    """Direito de eliminação (LGPD art. 18, VI).

    Duas decisões que definem se isto é eliminação ou teatro:

    1. **O bloqueio de contato sobrevive ao apagamento**, em `bloqueios_permanentes`
       e só como hash. Se apagasse a recusa junto com o dado, a pessoa que pediu
       para ser esquecida seria abordada na próxima campanha — o apagamento viraria
       a causa da violação.
    2. **A prova do atendimento não guarda o igsid**, só o hash e as contagens.
       Guardar quem pediu o apagamento é manter exatamente o dado que se apagou.
    """
    contagens: dict[str, int] = {}
    with db() as conn:
        for tabela, coluna in _TABELAS_COM_IGSID:
            if tabela == "opt_outs":
                continue
            cursor = conn.execute(
                f"DELETE FROM {tabela} WHERE {coluna} = ?", (igsid,)  # noqa: S608
            )
            contagens[tabela] = cursor.rowcount if cursor.rowcount > 0 else 0

        # A recusa vira bloqueio permanente por hash.
        recusou = conn.execute(
            "SELECT COUNT(*) AS n FROM opt_outs WHERE igsid = ?", (igsid,)
        ).fetchone()["n"]
        conn.execute("DELETE FROM opt_outs WHERE igsid = ?", (igsid,))
        contagens["opt_outs"] = recusou

        hash_id = hashlib.sha256(igsid.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT OR REPLACE INTO bloqueios_permanentes (igsid_hash, at, motivo) VALUES (?,?,?)",
            (hash_id, time.time(), motivo or "direito ao esquecimento"),
        )
        conn.execute(
            "INSERT INTO exclusoes (igsid_hash, at, motivo, contagens) VALUES (?,?,?,?)",
            (hash_id, time.time(), motivo or "direito ao esquecimento",
             json.dumps(contagens, ensure_ascii=False, sort_keys=True)),
        )

    return {
        "igsid_apagado": mascarar_id(igsid),
        "contagens": contagens,
        "bloqueio_permanente": True,
        "at": now_brt().isoformat(),
    }


def bloqueio_permanente_ativo(igsid: str) -> bool:
    """A pessoa pediu eliminação antes? Então nunca mais falar com ela."""
    if not igsid:
        return False
    hash_id = hashlib.sha256(igsid.encode("utf-8")).hexdigest()
    with db() as conn:
        linha = conn.execute(
            "SELECT 1 FROM bloqueios_permanentes WHERE igsid_hash = ?", (hash_id,)
        ).fetchone()
    return linha is not None


# ===========================================================================
# RN-019 — Status de pedido, pagamento e rastreio só com FONTE CONSULTADA
#
# O Documento Mestre cita Bling 36x, Clint 45x, rastreio 19x e "pedido" 34x. O agente
# entregue tem ZERO linha de integração. Sem esta regra, o caminho natural é o modelo
# improvisar um status plausível — "seu pedido foi enviado, chega em 3 dias úteis" —
# e isso é afirmação falsa com a assinatura do cliente, muito pior do que não ter a
# integração.
#
# A regra não diz "não fale de pedido": diz "não afirme o que não consultou". No dia em
# que BLING_API_TOKEN existir, `status_pedido_disponivel()` devolve True e o bloqueio
# se desfaz por CONFIGURAÇÃO — sem editar regra, sem tocar em teste.
# ===========================================================================

RN_STATUS_SEM_FONTE = "RN-019"

try:  # o motor não pode quebrar se o módulo de integrações faltar
    from integracoes import status_pedido_disponivel as _status_pedido_disponivel
except Exception:  # noqa: BLE001  # pragma: no cover
    def _status_pedido_disponivel() -> bool:  # type: ignore[misc]
        return False


def status_pedido_disponivel() -> bool:
    """Há fonte para afirmar status de pedido/pagamento/rastreio?

    Adapter quebrado NÃO libera afirmação: qualquer falha vira `False`, porque o
    custo do falso "indisponível" é o agente dizer "não consigo ver aqui" e o custo
    do falso "disponível" é afirmar ao cliente um status que ninguém consultou.
    """
    try:
        return bool(_status_pedido_disponivel())
    except Exception:  # noqa: BLE001
        return False


def integracoes_status() -> dict[str, bool]:
    """Retrato honesto do que existe, para o briefing. Nunca levanta."""
    try:
        from integracoes import status_integracoes

        return status_integracoes()
    except Exception:  # noqa: BLE001
        return {
            "bling": False,
            "clint": False,
            "whatsapp": False,
            "status_de_pedido": False,
        }


AFIRMACOES_DE_STATUS = (
    (
        "pedido_enviado",
        (
            r"\bpedido (ja )?(foi )?(enviado|postado|despachado)\b",
            r"\b(foi|ja foi) (enviado|postado|despachado)\b",
            r"\bsaiu para entrega\b",
            r"\bfoi despachado\b",
        ),
    ),
    (
        "previsao_de_entrega",
        (
            r"\bchega (em|ate) \d+\b",
            r"\bprevisao de entrega\b",
            r"\bem \d+ dias uteis\b",
            r"\bentrega (em|para|dia) \d+\b",
        ),
    ),
    (
        "em_transito",
        (
            r"\b(esta|ta) em transito\b",
            r"\bem rota de entrega\b",
            r"\bja esta com a transportadora\b",
        ),
    ),
    (
        "pagamento_confirmado",
        (
            r"\bpagamento (foi )?(aprovado|confirmado|recebido)\b",
            r"\bcompra (foi )?(aprovada|confirmada)\b",
            r"\bpix (foi )?(caiu|recebido|confirmado)\b",
            r"\bboleto (foi )?(pago|compensado)\b",
        ),
    ),
    (
        "codigo_de_rastreio",
        (
            r"\bcodigo de rastreio\b",
            r"\bnumero de rastreio\b",
            r"\bcodigo de acompanhamento\b",
            r"\brastreio (e|esta)\b",
        ),
    ),
    (
        "promessa_de_verificar",
        (
            r"\bvou (verificar|consultar|checar) (o |seu |a )?(pedido|rastreio|pagamento|entrega)\b",
            r"\bdeixa eu (ver|consultar|checar) (o |seu )?(pedido|rastreio|pagamento)\b",
            r"\bvou olhar (o |seu )?pedido\b",
        ),
    ),
)


def detect_afirmacao_status_sem_fonte(texto: str) -> list[str]:
    """Categorias de afirmação sobre pedido/pagamento/rastreio no texto de saída.

    Inclui `promessa_de_verificar` de propósito: prometer conferir um pedido que não
    se pode conferir é a mesma mentira com uma etapa a mais.
    """
    normalizado = normalize(texto or "")
    achados: list[str] = []
    for categoria, padroes in AFIRMACOES_DE_STATUS:
        if any(re.search(padrao, normalizado) for padrao in padroes):
            achados.append(categoria)
    return achados


def diretiva_sem_integracao() -> str:
    """Proibição permanente enquanto não houver integração (RN-019)."""
    return (
        "STATUS DE PEDIDO (RN-019): não há integração com Bling, Clint nem rastreio "
        "configurada. Você NÃO consegue consultar pedido, pagamento, entrega nem "
        "rastreio. Não afirme, não estime, não diga 'provavelmente já saiu' e não "
        "prometa verificar. Diga em uma linha que você não consegue ver isso daqui e "
        "encaminhe para o time, que confere e responde."
    )


# ===========================================================================
# RN-020 — Vínculo de identidade antes da ação
#
# Achado F02 do parecer OpenClaw: os handlers de `ig_private_reply` e
# `ig_reply_comment` chamavam o envio SEM igsid. O parâmetro tem valor vazio por
# padrão e as verificações eram puladas EM SILÊNCIO (`if igsid:`), não bloqueadas.
# O motor conhecia a restrição e a ferramenta chegava sem o vínculo para aplicá-la.
# Dar nomes diferentes às funções não muda o destino da chamada; omitir a identidade
# desligava a proteção.
#
# A correção não é "lembrar de passar o parâmetro" — quem lembra hoje esquece
# amanhã, e um teste que injeta o igsid à mão não prova o caminho real. É resolver o
# autor NO SERVIDOR, a partir do registro que o próprio intake gravou, e RECUSAR
# quando o vínculo não for confiável.
# ===========================================================================

RN_VINCULO_IDENTIDADE = "RN-020"

MOTIVOS_VINCULO = {
    "comentario_sem_vinculo": (
        "Comentário sem registro de autor. Sem vínculo confiável, a ação que exige "
        "identificação não segue."
    ),
    "alvo_de_outra_pessoa": (
        "O igsid declarado não é o autor deste comentário. Comentário de terceiro "
        "não é alvo."
    ),
}


def igsid_do_comentario(comment_id: str) -> str:
    """Autor do comentário, pelo registro que o intake gravou.

    Devolve "" quando não há vínculo — e "" NÃO é permissão. Quem chama trata como
    bloqueio (`vinculo_confiavel`), nunca como "sem restrição".
    """
    if not comment_id:
        return ""
    with db() as conn:
        linha = conn.execute(
            "SELECT igsid FROM interactions WHERE comment_id = ? "
            "AND igsid IS NOT NULL AND igsid != '' "
            "ORDER BY received_at DESC LIMIT 1",
            (comment_id,),
        ).fetchone()
    return (linha["igsid"] if linha else "") or ""


def vinculo_confiavel(comment_id: str, igsid: str = "") -> tuple[bool, str]:
    """(ok, motivo). O vínculo entre ação e pessoa é confiável?

    Reprova em três casos, e nenhum deles é "seguir com cautela":

    - comentário sem registro → não se sabe de quem é;
    - registro sem autor → idem;
    - igsid declarado diferente do dono do comentário → é o alvo de outra pessoa
      (critério T06 do parecer: comentário de terceiro não é alvo).
    """
    if not comment_id:
        return False, "comentario_sem_vinculo"
    dono = igsid_do_comentario(comment_id)
    if not dono:
        return False, "comentario_sem_vinculo"
    if igsid and igsid != dono:
        return False, "alvo_de_outra_pessoa"
    return True, ""


def vinculo_resolver(comment_id: str, igsid: str = "") -> str:
    """O igsid a usar na ação, ou levanta `VinculoInsuficiente`.

    Ponto ÚNICO de resolução: nenhuma ferramenta de comentário deve montar o igsid
    por conta própria, senão a correção do F02 volta a depender de quem lembra.
    """
    ok, motivo = vinculo_confiavel(comment_id, igsid)
    if not ok:
        raise VinculoInsuficiente(motivo)
    return igsid or igsid_do_comentario(comment_id)


class VinculoInsuficiente(PermissionError):
    """Levantada quando não há vínculo confiável entre a ação e a pessoa (RN-020).

    O motivo entra na mensagem como CHAVE (`alvo_de_outra_pessoa`) além da prosa: o
    log de auditoria precisa de um rótulo estável para contar ocorrências, e o
    operador precisa da frase que explica.
    """

    def __init__(self, motivo: str) -> None:
        self.motivo = motivo
        prosa = MOTIVOS_VINCULO.get(motivo, motivo)
        super().__init__(f"[{RN_VINCULO_IDENTIDADE}] {motivo}: {prosa}")


# ---------- reconciliação: efeito externo com resultado desconhecido ----------
#
# Achado F05 do parecer OpenClaw: "um arquivo de falhas, sozinho, não comprova
# recuperação automática". E o pedido explícito: "distinguir recusa confirmada de
# resultado desconhecido". Esta é a regra que sustenta essa distinção.

RN_RECONCILIACAO = "RN-021"

RECONCILIACAO_ESTADOS = ("incerto", "enviado", "nao_enviado")


def abrir_reconciliacao(
    *,
    acao: str,
    canal: str = "instagram",
    comment_id: str = "",
    igsid: str = "",
    detalhe: str = "",
) -> int:
    """Registra um efeito externo cujo resultado NÃO se sabe.

    Não é log: é uma pendência que trava nova tentativa cega no mesmo alvo. Sem
    isso, "não sei se saiu" vira "mando de novo" — e o cliente recebe duas vezes.
    """
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO reconciliacao "
            "(comment_id, igsid, acao, canal, estado, detalhe, criado_em) "
            "VALUES (?, ?, ?, ?, 'incerto', ?, ?)",
            (comment_id, igsid, acao, canal, detalhe[:500], time.time()),
        )
        return int(cur.lastrowid or 0)


def reconciliacao_pendente(comment_id: str = "", igsid: str = "") -> bool:
    """Existe efeito de resultado desconhecido para este alvo?"""
    if not (comment_id or igsid):
        return False
    with db() as conn:
        if comment_id:
            linha = conn.execute(
                "SELECT 1 FROM reconciliacao WHERE comment_id = ? AND estado = 'incerto' "
                "LIMIT 1",
                (comment_id,),
            ).fetchone()
        else:
            linha = conn.execute(
                "SELECT 1 FROM reconciliacao WHERE igsid = ? AND estado = 'incerto' LIMIT 1",
                (igsid,),
            ).fetchone()
    return linha is not None


def reconciliacoes_abertas() -> list[dict]:
    """Pendências de reconciliação, mais antigas primeiro (ordem de atenção)."""
    with db() as conn:
        linhas = conn.execute(
            "SELECT id, comment_id, igsid, acao, canal, detalhe, criado_em "
            "FROM reconciliacao WHERE estado = 'incerto' ORDER BY criado_em ASC"
        ).fetchall()
    return [dict(linha) for linha in linhas]


def resolver_reconciliacao(id: int, *, desfecho: str, por: str = "") -> int:
    """Fecha a pendência com o que de fato aconteceu (enviado | nao_enviado)."""
    if desfecho not in ("enviado", "nao_enviado"):
        raise ValueError("desfecho tem de ser 'enviado' ou 'nao_enviado'")
    with db() as conn:
        cur = conn.execute(
            "UPDATE reconciliacao SET estado = ?, resolvido_em = ?, resolvido_por = ? "
            "WHERE id = ? AND estado = 'incerto'",
            (desfecho, time.time(), por, id),
        )
        return cur.rowcount


def tem_interacao_recente(igsid: str, *, janela_segundos: int = DM_WINDOW_SECONDS) -> bool:
    """A pessoa escreveu para nós dentro da janela? Base da proatividade derivada.

    Lê `inbound.last_seen`, que é a tabela que a própria janela de 24h usa — não
    `interactions`, que é registro de atendimento e pode não existir para uma
    mensagem que chegou e ainda não virou linha.
    """
    if not igsid:
        return False
    corte = time.time() - janela_segundos
    with db() as conn:
        linha = conn.execute(
            "SELECT 1 FROM inbound WHERE igsid = ? AND last_seen > ? LIMIT 1",
            (igsid, corte),
        ).fetchone()
    return linha is not None



