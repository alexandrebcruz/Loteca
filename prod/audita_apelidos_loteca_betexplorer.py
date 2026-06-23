#!/usr/bin/env python3
"""
Auditoria do apelidos_loteca_betexplorer.json — análogo aos auditores do Sofascore
e do Flashscore, mas validando o de-para contra o BETEXPLORER (rede Livesport,
busca desambiguada por país/UF + arquivo profundo).

MESMA arquitetura dos outros dois (e por isso REUSA a camada de dados do auditor do
Sofascore — `carregar_loteca`, `etapa1`, `aplicar`, `_print_relatorio`):

ETAPA 1 (puro dado, zero rede) — IDÊNTICA: a chave de cada apelido ('de') é um nome
    como aparece na Loteca; chave que nunca apareceu em concurso nenhum é peso morto
    -> apagar; chave quase-igual a um nome real (typo) NÃO é apagada (insumo da Etapa 2).
    O apelidos do BetExplorer é raw-first (só guarda o que o casamento cru/canônico
    NÃO resolve), então a Etapa 1 tende a achar poucos órfãos.

ETAPA 2 (precisa de rede) — valida o DE-PARA, com motor BETEXPLORER:
    Ancora no ADVERSÁRIO (como nos outros). A "verdade de campo" vem da PÁGINA DO TIME
    do adversário — diferente do Flashscore (que prefere o feed do dia): o feed do
    BetExplorer (`/br/?year&month&day`) só expõe o SLUG `home-away`, que não dá p/
    separar os dois nomes quando se conhece só um lado. A página do time, por outro
    lado, traz os nomes de exibição reais (mandante/visitante) E o team-id, então:
      1. resolve o adversário -> team_id (busca desambiguada por país/UF, `_melhor_time`);
      2. lê os jogos do time (`/results/` + `/fixtures/`, histórico fundo) e acha o
         confronto naquela DATA; o time do OUTRO lado é a grafia REAL (BetExplorer) do
         time sob teste — obtida SEM confiar no apelido.
    Compara o nome real com o 'para': confere->ok; difere->corrigir; typo->re-chavear
    (e corrigir se também difere). Vota em 2+ aparições (adversários distintos).
    Aparições sem confronto verificável -> 'não-verificável'.

Por padrão é DRY-RUN. Com --apply grava (apaga órfãos / corrige / re-chaveia), sob
lock de arquivo, backup timestamped e escrita atômica (reusa `aplicar`).

Uso:
    python3 audita_apelidos_loteca_betexplorer.py                       # Etapa 1 (dry-run)
    python3 audita_apelidos_loteca_betexplorer.py --etapa 2 --alvo typos
    python3 audita_apelidos_loteca_betexplorer.py --etapa 12 --proxy fixo --country BR
    python3 audita_apelidos_loteca_betexplorer.py --etapa 12 --apply
"""
import sys
import json
import asyncio
import argparse
import datetime as dt
from collections import Counter

import nodriver as uc

# Camada de dados (idêntica): reusa o auditor do Sofascore — Etapa 1, gravação e
# relatório são puro-dado / path-agnósticos, então ficam em UM lugar só.
from audita_apelidos_loteca_sofascore import (
    carregar_loteca, etapa1, aplicar, _print_relatorio,
    MESMO_TIME, RAW_PADRAO,
)
# Casamento de nomes + geo (mesmas funções que o coletor de odds usa).
from buscar_eventid_sofascore import (
    normalize, name_score, _pista,
)
# Motor BetExplorer: browser+consent, busca desambiguada e página do time (com o
# histórico fundo /results//fixtures/ + team-ids já capturados).
from buscar_odds_betexplorer import (
    abrir_browser, _buscar_time, _melhor_time, _eventos_time, _APELIDOS_BX_PATH,
)


# --------------------------------------------------------------------------- #
# ETAPA 2 — verdade de campo (ancora no adversário) via PÁGINA DO TIME do BetExplorer
# --------------------------------------------------------------------------- #
def _mesmo_time(para, gt):
    if not para or not gt:
        return False
    return name_score(para, gt) >= MESMO_TIME


def _lado_oposto(ev, self_id):
    """Dado um jogo da página do time do ADVERSÁRIO (self_id = id do adversário),
    devolve o nome de exibição do OUTRO lado (= o time sob teste). Usa os team-ids
    (a célula do próprio time recebeu self_id); cai no name_score se preciso."""
    hi, ai = ev.get("_home_id"), ev.get("_away_id")
    hn = (ev.get("homeTeam") or {}).get("name")
    an = (ev.get("awayTeam") or {}).get("name")
    if hi == self_id and ai != self_id:
        return an
    if ai == self_id and hi != self_id:
        return hn
    return None


