#!/usr/bin/env python3
"""
Compara, lado a lado, as duas estratégias de resolução de jogo da Loteca:

  fuzzy  — a atual: casa NOMES contra a agenda (com apelidos.json) e, se falhar,
           cai no fallback por lista-de-time. de-para é peça central.
  id     — search-first: resolve cada time p/ um team_id via search/all do
           Sofascore (que já lida com tradução/sigla/truncamento) e casa o
           confronto por ID. de-para vira só exceção. Cacheia nome->id no lote.

Mede ACERTO (quantos cada modo resolveu) e CONCORDÂNCIA (mesmo event_id) — sem
gabarito, a concordância é o melhor sinal de correção — e o CUSTO DE REDE
(nº de requests) de cada modo, separando o que é comum (agenda) do extra de cada
estratégia. Um browser, um token, uma agenda em cache compartilhada.

Uso:
    python3 comparar_modos.py --n 8
    python3 comparar_modos.py --n 12 --janela 20 --proxy fixo --country BR
"""
import os
import sys
import json
import asyncio
import argparse
import datetime as dt

import nodriver as uc

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "prod"))  # ../prod = fonte única
import buscar_eventid_sofascore as B
from varredura_nomes import _carregar_jogos, _setup_browser


def _cands_agenda(agenda, date):
    vistos, out = set(), []
    for d in B._datas(date):
        for ev in agenda.get(d, []):
            if ev.get("id") not in vistos:
                vistos.add(ev.get("id"))
                out.append(ev)
    return out


async def _fuzzy(tab, j, token, agenda, janela, ph, pa):
    home, away = B._canonico(j["home"]), B._canonico(j["away"])
    cands = _cands_agenda(agenda, j["date"])
    ev, score, inv = B._melhor_evento(cands, home, away, ph, pa)
    if ev and score >= B.MATCH_THRESHOLD:
        return ev.get("id"), "agenda"
    try:
        r = await asyncio.wait_for(
            B._fallback_por_time(tab, j["date"], home, away, token,
                                 janela, ph, pa), timeout=100)
        return r["event_id"], "fallback"
    except Exception:
        return None, "falhou"


async def _por_id(tab, j, token, agenda, janela, ph, pa, cache):
    th = await B._resolver_time(tab, j["home"], token, ph, cache=cache)
    ta = await B._resolver_time(tab, j["away"], token, pa, cache=cache)
    if not th or not ta:
        return None, "sem-id"
    cands = _cands_agenda(agenda, j["date"])
    ev, _inv = B._match_por_id(cands, th[0], ta[0])
    if ev:
        return ev.get("id"), "agenda-id"
    # fallback por ID: lista da âncora, confronto por ID na data mais próxima.
    try:
        anc = max((th, ta), key=lambda t: t[2])
        alvo = dt.date.fromisoformat(j["date"])
        eventos = await asyncio.wait_for(
            B._eventos_do_time(tab, anc[0], token, alvo=alvo,
                               janela_dias=janela), timeout=100)
    except Exception:
        return None, "falhou"
    melhor, mdias = None, 10 ** 9
    for ev in eventos:
        m, _e = B._match_por_id([ev], th[0], ta[0])
        d = B._ev_date(ev)
        if m is not None and d is not None:
            dd = abs((d - alvo).days)
            if dd < mdias:
                melhor, mdias = ev, dd
    if melhor is not None and mdias <= janela:
        return melhor.get("id"), "fallback-id"
    return None, "sem-confronto"


