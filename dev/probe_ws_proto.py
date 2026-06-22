#!/usr/bin/env python3
"""Probe WS protocol: captura a URL do websocket de liveodds e os frames
ENVIADOS (handshake/subscription) + RECEBIDOS, p/ entender como assinar."""
import sys, os, asyncio, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prod"))
import nodriver as uc
from nodriver import cdp
import buscar_odds_flashscore as F

MID = sys.argv[1] if len(sys.argv) > 1 else "Cpq2Y2FE"

ws_created = []     # (request_id, url)
sent = []           # (request_id, payload)
recv = []           # (request_id, payload)


async def main():
    browser = await uc.start(browser_args=["--no-sandbox"], sandbox=False)
    tab = await browser.get("about:blank")
    await F._goto(tab, F.ORIGIN); await asyncio.sleep(2); await F._consent(tab); await asyncio.sleep(1)

    def on_created(ev):
        ws_created.append((str(ev.request_id), ev.url))
    def on_sent(ev):
        try: sent.append((str(ev.request_id), ev.response.payload_data))
        except Exception: pass
    def on_recv(ev):
        try: recv.append((str(ev.request_id), ev.response.payload_data))
        except Exception: pass

    tab.add_handler(cdp.network.WebSocketCreated, on_created)
    tab.add_handler(cdp.network.WebSocketFrameSent, on_sent)
    tab.add_handler(cdp.network.WebSocketFrameReceived, on_recv)
    await tab.send(cdp.network.enable())

    url_jogo = await F._canonical_from_mid(tab, MID)
    await tab.send(cdp.page.navigate(url=url_jogo + "#/resumo-de-jogo/cotacoes-do-jogo/cotacoes-1x2/tempo-integral"))
    for _ in range(18):
        await asyncio.sleep(1)

    print("== WEBSOCKETS CRIADOS ==", flush=True)
    for rid, u in ws_created:
        print(f"  id={rid} url={u}", flush=True)

    print(f"\n== FRAMES ENVIADOS (subscription handshake) — {len(sent)} ==", flush=True)
    for rid, pl in sent[:25]:
        print(f"  [{rid}] {pl[:600]}", flush=True)

    print(f"\n== FRAMES RECEBIDOS com 'liveodds' — amostra ==", flush=True)
    n = 0
    for rid, pl in recv:
        if "liveodds" in pl or "LiveOdds" in pl:
            print(f"  [{rid}] {pl[:400]}", flush=True)
            n += 1
            if n >= 6: break
    print(f"\n[totais] sent={len(sent)} recv={len(recv)}", flush=True)


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
