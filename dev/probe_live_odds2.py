#!/usr/bin/env python3
"""Probe 2: inspeciona o feed de odds CRU de jogos status=2 (ao vivo de verdade)."""
import sys, os, json, asyncio, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prod"))
import nodriver as uc
from nodriver import cdp
import buscar_odds_flashscore as F

NINJA = "https://global.flashscore.ninja/401/x/feed/{f}"
GQL = "https://global.ds.lsapp.eu/odds/pq_graphql?_hash=oce&eventId={mid}&projectId=401&geoIpCode=BR&geoIpSubdivisionCode=BRSP"

async def fetch_in_page(tab, url, sign=False):
    hdr = '{"x-fsign": "%s"}' % F.FSIGN if sign else "{}"
    expr = '(async()=>{try{const r=await fetch(%r,{headers:%s});return await r.text();}catch(e){return "ERR:"+e;}})()' % (url, hdr)
    return F._unwrap(await tab.evaluate(expr, await_promise=True)) or ""

async def main():
    browser = await uc.start(browser_args=["--no-sandbox"], sandbox=False)
    tab = await browser.get("about:blank")
    await F._goto(tab, F.ORIGIN); await asyncio.sleep(2); await F._consent(tab); await asyncio.sleep(1)
    hoje = datetime.date.today().isoformat()
    jogos = await F.listar_jogos_data(tab, hoje)
    lives = [j for j in jogos if (j.get("status") in {"2"})]
    print(f"[feed] {len(lives)} jogos status=2 (ao vivo)", flush=True)
    for j in lives:
        print(f"  mid={j['mid']} {j['home']} {j.get('placar')} {j['away']} | {j.get('liga')}", flush=True)
    if not lives:
        print("[!] sem status=2 agora", flush=True); return

    for j in lives:
        mid = j["mid"]
        print(f"\n===== {j['home']} x {j['away']} (mid={mid}) =====", flush=True)
        # feed de odds detail (df_dos) cru, duas vezes 25s apart
        t1 = await fetch_in_page(tab, NINJA.format(f=f"df_dos_1_{mid}_"), sign=True)
        g1 = await fetch_in_page(tab, GQL.format(mid=mid))
        print(f"[df_dos] len={len(t1)} | head: {t1[:400]!r}", flush=True)
        print(f"[graphql] len={len(g1)} | head: {g1[:600]!r}", flush=True)
        # procura marcadores de live
        for tag in ["live", "Live", "isLive", "inplay", "in-play", "AO VIVO"]:
            if tag in t1 or tag in g1:
                print(f"   marcador encontrado: {tag!r}", flush=True)
        await asyncio.sleep(25)
        t2 = await fetch_in_page(tab, NINJA.format(f=f"df_dos_1_{mid}_"), sign=True)
        g2 = await fetch_in_page(tab, GQL.format(mid=mid))
        print(f"[df_dos] mudou em 25s? {t1 != t2} (len {len(t1)}->{len(t2)})", flush=True)
        print(f"[graphql] mudou em 25s? {g1 != g2} (len {len(g1)}->{len(g2)})", flush=True)

if __name__ == "__main__":
    uc.loop().run_until_complete(main())
