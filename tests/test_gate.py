"""Testes do gate de envio — a última barreira antes da rede.

Cada teste aqui corresponde a um limite da Meta ou uma regra de política que, se
falhar, causa dano real: mensagem fora da janela não entrega, private reply
duplicada queima a cota única, opt-out ignorado é violação de LGPD.

`_post` é substituído para que nenhum teste toque na rede de verdade — e desde o
achado F01 do parecer OpenClaw os testes conferem ENDPOINT e PAYLOAD, não só que
"houve uma chamada".

NOTA SOBRE `acao=`: desde o achado F04, o caminho das ferramentas exige ação
declarada (RN-009). Estes testes usam `ACAO_OK`, uma ação A3 sempre permitida, para
não misturar o objeto de cada teste com a matriz de autonomia.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "plugins" / "instagram-seller"))

import instagram_api as api  # noqa: E402
import rules  # noqa: E402

# Ação A3 (agente age e reporta): sempre permitida, nunca é o objeto do teste.
ACAO_OK = "responder_elogio"


def _comentario_de(comment_id: str, igsid: str) -> None:
    """Vincula comentário → autor, como o intake faz na vida real (RN-020).

    Sem isto, `send_private_reply`/`reply_comment_public` recusam: a identidade
    passou a ser resolvida pelo registro do comentário, e não mais confiada ao
    chamador. É o comportamento que o achado F02 pediu.
    """
    rules.record_interaction(
        interaction_id=f"ig:comentario:comment_id:{comment_id}",
        igsid=igsid,
        channel="comentario",
        comment_id=comment_id,
        recommended_action="responder",
    )


class GateBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        os.environ["IG_STATE_DB"] = str(self.home / "estado.db")
        os.environ["IG_KILL_SWITCH"] = str(self.home / "ig-kill-switch")
        os.environ["IG_USER_ID"] = "17841400000000000"

        # Nenhum teste pode chegar na rede.
        self.chamadas: list[tuple[str, dict]] = []

        def falso_post(path: str, payload: dict) -> dict:
            self.chamadas.append((path, payload))
            return {"id": "ok"}

        self._post_original = api._post
        api._post = falso_post

    def tearDown(self) -> None:
        api._post = self._post_original
        for var in ("IG_STATE_DB", "IG_KILL_SWITCH", "IG_USER_ID"):
            os.environ.pop(var, None)
        self._tmp.cleanup()

    def enviou(self) -> bool:
        return bool(self.chamadas)


class TestKillSwitch(GateBase):
    def test_kill_switch_bloqueia_dm_e_nao_toca_na_rede(self) -> None:
        rules.record_inbound("u1")
        Path(os.environ["IG_KILL_SWITCH"]).write_text("", encoding="utf-8")
        with self.assertRaises(api.PolicyBlock) as cm:
            api.send_dm("u1", "oi", acao=ACAO_OK)
        self.assertIn("Kill switch", str(cm.exception))
        self.assertFalse(self.enviou(), "kill switch não pode nem tentar a rede")

    def test_kill_switch_bloqueia_resposta_publica(self) -> None:
        Path(os.environ["IG_KILL_SWITCH"]).write_text("", encoding="utf-8")
        _comentario_de("c1", "u1")
        with self.assertRaises(api.PolicyBlock):
            api.reply_comment_public("c1", "obrigado!", acao=ACAO_OK)
        self.assertFalse(self.enviou())

    def test_kill_switch_bloqueia_private_reply_sem_queimar_a_cota(self) -> None:
        rules.record_inbound("u1")
        _comentario_de("c1", "u1")
        Path(os.environ["IG_KILL_SWITCH"]).write_text("", encoding="utf-8")
        with self.assertRaises(api.PolicyBlock):
            api.send_private_reply("c1", "oi", acao=ACAO_OK)
        # A cota NÃO pode ter sido consumida por um envio que nunca saiu.
        self.assertIsNone(rules.private_reply_age("c1"))


class TestJanela(GateBase):
    def test_dm_sem_inbound_e_bloqueada(self) -> None:
        """Sem o usuário ter falado, a janela não existe. O Instagram não deixa enviar."""
        with self.assertRaises(api.PolicyBlock) as cm:
            api.send_dm("nunca_falou", "oi", acao=ACAO_OK)
        self.assertIn("24h", str(cm.exception))
        self.assertFalse(self.enviou())

    def test_dm_apos_inbound_passa(self) -> None:
        rules.record_inbound("u2")
        api.send_dm("u2", "tudo bem?", acao=ACAO_OK)
        self.assertTrue(self.enviou())

    def test_dm_com_janela_expirada_e_bloqueada(self) -> None:
        rules.record_inbound("u3", time.time() - 25 * 3600)
        with self.assertRaises(api.PolicyBlock):
            api.send_dm("u3", "oi", acao=ACAO_OK)
        self.assertFalse(self.enviou())


class TestRotaPrivadaVersusPublica(GateBase):
    """Achado F01 do parecer OpenClaw: as duas funções faziam a MESMA chamada.

    `POST /{comment_id}/replies` cria um IGComment — comentário PÚBLICO. Ou seja: o
    que o agente tratava como privado era publicado no post. Dar nomes diferentes às
    funções não mudava o destino da chamada.

    Estes testes travam endpoint e payload, que é exatamente o que o nome da função
    não garantia na revisão.
    """

    def test_privada_usa_messages_com_recipient_comment_id(self) -> None:
        rules.record_inbound("u4")
        _comentario_de("c10", "u4")
        api.send_private_reply("c10", "te mandei no direct", acao=ACAO_OK)
        caminho, payload = self.chamadas[-1]
        self.assertEqual(caminho, f"{os.environ['IG_USER_ID']}/messages")
        self.assertEqual(payload["recipient"], {"comment_id": "c10"})
        self.assertEqual(payload["message"], {"text": "te mandei no direct"})

    def test_publica_usa_replies_com_message(self) -> None:
        rules.record_inbound("u9")
        _comentario_de("c30", "u9")
        api.reply_comment_public("c30", "obrigado!", acao=ACAO_OK)
        caminho, payload = self.chamadas[-1]
        self.assertEqual(caminho, "c30/replies")
        self.assertEqual(payload, {"message": "obrigado!"})

    def test_os_dois_destinos_sao_diferentes(self) -> None:
        """O teste que teria pegado o F01 na primeira revisão: se as duas chamadas
        são idênticas, uma delas está no lugar errado."""
        rules.record_inbound("u4")
        _comentario_de("c10", "u4")
        api.send_private_reply("c10", "conteudo privado", acao=ACAO_OK)
        privado = self.chamadas[-1]
        self.chamadas.clear()
        _comentario_de("c11", "u4")
        api.reply_comment_public("c11", "conteudo publico", acao=ACAO_OK)
        publico = self.chamadas[-1]
        self.assertNotEqual(privado[0], publico[0], "endpoints iguais = F01 de volta")
        self.assertNotEqual(privado[1], publico[1], "payloads iguais = F01 de volta")


class TestPrivateReply(GateBase):
    def test_primeira_private_reply_passa(self) -> None:
        rules.record_inbound("u4")
        _comentario_de("c10", "u4")
        api.send_private_reply("c10", "te mandei no direct", igsid="u4", acao=ACAO_OK)
        self.assertTrue(self.enviou())

    def test_segunda_private_reply_no_mesmo_comentario_e_bloqueada(self) -> None:
        """São 1 por comentário. Não existe retry — e a que falha também queima."""
        rules.record_inbound("u4")
        _comentario_de("c11", "u4")
        api.send_private_reply("c11", "primeira", igsid="u4", acao=ACAO_OK)
        self.chamadas.clear()
        with self.assertRaises(api.PolicyBlock) as cm:
            api.send_private_reply("c11", "de novo", igsid="u4", acao=ACAO_OK)
        self.assertIn("já consumida", str(cm.exception))
        self.assertFalse(self.enviou())

    def test_private_reply_funciona_fora_da_janela_de_24h(self) -> None:
        """É justamente para isso que ela existe: alcançar quem comentou até 7 dias."""
        rules.record_inbound("u5", time.time() - 30 * 3600)
        _comentario_de("c12", "u5")
        api.send_private_reply("c12", "oi", igsid="u5", acao=ACAO_OK)
        self.assertTrue(self.enviou())


class TestVinculoDeIdentidade(GateBase):
    """Achado F02: os handlers passavam identidade VAZIA.

    O parâmetro tinha valor vazio por padrão e as verificações eram puladas em
    silêncio (`if igsid:`), não bloqueadas. O motor conhecia a restrição e a
    ferramenta chegava sem o vínculo para aplicá-la.
    """

    def test_comentario_sem_registro_e_recusado(self) -> None:
        rules.record_inbound("u4")
        with self.assertRaises(api.PolicyBlock) as cm:
            api.send_private_reply("comentario_desconhecido", "oi", acao=ACAO_OK)
        self.assertIn(rules.RN_VINCULO_IDENTIDADE, str(cm.exception))
        self.assertFalse(self.enviou())

    def test_igsid_declarado_que_nao_e_o_dono_e_recusado(self) -> None:
        """Critério T06: comentário de terceiro não é alvo."""
        rules.record_inbound("dono")
        _comentario_de("c50", "dono")
        with self.assertRaises(api.PolicyBlock) as cm:
            api.send_private_reply("c50", "oi", igsid="outra_pessoa", acao=ACAO_OK)
        self.assertIn("alvo_de_outra_pessoa", str(cm.exception))
        self.assertFalse(self.enviou())

    def test_autor_resolvido_pelo_comentario_aplica_optout(self) -> None:
        """O caso que antes era impossível: o chamador NÃO passa igsid nenhum, e a
        proteção por PESSOA continua valendo — porque o autor vem do registro.

        A primeira despedida é a única concessão (e é por isso que o vínculo precisa
        existir: sem ele, a segunda tentativa passaria)."""
        _comentario_de("c51", "com_optout")
        rules.add_opt_out("com_optout", "pediu para parar")
        api.send_private_reply("c51", "Entendido, obrigado pelo contato.", acao=ACAO_OK)
        self.assertTrue(self.enviou())

        self.chamadas.clear()
        _comentario_de("c53", "com_optout")
        with self.assertRaises(api.PolicyBlock) as cm:
            api.send_private_reply("c53", "aproveita a promoção", acao=ACAO_OK)
        self.assertIn("Opt-out registrado", str(cm.exception))
        self.assertFalse(self.enviou())

    def test_optout_do_autor_bloqueia_resposta_publica(self) -> None:
        _comentario_de("c52", "com_optout")
        rules.add_opt_out("com_optout", "pediu para parar")
        with self.assertRaises(api.PolicyBlock):
            api.reply_comment_public("c52", "oi", acao=ACAO_OK)
        self.assertFalse(self.enviou())

    def test_kill_switch_tem_prioridade_sobre_o_vinculo(self) -> None:
        """Ordem importa para o operador: "parada de emergência ligada" é a
        informação útil, não "comentário sem autor registrado"."""
        Path(os.environ["IG_KILL_SWITCH"]).write_text("", encoding="utf-8")
        with self.assertRaises(api.PolicyBlock) as cm:
            api.send_private_reply("comentario_qualquer", "oi", acao=ACAO_OK)
        self.assertIn("Kill switch", str(cm.exception))


class TestAcaoObrigatoria(GateBase):
    """Achado F04: `acao` opcional deixava a matriz de autonomia desligável por
    omissão. Quem pede autorização não decide quais permissões serão verificadas."""

    def test_dm_sem_acao_e_recusada(self) -> None:
        rules.record_inbound("u11")
        bloqueios = rules.avaliar_envio(texto="oi", igsid="u11", exige_acao=True)
        self.assertTrue(bloqueios)
        self.assertEqual(bloqueios[0].regra, rules.RN_MATRIZ_AUTONOMIA)

    def test_acao_desconhecida_e_recusada(self) -> None:
        """Vocabulário fechado: rótulo inventado não tem nível de autonomia — e o que
        não tem nível não passa por omissão."""
        bloqueios = rules.avaliar_envio(texto="oi", igsid="u11", acao="dar_desconto_de_50")
        self.assertEqual(bloqueios[0].regra, rules.RN_MATRIZ_AUTONOMIA)
        self.assertIn("vocabulário fechado", str(bloqueios[0]))

    def test_acao_real_da_matriz_continua_avaliada(self) -> None:
        """A ação A0 continua barrada pelo MOTIVO certo (a matriz), não pela
        validação de vocabulário."""
        bloqueios = rules.avaliar_envio(texto="te dou 50% off", igsid="u11", acao="desconto")
        self.assertEqual(bloqueios[0].regra, rules.RN_MATRIZ_AUTONOMIA)
        self.assertNotIn("vocabulário fechado", str(bloqueios[0]))


class TestResultadoIncerto(GateBase):
    """RN-021: timeout não prova que saiu nem que não saiu.

    Antes, a cota era marcada ANTES da chamada — tratando falha de rede como envio
    consumado. O parecer pediu o contrário: distinguir recusa confirmada de resultado
    desconhecido, e nunca repetir no escuro.
    """

    def _post_que_cai(self) -> None:
        def cai(path: str, payload: dict) -> dict:
            self.chamadas.append((path, payload))
            raise RuntimeError("timeout depois do envio")

        api._post = cai

    def test_timeout_nao_marca_a_cota_e_abre_pendencia(self) -> None:
        rules.record_inbound("u4")
        _comentario_de("c60", "u4")
        self._post_que_cai()
        with self.assertRaises(api.ResultadoIncerto):
            api.send_private_reply("c60", "oi", acao=ACAO_OK)
        self.assertIsNone(
            rules.private_reply_age("c60"), "não sei se saiu != saiu: não marca a cota"
        )
        self.assertTrue(rules.reconciliacao_pendente(comment_id="c60"))

    def test_nao_repete_no_escuro(self) -> None:
        rules.record_inbound("u4")
        _comentario_de("c61", "u4")
        self._post_que_cai()
        with self.assertRaises(api.ResultadoIncerto):
            api.send_private_reply("c61", "oi", acao=ACAO_OK)
        with self.assertRaises(api.PolicyBlock) as cm:
            api.send_private_reply("c61", "oi de novo", acao=ACAO_OK)
        self.assertIn(rules.RN_RECONCILIACAO, str(cm.exception))

    def test_recusa_confirmada_marca_a_cota(self) -> None:
        """Recusa 4xx da Meta é resultado CONHECIDO: a cota foi consumida de fato."""

        def recusa(path: str, payload: dict) -> dict:
            self.chamadas.append((path, payload))
            raise RuntimeError("Graph API 400 em /messages: parametro invalido")

        rules.record_inbound("u4")
        _comentario_de("c62", "u4")
        api._post = recusa
        with self.assertRaises(RuntimeError):
            api.send_private_reply("c62", "oi", acao=ACAO_OK)
        self.assertIsNotNone(
            rules.private_reply_age("c62"), "recusa confirmada queima a cota"
        )
        self.assertFalse(rules.reconciliacao_pendente(comment_id="c62"))

    def test_resolver_como_nao_enviado_libera_nova_tentativa(self) -> None:
        rules.record_inbound("u4")
        _comentario_de("c63", "u4")
        self._post_que_cai()
        with self.assertRaises(api.ResultadoIncerto):
            api.send_private_reply("c63", "oi", acao=ACAO_OK)

        pendencia = rules.reconciliacoes_abertas()[0]
        self.assertEqual(pendencia["comment_id"], "c63")
        self.assertEqual(
            rules.resolver_reconciliacao(pendencia["id"], desfecho="nao_enviado", por="plantao"),
            1,
        )
        self.assertFalse(rules.reconciliacao_pendente(comment_id="c63"))

        def sucesso(path: str, payload: dict) -> dict:
            self.chamadas.append((path, payload))
            return {"id": "ok"}

        api._post = sucesso
        self.chamadas.clear()
        api.send_private_reply("c63", "oi", acao=ACAO_OK)
        self.assertTrue(self.enviou())


class TestOptOutNoGate(GateBase):
    def test_optout_permite_exatamente_uma_despedida(self) -> None:
        """Política: agradecer em uma linha e encerrar para sempre."""
        rules.record_inbound("u6")
        rules.add_opt_out("u6", "pediu para parar")

        api.send_dm("u6", "Entendido, obrigado pelo contato.", acao=ACAO_OK)
        self.assertTrue(self.enviou())

        self.chamadas.clear()
        with self.assertRaises(api.PolicyBlock) as cm:
            api.send_dm("u6", "mas espera, tenho uma promoção", acao=ACAO_OK)
        self.assertIn("Opt-out registrado", str(cm.exception))
        self.assertFalse(self.enviou(), "depois da despedida, silêncio permanente")

    def test_optout_bloqueia_resposta_publica_mesmo_antes_da_despedida(self) -> None:
        """A concessão da despedida é só PRIVADA. Engajar em público quem pediu para
        ser deixado em paz repete a exposição na frente de todos."""
        rules.add_opt_out("u7", "pediu para parar")
        _comentario_de("c20", "u7")
        with self.assertRaises(api.PolicyBlock):
            api.reply_comment_public("c20", "oi", igsid="u7", acao=ACAO_OK)
        self.assertFalse(self.enviou())
        # E a concessão privada continua intacta.
        self.assertTrue(rules.pode_enviar_despedida("u7"))

    def test_quem_nao_pediu_parar_nao_e_afetado(self) -> None:
        rules.record_inbound("u8")
        api.send_dm("u8", "oi", acao=ACAO_OK)
        api.send_dm("u8", "tudo bem?", acao=ACAO_OK)
        self.assertEqual(len(self.chamadas), 2)


class TestAuditoria(GateBase):
    def _interacao(self, **kw) -> str:
        padrao = dict(interaction_id="i-teste", igsid="u9", comment_id="c30", channel="comentario")
        padrao.update(kw)
        rules.record_interaction(**padrao)
        return padrao["interaction_id"]

    def _linha(self, interaction_id: str) -> dict:
        with rules.db() as conn:
            return dict(
                conn.execute(
                    "SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)
                ).fetchone()
            )

    def test_envio_confirmado_grava_executed_action(self) -> None:
        """PDF §13: recomendada, aprovada e executada são campos distintos — e o
        último só existe depois que o envio realmente saiu."""
        iid = self._interacao()
        rules.record_inbound("u9")
        self.assertIsNone(self._linha(iid)["executed_action"])

        api.send_private_reply("c30", "oi", igsid="u9", acao=ACAO_OK)

        linha = self._linha(iid)
        self.assertEqual(linha["executed_action"], "ig_private_reply")
        self.assertEqual(linha["status"], "enviada")

    def test_envio_bloqueado_nao_marca_execucao(self) -> None:
        iid = self._interacao()
        rules.record_inbound("u9")
        Path(os.environ["IG_KILL_SWITCH"]).write_text("", encoding="utf-8")
        with self.assertRaises(api.PolicyBlock):
            api.send_private_reply("c30", "oi", igsid="u9", acao=ACAO_OK)
        self.assertIsNone(self._linha(iid)["executed_action"])

    def test_acao_recomendada_sobrevive_ao_envio(self) -> None:
        iid = self._interacao(recommended_action="responder_publico")
        rules.record_inbound("u9")
        api.reply_comment_public("c30", "obrigado!", igsid="u9", acao=ACAO_OK)
        linha = self._linha(iid)
        self.assertEqual(linha["recommended_action"], "responder_publico")
        self.assertEqual(linha["executed_action"], "ig_reply_comment")


class TestMensagemVazia(GateBase):
    def test_texto_vazio_e_recusado_antes_de_qualquer_coisa(self) -> None:
        rules.record_inbound("u10")
        for vazio in ("", "   ", "\n"):
            with self.subTest(repr(vazio)):
                with self.assertRaises(api.PolicyBlock):
                    api.send_dm("u10", vazio, acao=ACAO_OK)
        self.assertFalse(self.enviou())


if __name__ == "__main__":
    unittest.main(verbosity=2)
