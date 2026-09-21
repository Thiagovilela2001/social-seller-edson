"""Testes da PROTEÇÃO DE DADOS — RN-014 a RN-018 (LGPD).

Contexto: as RN-001..013 diziam o que o agente pode FAZER. Nenhuma dizia o que ele
pode GUARDAR. O banco acumulava o texto enviado (`followups.mensagem`), a pergunta
literal da pessoa (`lacunas.pergunta`) e o briefing com o texto da crise dentro da
fila humana (`fila_humana.resumo`), que ainda saía numa mensagem de Telegram.

Estes testes provam o que o código faz — não o que a documentação promete. Em
especial: apagar o titular NÃO pode ressuscitar o contato com ele.

Rodar:  python -m unittest tests.test_lgpd -v
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "plugins" / "instagram-seller"))

import rules  # noqa: E402


class BaseLGPD(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["IG_STATE_DB"] = str(Path(self._tmp.name) / "estado.db")
        os.environ["IG_KILL_SWITCH"] = str(Path(self._tmp.name) / "kill")
        os.environ.pop("IG_POLITICA_CONSUMIDOR", None)

    def tearDown(self) -> None:
        for var in ("IG_STATE_DB", "IG_KILL_SWITCH"):
            os.environ.pop(var, None)
        self._tmp.cleanup()

    def linhas(self, tabela: str) -> list[dict]:
        with rules.db() as conn:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {tabela}").fetchall()]


# ===========================================================================
# RN-014 — minimização: guardar o necessário, sem o texto da pessoa
# ===========================================================================

class TestRN014Minimizacao(BaseLGPD):
    def test_resumo_da_fila_nao_leva_o_texto_da_pessoa(self) -> None:
        """O caso que originou esta regra: mensagem de crise ia verbatim para a
        fila humana e de lá para o Telegram."""
        texto_crise = "nao aguento mais, penso em desistir de tudo"
        d = rules.evaluate_intake({"igsid": "9437", "text": texto_crise})
        self.assertEqual(d.acao, "escalar")

        resumo = rules.resumo_para_fila(d)
        self.assertNotIn("desistir de tudo", resumo, "texto da pessoa no resumo do caso")
        self.assertNotIn("nao aguento mais", resumo)
        # Mas o resumo precisa ser útil para o time agir.
        self.assertIn("crise_emocional", resumo)
        self.assertIn("P0", resumo)

    def test_caso_aberto_pelo_intake_nao_guarda_o_texto(self) -> None:
        rules.abrir_caso_humano(
            igsid="9437", prioridade="P0", motivo="crise_emocional",
            resumo=rules.resumo_para_fila(
                rules.evaluate_intake({"igsid": "9437", "text": "quero desaparecer"})
            ),
        )
        caso = self.linhas("fila_humana")[0]
        self.assertNotIn("desaparecer", (caso["resumo"] or ""))

    def test_redigir_remove_documento_telefone_email_arroba(self) -> None:
        texto = (
            "meu cpf e 123.456.789-00, telefone 11 98765-4321, "
            "email joao@exemplo.com e meu @joaosilva"
        )
        limpo = rules.redigir(texto)
        for vazamento in ("123.456.789-00", "98765-4321", "joao@exemplo.com", "@joaosilva"):
            self.assertNotIn(vazamento, limpo, f"'{vazamento}' sobreviveu à redação")
        self.assertIn("[cpf]", limpo)
        self.assertIn("[email]", limpo)

    def test_redigir_limita_o_tamanho(self) -> None:
        self.assertLessEqual(len(rules.redigir("palavra " * 500, limite=160)), 160)


# ===========================================================================
# RN-015 — retenção: prazo por tabela, expurgo executado
# ===========================================================================

class TestRN015Retencao(BaseLGPD):
    def test_toda_tabela_de_estado_tem_prazo_definido(self) -> None:
        """Tabela nova sem prazo é dado novo guardado para sempre. Este teste
        existe para doer quando alguém adicionar uma tabela e esquecer disto."""
        with rules.db() as conn:
            tabelas = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        conhecidas = set(rules.RETENCAO_DIAS) | set(rules.RETENCAO_PERMANENTE)
        # `manutencao` guarda o relógio do expurgo e `sqlite_sequence` é do próprio
        # SQLite — nenhum dos dois é dado de pessoa.
        conhecidas |= {"manutencao", "sqlite_sequence"}
        sem_prazo = tabelas - conhecidas
        self.assertFalse(
            sem_prazo,
            f"tabela(s) sem prazo de guarda definido: {sorted(sem_prazo)}",
        )

    def test_prazo_e_positivo_e_faz_sentido(self) -> None:
        for tabela, dias in rules.RETENCAO_DIAS.items():
            with self.subTest(tabela=tabela):
                self.assertIsInstance(dias, int)
                self.assertGreater(dias, 0, f"{tabela}: prazo zero ou negativo")
                self.assertLessEqual(dias, 730, f"{tabela}: prazo maior que 2 anos")

    def test_lead_flags_expiram(self) -> None:
        """Marca sensível não pode durar para sempre: marcar a pessoa como 'em
        crise' dois anos depois é o que a LGPD proíbe."""
        rules.registrar_flag("u1", "crise_emocional", "P0")
        rules.registrar_flag("u2", "crise_emocional", "P0")
        with rules.db() as conn:
            conn.execute(
                "UPDATE lead_flags SET at = ? WHERE igsid = ?",
                (__import__("time").time() - 100 * 86400, "u1"),
            )
        rules.expurgar_expirados()
        self.assertEqual(rules.flags_ativas("u1"), {}, "flag antiga deveria expirar")
        self.assertEqual(rules.flags_ativas("u2"), {"crise_emocional": "P0"})

    def test_caso_aberto_nunca_e_expurgado(self) -> None:
        """Expurgar caso aberto não protegeria o titular — esconderia o
        atendimento que ninguém fez."""
        agora = __import__("time").time()
        rules.abrir_caso_humano(igsid="a", prioridade="P0", motivo="crise", now=agora - 400 * 86400)
        rules.expurgar_expirados()
        self.assertEqual(len(self.linhas("fila_humana")), 1)

    def test_caso_resolvido_e_expurgado_depois_do_prazo(self) -> None:
        agora = __import__("time").time()
        caso = rules.abrir_caso_humano(igsid="a", prioridade="P2", motivo="duvida")
        rules.resolver_caso_humano(caso, resolucao="ok", por="time")
        with rules.db() as conn:
            conn.execute(
                "UPDATE fila_humana SET resolvido_em = ?", (agora - 100 * 86400,)
            )
        rules.expurgar_expirados()
        self.assertEqual(self.linhas("fila_humana"), [])

    def test_opt_out_nunca_e_expurgado(self) -> None:
        """A recusa é a base para NÃO contatar. Apagá-la transformaria opt-out em
        consentimento — o oposto do que a pessoa pediu."""
        rules.add_opt_out("u1", "pediu para sair")
        with rules.db() as conn:
            conn.execute("UPDATE opt_outs SET at = ?", (0.0,))
        rules.expurgar_expirados()
        self.assertTrue(rules.is_opted_out("u1"))

    def test_conteudo_literal_da_pessoa_expira(self) -> None:
        with rules.db() as conn:
            conn.execute(
                "INSERT INTO lacunas (pergunta, igsid, at) VALUES (?,?,?)",
                ("qual a garantia?", "u1", 0.0),
            )
        rules.expurgar_expirados()
        self.assertEqual(self.linhas("lacunas"), [])

    def test_expurgo_e_idempotente(self) -> None:
        primeira = rules.expurgar_expirados()
        segunda = rules.expurgar_expirados()
        self.assertEqual(set(primeira), set(segunda))
        for valores in segunda.values():
            self.assertEqual(valores, 0)

    def test_expurgo_roda_no_maximo_uma_vez_por_dia(self) -> None:
        agora = __import__("time").time()
        with rules.db() as conn:
            conn.execute(
                "INSERT INTO lacunas (pergunta, igsid, at) VALUES (?,?,?)",
                ("pergunta velha", "u1", 0.0),
            )
        primeira = rules.expurgar_se_preciso(now=agora)
        self.assertEqual(primeira["lacunas"], 1)

        # A marca impede a segunda execução dentro do intervalo.
        with rules.db() as conn:
            conn.execute(
                "INSERT INTO lacunas (pergunta, igsid, at) VALUES (?,?,?)",
                ("outra velha", "u1", 0.0),
            )
        self.assertEqual(rules.expurgar_se_preciso(now=agora + 3600), {})
        self.assertEqual(len(self.linhas("lacunas")), 1)

        # Passado o intervalo, roda de novo.
        self.assertEqual(rules.expurgar_se_preciso(now=agora + 25 * 3600)["lacunas"], 1)