async def _run(n, janela, proxy, country):
    jogos = _carregar_jogos(n)
    browser, tab = await _setup_browser(proxy, country, True)
    try:
        token = await B._capturar_token(tab)
        if not token:
            raise RuntimeError("sem token (IP bloqueado?).")

        # agenda compartilhada (1 fetch por data distinta) = custo COMUM aos 2 modos
        datas = set()
        for j in jogos:
            datas.update(B._datas(j["date"]))
        B._reset_req()
        agenda = {}
        for i, d in enumerate(sorted(datas), 1):
            try:
                data = await B._fetch_json(
                    tab, f"sport/football/scheduled-events/{d}", token)
                agenda[d] = data.get("events", [])
            except Exception:
                agenda[d] = []
            if i % 20 == 0 or i == len(datas):
                print(f"[agenda] {i}/{len(datas)} datas", file=sys.stderr)
        req_comum = sum(B._REQ.values())

        linhas = []
        cache = {}

        # passe FUZZY
        B._reset_req()
        for k, j in enumerate(jogos, 1):
            ph = B._pista(j["home_uf"], j["home_pais"])
            pa = B._pista(j["away_uf"], j["away_pais"])
            fid, fmet = await _fuzzy(tab, j, token, agenda, janela, ph, pa)
            linhas.append({"j": j, "fid": fid, "fmet": fmet})
            print(f"[fuzzy] {k}/{len(jogos)} {j['home']} x {j['away']} -> "
                  f"{fmet} {fid}", file=sys.stderr)
        req_fuzzy = sum(B._REQ.values())

        # passe ID
        B._reset_req()
        for k, (lin, j) in enumerate(zip(linhas, jogos), 1):
            ph = B._pista(j["home_uf"], j["home_pais"])
            pa = B._pista(j["away_uf"], j["away_pais"])
            iid, imet = await _por_id(tab, j, token, agenda, janela, ph, pa, cache)
            lin["iid"], lin["imet"] = iid, imet
            print(f"[id]    {k}/{len(jogos)} {j['home']} x {j['away']} -> "
                  f"{imet} {iid}", file=sys.stderr)
        req_id = sum(B._REQ.values())

        return {
            "linhas": linhas,
            "req_comum": req_comum, "req_fuzzy": req_fuzzy, "req_id": req_id,
            "times_distintos": len(cache),
        }
    finally:
        try:
            browser.stop()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--janela", type=int, default=20)
    ap.add_argument("--proxy", nargs="?", choices=["none", "rotativo", "fixo"],
                    const="fixo", default="none")
    ap.add_argument("--country", default="BR")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    r = uc.loop().run_until_complete(_run(a.n, a.janela, a.proxy, a.country))
    linhas = r["linhas"]

    if a.json:
        print(json.dumps(
            [{"concurso": x["j"]["concurso"], "date": x["j"]["date"],
              "home": x["j"]["home"], "away": x["j"]["away"],
              "fuzzy": [x["fid"], x["fmet"]], "id": [x["iid"], x["imet"]]}
             for x in linhas], ensure_ascii=False, indent=2))
        return

    n = len(linhas)
    f_ok = sum(1 for x in linhas if x["fid"])
    i_ok = sum(1 for x in linhas if x["iid"])
    ambos = [x for x in linhas if x["fid"] and x["iid"]]
    acordo = [x for x in ambos if x["fid"] == x["iid"]]
    discord = [x for x in ambos if x["fid"] != x["iid"]]
    so_fuzzy = [x for x in linhas if x["fid"] and not x["iid"]]
    so_id = [x for x in linhas if x["iid"] and not x["fid"]]

    print(f"\n=== {n} jogos | fuzzy resolveu {f_ok} | id resolveu {i_ok} ===")
    print(f"ambos resolveram: {len(ambos)} | concordam (mesmo event_id): "
          f"{len(acordo)} | DISCORDAM: {len(discord)}")
    print(f"só fuzzy resolveu: {len(so_fuzzy)} | só id resolveu: {len(so_id)}")
    print(f"times distintos resolvidos via search (cache): {r['times_distintos']}")

    print(f"\n--- CUSTO DE REDE (requests) ---")
    print(f"  comum (agenda):        {r['req_comum']:>5}")
    print(f"  fuzzy (extra):         {r['req_fuzzy']:>5}  "
          f"=> total {r['req_comum'] + r['req_fuzzy']}")
    print(f"  id    (extra):         {r['req_id']:>5}  "
          f"=> total {r['req_comum'] + r['req_id']}")

    def _linha(x):
        j = x["j"]
        return (f"  {j['concurso']} {j['home']} x {j['away']} | "
                f"fuzzy={x['fmet']}:{x['fid']} | id={x['imet']}:{x['iid']}")

    if discord:
        print(f"\n--- DISCORDÂNCIAS ({len(discord)}) — investigar ---")
        for x in discord:
            print(_linha(x))
    if so_fuzzy:
        print(f"\n--- SÓ FUZZY RESOLVEU ({len(so_fuzzy)}) ---")
        for x in so_fuzzy:
            print(_linha(x))
    if so_id:
        print(f"\n--- SÓ ID RESOLVEU ({len(so_id)}) ---")
        for x in so_id:
            print(_linha(x))


if __name__ == "__main__":
    main()
