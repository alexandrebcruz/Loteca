#!/usr/bin/env python3
"""
calcula_backtest_loteca.py — Backtest dos bilhetes da Loteca contra o histórico.

Varre os RAW da Caixa em data/raw/ (do MAIOR concurso para o menor) e, para cada
concurso JÁ APURADO:
  1. Coleta as odds 1X2 multi-casa de cada um dos 14 jogos EXATAMENTE como o
     analise_loteca.py faz (reusa `_abrir_browser` + `_analisar_jogo` do Flashscore,
     uma só sessão de Chrome para tudo) e estima a probabilidade de consenso.
  2. Roda o MESMO otimizador (DP exata) e materializa o melhor bilhete de CADA
     valor vendável (os pares (D,T) oficiais).
  3. Como o resultado real já é conhecido (derivado dos gols no RAW), conta para
     cada bilhete QUANTOS jogos teriam sido acertados (= a faixa que ele pegaria).

Saída: um JSON por concurso em data/backtest/loteca-NNNN.json com:
  - jogos_total / jogos_com_odds (quantos renderam probabilidade)
  - rateio oficial (ganhadores + prêmio das faixas 14 e 13) e os resultados reais
  - apostas[]: para cada valor apostável -> {valor, combos, d, t, p14, p13,
    p13mais (= P(14 ou 13)), acertos, faixa}

FONTE (--fonte, default betexplorer): o feed do Flashscore só cobre datas RECENTES
(~7 dias) — concursos antigos resolvem poucas/nenhuma odd. Por isso o backtest usa
por padrão o BUSCAR_ODDS_BETEXPLORER (busca desambiguada por país/UF + arquivo
histórico profundo), que resolve concursos antigos e homônimos/seleções que o
Flashscore erra. `--fonte flashscore` mantém o caminho antigo (só p/ comparar).
Jogo sem odd vira triplo forçado (como no pipeline); a varredura é do mais novo
para o mais velho e há `--parar-sem-odds`.

Retoma: pula concursos já gravados em data/backtest/ (use --refazer p/ ignorar).

Uso:
    python3 calcula_backtest_loteca.py                  # todos, mais novo->mais velho
    python3 calcula_backtest_loteca.py --max-concursos 20
    python3 calcula_backtest_loteca.py --parar-sem-odds 3   # para após 3 secos seguidos
    python3 calcula_backtest_loteca.py --de 1200 --ate 1256 # faixa de concursos
"""
import os
import sys
import json
import asyncio
import argparse

from analise_loteca import _abrir_browser, _analisar_jogo, jogos_do_concurso
import buscar_odds_betexplorer as BX
from precos_loteca import obter_precos
import otimizador_loteca as OPT
from otimizador_loteca import (
    aplicar_precos, otimizar, todos_bilhetes, metricas_bilhete, marcacoes_bilhete,
)

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
RAW_DIR = os.path.join(RAIZ, "data", "raw")
BACKTEST_DIR = os.path.join(RAIZ, "data", "backtest")


# --------------------------------------------------------------------------- #
# IO + leitura do RAW (jogos e resultados reais)
# --------------------------------------------------------------------------- #
def _salvar_json(path, obj):
    """Escrita ATÔMICA (tmp + replace) — padrão do projeto."""
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


def _resultado_jogo(g):
    """Deriva a coluna 1/X/2 dos gols (1 = nomeEquipeUm venceu). None se não jogado."""
    gu, gd = g.get("nuGolEquipeUm"), g.get("nuGolEquipeDois")
    if gu is None or gd is None:
        return None
    gu, gd = int(gu), int(gd)
    return "1" if gu > gd else ("X" if gu == gd else "2")


def concursos_disponiveis():
    """Números dos RAW válidos em data/raw, do MAIOR para o menor."""
    nums = []
    for nome in os.listdir(RAW_DIR):
        if nome.startswith("loteca-") and nome.endswith(".json"):
            try:
                nums.append(int(nome[len("loteca-"):-len(".json")]))
            except ValueError:
                continue
    return sorted(nums, reverse=True)