# ===========================================================================
# RN-016 — dado sensível: guarda a marca, não o conteúdo
# ===========================================================================

class TestRN016DadoSensivel(BaseLGPD):
    def test_lead_flags_nao_tem_coluna_de_conteudo(self) -> None:
        """A tabela guarda QUAL flag, não o que a pessoa escreveu. Se alguém
        adicionar uma coluna de texto aqui, este teste cai."""
        with rules.db() as conn:
            colunas = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(lead_flags)").fetchall()
            }
        self.assertEqual(colunas, {"igsid", "flag", "severity", "at", "ativo"})

    def test_flag_de_crise_nao_guarda_o_texto_da_crise(self) -> None:
        rules.registrar_flag("u1", "crise_emocional", "P0")
        linha = self.linhas("lead_flags")[0]
        juntos = " ".join(str(v) for v in linha.values()).lower()
        self.assertNotIn("desistir", juntos)
        self.assertNotIn("morrer", juntos)
        self.assertIn("crise_emocional", juntos)

    def test_prazo_de_flag_sensivel_e_menor_que_auditoria(self) -> None:
        """Marca de saúde pode durar menos que prova de autorização humana — a
        segunda é auditoria, a primeira é dado sensível sobre a pessoa."""
        self.assertLess(rules.RETENCAO_DIAS["lead_flags"], rules.RETENCAO_DIAS["aprovacoes"])


