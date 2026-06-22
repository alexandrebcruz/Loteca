#!/usr/bin/env python3
"""Probe: o Flashscore tem 1X2 que atualiza no intragame?

Acha um jogo AO VIVO no feed de hoje, abre a pagina de odds dele, instrumenta a
rede (CDP Network) e observa por ~70s se (a) algum feed *.ninja/lsapp entrega
odds e (b) os valores 1X2 no DOM mudam. Loga tudo em stdout.
"""
import sys, os, json, asyncio, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prod"))

import nodriver as uc
from nodriver import cdp
import buscar_odds_flashscore as F


def _is_live(j):
    """Heuristica: status nao-agendado/nao-final + kickoff recente."""
    st = (j.get("status") or "")
    # codigos Livesport: 1=scheduled, 2/3..=live-ish, varia. Cobre por placar+tempo.
    dt = j.get("dt")
    recente = dt and (datetime.datetime.now() - dt).total_seconds() < 3.5 * 3600 and dt <= datetime.datetime.now()
    tem_placar = bool(j.get("placar")) and any(c.isdigit() for c in (j.get("placar") or ""))
    return (recente and tem_placar) or st in {"2", "3", "4", "5", "6", "7"}


async def main():
    browser = await uc.start(browser_args=["--no-sandbox"], sandbox=False)
    tab = await browser.get("about:blank")
    await F._goto(tab, F.ORIGIN)
    await asyncio.sleep(2)
    await F._consent(tab)
    await asyncio.sleep(1)

    hoje = datetime.date.today().isoformat()
    jogos = await F.listar_jogos_data(tab, hoje)
    print(f"[feed] {len(jogos)} jogos hoje ({hoje})", flush=True)
    # distribuicao de status
    from collections import Counter
    print("[feed] status dist:", dict(Counter(j.get("status") for j in jogos)), flush=True)

    lives = [j for j in jogos if _is_live(j)]
    print(f"[feed] {len(lives)} candidatos a AO VIVO", flush=True)
    for j in lives[:15]:
        print(f"   mid={j['mid']} st={j.get('status')} {j['home']} {j.get('placar')} {j['away']} | {j.get('hora')} {j.get('liga')}", flush=True)

    if not lives:
        print("[!] nenhum jogo ao vivo agora; nao da pra observar intragame.", flush=True)
        return

    alvo = lives[0]
    print(f"\n[alvo] {alvo['home']} x {alvo['away']} (mid={alvo['mid']})", flush=True)
    url = await F._canonical_from_mid(tab, alvo["mid"])
    odds_url = F._odds_url(url)
    print(f"[alvo] odds_url={odds_url}", flush=True)

    # ---- instrumenta a rede ----
    capturas = []  # (ts, url)
    def on_req(ev):
        u = ev.request.url
        if any(k in u for k in [".ninja", "lsapp", "/odds", "df_dos", "feed/"]):
            capturas.append(u)
    tab.add_handler(cdp.network.RequestWillBeSent, on_req)
    await tab.send(cdp.network.enable())

    await F._goto(tab, odds_url)
    await asyncio.sleep(3)

    # snapshot dos valores 1X2 ao longo do tempo
    JS = r"""JSON.stringify((()=>{
        const out=[];
        document.querySelectorAll('.ui-table__row').forEach(row=>{
          const cell=row.querySelector('[data-analytics-element="ODDS_COMPARISONS_BOOKMAKER_CELL"]');
          const a=cell&&cell.querySelector('a[title]');
          const nome=a?a.getAttribute('title').trim():null;
          const odds=[...row.querySelectorAll('a.oddsCell__odd, [class*="oddsCell__odd"]')]
            .map(e=>(e.textContent||'').trim()).filter(t=>/^\d+(\.\d+)?$/.test(t));
          if(nome) out.push(nome+': '+odds.join('/'));
        });
        // indicios de "live odds" na pagina
        const liveTxt = (document.body.innerText.match(/ao vivo|live odds|in.?play/gi)||[]).length;
        const tabs = [...document.querySelectorAll('a,div,button')].map(e=>(e.textContent||'').trim())
            .filter(t=>/odds|ao vivo|live|over|handicap|1x2/i.test(t)&&t.length<30);
        return {linhas: out, liveHits: liveTxt, tabs: [...new Set(tabs)].slice(0,20)};
    })())"""

    snaps = []
    for i in range(7):
        snap = await F._eval_json(tab, JS) or {}
        snaps.append(snap.get("linhas") or [])
        if i == 0:
            print(f"\n[page] liveHits={snap.get('liveHits')} tabs={snap.get('tabs')}", flush=True)
            print(f"[page] {len(snaps[0])} casas no 1X2", flush=True)
        await asyncio.sleep(10)

    # detecta mudanca entre o 1o e o ultimo snapshot
    base, last = snaps[0], snaps[-1]
    print(f"\n[obs] snapshots a cada 10s, total ~{10*(len(snaps)-1)}s", flush=True)
    mudou = []
    bd = dict(s.split(': ',1) for s in base if ': ' in s)
    ld = dict(s.split(': ',1) for s in last if ': ' in s)
    for k in bd:
        if k in ld and bd[k] != ld[k]:
            mudou.append(f"{k}: {bd[k]} -> {ld[k]}")
    print(f"[obs] casas que MUDARAM 1X2 em ~60s: {len(mudou)}", flush=True)
    for m in mudou[:20]:
        print("   ", m, flush=True)

    print(f"\n[net] {len(capturas)} requests de odds/feed capturados (unicos):", flush=True)
    for u in sorted(set(capturas))[:40]:
        print("   ", u, flush=True)


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
