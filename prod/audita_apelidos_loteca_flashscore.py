#!/usr/bin/env python3
"""
Auditoria do apelidos_loteca_flashscore.json — equivalente ao
audita_apelidos_loteca_sofascore.py, mas validando o de-para contra o FLASHSCORE
(fonte Livesport, PT) em vez do Sofascore.

MESMA arquitetura do auditor do Sofascore (e por isso REUSA a camada de dados
dele — `carregar_loteca`, `etapa1`, `aplicar`, `_print_relatorio`):

ETAPA 1 (puro dado, zero rede) — IDÊNTICA à do Sofascore:
    A chave de cada apelido ('de') É um nome de time como aparece na Loteca. Uma
    chave que nunca apareceu em concurso nenhum é peso morto -> apagar; uma chave
    quase-igual a um nome real (typo) NÃO é apagada (vira insumo da Etapa 2).
    Como o apelidos.json do Flashscore é RAW-FIRST (só guarda os nomes que o
    casamento cru PT<->PT NÃO resolve), a Etapa 1 tende a achar poucos órfãos.

ETAPA 2 (precisa de rede) — valida o DE-PARA, com motor FLASHSCORE:
    Ancora no ADVERSÁRIO, como no Sofascore, mas a "verdade de campo" vem do
    Flashscore, em duas vias:
      (a) FEED da agenda do dia (via primária, reusa `listar_jogos_data`): acha,
          na agenda da data, o jogo cujo um lado casa com o adversário e lê o
          OUTRO lado — esse é o nome REAL (grafia Flashscore) do time sob teste,
          obtido SEM confiar no apelido. Cobre bem datas recentes (o feed é uma
          agenda em torno de "hoje").
      (b) PÁGINA DO TIME (fallback opcional `--via-time`, reusa `_buscar_time` +
          `_eventos_time`): resolve a página /equipe/ do adversário e lê, no
          jogo daquela data, o time do outro lado. Cobre datas fora da janela do
          feed, mas a busca do Flashscore é instável/PT e a data exibida não traz
          ano (ambígua entre temporadas) -> usado só sob demanda.
    Compara o nome real com o 'para':
      - confere            -> ok
      - difere             -> corrigir o 'para'
      - typo + confere     -> só re-chavear (chave -> grafia real da Loteca)
      - typo + difere      -> re-chavear E corrigir o 'para'
    Vota em 2+ aparições (de adversários distintos) p/ ter confiança. Aparições
    sem jogo verificável (data fora do feed e sem --via-time) -> 'não-verificável'.

Por padrão é DRY-RUN. Com --apply grava (apaga órfãos / corrige / re-chaveia),
sob lock de arquivo, com backup timestamped e escrita atômica (reusa `aplicar`).

Uso:
    python3 audita_apelidos_loteca_flashscore.py                       # Etapa 1 (dry-run)
    python3 audita_apelidos_loteca_flashscore.py --etapa 2 --alvo typos
    python3 audita_apelidos_loteca_flashscore.py --etapa 12 --proxy fixo --country BR
    python3 audita_apelidos_loteca_flashscore.py --etapa 12 --via-time --apply
"""
import sys
import json
import asyncio
import argparse
import datetime as dt
from collections import Counter

import nodriver as uc
from nodriver import cdp

# Camada de dados (idêntica): reusa o auditor do Sofascore — Etapa 1, gravação e
# relatório são puro-dado / path-agnósticos, então ficam em UM lugar só.
from audita_apelidos_loteca_sofascore import (
    carregar_loteca, etapa1, aplicar, _print_relatorio,
    MESMO_TIME, RAW_PADRAO,
)
# Casamento de nomes + geo (mesmas funções que o coletor de odds usa).
from buscar_eventid_sofascore import (
    resolver_proxy, _egress_ip, normalize, name_score, _pista, _uf_no_texto,
    MATCH_THRESHOLD,
)
# Motor Flashscore: agenda (feed), página do time, de-para canônico e navegação.
from buscar_odds_flashscore import (
    ORIGIN, _APELIDOS_FS_PATH, _goto, _consent, _norm, _canonico,
    listar_jogos_data, _buscar_time, _melhor_time, _eventos_time,
)