async def verdade_de_campo(tab, date_iso, opp_nome, pista_opp, janela_dias, cache):
    """Resolve a página do ADVERSÁRIO e lê, no confronto daquela DATA, o time do
    outro lado (grafia BetExplorer). -> dict ou None. `cache`: resoluções de time
    por (nome_norm, pista) p/ não re-buscar o mesmo adversário entre aparições."""
    try:
        alvo = dt.date.fromisoformat(date_iso)
    except (TypeError, ValueError):
        return None

    chave = (normalize(opp_nome), (pista_opp or {}).get("uf"),
             (pista_opp or {}).get("alpha3"))
    if cache is not None and chave in cache:
        time = cache[chave]
    else:
        cands = await _buscar_time(tab, opp_nome)
        time, _sc = _melhor_time(cands, opp_nome, pista_opp) if cands else (None, 0.0)
        if cache is not None:
            cache[chave] = time
    if not time:
        return None

    eventos = await _eventos_time(tab, time["href"], self_id=time["id"],
                                  data=date_iso, janela_dias=janela_dias)
    melhor, mdist = None, 10 ** 9
    for ev in eventos:
        d = ev.get("_date")
        if not d:
            continue
        dist = abs((d - alvo).days)
        if dist <= janela_dias and dist < mdist:
            gt = _lado_oposto(ev, time["id"])
            if gt:
                melhor, mdist = (ev, gt), dist
    if not melhor:
        return None
    ev, gt = melhor
    return {"gt_nome": gt, "mid": ev.get("_match_href"), "torneio": None,
            "data_encontrada": ev["_date"].isoformat() if ev.get("_date") else None,
            "via": "time", "adv_score": round(name_score(opp_nome, time["nome"]), 3)}


async def validar_entrada(tab, entry, aparicoes, por_entrada, janela_dias, cache):
    """Valida um apelido pela verdade de campo, votando em aparições recentes com
    adversários distintos. -> dict com status/correção/evidência. Espelha o
    validar_entrada dos outros auditores (mesmos status e chaves)."""
    nn = entry["loteca_norm"]
    para = entry["para"]
    aps = [a for a in aparicoes.get(nn, []) if a["opp"]]
    aps.sort(key=lambda a: a["date"], reverse=True)

    votos = []                      # [(gt_norm, gt_nome, evidencia)]
    opps_usados = set()
    for ap in aps:
        if len(votos) >= por_entrada:
            break
        oppn = normalize(ap["opp"])
        if oppn == nn or oppn in opps_usados:
            continue                # evita auto-confronto e adversário repetido
        opps_usados.add(oppn)
        pista_opp = _pista(ap["opp_uf"], ap["opp_pais"])
        try:
            vc = await verdade_de_campo(tab, ap["date"], ap["opp"], pista_opp,
                                        janela_dias, cache)
        except Exception as e:  # noqa: BLE001
            print(f"[aviso] vc {entry.get('de')} vs {ap['opp']} "
                  f"{ap['date']}: {e}", file=sys.stderr)
            vc = None
        if vc and vc.get("gt_nome"):
            vc["adversario_loteca"] = ap["opp"]
            vc["data_loteca"] = ap["date"]
            votos.append((normalize(vc["gt_nome"]), vc["gt_nome"], vc))

    if not votos:
        return {**entry, "status": "nao-verificavel"}

    tally = Counter(v[0] for v in votos)
    gt_norm, n = tally.most_common(1)[0]
    gt_nome = next(v[1] for v in votos if v[0] == gt_norm)
    evid = next(v[2] for v in votos if v[0] == gt_norm)
    confianca = "alta" if n >= 2 else "media"
    confere = _mesmo_time(para, gt_nome)

    is_typo = "parece_ser" in entry
    if is_typo:
        status = "rekey" if confere else "rekey+valor"
        nova_chave, novo_valor = entry["loteca"], gt_nome
    else:
        status = "ok" if confere else "corrigir-valor"
        nova_chave, novo_valor = entry["de"], gt_nome

    return {**entry, "status": status, "confianca": confianca,
            "votos": n, "verificadas": len(votos),
            "gt_nome": gt_nome, "nova_chave": nova_chave, "novo_valor": novo_valor,
            "evidencia": {"adversario": evid["adversario_loteca"],
                          "data": evid["data_loteca"],
                          "event_id": evid["mid"], "dias": evid.get("via"),
                          "torneio": evid["torneio"]}}


