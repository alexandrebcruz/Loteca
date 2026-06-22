#!/usr/bin/env python3
"""
Auditoria do apelidos_loteca_sofascore.json.

ETAPA 1 (puro dado, zero rede) — apelidos ÓRFÃOS:
    A chave de cada apelido ('de') É um nome de time como aparece na Loteca. O
    runtime só aciona um apelido se a chave normalizada bater EXATAMENTE com um
    nome listado em algum concurso (data/raw/loteca-*.json). Uma chave que nunca
    apareceu é peso morto -> apagar. Para não apagar um apelido só MAL GRAFADO,
    se a chave for quase-igual a um nome real (ratio alto) marco como
    'suspeito de typo na chave' e NÃO apago (vira insumo da Etapa 2).

ETAPA 2 (precisa de rede) — valida o DE-PARA:
    Para cada apelido cujo 'de' (ou, no caso de typo, o nome real correspondente)
    aparece na Loteca, descobre a VERDADE DE CAMPO ancorando no ADVERSÁRIO:
    resolve o adversário daquele jogo, pega a lista de confrontos DELE, acha o
    jogo na data e lê — por ID — o time do OUTRO lado. Esse é o time real, obtido
    sem confiar no apelido sob teste. Compara com o 'para':
      - confere            -> ok
      - difere             -> corrigir o 'para'
      - typo + confere     -> só re-chavear (chave -> grafia real da Loteca)
      - typo + difere      -> re-chavear E corrigir o 'para'
    Vota em 2+ aparições (de adversários distintos) p/ ter confiança. Aparições
    fora da cobertura do Sofascore -> 'não-verificável' (não mexe).

Por padrão é DRY-RUN. Com --apply grava (apaga órfãos / corrige / re-chaveia),
sob lock de arquivo, com backup timestamped e escrita atômica.

Uso:
    python3 audita_apelidos_loteca_sofascore.py                       # Etapa 1 (dry-run)
    python3 audita_apelidos_loteca_sofascore.py --etapa 2 --alvo typos
    python3 audita_apelidos_loteca_sofascore.py --etapa 12 --apply
"""
import os
import sys
import glob
import json
import fcntl
import argparse
import datetime as dt
from difflib import SequenceMatcher
from collections import Counter, defaultdict

import nodriver as uc
from nodriver import cdp

import buscar_eventid_sofascore as B  # mesma pasta (prod/)

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
RAW_PADRAO = os.path.join(RAIZ, "data", "raw")

TYPO_RATIO = 0.86       # acima: chave provável typo (não apaga; Etapa 2 re-chaveia)
MESMO_TIME = 0.85       # name_score acima do qual 'para' == verdade de campo


# --------------------------------------------------------------------------- #
# Carga dos concursos: universo de nomes + aparições (data, adversário, geo)
# --------------------------------------------------------------------------- #
def _iso(dtjogo):
    try:
        d, m, y = (dtjogo or "").split("/")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except Exception:
        return None


def carregar_loteca(raw_dir):
    """-> (universo, aparicoes, n_concursos).
    universo  : {nome_norm: grafia_representativa}
    aparicoes : {nome_norm: [ {date, opp, opp_uf, opp_pais, uf, pais} ... ]}"""
    universo = {}
    aparicoes = defaultdict(list)
    arquivos = sorted(glob.glob(os.path.join(raw_dir, "loteca-*.json")))
    for f in arquivos:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for g in (d.get("listaResultadoEquipeEsportiva") or []):
            iso = _iso(g.get("dtJogo"))
            um = (g.get("nomeEquipeUm") or "").strip()
            dois = (g.get("nomeEquipeDois") or "").strip()
            uf1, uf2 = g.get("siglaUFUm") or None, g.get("siglaUFDois") or None
            p1, p2 = g.get("siglaPaisUm") or None, g.get("siglaPaisDois") or None
            for nome, opp, n_uf, n_p, o_uf, o_p in (
                    (um, dois, uf1, p1, uf2, p2),
                    (dois, um, uf2, p2, uf1, p1)):
                if not nome:
                    continue
                nn = B.normalize(nome)
                if nn and nn not in universo:
                    universo[nn] = nome
                if nn and iso and opp:
                    aparicoes[nn].append({"date": iso, "opp": opp,
                                          "opp_uf": o_uf, "opp_pais": o_p,
                                          "uf": n_uf, "pais": n_p})
    return universo, aparicoes, len(arquivos)


