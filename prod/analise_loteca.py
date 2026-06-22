#!/usr/bin/env python3
"""
Análise ponta-a-ponta de um concurso da Loteca: pega a PROGRAMAÇÃO aberta, coleta
as odds 1X2 MULTI-CASA de cada um dos 14 jogos no Flashscore.com.br e ESTIMA a
probabilidade de 1 (mandante) / X (empate) / 2 (visitante) de cada jogo.

Pipeline (reusa os módulos de prod, sem duplicar lógica):
  1. `baixar_programacao_loteca.escolher_proximo_aberto` -> o concurso ainda ABERTO a
     apostas com a data-limite mais próxima (mesma regra do baixar_programacao_loteca.py).
  2. UMA sessão de Chrome (nodriver headful) reusada p/ os 14 jogos — abre a home do
     Flashscore e aceita o consent UMA vez; depois, por jogo, chama os internos
     `_resolver_jogo` (agenda/feed + apelidos + auditoria opcional) e `coletar_odds`
     do buscar_odds_flashscore. Reusar o browser evita 14 boots de Chrome.
  3. Estima a probabilidade 1X2 (`estimar_prob`): de cada casa tira o overround
     (normaliza 1/odd p/ somar 1) e faz a MÉDIA entre as casas -> probabilidade de
     consenso, robusta ao vig de cada casa.

Checkpoint / retomada: tudo é gravado incrementalmente em data/analise/<concurso>/
  - programacao.json   -> o concurso (gravado ANTES de coletar)
  - jogo-NN.json        -> um arquivo por jogo (NN = nuSequencial), assim que coletado
  - analise.json        -> o agregado final
Rodar de novo RETOMA: jogos já resolvidos (com prob) são lidos do disco e só os
faltantes/falhos sobem o browser. `--refazer` ignora o cache e refaz tudo.

Orientação das colunas: as odds do Flashscore são [1, X, 2] do MANDANTE DO
FLASHSCORE. Se o casamento ficou `invertido` (mandante da Loteca == visitante do
Flashscore), troco casa<->fora p/ a probabilidade sair SEMPRE nas colunas da Loteca
(1 = nomeEquipeUm, 2 = nomeEquipeDois).

Uso:
    python3 analise_loteca.py                 # concurso aberto, IP da máquina
    python3 analise_loteca.py --auditar       # liga a auditoria por LLM por jogo
    python3 analise_loteca.py --proxy fixo --country BR   # IP sujo -> casas BR
    python3 analise_loteca.py --concurso-json prog.json   # usa uma programação já salva

Saída: JSON no stdout {concurso, fim_apostas, jogos:[{loteca, prob_1x2, ...}]}.
Logs/progresso vão p/ stderr. Pré-requisitos: nodriver + Chrome/Xvfb; Hub p/ --auditar.
"""
import os
import sys
import json
import asyncio
import argparse
import datetime as dt

import nodriver as uc
from nodriver import cdp

from baixar_programacao_loteca import (
    fetch as _fetch_prog, escolher_proximo_aberto, _prazo_apostas,
)
from buscar_odds_flashscore import (
    resolver_proxy, _egress_ip, ORIGIN, _goto, _consent,
    _resolver_jogo, coletar_odds, _pista,
)

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
ANALISE_DIR = os.path.join(RAIZ, "data", "analise")   # data/analise/<concurso>/


# --------------------------------------------------------------------------- #
# Checkpoint em disco: data/analise/<concurso>/ (programacao + um jogo por arquivo)
# --------------------------------------------------------------------------- #
# Nome da subpasta sob data/analise. None = usa o número do concurso (default
# histórico). O pipeline injeta aqui um nome custom (ex.: "1257_202606221830").
SAIDA_OVERRIDE = None


def _dir_concurso(numero):
    nome = SAIDA_OVERRIDE if SAIDA_OVERRIDE else str(numero)
    return os.path.join(ANALISE_DIR, nome)


def _path_jogo(cdir, seq):
    return os.path.join(cdir, f"jogo-{int(seq):02d}.json")