async def etapa2(entradas, aparicoes, proxy, country, por_entrada, janela_dias,
                 verbose):
    browser, tab = await abrir_browser(proxy, country, verbose)
    try:
        cache, out = {}, []
        for i, e in enumerate(entradas, 1):
            r = await validar_entrada(tab, e, aparicoes, por_entrada,
                                      janela_dias, cache)
            out.append(r)
            if verbose:
                print(f"[etapa2] {i}/{len(entradas)} '{e['de']}' -> "
                      f"{r['status']}", file=sys.stderr)
        return out
    finally:
        try:
            browser.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Auditoria do apelidos_loteca_betexplorer.json (motor BetExplorer).")
    ap.add_argument("--apelidos", default=_APELIDOS_BX_PATH)
    ap.add_argument("--data", default=RAW_PADRAO, dest="raw_dir")
    ap.add_argument("--etapa", default="1", choices=["1", "2", "12"],
                    help="1=órfãos | 2=valida de-para | 12=ambas (default: 1)")
    ap.add_argument("--alvo", default="all", choices=["all", "usados", "typos"],
                    help="quais entradas a Etapa 2 valida (default: all)")
    ap.add_argument("--max", type=int, default=0, dest="max_n",
                    help="limita nº de entradas validadas na Etapa 2 (0 = todas)")
    ap.add_argument("--por-entrada", type=int, default=3, dest="por_entrada",
                    help="aparições (recentes) a tentar por entrada (default: 3)")
    ap.add_argument("--janela-dias", type=int, default=2, dest="janela_dias",
                    help="janela ±N dias p/ casar o confronto na página do time (default: 2)")
    # aceito p/ compat. com o pipeline (no BetExplorer a página do time JÁ é a via;
    # não há fallback distinto, então é no-op).
    ap.add_argument("--via-time", action="store_true", dest="via_time",
                    help="[no-op no BetExplorer] a página do time já é a via primária")
    ap.add_argument("--proxy", nargs="?", choices=["none", "rotativo", "fixo"],
                    const="fixo", default="none")
    ap.add_argument("--country", default="BR")
    ap.add_argument("--incluir-incertos", action="store_true", dest="incertos",
                    help="na Etapa 2, aplica também correções de confiança média")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args()

    universo, aparicoes, n_conc = carregar_loteca(a.raw_dir)
    if not universo:
        print(f"erro: nenhum concurso lido em {a.raw_dir}", file=sys.stderr)
        sys.exit(2)
    with open(a.apelidos, encoding="utf-8") as f:
        data = json.load(f)
    times = data.get("times") or {}
    c = etapa1(times, universo)
    usados, orfaos = c["usados"], c["orfaos"]
    duplicatas, typos, conflitos = c["duplicatas"], c["typos"], c["conflitos"]

    rel = {"apelidos": a.apelidos, "concursos": n_conc,
           "nomes_distintos": len(universo), "total_times": len(times),
           "etapa1": {"usados": len(usados), "orfaos": orfaos,
                      "duplicatas": duplicatas, "typos": typos,
                      "conflitos": conflitos}}

    # ----- Etapa 2 -----
    val = None
    if a.etapa in ("2", "12"):
        alvo = []
        if a.alvo in ("all", "typos"):
            alvo += typos                # só os typos GENUÍNOS precisam de re-key
        if a.alvo in ("all", "usados"):
            alvo += usados
        if a.max_n:
            alvo = alvo[:a.max_n]
        print(f"[etapa2] validando {len(alvo)} entradas via BetExplorer "
              f"(proxy={a.proxy})…", file=sys.stderr)
        val = uc.loop().run_until_complete(
            etapa2(alvo, aparicoes, a.proxy, a.country, a.por_entrada,
                   a.janela_dias, verbose=True))
        rel["etapa2"] = val

    # ----- aplicação -----
    aplicado = None
    if a.apply:
        apagar, corrigir, rechavear = [], [], []
        if a.etapa in ("1", "12"):
            apagar = [o["de"] for o in orfaos] + [d["de"] for d in duplicatas]
        if val:
            ok_conf = ("alta", "media") if a.incertos else ("alta",)
            for r in val:
                if r.get("confianca") not in ok_conf:
                    continue
                if r["status"] == "corrigir-valor":
                    corrigir.append((r["de"], r["novo_valor"]))
                elif r["status"] in ("rekey", "rekey+valor"):
                    rechavear.append((r["de"], r["nova_chave"], r["novo_valor"]))
        if apagar or corrigir or rechavear:
            aplicado = aplicar(a.apelidos, apagar, corrigir, rechavear)
            rel["aplicado"] = aplicado

    if a.as_json:
        print(json.dumps(rel, ensure_ascii=False, indent=2))
        return

    _print_relatorio(rel, usados, a)


if __name__ == "__main__":
    main()
