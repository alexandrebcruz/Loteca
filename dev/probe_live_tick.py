#!/usr/bin/env python3
"""Probe: a odd 'ao vivo' do Flashscore (findOddsByEventId) realmente TICA?
Bate 2x no GraphQL ~40s apart p/ um mid fixo e compara value/opening/active
por casa. Mostra também quantas têm hasLiveBettingOffers e a 1ª casa crua."""
import sys, os, json, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prod"))
import nodriver as uc
import buscar_odds_flashscore as F

MID = sys.argv[1] if len(sys.argv) > 1 else "Cpq2Y2FE"
GQL = ("https://global.ds.lsapp.eu/odds/pq_graphql?_hash=oce&eventId={mid}"
       "&projectId=401&geoIpCode=BR&geoIpSubdivisionCode=BRSP")


async def fp(tab, url):
    expr = ("(async()=>{try{const r=await fetch(%r);return await r.text();}"
            "catch(e){return '';}})()" % url)
    return F._unwrap(await tab.evaluate(expr, await_promise=True)) or ""


def snap(raw):
    """-> {bookmaker_id: {'live':bool, 'items':[(pid,value,opening,active)]}} p/ 1X2 FT."""
    node = json.loads(raw)["data"]["findOddsByEventId"]
    out = {}
    for m in (node.get("odds") or []):
        if m.get("bettingType") != "HOME_DRAW_AWAY" or m.get("bettingScope") != "FULL_TIME":
            continue
        bid = m.get("bookmakerId")
        items = [(it.get("eventParticipantId"), it.get("value"),
                  it.get("opening"), it.get("active")) for it in (m.get("odds") or [])]
        out[bid] = {"live": bool(m.get("hasLiveBettingOffers")), "items": items}
    return out


async def main():
    browser = await uc.start(browser_args=["--no-sandbox"], sandbox=False)
    tab = await browser.get("about:blank")
    await F._goto(tab, F.ORIGIN); await asyncio.sleep(2); await F._consent(tab); await asyncio.sleep(1)

    url = GQL.format(mid=MID)
    raw1 = await fp(tab, url)
    s1 = snap(raw1)
    live_ids = [b for b, v in s1.items() if v["live"]]
    print(f"[snap1] {len(s1)} casas 1X2-FT, {len(live_ids)} com hasLiveBettingOffers", flush=True)

    print("[espera 40s...]", flush=True)
    await asyncio.sleep(40)

    raw2 = await fp(tab, url)
    s2 = snap(raw2)
    print(f"[snap2] {len(s2)} casas, payload mudou byte-a-byte? {raw1 != raw2}", flush=True)

    print("\n== comparacao por casa (so as live) ==", flush=True)
    mudou_alguma = False
    for b in live_ids:
        a = s1.get(b, {}).get("items") or []
        c = s2.get(b, {}).get("items") or []
        # value por posicao
        va = [x[1] for x in a]; vc = [x[1] for x in c]
        oa = [x[2] for x in a]
        dif = va != vc
        if dif: mudou_alguma = True
        flag = "  <-- MUDOU" if dif else ""
        print(f"  bk{b}: value {va} -> {vc} | opening {oa}{flag}", flush=True)

    print(f"\n[VEREDITO] alguma casa live mudou o value em 40s? {mudou_alguma}", flush=True)
    # value == opening em todas? sinal de que estamos lendo a abertura/congelado
    iguais_abertura = all(
        all(it[1] == it[2] for it in (s2.get(b, {}).get("items") or []))
        for b in live_ids
    )
    print(f"[CHECK] em TODAS as casas live, value == opening? {iguais_abertura}", flush=True)


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