def ler_concurso(numero):
    """Lê o RAW e devolve (raw, jogos, resultados, finalizado).

    jogos: no formato de jogos_do_concurso (seq/home/away/data/ufs/pais/camp).
    resultados: {seq -> '1'/'X'/'2' ou None}. finalizado: True se todos têm gols.
    """
    raw = _carregar_json(os.path.join(RAW_DIR, f"loteca-{numero:04d}.json"))
    if not raw:
        return None, [], {}, False
    lst = raw.get("listaResultadoEquipeEsportiva") or []
    # jogos_do_concurso lê concurso["listaJogos"]; os campos batem 1:1.
    raw_like = dict(raw)
    raw_like["listaJogos"] = lst
    jogos = jogos_do_concurso(raw_like)
    resultados = {g.get("nuSequencial"): _resultado_jogo(g) for g in lst}
    finalizado = bool(lst) and all(v is not None for v in resultados.values())
    return raw, jogos, resultados, finalizado


def _rateio(raw):
    """Faixas 14/13 do RAW: ganhadores + prêmio (p/ EV/diluição futura)."""
    out = []
    for r in (raw.get("listaRateioPremio") or []):
        out.append({"faixa": r.get("faixa"),
                    "descricao": r.get("descricaoFaixa"),
                    "ganhadores": r.get("numeroDeGanhadores"),
                    "premio": r.get("valorPremio")})
    return out


# --------------------------------------------------------------------------- #
# Backtest de um concurso
# --------------------------------------------------------------------------- #
async def backtest_concurso(tab, numero, jogos, resultados, modo, janela_dias,
                            auditar, llm_model, fonte, verbose):
    """Coleta odds dos 14 jogos, otimiza e conta acertos por bilhete. -> dict."""
    jogos_opt = []
    for jg in jogos:
        if fonte == "betexplorer":
            reg = await BX.analisar_jogo(tab, jg, janela_dias=janela_dias,
                                         verbose=verbose, auditar=auditar,
                                         llm_model=llm_model)
        else:
            reg = await _analisar_jogo(tab, jg, modo, janela_dias,
                                       auditar=auditar, llm_model=llm_model,
                                       verbose=verbose)
        pr = reg.get("prob_1x2")
        p = ({"1": pr["casa"], "X": pr["empate"], "2": pr["fora"]} if pr else None)
        jogos_opt.append({"seq": reg["seq"], "home": jg["home"], "away": jg["away"],
                          "p": p, "n_casas": reg.get("n_casas", 0),
                          "erro": reg.get("erro")})

    jogos_com_odds = sum(1 for j in jogos_opt if j["p"])

    apostas = []
    dp = otimizar(jogos_opt, OPT.MAX_COMBOS_LIMITE)
    for combos, _cov, niveis in todos_bilhetes(dp):
        m = metricas_bilhete(jogos_opt, niveis)
        marc = marcacoes_bilhete(jogos_opt, niveis)
        acertos = 0
        for mk in marc:
            res = resultados.get(mk["seq"])
            if res is not None and res in mk["resultados"]:
                acertos += 1
        d = sum(1 for x in niveis if x == 2)
        t = sum(1 for x in niveis if x == 3)
        apostas.append({
            "valor": round(combos * OPT.PRECO, 2),
            "combos": combos, "d": d, "t": t,
            "p14": round(m["p14"], 8),
            "p13": round(m["p13"], 8),
            "p13mais": round(m["p13mais"], 8),
            "acertos": acertos,
            "faixa": 1 if acertos == 14 else (2 if acertos == 13 else 0),
        })

    return {
        "jogos_com_odds": jogos_com_odds,
        "jogos_opt": jogos_opt,
        "apostas": apostas,
    }


def montar_saida(numero, raw, jogos, resultados, res):
    """Monta o JSON final do concurso."""
    return {
        "concurso": numero,
        "data_apuracao": raw.get("dataApuracao"),
        "jogos_total": len(jogos),
        "jogos_com_odds": res["jogos_com_odds"],
        "rateio": _rateio(raw),
        "resultados": [
            {"seq": jg["seq"], "home": jg["home"], "away": jg["away"],
             "data": jg["data"], "resultado": resultados.get(jg["seq"]),
             "n_casas": jo["n_casas"], "prob_1x2": jo["p"]}
            for jg, jo in zip(jogos, res["jogos_opt"])
        ],
        "apostas": res["apostas"],
    }


