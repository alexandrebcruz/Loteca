#!/usr/bin/env python3
"""
Varredura de NOMES: roda os jogos dos últimos N concursos da Loteca contra a
agenda do Sofascore e lista os confrontos que casam MAL (score baixo) — i.e. os
candidatos a entrar no apelidos.json.

Eficiência: NÃO abre um browser por jogo. Abre UM browser, captura o token uma
vez e — como a agenda do Sofascore é por data — busca cada data UMA vez (cache),
casando todos os jogos daquela data contra a agenda em memória. Custo: ~1 fetch
por data distinta (+ vizinhos ±1), não por jogo.

Só usa o caminho RÁPIDO (agenda). Não faz o fallback por lista-de-time: aqui o
objetivo é justamente flagrar quem NÃO casa na agenda — esses são os candidatos a
apelido/divergência. Para cada flag, mostra o melhor candidato que a agenda
ofereceu (nome Sofascore), pra facilitar decidir o de-para.

Uso:
    python3 varredura_nomes.py            # últimos 12 concursos, sem proxy
    python3 varredura_nomes.py --n 20 --limite 0.70
    python3 varredura_nomes.py --n 12 --proxy fixo --country BR
"""
import os
import sys
import glob
import json
import asyncio
import argparse
import datetime as dt

import nodriver as uc
from nodriver import cdp

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "prod"))  # ../prod = fonte única
import buscar_eventid_sofascore as B

RAW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw")


def _iso(dtjogo):
    """DD/MM/YYYY -> AAAA-MM-DD (ou None)."""
    try:
        d, m, y = (dtjogo or "").split("/")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except Exception:
        return None


def _carregar_jogos(n):
    """Últimos n concursos -> lista de dicts de jogo (com data iso + pistas)."""
    fs = sorted(glob.glob(os.path.join(RAW, "loteca-*.json")))[-n:]
    jogos = []
    for f in fs:
        d = json.load(open(f, encoding="utf-8"))
        nu = d.get("numero") or d.get("numeroJogo")
        for g in (d.get("listaResultadoEquipeEsportiva") or []):
            iso = _iso(g.get("dtJogo"))
            if not iso:
                continue
            jogos.append({
                "concurso": nu,
                "date": iso,
                "home": g.get("nomeEquipeUm") or "",
                "away": g.get("nomeEquipeDois") or "",
                "home_uf": g.get("siglaUFUm") or None,
                "away_uf": g.get("siglaUFDois") or None,
                "home_pais": g.get("siglaPaisUm") or None,
                "away_pais": g.get("siglaPaisDois") or None,
            })
    return jogos


async def _setup_browser(proxy, country, verbose):
    pr = B.resolver_proxy(proxy, country)
    args = ["--no-sandbox"]
    if pr:
        host, port, user, pw = pr
        args.append(f"--proxy-server=http://{host}:{port}")
        if verbose:
            print(f"[proxy] {proxy}/{country} via {host}:{port}", file=sys.stderr)
    browser = await uc.start(browser_args=args, sandbox=False)
    tab = await browser.get("about:blank")
    if pr:
        host, port, user, pw = pr

        async def on_auth(ev):
            await tab.send(cdp.fetch.continue_with_auth(
                request_id=ev.request_id,
                auth_challenge_response=cdp.fetch.AuthChallengeResponse(
                    response="ProvideCredentials", username=user, password=pw)))

        async def on_req(ev):
            try:
                await tab.send(cdp.fetch.continue_request(request_id=ev.request_id))
            except Exception:
                pass

        tab.add_handler(cdp.fetch.AuthRequired, on_auth)
        tab.add_handler(cdp.fetch.RequestPaused, on_req)
        await tab.send(cdp.fetch.enable(handle_auth_requests=True))
    return browser, tab