def carregar_apelidos_raw(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# ETAPA 1 — órfãos / suspeitos de typo (puro dado)
# --------------------------------------------------------------------------- #
def quase_igual(chave_norm, universo):
    melhor, melhor_r = None, 0.0
    for nn, grafia in universo.items():
        r = SequenceMatcher(None, chave_norm, nn).ratio()
        if r > melhor_r:
            melhor, melhor_r = grafia, r
    return melhor, melhor_r


def etapa1(times, universo):
    """Classifica cada chave de 'times'. -> dict com listas:
      usados     : a chave É um nome real da Loteca (Etapa 2 valida o 'para').
      orfaos     : nunca listada e SEM nome real parecido -> apagar (puro dado).
      duplicatas : chave quebrada (typo), mas a grafia CERTA já existe como chave
                   com o MESMO valor -> redundante -> apagar (puro dado).
      typos      : chave quebrada e a grafia certa NÃO existe -> Etapa 2 re-chaveia.
      conflitos  : a grafia certa já existe, porém com valor DIFERENTE -> manual."""
    norm_keys = {B.normalize(k): (k, v) for k, v in times.items()}
    usados, orfaos, duplicatas, typos, conflitos = [], [], [], [], []
    for de, para in times.items():
        nd = B.normalize(de)
        if nd in universo:
            usados.append({"de": de, "para": para,
                           "loteca": universo[nd], "loteca_norm": nd})
            continue
        nome_real, r = quase_igual(nd, universo)
        item = {"de": de, "para": para}
        if r < TYPO_RATIO:
            if nome_real:
                item.update({"mais_proximo": nome_real, "ratio": round(r, 3)})
            orfaos.append(item)
            continue
        # chave quase-igual a um nome real R da Loteca (provável typo na chave)
        item.update({"parece_ser": nome_real, "ratio": round(r, 3),
                     "loteca": nome_real, "loteca_norm": B.normalize(nome_real)})
        gemea = norm_keys.get(B.normalize(nome_real))   # a grafia certa já é chave?
        if gemea and gemea[0] != de:
            item["gemea"] = {"de": gemea[0], "para": gemea[1]}
            if _mesmo_time(para, gemea[1]):
                duplicatas.append(item)                 # redundante -> apagar
            else:
                conflitos.append(item)                  # mesma grafia, valor != -> manual
        else:
            typos.append(item)                          # re-key genuíno -> Etapa 2
    return {"usados": usados, "orfaos": orfaos, "duplicatas": duplicatas,
            "typos": typos, "conflitos": conflitos}


# --------------------------------------------------------------------------- #
# ETAPA 2 — verdade de campo (ancora no adversário) + validação do de-para
# --------------------------------------------------------------------------- #
def _mesmo_time(para, gt):
    if not para or not gt:
        return False
    return B.name_score(para, gt) >= MESMO_TIME


async def verdade_de_campo(tab, date_iso, opp_nome, token, pista_opp,
                           janela_dias=2, cache=None):
    """Resolve o adversário e lê, da lista de jogos DELE, o time do OUTRO lado no
    confronto daquela data. -> dict ou None (não verificável).

    A paginação no histórico do adversário usa a data da Loteca como alvo e PARA
    sozinha assim que a cobre (ou quando o histórico acaba) — sem trava de páginas,
    então alcança até aparições de muitos anos atrás."""
    t = await B._resolver_time(tab, opp_nome, token, pista_opp, cache=cache)
    if not t:
        return None
    opp_id, opp_sof, opp_base = t
    if opp_id is None or opp_base < 0.5:    # âncora fraca -> não confio
        return None
    alvo = dt.date.fromisoformat(date_iso)
    eventos = await B._eventos_do_time(tab, opp_id, token, alvo=alvo,
                                       janela_dias=janela_dias)
    melhor = None
    for ev in eventos:
        d = B._ev_date(ev)
        if not d:
            continue
        dias = abs((d - alvo).days)
        if dias > janela_dias:
            continue
        h, a = B._ids_evento(ev)
        if opp_id == h:
            gt = ev.get("awayTeam") or {}
        elif opp_id == a:
            gt = ev.get("homeTeam") or {}
        else:
            continue
        if melhor is None or dias < melhor[0]:
            melhor = (dias, ev, gt)
    if not melhor:
        return None
    dias, ev, gt = melhor
    return {"gt_id": gt.get("id"), "gt_nome": gt.get("name"),
            "opp_id": opp_id, "opp_sof": opp_sof,
            "event_id": ev.get("id"), "dias": dias,
            "data_encontrada": B._ev_date(ev).isoformat(),
            "torneio": (ev.get("tournament") or {}).get("name")}


async def validar_entrada(tab, token, entry, aparicoes, por_entrada,
                          janela_dias, cache):
    """Valida um apelido pela verdade de campo, votando em aparições recentes
    com adversários distintos. -> dict com status/correção/evidência.

    Tenta as aparições da MAIS RECENTE p/ a mais antiga e para quando junta
    `por_entrada` votos — então jogos antigos só são consultados se os recentes
    não verificarem (e, se nenhum verificar, o veredito é 'não-verificável')."""
    nn = entry["loteca_norm"]
    para = entry["para"]
    aps = [a for a in aparicoes.get(nn, []) if a["opp"]]
    aps.sort(key=lambda a: a["date"], reverse=True)

    votos = []                     # [(gt_id, gt_nome, evidencia)]
    opps_usados = set()
    for ap in aps:
        if len(votos) >= por_entrada:
            break
        oppn = B.normalize(ap["opp"])
        if oppn == nn or oppn in opps_usados:
            continue               # evita auto-confronto e adversário repetido
        opps_usados.add(oppn)
        pista_opp = B._pista(ap["opp_uf"], ap["opp_pais"])
        try:
            vc = await verdade_de_campo(tab, ap["date"], ap["opp"], token,
                                        pista_opp, janela_dias, cache)
        except Exception as e:
            print(f"[aviso] vc {entry.get('de')} vs {ap['opp']} "
                  f"{ap['date']}: {e}", file=sys.stderr)
            vc = None
        if vc and vc["gt_id"]:
            vc["adversario_loteca"] = ap["opp"]
            vc["data_loteca"] = ap["date"]
            votos.append((vc["gt_id"], vc["gt_nome"], vc))

    if not votos:
        return {**entry, "status": "nao-verificavel"}

    tally = Counter(v[0] for v in votos)
    gt_id, n = tally.most_common(1)[0]
    gt_nome = next(v[1] for v in votos if v[0] == gt_id)
    evid = next(v[2] for v in votos if v[0] == gt_id)
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
            "gt_nome": gt_nome, "gt_id": gt_id,
            "nova_chave": nova_chave, "novo_valor": novo_valor,
            "evidencia": {"adversario": evid["adversario_loteca"],
                          "data": evid["data_loteca"],
                          "event_id": evid["event_id"], "dias": evid["dias"],
                          "torneio": evid["torneio"]}}


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