# --------------------------------------------------------------------------- #
# Varredura
# --------------------------------------------------------------------------- #
async def _run(proxy, country, modo, janela_dias, de, ate, max_concursos,
               parar_sem_odds, auditar, llm_model, fonte, refazer, verbose):
    aplicar_precos(obter_precos())

    nums = concursos_disponiveis()
    if de is not None:
        nums = [n for n in nums if n >= de]
    if ate is not None:
        nums = [n for n in nums if n <= ate]
    if max_concursos:
        nums = nums[:max_concursos]

    # Pendentes (retomada): pula os já gravados, salvo --refazer.
    pend = []
    for n in nums:
        if not refazer and os.path.exists(os.path.join(BACKTEST_DIR,
                                                        f"loteca-{n:04d}.json")):
            if verbose:
                print(f"[{n}] já em data/backtest — pulado", file=sys.stderr)
            continue
        pend.append(n)

    if not pend:
        print("[backtest] nada a fazer (todos já gravados).", file=sys.stderr)
        return

    if fonte == "betexplorer":
        browser, tab = await BX.abrir_browser(proxy, country, verbose)
    else:
        browser, tab = await _abrir_browser(proxy, country, verbose)
    secos = 0
    try:
        for n in pend:
            raw, jogos, resultados, finalizado = ler_concurso(n)
            if not finalizado:
                if verbose:
                    print(f"[{n}] sem resultado completo no RAW — pulado",
                          file=sys.stderr)
                continue
            if verbose:
                print(f"\n[concurso {n}] {raw.get('dataApuracao')} — coletando "
                      f"odds dos {len(jogos)} jogos...", file=sys.stderr)
            res = await backtest_concurso(tab, n, jogos, resultados, modo,
                                          janela_dias, auditar, llm_model, fonte,
                                          verbose)
            saida = montar_saida(n, raw, jogos, resultados, res)
            _salvar_json(os.path.join(BACKTEST_DIR, f"loteca-{n:04d}.json"), saida)
            if verbose:
                melhor = max((a["acertos"] for a in res["apostas"]), default=0)
                print(f"[{n}] {res['jogos_com_odds']}/{len(jogos)} jogos com odds | "
                      f"{len(res['apostas'])} apostas | melhor acerto={melhor}",
                      file=sys.stderr)
            # Para a varredura se entrar numa sequência de concursos sem odds.
            if parar_sem_odds:
                secos = secos + 1 if res["jogos_com_odds"] == 0 else 0
                if secos >= parar_sem_odds:
                    print(f"[backtest] {secos} concursos sem odds seguidos — "
                          f"parando (use --parar-sem-odds 0 p/ ir até o fim).",
                          file=sys.stderr)
                    break
    finally:
        try:
            browser.stop()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(
        description="Backtest dos bilhetes da Loteca contra o histórico (data/raw).")
    ap.add_argument("--fonte", choices=["flashscore", "betexplorer"],
                    default="betexplorer",
                    help="origem das odds históricas. betexplorer (default): busca "
                         "desambiguada por país/UF + arquivo profundo (resolve "
                         "concursos antigos e homônimos/seleções). flashscore: feed "
                         "de agenda (só ~7 dias; quebra em concursos antigos).")
    ap.add_argument("--modo", choices=["fuzzy", "id"], default="fuzzy")
    ap.add_argument("--janela-dias", type=int, default=2, dest="janela_dias")
    ap.add_argument("--de", type=int, default=None,
                    help="só concursos >= este número")
    ap.add_argument("--ate", type=int, default=None,
                    help="só concursos <= este número")
    ap.add_argument("--max-concursos", type=int, default=None, dest="max_concursos",
                    help="limita a quantos concursos processar (do mais novo)")
    ap.add_argument("--parar-sem-odds", type=int, default=3, dest="parar_sem_odds",
                    help="para após N concursos seguidos com 0 odds (0=ir até o fim; "
                         "default 3)")
    ap.add_argument("--auditar", action="store_true",
                    help="liga o resgate/validação por LLM (etapa 3) — essencial p/ "
                         "rodadas de seleção (Copa); precisa de HUB_SERVICE_URL/HUB_API_KEY")
    ap.add_argument("--llm-model", default="claude-sonnet-4-5", dest="llm_model",
                    help="modelo do Hub p/ a auditoria (default: claude-sonnet-4-5)")
    ap.add_argument("--proxy", nargs="?", choices=["none", "rotativo", "fixo"],
                    const="fixo", default="none")
    ap.add_argument("--country", default="BR")
    ap.add_argument("--refazer", action="store_true",
                    help="reprocessa mesmo os concursos já gravados")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    asyncio.run(_run(proxy=a.proxy, country=a.country, modo=a.modo,
                     janela_dias=a.janela_dias, de=a.de, ate=a.ate,
                     max_concursos=a.max_concursos, parar_sem_odds=a.parar_sem_odds,
                     auditar=a.auditar, llm_model=a.llm_model, fonte=a.fonte,
                     refazer=a.refazer, verbose=not a.quiet))


if __name__ == "__main__":
    main()