# ===========================================================================
# RN-017 — direito do titular: acesso e eliminação
# ===========================================================================

class TestRN017DireitoTitular(BaseLGPD):
    def preparar(self) -> None:
        rules.record_inbound("9437")
        rules.add_opt_out("9437", "pediu para sair")
        rules.registrar_flag("9437", "reclamacao", "P1")
        rules.registrar_toque_proativo("9437", tipo="followup")
        rules.abrir_caso_humano(igsid="9437", prioridade="P1", motivo="reclamacao")
        rules.record_interaction(
            interaction_id="ig:comentario:1", igsid="9437", channel="comentario"
        )

    def test_exportar_traz_o_que_existe_sobre_a_pessoa(self) -> None:
        self.preparar()
        dados = rules.exportar_titular("9437")
        self.assertEqual(dados["igsid"], "9437")
        self.assertEqual(len(dados["tabelas"]["lead_flags"]), 1)
        self.assertEqual(len(dados["tabelas"]["fila_humana"]), 1)
        self.assertEqual(len(dados["tabelas"]["opt_outs"]), 1)
        # O que NÃO entra precisa estar dito, senão quem responde ao titular
        # afirmaria ter entregado tudo.
        self.assertIn("private_replies", dados["nao_incluido"])

    def test_exportar_nao_vaza_dado_de_outra_pessoa(self) -> None:
        self.preparar()
        rules.registrar_flag("outro", "crise_emocional", "P0")
        dados = rules.exportar_titular("9437")
        for linhas in dados["tabelas"].values():
            for linha in linhas:
                self.assertNotIn("outro", str(linha.values()))

    def test_apagar_remove_tudo_o_que_identifica(self) -> None:
        self.preparar()
        rules.apagar_titular("9437", motivo="direito ao esquecimento")
        for tabela, _ in rules._TABELAS_COM_IGSID:
            with self.subTest(tabela=tabela):
                self.assertEqual(
                    [linha for linha in self.linhas(tabela) if linha.get("igsid") == "9437"],
                    [],
                    f"{tabela} ainda guarda o igsid",
                )

    def test_apagar_deixa_o_bloqueio_permanente_por_hash(self) -> None:
        self.preparar()
        resultado = rules.apagar_titular("9437")
        self.assertTrue(resultado["bloqueio_permanente"])
        bloqueios = self.linhas("bloqueios_permanentes")
        self.assertEqual(len(bloqueios), 1)
        esperado = hashlib.sha256(b"9437").hexdigest()
        self.assertEqual(bloqueios[0]["igsid_hash"], esperado)
        self.assertNotIn("9437", str(bloqueios[0].values()))

    def test_depois_de_apagar_nunca_mais_contata(self) -> None:
        """O ponto inteiro da regra: o expurgo não pode virar a causa de uma
        violação. Quem pediu para ser esquecido continua bloqueado."""
        self.preparar()
        rules.apagar_titular("9437")
        self.assertTrue(
            rules.bloqueio_permanente_ativo("9437"),
            "a recusa precisa sobreviver ao apagamento",
        )
        self.assertFalse(rules.pode_contatar_por("9437", "whatsapp").ok)
        self.assertTrue(rules.is_opted_out("9437"))
        bloqueios = rules.avaliar_envio(texto="oi", igsid="9437", canal="email")
        self.assertTrue(bloqueios, "bloqueio permanente não pode ser contornado por canal")

    def test_apagamento_deixa_prova_sem_o_identificador(self) -> None:
        self.preparar()
        rules.apagar_titular("9437", motivo="titular solicitou")
        prova = self.linhas("exclusoes")
        self.assertEqual(len(prova), 1)
        self.assertIn("titular solicitou", prova[0]["motivo"])
        self.assertIn("lead_flags", prova[0]["contagens"])
        self.assertNotIn(
            "9437", str(prova[0].values()),
            "a prova do apagamento não pode guardar o dado apagado",
        )

    def test_apagar_nao_afeta_outra_pessoa(self) -> None:
        self.preparar()
        rules.registrar_flag("vizinho", "crise_emocional", "P0")
        rules.add_opt_out("vizinho", "recusa")
        rules.apagar_titular("9437")
        self.assertEqual(rules.flags_ativas("vizinho"), {"crise_emocional": "P0"})
        self.assertTrue(rules.is_opted_out("vizinho"))

    def test_apagar_id_inexistente_nao_quebra(self) -> None:
        resultado = rules.apagar_titular("nunca-existiu")
        self.assertTrue(resultado["bloqueio_permanente"])
        self.assertEqual(resultado["contagens"]["lead_flags"], 0)