# Margem mínima entre o melhor e o 2º melhor casamento do adversário (em jogos
# DISTINTOS, apontando p/ verdades diferentes) p/ não confiar num homônimo.
MARGEM_UNICIDADE = 0.10


# --------------------------------------------------------------------------- #
# ETAPA 2 — verdade de campo (ancora no adversário) via Flashscore
# --------------------------------------------------------------------------- #
def _mesmo_time(para, gt):
    if not para or not gt:
        return False
    return name_score(para, gt) >= MESMO_TIME


def _geo_bonus(jogo, pista_opp):
    """Pequeno bônus se a UF do adversário (Loteca) aparece no nome da liga/times
    do jogo do feed — desempata homônimos brasileiros (mesma ideia do Sofascore,
    que usa a UF no nome do time). País do feed vem por NOME (sem alpha-2/3), então
    só a UF textual dispara."""
    if not pista_opp:
        return 0.0
    txt = _norm(f"{jogo.get('liga','')} {jogo.get('home','')} {jogo.get('away','')}")
    return 0.05 if _uf_no_texto(txt, pista_opp.get("uf")) else 0.0


async def _agenda(tab, date_iso, janela_dias, cache):
    """Agenda do feed da data + vizinhos ±janela (tolera fuso). Cacheada por dia.
    -> [jogo] deduplicado por mid (cada jogo carrega sua própria 'data')."""
    alvo = dt.date.fromisoformat(date_iso)
    offs = [0] + [s * k for k in range(1, max(janela_dias, 0) + 1) for s in (1, -1)]
    out = []
    for k in offs:
        d = (alvo + dt.timedelta(days=k)).isoformat()
        if cache is not None and d in cache:
            js = cache[d]
        else:
            js = await listar_jogos_data(tab, d)
            if cache is not None:
                cache[d] = js
        out.extend(js)
    vistos, uniq = set(), []
    for j in out:
        if j.get("mid") and j["mid"] not in vistos:
            vistos.add(j["mid"])
            uniq.append(j)
    return uniq


def _dist_dias(data_jogo, date_iso):
    try:
        return abs((dt.date.fromisoformat(data_jogo) - dt.date.fromisoformat(date_iso)).days)
    except Exception:
        return 9


def _lado_adversario(jogo, opp_nome, opp_canon):
    """Casa o adversário contra os DOIS lados do jogo (cru + canônico). -> (score,
    nome_do_outro_lado, lado_do_adversario)."""
    sh = max(name_score(opp_nome, jogo.get("home", "")),
             name_score(opp_canon, jogo.get("home", "")))
    sa = max(name_score(opp_nome, jogo.get("away", "")),
             name_score(opp_canon, jogo.get("away", "")))
    if sh >= sa:
        return sh, jogo.get("away"), "home"
    return sa, jogo.get("home"), "away"


async def _via_feed(tab, date_iso, opp_nome, pista_opp, janela_dias, cache):
    """Verdade de campo pela agenda do dia: acha o jogo do adversário e lê o outro
    lado. -> dict ou None. Anti-homônimo: exige que o 2º melhor jogo (distinto,
    com verdade diferente) esteja a >= MARGEM_UNICIDADE de distância."""
    jogos = await _agenda(tab, date_iso, janela_dias, cache)
    if not jogos:
        return None
    opp_canon = _canonico(opp_nome)
    cand = []                       # (score_efetivo, jogo, gt_nome)
    for j in jogos:
        s, gt, _lado = _lado_adversario(j, opp_nome, opp_canon)
        if not gt:
            continue
        s += _geo_bonus(j, pista_opp) - 0.02 * _dist_dias(j.get("data"), date_iso)
        cand.append((s, j, gt))
    if not cand:
        return None
    cand.sort(key=lambda c: c[0], reverse=True)
    if cand[0][0] < MATCH_THRESHOLD:
        return None
    for s2, j2, gt2 in cand[1:]:    # 1º candidato de OUTRO jogo com verdade !=
        if j2.get("mid") != cand[0][1].get("mid"):
            if (cand[0][0] - s2) < MARGEM_UNICIDADE and \
               normalize(gt2 or "") != normalize(cand[0][2] or ""):
                return None         # adversário homônimo -> não confio
            break
    sc, j, gt = cand[0]
    return {"gt_nome": gt, "mid": j.get("mid"), "torneio": j.get("liga"),
            "data_encontrada": j.get("data"), "via": "feed",
            "adv_score": round(sc, 3)}


