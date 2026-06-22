#!/usr/bin/env python3
"""Probe 3: disseca o GraphQL findOddsByEventId — onde esta o 1X2 live e quantas casas."""
import sys, os, json, asyncio, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prod"))
import nodriver as uc
import buscar_odds_flashscore as F

GQL = "https://global.ds.lsapp.eu/odds/pq_graphql?_hash=oce&eventId={mid}&projectId=401&geoIpCode=BR&geoIpSubdivisionCode=BRSP"

async def fp(tab, url):
    expr = '(async()=>{try{const r=await fetch(%r);return await r.text();}catch(e){return "ERR:"+e;}})()' % url
    return F._unwrap(await tab.evaluate(expr, await_promise=True)) or ""

def walk_keys(o, prefix="", out=None, depth=0):
    if out is None: out = set()
    if depth > 6: return out
    if isinstance(o, dict):
        for k,v in o.items():
            out.add(k); walk_keys(v, prefix+"."+k, out, depth+1)
    elif isinstance(o, list) and o:
        walk_keys(o[0], prefix+"[]", out, depth+1)
    return out

async def main():
    browser = await uc.start(browser_args=["--no-sandbox"], sandbox=False)
    tab = await browser.get("about:blank")
    await F._goto(tab, F.ORIGIN); await asyncio.sleep(2); await F._consent(tab); await asyncio.sleep(1)
    hoje = datetime.date.today().isoformat()
    jogos = await F.listar_jogos_data(tab, hoje)
    lives = [j for j in jogos if j.get("status")=="2"]
    if not lives:
        print("[!] sem live agora"); return
    j = lives[-1]  # o que mudou no probe2 (Khovd) tende a ter mercado ativo
    mid = j["mid"]
    print(f"[alvo] {j['home']} x {j['away']} mid={mid}", flush=True)
    raw = await fp(tab, GQL.format(mid=mid))
    d = json.loads(raw)["data"]["findOddsByEventId"]
    print("[top-level keys]", list(d.keys()), flush=True)
    print("[all keys (depth<=6)]", sorted(walk_keys(d)), flush=True)
    # tenta achar a lista de mercados
    od = d.get("odds") or d.get("oddsData") or []
    print(f"\n[markets] tipo={type(od).__name__} n={len(od) if hasattr(od,'__len__') else '?'}", flush=True)
    # imprime estrutura resumida de cada mercado
    def short(x, n=300):
        s = json.dumps(x, ensure_ascii=False)
        return s[:n]
    if isinstance(od, list):
        for m in od[:12]:
            nm = m.get("name") or m.get("bettingType") or m.get("betName") or m.get("__typename")
            sub = m.get("odds") or m.get("oddsValues") or m.get("bookmakers") or m
            cnt = len(sub) if hasattr(sub,'__len__') else '?'
            print(f"  - market={nm} subitems={cnt}", flush=True)
    # dump 1 mercado inteiro pra ver odd live
    print("\n[RAW primeiro mercado]:", short(od[0], 1500) if isinstance(od,list) and od else short(d,1500), flush=True)

if __name__ == "__main__":
    uc.loop().run_until_complete(main())