async def etapa2(entradas, aparicoes, proxy, country, por_entrada,
                 janela_dias, verbose):
    browser, tab = await _setup_browser(proxy, country, verbose)
    try:
        token = await B._capturar_token(tab)
        if not token:
            raise RuntimeError("não capturei o X-Requested-With na home.")
        cache, out = {}, []
        for i, e in enumerate(entradas, 1):
            r = await validar_entrada(tab, token, e, aparicoes,
                                      por_entrada, janela_dias, cache)
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
# Aplicação (lock + backup + escrita atômica)
# --------------------------------------------------------------------------- #
def aplicar(path, apagar=(), corrigir=(), rechavear=()):
    """apagar: [chave]; corrigir: [(chave, valor)]; rechavear: [(chave_velha,
    chave_nova, valor)]. -> resumo dict."""
    lock_f = open(path + ".lock", "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        times = data.get("times") or {}

        carimbo = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{path}.bak-{carimbo}"
        with open(backup, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")

        res = {"apagados": [], "corrigidos": [], "rechaveados": [],
               "pulados": [], "backup": backup}
        for de in apagar:
            if times.pop(de, None) is not None:
                res["apagados"].append(de)
        for de, val in corrigir:
            if de in times:
                times[de] = val
                res["corrigidos"].append((de, val))
        for velha, nova, val in rechavear:
            existentes = {B.normalize(k) for k in times if k != velha}
            if B.normalize(nova) in existentes:
                res["pulados"].append((velha, nova, "chave-nova-ja-existe"))
                continue
            times.pop(velha, None)
            times[nova] = val
            res["rechaveados"].append((velha, nova, val))

        data["times"] = times
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return res
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Auditoria do apelidos_loteca_sofascore.json.")
    ap.add_argument("--apelidos", default=B._APELIDOS_PATH)
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
                    help="janela ±N dias p/ casar o jogo na lista do adversário "
                         "(default: 2)")
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
    data = carregar_apelidos_raw(a.apelidos)
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
        print(f"[etapa2] validando {len(alvo)} entradas via rede "
              f"(proxy={a.proxy})…", file=sys.stderr)
        val = uc.loop().run_until_complete(
            etapa2(alvo, aparicoes, a.proxy, a.country,
                   a.por_entrada, a.janela_dias, verbose=True))
        rel["etapa2"] = val

    # ----- aplicação -----
    aplicado = None
    if a.apply:
        apagar, corrigir, rechavear = [], [], []
        if a.etapa in ("1", "12"):
            # órfãos (nunca listados) + duplicatas (grafia certa já existe igual)
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


def _print_relatorio(rel, usados, a):
    print(f"apelidos : {rel['apelidos']}")
    print(f"concursos: {rel['concursos']} | nomes distintos: {rel['nomes_distintos']}"
          f" | entradas 'times': {rel['total_times']}")
    e1 = rel["etapa1"]
    print(f"\n== ETAPA 1 ==  usados {e1['usados']} | órfãos {len(e1['orfaos'])} | "
          f"duplicatas {len(e1['duplicatas'])} | typos {len(e1['typos'])} | "
          f"conflitos {len(e1['conflitos'])}")
    if e1["orfaos"]:
        print("\n— ÓRFÃOS (nunca listados, sem grafia parecida → apagar) —")
        for o in e1["orfaos"]:
            print(f"  '{o['de']}' → '{o['para']}'")
    if e1["duplicatas"]:
        print("\n— DUPLICATAS (grafia certa já existe c/ mesmo valor → apagar) —")
        for d in e1["duplicatas"]:
            g = d["gemea"]
            print(f"  '{d['de']}' → '{d['para']}'   já coberto por "
                  f"'{g['de']}' → '{g['para']}'")
    if e1["typos"]:
        print("\n— TYPOS GENUÍNOS (grafia certa não existe → Etapa 2 re-chaveia) —")
        for s in e1["typos"]:
            print(f"  '{s['de']}' → '{s['para']}'   chave real Loteca: "
                  f"'{s['parece_ser']}' (ratio {s['ratio']})")
    if e1["conflitos"]:
        print("\n— CONFLITOS (grafia certa existe, mas com valor DIFERENTE → manual) —")
        for c in e1["conflitos"]:
            g = c["gemea"]
            print(f"  '{c['de']}' → '{c['para']}'   vs gêmea "
                  f"'{g['de']}' → '{g['para']}'")

    if "etapa2" in rel:
        v = rel["etapa2"]
        cnt = Counter(r["status"] for r in v)
        print(f"\n== ETAPA 2 ==  " + " | ".join(f"{k} {n}" for k, n in cnt.items()))
        for r in v:
            ev = r.get("evidencia") or {}
            base = f"  [{r['status']}] '{r['de']}' → '{r['para']}'"
            if r["status"] == "ok":
                print(base + f"   ✓ campo: {r['gt_nome']} "
                      f"({r['confianca']}, {r['votos']}v)")
            elif r["status"] == "nao-verificavel":
                print(base + "   — sem jogo verificável na cobertura")
            else:
                print(base + f"\n        ⇒ {r['nova_chave']!r}: {r['novo_valor']!r}"
                      f"   (campo, {r['confianca']}, {r['votos']}v; "
                      f"via {ev.get('adversario')} {ev.get('data')})")

    if rel.get("aplicado"):
        ap_ = rel["aplicado"]
        print(f"\n[apply] apagados={len(ap_['apagados'])} "
              f"corrigidos={len(ap_['corrigidos'])} "
              f"rechaveados={len(ap_['rechaveados'])} "
              f"pulados={len(ap_['pulados'])}  | backup: "
              f"{os.path.basename(ap_['backup'])}")
        for v in ap_["pulados"]:
            print(f"   pulado: {v}")
    elif not a.apply:
        print("\n(dry-run) use --apply para gravar.")


if __name__ == "__main__":
    main()
