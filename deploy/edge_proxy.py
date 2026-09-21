#!/usr/bin/env python3
"""
Proxy de borda para o webhook da Meta.

POR QUE ISSO EXISTE
-------------------
O webhook adapter do Hermes aceita apenas POST.
A Meta exige um GET de verificacao (hub.mode / hub.verify_token / hub.challenge)
antes de aceitar a URL do webhook. Sem responder esse GET, nao ha cadastro.

  GET  /webhooks/instagram  -> responde o hub.challenge em texto puro
  POST /webhooks/instagram  -> encaminha para o Hermes :8644, corpo INTACTO

O corpo tem que chegar byte a byte igual ao que a Meta enviou, senao a
assinatura X-Hub-Signature-256 nao valida no Hermes.

USO
---
    # variaveis de ambiente
    META_VERIFY_TOKEN=seu-token-forte
    HERMES_WEBHOOK_URL=http://127.0.0.1:8644/webhooks/instagram   (padrao)
    EDGE_PORT=8080                                                (padrao)

    python spike/edge_proxy.py

PRODUCAO
--------
O `Caddyfile` em spike/1-handshake/ faz o mesmo com TLS automatico.
Este script e a versao sem dependencia externa - serve para o spike e como
plano B se o cliente nao quiser instalar Caddy.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEBHOOK_PATH = "/webhooks/instagram"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class EdgeHandler(BaseHTTPRequestHandler):
    server_version = "SocialSellerEdge/0.1"

    # ------------------------------------------------------------------
    # GET - handshake da Meta
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != WEBHOOK_PATH:
            self._text(404, "not found")
            return

        params = urllib.parse.parse_qs(parsed.query)
        mode = (params.get("hub.mode") or [""])[0]
        token = (params.get("hub.verify_token") or [""])[0]
        challenge = (params.get("hub.challenge") or [""])[0]

        if mode != "subscribe":
            self._log("GET rejeitado: hub.mode != subscribe")
            self._text(403, "forbidden")
            return

        # Comparacao em tempo constante - nao vaza o token por timing.
        if not hmac.compare_digest(token, self.server.verify_token):  # type: ignore[attr-defined]
            self._log("GET rejeitado: verify_token invalido")
            self._text(403, "forbidden")
            return

        if not challenge:
            self._log("GET rejeitado: hub.challenge ausente")
            self._text(400, "missing challenge")
            return

        # Ecoar o challenge em texto puro, HTTP 200. E so isso que a Meta quer.
        self._log(f"GET handshake OK (challenge com {len(challenge)} chars)")
        self._text(200, challenge)

    # ------------------------------------------------------------------
    # POST - eventos da Meta
    # ------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != WEBHOOK_PATH:
            self._text(404, "not found")
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        # Repassar a assinatura como veio. O Hermes valida com o App Secret.
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        for h in ("X-Hub-Signature-256", "X-Hub-Signature"):
            if self.headers.get(h):
                headers[h] = self.headers[h]

        upstream = self.server.upstream_url  # type: ignore[attr-defined]
        req = urllib.request.Request(upstream, data=body, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = resp.read()
                code = resp.getcode()
        except urllib.error.HTTPError as e:
            payload = e.read()
            code = e.code
        except Exception as e:  # noqa: BLE001
            self._log(f"POST falhou ao encaminhar: {e}")
            # 502 faz a Meta reentregar - melhor que engolir o evento.
            self._text(502, "upstream unavailable")
            return

        self._log(f"POST encaminhado -> upstream {code} ({len(body)} bytes)")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ------------------------------------------------------------------
    def _text(self, code: int, text: str) -> None:
        raw = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _log(self, msg: str) -> None:
        if getattr(self.server, "quiet", False):
            return
        print(f"[edge] {msg}", file=sys.stderr)

    def log_message(self, fmt: str, *args) -> None:  # silencia o log padrao
        if getattr(self.server, "quiet", False):
            return
        self._log(fmt % args)


def build_server(
    port: int,
    verify_token: str,
    upstream_url: str,
    quiet: bool = False,
) -> ThreadingHTTPServer:
    """Cria (sem iniciar) o servidor de borda. Usado pelo spike e pelos testes."""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), EdgeHandler)
    httpd.verify_token = verify_token        # type: ignore[attr-defined]
    httpd.upstream_url = upstream_url        # type: ignore[attr-defined]
    httpd.quiet = quiet                      # type: ignore[attr-defined]
    return httpd


def main() -> int:
    ap = argparse.ArgumentParser(description="Proxy de borda do webhook da Meta")
    ap.add_argument("--port", type=int, default=int(_env("EDGE_PORT", "8080")))
    ap.add_argument("--verify-token", default=_env("META_VERIFY_TOKEN"))
    ap.add_argument(
        "--upstream",
        default=_env("HERMES_WEBHOOK_URL", "http://127.0.0.1:8644" + WEBHOOK_PATH),
    )
    args = ap.parse_args()

    if not args.verify_token:
        print("ERRO: defina META_VERIFY_TOKEN ou use --verify-token", file=sys.stderr)
        return 2

    httpd = build_server(args.port, args.verify_token, args.upstream)
    print(f"[edge] ouvindo em http://127.0.0.1:{args.port}{WEBHOOK_PATH}")
    print(f"[edge] encaminhando POST para {args.upstream}")
    print("[edge] Ctrl+C para parar")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[edge] parado")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