def _salvar_json(path, obj):
    """Escrita ATÔMICA (tmp + replace) p/ o checkpoint nunca ficar truncado."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _carregar_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Programação -> os 14 jogos do concurso aberto
# --------------------------------------------------------------------------- #
def _data_iso(j):
    """dtJogo 'DD/MM/AAAA HH:MM:SS' -> 'AAAA-MM-DD' (data do jogo). None se ilegível."""
    s = (j.get("dtJogo") or "").strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def obter_concurso(concurso_json=None):
    """Retorna o objeto do concurso aberto (da API) e se está aberto. Aceita um JSON
    já salvo (um concurso OU a lista da programação) p/ evitar nova chamada à Caixa."""
    if concurso_json:
        with open(concurso_json, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, list):
            return escolher_proximo_aberto(d)
        return d, True                       # já é um concurso específico
    programacao = json.loads(_fetch_prog())
    if not isinstance(programacao, list) or not programacao:
        raise RuntimeError("resposta inesperada da API de programação.")
    return escolher_proximo_aberto(programacao)


def jogos_do_concurso(concurso):
    """Extrai os jogos (ordenados por nuSequencial) no formato do orquestrador."""
    jogos = list(concurso.get("listaJogos") or [])
    jogos.sort(key=lambda j: j.get("nuSequencial") or 0)
    out = []
    for j in jogos:
        out.append({
            "seq": j.get("nuSequencial"),
            "home": (j.get("nomeEquipeUm") or "").strip(),
            "away": (j.get("nomeEquipeDois") or "").strip(),
            "data": _data_iso(j),
            "home_uf": (j.get("siglaUFUm") or "").strip() or None,
            "away_uf": (j.get("siglaUFDois") or "").strip() or None,
            "home_pais": (j.get("siglaPaisUm") or "").strip() or None,
            "away_pais": (j.get("siglaPaisDois") or "").strip() or None,
            "campeonato": (j.get("nomeCampeonato") or "").strip() or None,
        })
    return out


# --------------------------------------------------------------------------- #
# Estimativa de probabilidade 1X2 a partir das odds multi-casa
# --------------------------------------------------------------------------- #
def _mediana(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    m = n // 2
    return xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2


def estimar_prob(odds_res, invertido=False):
    """Probabilidade de consenso (1X2) a partir das odds de todas as casas.

    Para CADA casa com as 3 cotações válidas (>1), converte em prob implícita e
    NORMALIZA (1/odd dividido pela soma -> remove o overround/vig daquela casa);
    depois faz a MÉDIA dessas probabilidades entre as casas. Como cada vetor já
    soma 1, a média também soma 1. `invertido` troca casa<->fora p/ as colunas da
    Loteca. -> dict {casa, empate, fora, n_casas_validas, odds_medianas} ou None.
    """
    vetores, brutas = [], {"casa": [], "empate": [], "fora": []}
    for c in (odds_res.get("odds_por_casa") or []):
        o = c.get("odds_1x2") or {}
        a, e, f = o.get("casa"), o.get("empate"), o.get("fora")
        if None in (a, e, f) or min(a, e, f) <= 1:
            continue
        inv = [1.0 / a, 1.0 / e, 1.0 / f]
        s = sum(inv)
        vetores.append([x / s for x in inv])
        brutas["casa"].append(a)
        brutas["empate"].append(e)
        brutas["fora"].append(f)
    if not vetores:
        return None
    n = len(vetores)
    p = [sum(v[i] for v in vetores) / n for i in range(3)]
    od = {k: (round(_mediana(v), 2) if v else None) for k, v in brutas.items()}
    if invertido:                            # odds do Flashscore -> colunas da Loteca
        p = [p[2], p[1], p[0]]
        od = {"casa": od["fora"], "empate": od["empate"], "fora": od["casa"]}
    return {
        "casa": round(p[0], 4),
        "empate": round(p[1], 4),
        "fora": round(p[2], 4),
        "n_casas_validas": n,
        "odds_medianas": od,
    }


def _palpite(prob):
    """Coluna mais provável (1/X/2) a partir do dict de probabilidade."""
    if not prob:
        return None
    return max((("1", prob["casa"]), ("X", prob["empate"]), ("2", prob["fora"])),
               key=lambda kv: kv[1])[0]


# --------------------------------------------------------------------------- #
# Orquestração: uma sessão de Chrome p/ os 14 jogos
# --------------------------------------------------------------------------- #
async def _abrir_browser(proxy, country, verbose):
    """Sobe o Chrome (nodriver) com proxy opcional e devolve (browser, tab) com o
    consent já aceito. Replica o setup de proxy/consent do buscar_odds_flashscore."""
    pr = resolver_proxy(proxy, country)
    args = ["--no-sandbox"]
    if pr:
        host, port, _u, _p = pr
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


async def _analisar_jogo(tab, jg, modo, janela_dias, auditar, llm_model, verbose):
    """Resolve UM jogo no Flashscore e estima a probabilidade 1X2. -> dict do jogo.
    Nunca levanta: em falha, devolve o registro com `erro` preenchido (e prob=None),
    p/ o concurso inteiro sair mesmo que um jogo não resolva."""
    reg = {"seq": jg["seq"], "loteca": {"home": jg["home"], "away": jg["away"],
                                        "data": jg["data"]},
           "campeonato": jg["campeonato"], "resolvido": None, "prob_1x2": None,
           "palpite": None, "n_casas": 0, "erro": None}
    if not jg["data"]:
        reg["erro"] = "data do jogo ilegível na programação"
        return reg
    ph = _pista(jg["home_uf"], jg["home_pais"])
    pa = _pista(jg["away_uf"], jg["away_pais"])
    try:
        ev = await _resolver_jogo(tab, jg["home"], jg["away"], jg["data"],
                                  ph=ph, pa=pa, janela_dias=janela_dias, modo=modo,
                                  auditar=auditar, llm_model=llm_model)
        odds = await coletar_odds(tab, ev["url"])
        prob = estimar_prob(odds, invertido=bool(ev.get("invertido")))
        reg["resolvido"] = {"home": ev.get("home"), "away": ev.get("away"),
                            "data_exibida": ev.get("data"), "mid": ev.get("mid"),
                            "metodo": ev.get("metodo"), "score": ev.get("score"),
                            "invertido": ev.get("invertido")}
        if ev.get("auditoria") is not None:
            reg["resolvido"]["auditoria"] = ev.get("auditoria")
        reg["n_casas"] = odds.get("n_casas", 0)
        reg["prob_1x2"] = prob
        reg["palpite"] = _palpite(prob)
        if prob is None:
            reg["erro"] = "jogo resolvido, mas sem odds 1X2 válidas"
    except Exception as e:  # noqa: BLE001 — um jogo falho não derruba o concurso
        reg["erro"] = str(e)[:300]
    if verbose:
        if reg["prob_1x2"]:
            p = reg["prob_1x2"]
            print(f"[{jg['seq']:>2}] {jg['home']} x {jg['away']} -> "
                  f"1={p['casa']:.0%} X={p['empate']:.0%} 2={p['fora']:.0%} "
                  f"({reg['n_casas']} casas) palpite={reg['palpite']}",
                  file=sys.stderr)
        else:
            print(f"[{jg['seq']:>2}] {jg['home']} x {jg['away']} -> FALHOU: "
                  f"{reg['erro']}", file=sys.stderr)
    return reg


def _resumo(concurso, aberto, prazo, analises):
    """Monta o dict de análise do concurso a partir dos registros dos jogos."""
    ok = sum(1 for a in analises if a.get("prob_1x2"))
    return {
        "concurso": concurso.get("nuConcurso"),
        "aberto": aberto,
        "fim_apostas": (f"{prazo:%d/%m/%Y %H}h" if prazo else None),
        "fonte_odds": "flashscore.com.br",
        "jogos_resolvidos": ok,
        "jogos_total": len(analises),
        "jogos": analises,
    }


async def _run(proxy="none", country="BR", modo="fuzzy", janela_dias=2,
               auditar=False, llm_model="claude-sonnet-4-5", concurso_json=None,
               refazer=False, verbose=True, saida=None):
    global SAIDA_OVERRIDE
    if saida:
        SAIDA_OVERRIDE = saida
    concurso, aberto = obter_concurso(concurso_json)
    prazo = _prazo_apostas(concurso)
    jogos = jogos_do_concurso(concurso)

    # Checkpoint: data/analise/<concurso>/ — grava a programação ANTES de coletar,
    # p/ a retomada não depender de nova chamada à Caixa.
    cdir = _dir_concurso(concurso.get("nuConcurso"))
    _salvar_json(os.path.join(cdir, "programacao.json"), concurso)
    if verbose:
        est = ("aberto" if aberto else "FECHADO (mais à frente)")
        print(f"[concurso] {concurso.get('nuConcurso')} ({est}); "
              f"apostas até {prazo:%d/%m/%Y %H}h (BR); {len(jogos)} jogos.",
              file=sys.stderr)
        print(f"[checkpoint] {cdir}", file=sys.stderr)

    # Retomada: jogos já resolvidos (com prob) são reaproveitados do disco; só os
    # faltantes/falhos sobem o browser. --refazer ignora o cache e refaz tudo.
    analises = [None] * len(jogos)
    pendentes = []
    for i, jg in enumerate(jogos):
        cache = _carregar_json(_path_jogo(cdir, jg["seq"]))
        if cache and cache.get("prob_1x2") and not refazer:
            analises[i] = cache
            if verbose:
                p = cache["prob_1x2"]
                print(f"[{jg['seq']:>2}] {jg['home']} x {jg['away']} -> CACHE "
                      f"1={p['casa']:.0%} X={p['empate']:.0%} 2={p['fora']:.0%}",
                      file=sys.stderr)
        else:
            pendentes.append((i, jg))

    if pendentes:
        browser, tab = await _abrir_browser(proxy, country, verbose)
        try:
            for i, jg in pendentes:
                reg = await _analisar_jogo(
                    tab, jg, modo, janela_dias, auditar, llm_model, verbose)
                _salvar_json(_path_jogo(cdir, jg["seq"]), reg)   # checkpoint do jogo
                analises[i] = reg
        finally:
            try:
                browser.stop()
            except Exception:
                pass
    elif verbose:
        print("[checkpoint] todos os jogos já estavam resolvidos no disco.",
              file=sys.stderr)

    res = _resumo(concurso, aberto, prazo, analises)
    _salvar_json(os.path.join(cdir, "analise.json"), res)        # agregado final
    return res


def analisar_loteca(proxy="none", country="BR", modo="fuzzy", janela_dias=2,
                    auditar=False, llm_model="claude-sonnet-4-5",
                    concurso_json=None, refazer=False, verbose=True, saida=None):
    """API síncrona: roda o pipeline completo e devolve o dict de análise (ver _run).
    Retoma do checkpoint em data/analise/<concurso>/; `refazer=True` ignora o cache."""
    return asyncio.run(_run(proxy=proxy, country=country, modo=modo,
                            janela_dias=janela_dias, auditar=auditar,
                            llm_model=llm_model, concurso_json=concurso_json,
                            refazer=refazer, verbose=verbose, saida=saida))


def main():
    ap = argparse.ArgumentParser(
        description="Análise 1X2 do concurso aberto da Loteca via odds multi-casa "
                    "do Flashscore.com.br.")
    ap.add_argument("--modo", choices=["fuzzy", "id"], default="fuzzy",
                    help="fuzzy: agenda(feed)+apelidos (default). id: search-first")
    ap.add_argument("--janela-dias", type=int, default=2, dest="janela_dias",
                    help="janela (dias) p/ tolerar jogo em data próxima (default: 2)")
    ap.add_argument("--auditar", action="store_true",
                    help="liga a auditoria por LLM (Claude via Hub) em CADA jogo "
                         "(mais lento/caro; desligada por padrão no modo batch)")
    ap.add_argument("--llm-model", default="claude-sonnet-4-5", dest="llm_model",
                    help="modelo do Hub p/ a auditoria (default: claude-sonnet-4-5)")
    ap.add_argument("--proxy", nargs="?", choices=["none", "rotativo", "fixo"],
                    const="fixo", default="none",
                    help="sem o flag: sem proxy. --proxy sozinho: fixo BR")
    ap.add_argument("--country", default="BR",
                    help="ISO-2 do país p/ --proxy fixo (default: BR)")
    ap.add_argument("--concurso-json", default=None, dest="concurso_json",
                    help="usa um JSON já salvo (um concurso OU a lista da programação) "
                         "em vez de consultar a Caixa")
    ap.add_argument("--refazer", action="store_true",
                    help="ignora o checkpoint em data/analise/<concurso>/ e refaz "
                         "todos os jogos (por padrão retoma de onde parou)")
    ap.add_argument("--quiet", action="store_true", help="sem logs/progresso em stderr")
    ap.add_argument("--saida", default=None,
                    help="nome da subpasta sob data/analise p/ o checkpoint "
                         "(default: o número do concurso)")
    a = ap.parse_args()

    try:
        res = analisar_loteca(proxy=a.proxy, country=a.country, modo=a.modo,
                              janela_dias=a.janela_dias, auditar=a.auditar,
                              llm_model=a.llm_model, concurso_json=a.concurso_json,
                              refazer=a.refazer, verbose=not a.quiet, saida=a.saida)
    except Exception as e:  # noqa: BLE001
        print(f"[erro] {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
