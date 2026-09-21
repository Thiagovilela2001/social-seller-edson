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
RULES_VERSION = "1.0.0"

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
        ),
        corroborativos=(
            # Ambíguos sozinhos — hipérbole comum. Precisam de companhia.
            r"\bnao aguento mais\b",
            r"\bnao vejo sentido\b",
            r"\bacabar com tudo\b",
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
    with db() as conn:
        return (
            conn.execute("SELECT 1 FROM opt_outs WHERE igsid = ?", (igsid,)).fetchone()
            is not None
        )


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
