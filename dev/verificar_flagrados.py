#!/usr/bin/env python3
"""
Passo de VERIFICAÇÃO: para cada jogo que a AGENDA não resolve bem (score baixo),
roda o FALLBACK por lista-de-time (paginação adaptativa + janela larga) p/ revelar
o nome canônico do Sofascore — i.e. a sigla/divergência que vira apelido.

Difere de varredura_nomes.py (que é agenda-only): aqui, quem fica abaixo do limite
é investigado a fundo via fallback, exatamente o caminho do resolver real. Um
browser, um token, cache da agenda compartilhado.

Uso:
    python3 verificar_flagrados.py --n 40 --flag 0.70 --janela 20
"""
import os
import sys
import json
import asyncio
import argparse

import nodriver as uc

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "prod"))  # ../prod = fonte única
import buscar_eventid_sofascore as B
from varredura_nomes import _carregar_jogos, _setup_browser


async def _run(n, flag, janela, proxy, country):
    jogos = _carregar_jogos(n)
    browser, tab = await _setup_browser(proxy, country, True)
    try:
        token = await B._capturar_token(tab)
        if not token:
            raise RuntimeError("sem token (IP bloqueado?).")

        # cache da agenda (1 fetch por data distinta)
        datas = set()
        for j in jogos:
            datas.update(B._datas(j["date"]))
        cache = {}
        for i, d in enumerate(sorted(datas), 1):
            try:
                data = await B._fetch_json(
                    tab, f"sport/football/scheduled-events/{d}", token)
                cache[d] = data.get("events", [])
            except Exception:
                cache[d] = []
            if i % 20 == 0 or i == len(datas):
                print(f"[agenda] {i}/{len(datas)} datas", file=sys.stderr)

        out = []
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
            if score >= flag:
                continue  # agenda já resolve bem -> não investiga

            rec = {
                "concurso": j["concurso"], "date": j["date"],
                "home": j["home"], "away": j["away"],
                "home_canon": home, "away_canon": away,
                "agenda_score": round(score, 3),
                "agenda_cand": (f"{(ev.get('homeTeam') or {}).get('name')} x "
                                f"{(ev.get('awayTeam') or {}).get('name')}"
                                if ev else None),
                "agenda_passa": score >= B.MATCH_THRESHOLD,
            }
            print(f"[fb] {j['concurso']} {j['home']} x {j['away']} "
                  f"(agenda {score:.2f})…", file=sys.stderr)
            try:
                r = await asyncio.wait_for(
                    B._fallback_por_time(tab, j["date"], home, away, token,
                                         janela, ph, pa), timeout=100)
                rec.update(
                    fb_ok=True, fb_score=r["score"], fb_id=r["event_id"],
                    fb_match=f"{r['home_sofascore']} x {r['away_sofascore']}",
                    fb_date=r["data_encontrada"], fb_dias=r["dias_diferenca"],
                    fb_torneio=r["torneio"])
            except Exception as e:
                rec.update(fb_ok=False, fb_erro=str(e)[:160])
            out.append(rec)
        return out
    finally:
        try:
            browser.stop()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--flag", type=float, default=0.70,
                    help="agenda score abaixo do qual investiga via fallback")
    ap.add_argument("--janela", type=int, default=20)
    ap.add_argument("--proxy", nargs="?", choices=["none", "rotativo", "fixo"],
                    const="fixo", default="none")
    ap.add_argument("--country", default="BR")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    res = uc.loop().run_until_complete(
        _run(a.n, a.flag, a.janela, a.proxy, a.country))
    res.sort(key=lambda r: (not r.get("fb_ok"), -(r.get("fb_score") or 0)))

    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    achou = [r for r in res if r.get("fb_ok")]
    falhou = [r for r in res if not r.get("fb_ok")]
    print(f"\n=== {len(res)} flagrados investigados; fallback achou {len(achou)}, "
          f"não achou {len(falhou)} ===\n")
    print("--- FALLBACK ACHOU (candidatos a apelido / cross-check) ---")
    print(f"{'CONC':>5} {'AG':>4} {'FB':>4}  {'LOTECA':<32} -> "
          f"{'SOFASCORE (fallback)':<40} {'DATA':>10} TORNEIO")
    for r in achou:
        lot = f"{r['home']} x {r['away']}"[:32]
        print(f"{str(r['concurso']):>5} {r['agenda_score']:>4.2f} "
              f"{r['fb_score']:>4.2f}  {lot:<32} -> {r['fb_match'][:40]:<40} "
              f"{r['fb_date'] or '':>10} (±{r['fb_dias']}d) {r['fb_torneio'] or ''}")
    print("\n--- FALLBACK NÃO ACHOU (provável ausência real / time não indexado) ---")
    for r in falhou:
        lot = f"{r['home']} x {r['away']}"[:32]
        print(f"{str(r['concurso']):>5} {r['agenda_score']:>4.2f}   "
              f"{lot:<32}  agenda->{(r['agenda_cand'] or '')[:32]:<32} | {r['fb_erro']}")


if __name__ == "__main__":
    main()
