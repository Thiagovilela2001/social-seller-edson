"""Testes do gate de envio — a última barreira antes da rede.

Cada teste aqui corresponde a um limite da Meta ou uma regra de política que, se
falhar, causa dano real: mensagem fora da janela não entrega, private reply
duplicada queima a cota única, opt-out ignorado é violação de LGPD.

`_post` é substituído para que nenhum teste toque na rede de verdade.
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
            api.send_dm("u1", "oi")
        self.assertIn("Kill switch", str(cm.exception))
        self.assertFalse(self.enviou(), "kill switch não pode nem tentar a rede")

    def test_kill_switch_bloqueia_resposta_publica(self) -> None:
        Path(os.environ["IG_KILL_SWITCH"]).write_text("", encoding="utf-8")
        with self.assertRaises(api.PolicyBlock):
            api.reply_comment_public("c1", "obrigado!")
        self.assertFalse(self.enviou())

    def test_kill_switch_bloqueia_private_reply_sem_queimar_a_cota(self) -> None:
        rules.record_inbound("u1")
        Path(os.environ["IG_KILL_SWITCH"]).write_text("", encoding="utf-8")
        with self.assertRaises(api.PolicyBlock):
            api.send_private_reply("c1", "oi", igsid="u1")
        # A cota NÃO pode ter sido consumida por um envio que nunca saiu.
        self.assertIsNone(rules.private_reply_age("c1"))


class TestJanela(GateBase):
    def test_dm_sem_inbound_e_bloqueada(self) -> None:
        """Sem o usuário ter falado, a janela não existe. O Instagram não deixa enviar."""
        with self.assertRaises(api.PolicyBlock) as cm:
            api.send_dm("nunca_falou", "oi")
        self.assertIn("24h", str(cm.exception))
        self.assertFalse(self.enviou())

    def test_dm_apos_inbound_passa(self) -> None:
        rules.record_inbound("u2")
        api.send_dm("u2", "tudo bem?")
        self.assertTrue(self.enviou())

    def test_dm_com_janela_expirada_e_bloqueada(self) -> None:
        rules.record_inbound("u3", time.time() - 25 * 3600)
        with self.assertRaises(api.PolicyBlock):
            api.send_dm("u3", "oi")
        self.assertFalse(self.enviou())


class TestPrivateReply(GateBase):
    def test_primeira_private_reply_passa(self) -> None:
        rules.record_inbound("u4")
        api.send_private_reply("c10", "te mandei no direct", igsid="u4")
        self.assertTrue(self.enviou())

    def test_segunda_private_reply_no_mesmo_comentario_e_bloqueada(self) -> None:
        """São 1 por comentário. Não existe retry — e a que falha também queima."""
        rules.record_inbound("u4")
        api.send_private_reply("c11", "primeira", igsid="u4")
        self.chamadas.clear()
        with self.assertRaises(api.PolicyBlock) as cm:
            api.send_private_reply("c11", "de novo", igsid="u4")
        self.assertIn("já consumida", str(cm.exception))
        self.assertFalse(self.enviou())

    def test_private_reply_funciona_fora_da_janela_de_24h(self) -> None:
        """É justamente para isso que ela existe: alcançar quem comentou até 7 dias."""
        rules.record_inbound("u5", time.time() - 30 * 3600)
        api.send_private_reply("c12", "oi", igsid="u5")
        self.assertTrue(self.enviou())


class TestOptOutNoGate(GateBase):
    def test_optout_permite_exatamente_uma_despedida(self) -> None:
        """Política: agradecer em uma linha e encerrar para sempre."""
        rules.record_inbound("u6")
        rules.add_opt_out("u6", "pediu para parar")

        api.send_dm("u6", "Entendido, obrigado pelo contato.")
        self.assertTrue(self.enviou())

        self.chamadas.clear()
        with self.assertRaises(api.PolicyBlock) as cm:
            api.send_dm("u6", "mas espera, tenho uma promoção")
        self.assertIn("Opt-out registrado", str(cm.exception))
        self.assertFalse(self.enviou(), "depois da despedida, silêncio permanente")

    def test_optout_bloqueia_resposta_publica_mesmo_antes_da_despedida(self) -> None:
        """A concessão da despedida é só PRIVADA. Engajar em público quem pediu para
        ser deixado em paz repete a exposição na frente de todos."""
        rules.add_opt_out("u7", "pediu para parar")
        with self.assertRaises(api.PolicyBlock):
            api.reply_comment_public("c20", "oi", igsid="u7")
        self.assertFalse(self.enviou())
        # E a concessão privada continua intacta.
        self.assertTrue(rules.pode_enviar_despedida("u7"))

    def test_quem_nao_pediu_parar_nao_e_afetado(self) -> None:
        rules.record_inbound("u8")
        api.send_dm("u8", "oi")
        api.send_dm("u8", "tudo bem?")
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

        api.send_private_reply("c30", "oi", igsid="u9")

        linha = self._linha(iid)
        self.assertEqual(linha["executed_action"], "ig_private_reply")
        self.assertEqual(linha["status"], "enviada")

    def test_envio_bloqueado_nao_marca_execucao(self) -> None:
        iid = self._interacao()
        rules.record_inbound("u9")
        Path(os.environ["IG_KILL_SWITCH"]).write_text("", encoding="utf-8")
        with self.assertRaises(api.PolicyBlock):
            api.send_private_reply("c30", "oi", igsid="u9")
        self.assertIsNone(self._linha(iid)["executed_action"])

    def test_acao_recomendada_sobrevive_ao_envio(self) -> None:
        iid = self._interacao(recommended_action="responder_publico")
        rules.record_inbound("u9")
        api.reply_comment_public("c30", "obrigado!", igsid="u9")
        linha = self._linha(iid)
        self.assertEqual(linha["recommended_action"], "responder_publico")
        self.assertEqual(linha["executed_action"], "ig_reply_comment")


class TestMensagemVazia(GateBase):
    def test_texto_vazio_e_recusado_antes_de_qualquer_coisa(self) -> None:
        rules.record_inbound("u10")
        for vazio in ("", "   ", "\n"):
            with self.subTest(repr(vazio)):
                with self.assertRaises(api.PolicyBlock):
                    api.send_dm("u10", vazio)
        self.assertFalse(self.enviou())


if __name__ == "__main__":
    unittest.main(verbosity=2)