# ===========================================================================
# RN-018 — relatório sem dado pessoal
# ===========================================================================

class TestRN018RelatorioSemDado(BaseLGPD):
    def test_mascarar_id_nao_devolve_o_id(self) -> None:
        mascarado = rules.mascarar_id("9437")
        self.assertNotIn("9437", mascarado)
        self.assertTrue(mascarado.startswith("…"))

    def test_mascara_e_estavel_e_distinta(self) -> None:
        """Estável: o mesmo id dá sempre a mesma máscara, senão o time não
        consegue correlacionar dois relatórios da mesma pessoa."""
        self.assertEqual(rules.mascarar_id("9437"), rules.mascarar_id("9437"))
        self.assertNotEqual(rules.mascarar_id("9437"), rules.mascarar_id("2210"))

    def test_id_vazio_nao_quebra(self) -> None:
        self.assertEqual(rules.mascarar_id(""), "-")

    def test_relatorio_do_vigia_nao_traz_igsid(self) -> None:
        """Integração com o script real: o que sai do vigia é o que vai para o
        Telegram. Se o igsid reaparecer no relatório, este teste cai."""
        import importlib.util

        caminho = RAIZ / "scripts" / "fila-humana-sla.py"
        spec = importlib.util.spec_from_file_location("vigia_sla", caminho)
        vigia = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(vigia)

        agora = __import__("time").time()
        rules.abrir_caso_humano(
            igsid="9437", prioridade="P0", motivo="crise_emocional", now=agora - 3600
        )
        relatorio = vigia.montar_relatorio(registrar_alerta=False)
        self.assertEqual(len(relatorio["atrasados"]), 1)
        caso = relatorio["atrasados"][0]
        self.assertNotIn("9437", str(caso))
        self.assertEqual(caso["igsid"], rules.mascarar_id("9437"))
        self.assertNotIn("9437", vigia.texto(relatorio))