async def _via_time(tab, date_iso, opp_nome, pista_opp):
    """Fallback: resolve a página /equipe/ do ADVERSÁRIO e lê, no jogo da data, o
    time do outro lado. -> dict ou None. (Busca instável + data sem ano: melhor
    esforço, p/ datas fora da janela do feed.)"""
    cands = await _buscar_time(tab, opp_nome)
    if not cands:
        cands = await _buscar_time(tab, _canonico(opp_nome))
    if not cands:
        return None
    time = _melhor_time(cands, _canonico(opp_nome))
    eventos = await _eventos_time(tab, time["slug"], time["id"])
    m = date_iso.split("-")
    dd_mm = f"{m[2]}.{m[1]}." if len(m) == 3 else None
    for e in eventos:
        if dd_mm and dd_mm in (e.get("data") or ""):
            s, gt, _lado = _lado_adversario(e, opp_nome, _canonico(opp_nome))
            if gt:
                return {"gt_nome": gt, "mid": e.get("mid"), "torneio": None,
                        "data_encontrada": e.get("data"), "via": "time",
                        "adv_score": round(s, 3)}
    return None


async def verdade_de_campo(tab, date_iso, opp_nome, pista_opp, janela_dias,
                           cache, via_time):
    """Tenta o feed (primário) e, se nada e --via-time, a página do time."""
    vc = await _via_feed(tab, date_iso, opp_nome, pista_opp, janela_dias, cache)
    if vc is None and via_time:
        try:
            vc = await _via_time(tab, date_iso, opp_nome, pista_opp)
        except Exception as e:
            print(f"[aviso] via-time {opp_nome} {date_iso}: {e}", file=sys.stderr)
    return vc


async def validar_entrada(tab, entry, aparicoes, por_entrada, janela_dias,
                          cache, via_time):
    """Valida um apelido pela verdade de campo, votando em aparições recentes com
    adversários distintos. -> dict com status/correção/evidência. Espelha o
    validar_entrada do auditor do Sofascore (mesmos status e chaves)."""
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
                                        janela_dias, cache, via_time)
        except Exception as e:
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


async def _setup_browser(proxy, country, verbose):
    """Sobe Chrome (proxy opcional), abre a home do Flashscore e ACEITA o consent
    (sem isso o feed in-page não responde)."""
    pr = resolver_proxy(proxy, country)
    args = ["--no-sandbox"]
    if pr:
        host, port, user, pw = pr
        args.append(f"--proxy-server=http://{host}:{port}")
        if verbose:
            print(f"[proxy] {proxy}/{(country or '').upper()} via {host}:{port}",
                  file=sys.stderr)
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
        if verbose:
            print(f"[proxy] IP de saída: {await _egress_ip(tab)}", file=sys.stderr)
    await _goto(tab, ORIGIN + "/")
    await asyncio.sleep(3)
    await _consent(tab)
    await asyncio.sleep(1)
    return browser, tab


async def etapa2(entradas, aparicoes, proxy, country, por_entrada, janela_dias,
                 via_time, verbose):
    browser, tab = await _setup_browser(proxy, country, verbose)
    try:
        cache, out = {}, []
        for i, e in enumerate(entradas, 1):
            r = await validar_entrada(tab, e, aparicoes, por_entrada,
                                      janela_dias, cache, via_time)
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
        description="Auditoria do apelidos_loteca_flashscore.json (motor Flashscore).")
    ap.add_argument("--apelidos", default=_APELIDOS_FS_PATH)
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
                    help="janela ±N dias p/ casar o jogo no feed (default: 2)")
    ap.add_argument("--via-time", action="store_true", dest="via_time",
                    help="fallback pela página /equipe/ do adversário quando o feed "
                         "não cobre a data (busca instável; data sem ano)")
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
        print(f"[etapa2] validando {len(alvo)} entradas via Flashscore "
              f"(proxy={a.proxy}, via-time={a.via_time})…", file=sys.stderr)
        val = uc.loop().run_until_complete(
            etapa2(alvo, aparicoes, a.proxy, a.country, a.por_entrada,
                   a.janela_dias, a.via_time, verbose=True))
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