async def _varrer(jogos, proxy, country, limite, verbose):
    browser, tab = await _setup_browser(proxy, country, verbose)
    try:
        token = await B._capturar_token(tab)
        if not token:
            raise RuntimeError("não capturei o X-Requested-With na home "
                               "(IP bloqueado/país sem cobertura?).")

        # 1) todas as datas distintas (com vizinhos ±1) -> busca cada uma 1x.
        datas = set()
        for j in jogos:
            datas.update(B._datas(j["date"]))
        cache = {}
        for i, d in enumerate(sorted(datas), 1):
            try:
                data = await B._fetch_json(
                    tab, f"sport/football/scheduled-events/{d}", token)
                cache[d] = data.get("events", [])
            except Exception as e:
                print(f"[aviso] agenda {d}: {e}", file=sys.stderr)
                cache[d] = []
            if verbose:
                print(f"[agenda] {i}/{len(datas)} {d}: {len(cache[d])} eventos",
                      file=sys.stderr)

        # 2) casa cada jogo contra a agenda das suas datas (já em cache).
        resultados = []
        for j in jogos:
            home, away = B._canonico(j["home"]), B._canonico(j["away"])
            ph = B._pista(j["home_uf"], j["home_pais"])
            pa = B._pista(j["away_uf"], j["away_pais"])
            vistos, cands = set(), []
            for d in B._datas(j["date"]):
                for ev in cache.get(d, []):
                    if ev.get("id") not in vistos:
                        vistos.add(ev.get("id"))
                        cands.append(ev)
            ev, score, inv = B._melhor_evento(cands, home, away, ph, pa)
            resultados.append({
                **j,
                "home_canon": home, "away_canon": away,
                "score": round(score, 3) if ev else 0.0,
                "event_id": ev.get("id") if ev else None,
                "sof_home": (ev.get("homeTeam") or {}).get("name") if ev else None,
                "sof_away": (ev.get("awayTeam") or {}).get("name") if ev else None,
                "torneio": (ev.get("tournament") or {}).get("name") if ev else None,
                "invertido": inv,
            })
        return resultados
    finally:
        try:
            browser.stop()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(
        description="Varre últimos N concursos e lista nomes que casam mal.")
    ap.add_argument("--n", type=int, default=12,
                    help="quantos concursos recentes varrer (default: 12)")
    ap.add_argument("--limite", type=float, default=0.70,
                    help="score abaixo do qual o jogo é flagrado (default: 0.70)")
    ap.add_argument("--proxy", nargs="?", choices=["none", "rotativo", "fixo"],
                    const="fixo", default="none")
    ap.add_argument("--country", default="BR")
    ap.add_argument("--json", action="store_true",
                    help="imprime todos os resultados crus em JSON")
    a = ap.parse_args()

    jogos = _carregar_jogos(a.n)
    print(f"[varredura] {len(jogos)} jogos de {a.n} concursos; "
          f"limite de flag = {a.limite}", file=sys.stderr)

    res = uc.loop().run_until_complete(
        _varrer(jogos, a.proxy, a.country, a.limite, verbose=True))

    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    flag = [r for r in res if r["score"] < a.limite]
    flag.sort(key=lambda r: r["score"])
    ok = len(res) - len(flag)
    print(f"\n=== RESUMO: {ok}/{len(res)} casaram >= {a.limite}; "
          f"{len(flag)} flagrados ===\n")
    if flag:
        print(f"{'CONC':>5} {'SCORE':>5}  {'LOTECA':<37} {'->':2} "
              f"{'MELHOR CANDIDATO SOFASCORE':<40} TORNEIO")
        for r in flag:
            lot = f"{r['home']} x {r['away']}"[:37]
            sof = (f"{r['sof_home']} x {r['sof_away']}"
                   if r["sof_home"] else "(nada)")[:40]
            print(f"{str(r['concurso']):>5} {r['score']:>5.2f}  {lot:<37} -> "
                  f"{sof:<40} {r['torneio'] or ''}")


if __name__ == "__main__":
    main()
