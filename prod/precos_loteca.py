#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
precos_loteca.py — Preços/regras OFICIAIS da Loteca, buscados na fonte.

Em vez de cravar o preço no otimizador, este módulo busca a tabela na landing
oficial da Caixa e valida, com fallback embutido se a rede/parse falhar.

Fonte: https://loterias.caixa.gov.br/Paginas/Loteca.aspx
O HTML estático (sem JS) contém dois âncoras redundantes que se cruzam:
  1. "A aposta mínima é de R$ 4,00 e dá direito a um duplo."
        -> mínimo = R$ 4,00 ; 1 duplo = 2 combinações  =>  unitário = 4/2 = R$ 2,00
  2. A tabela de VALOR DA APOSTA lista TODAS as combinações válidas, colunas
        | ... | Duplos (D) | Triplos (T) | Nº de Apostas | Valor |
     onde `Nº de Apostas` = combinações e `Valor` = preço DA APOSTA (sempre > 0,
     R$ 4,00 a R$ 1.728,00). Cada linha é um par (D,T) REAL; daí extraímos e
     VALIDAMOS, ponto a ponto, a fórmula
        combinações = 2^D · 3^T     e     valor = combinações × unitário,
     conferindo inclusive contra o `Nº de Apostas` que o próprio site imprime.
     O conjunto publicado é exatamente {(D,T) : 2 <= 2^D·3^T <= 864}; o maior é
        D=5, T=3  ->  2^5·3^3 = 864 apostas = R$ 1.728,00.
     (NÃO é 729: esse é só o máximo de 6 triplos; misturando duplos passa disso.)

A API servicebus (api/loteca) NÃO traz preço — só prêmio/arrecadação — por isso
a fonte é a landing HTML.

Estratégia defensiva
--------------------
- `obter_precos()` tenta cache fresco -> busca online -> fallback embutido.
- Toda extração é validada por sanidade (unitário e teto em faixas plausíveis)
  e por cruzamento ponto a ponto: para CADA par (D,T) publicado, confere
  `valor == 2^D·3^T × unitário`. Qualquer divergência cai no fallback SEM
  derrubar o chamador.
- O resultado é cacheado em data/loteca_precos.json com timestamp.

Uso:
    python3 precos_loteca.py                # mostra a tabela resolvida
    python3 precos_loteca.py --forcar       # ignora o cache e rebusca
"""

import os
import re
import json
import html
import argparse
import datetime as dt

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
CACHE_PATH = os.path.join(RAIZ, "data", "loteca_precos.json")
URL = "https://loterias.caixa.gov.br/Paginas/Loteca.aspx"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

def _gerar_tabela_dt(unit, teto):
    """Reconstrói o conjunto {(D,T) : 2 <= 2^D·3^T <= teto} — exatamente as
    linhas que a tabela oficial publica — com combinações e valor. Usado no
    fallback quando a rede falha."""
    linhas = []
    d = 0
    while 2 ** d <= teto:
        t = 0
        while (2 ** d) * (3 ** t) <= teto:
            combos = (2 ** d) * (3 ** t)
            if combos >= 2:  # 1 combinação (14 secos) não é vendável
                linhas.append({"d": d, "t": t, "combos": combos,
                               "valor": round(combos * unit, 2)})
            t += 1
        d += 1
    linhas.sort(key=lambda x: x["combos"])
    return linhas


# Fallback embutido — maior bilhete publicado na tabela oficial (2026-06-21).
# OBS: o teto NÃO é 729 (esse é só o máximo de 6 triplos). Misturando duplos e
# triplos vai além: 8 duplos+1 triplo=768, e o maior publicado é
# 5 duplos+3 triplos = 2^5·3^3 = 864 apostas (R$ 1.728,00).
_FB_TABELA = _gerar_tabela_dt(2.00, 864)
FALLBACK = {
    "preco_unitario": 2.00,   # R$ por combinação
    "min_combos": 2,          # aposta mínima = 1 duplo (seco não é vendável)
    "min_valor": 4.00,        # R$ 4,00
    "max_combos": 864,        # maior bilhete oficial = 2^5·3^3 (5 duplos+3 triplos)
    "max_valor": 1728.00,     # R$ 1.728,00
    "max_duplos": 9,          # T=0 -> 2^9=512 <= 864 (D=10 daria 1024)
    "max_triplos": 6,         # D=0 -> 3^6=729 <= 864 (T=7 daria 2187)
    "tabela_validada": True,  # fórmula 2^D·3^T conferida ponto a ponto
    "linhas_tabela": len(_FB_TABELA),  # nº de pares (D,T) publicados
    "tabela_dt": _FB_TABELA,
    "fonte": "fallback-embutido",
    "atualizado_em": None,
}

# Faixas de sanidade — qualquer valor fora disso invalida o parse.
_LIM_UNIT = (0.50, 20.00)
_LIM_MAXCOMBOS = (100, 5000)
_MIN_LINHAS = 15  # a tabela oficial tem ~30 pares (D,T); exija boa parte


def _brl(s):
    """'1.458,00' -> 1458.0"""
    return float(s.replace(".", "").replace(",", "."))


def _texto_limpo(htmltxt):
    t = html.unescape(htmltxt).replace("\xa0", " ")
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[ \t]+", " ", t)
    return t


def _brl_celula(s):
    """'R$ 1.728,00' / 'R$ 0,00' -> float (0.0 se não houver número)."""
    m = re.search(r"([\d.]*\d,\d{2})", s)
    return _brl(m.group(1)) if m else 0.0


def _ths(tabela_html):
    return " ".join(_texto_limpo(c) for c in
                    re.findall(r"<th\b[^>]*>(.*?)</th>", tabela_html,
                               re.I | re.S)).lower()


def _extrair_tabela_dt(htmltxt):
    """Lê a tabela oficial de VALOR DA APOSTA e devolve os pares (D,T) reais.

    Cabeçalho: | Apostas | Qtd de números jogados | Duplos | Triplos |
               | Nº de Apostas | Valor |
    Cada linha de dados traz, à direita: Duplos, Triplos, Nº de Apostas
    (= combinações que o site imprime) e Valor (= preço DA APOSTA, sempre > 0).

    Validamos a fórmula ponto a ponto: o `Nº de Apostas` impresso tem de bater
    com `2^D·3^T`. Lança ValueError se a tabela sumir ou alguma linha divergir.
    """
    alvo = None
    for tb in re.findall(r"<table\b.*?</table>", htmltxt, re.I | re.S):
        h = _ths(tb)
        if ("duplos" in h and "triplos" in h and "valor" in h
                and "apostas" in h and "bol" not in h and "pontos" not in h):
            alvo = tb
            break
    if alvo is None:
        raise ValueError("tabela de valor da aposta não encontrada")

    linhas = []
    for bloco in re.split(r"<tr\b", alvo, flags=re.I)[1:]:
        celulas = [_texto_limpo(c).replace("​", "").strip()
                   for c in re.findall(r"<td\b[^>]*>(.*?)</td>", bloco,
                                       re.I | re.S)]
        if len(celulas) < 4:
            continue
        # lê da direita: Valor | Nº Apostas | Triplos | Duplos
        d_s, t_s, combos_s, valor_s = celulas[-4], celulas[-3], celulas[-2], celulas[-1]
        combos_s = combos_s.replace(".", "")
        if not (d_s.isdigit() and t_s.isdigit() and combos_s.isdigit()):
            continue  # pula cabeçalho e linhas-rótulo
        if not valor_s.lstrip().startswith("R$"):
            continue
        d_, t_ = int(d_s), int(t_s)
        combos_site = int(combos_s)
        combos = (2 ** d_) * (3 ** t_)
        if combos_site != combos:  # verificação ponto a ponto da fórmula
            raise ValueError(
                f"linha (D={d_},T={t_}): site diz {combos_site} apostas, "
                f"mas 2^D·3^T = {combos}")
        if combos >= 2:  # 1 combinação (14 secos) não é vendável
            linhas.append({"d": d_, "t": t_, "combos": combos,
                           "valor": _brl_celula(valor_s)})
    linhas.sort(key=lambda x: x["combos"])
    return linhas


def parse_precos(htmltxt):
    """Extrai {preco_unitario, min_*, max_*, tabela_dt, ...} do HTML.

    A tabela de Bolão dá os pares (D,T) reais; validamos 2^D·3^T == combos e
    valor == combos × unitário para CADA linha que publica preço. Lança
    ValueError se o conteúdo não bater nos âncoras ou falhar a validação.
    """
    t = _texto_limpo(htmltxt)

    # Âncora 1: frase da aposta mínima (1 duplo = 2 combinações)
    m = re.search(
        r"aposta\s+m[ií]nima\s+é\s+de\s+R\$\s*([\d.,]+)\s+e\s+d[áa]\s+"
        r"direito\s+a\s+um\s+duplo", t, re.I)
    if not m:
        raise ValueError("frase da aposta mínima não encontrada")
    min_valor = _brl(m.group(1))
    unit = round(min_valor / 2.0, 2)  # 1 duplo => 2 combinações

    if not (_LIM_UNIT[0] <= unit <= _LIM_UNIT[1]):
        raise ValueError(f"unitário implausível: {unit}")

    # Âncora 2: tabela (D,T) — extrai e valida a fórmula ponto a ponto.
    linhas = _extrair_tabela_dt(htmltxt)
    if len(linhas) < _MIN_LINHAS:
        raise ValueError(f"tabela (D,T) não reconhecida ({len(linhas)} linhas)")
    for ln in linhas:
        # 2^D·3^T já é como `combos` foi calculado; conferimos o preço publicado.
        if ln["valor"] > 0 and abs(ln["valor"] - ln["combos"] * unit) > 0.005:
            raise ValueError(
                f"linha (D={ln['d']},T={ln['t']}) diverge: "
                f"{ln['combos']}×{unit} != R$ {ln['valor']:.2f}")

    maior = max(linhas, key=lambda x: x["combos"])
    max_combos = maior["combos"]
    max_valor = round(max_combos * unit, 2)

    if not (_LIM_MAXCOMBOS[0] <= max_combos <= _LIM_MAXCOMBOS[1]):
        raise ValueError(f"teto implausível: {max_combos}")

    return {
        "preco_unitario": unit,
        "min_combos": int(round(min_valor / unit)),
        "min_valor": round(min_valor, 2),
        "max_combos": int(max_combos),
        "max_valor": max_valor,
        "max_duplos": max(ln["d"] for ln in linhas),
        "max_triplos": max(ln["t"] for ln in linhas),
        "tabela_validada": True,
        "linhas_tabela": len(linhas),
        "tabela_dt": linhas,
    }


def fetch_precos_online(tentativas=3, timeout=20, verbose=False):
    """Busca e parseia os preços. Retorna dict completo (com fonte/timestamp)
    ou lança a última exceção após `tentativas`."""
    if requests is None:
        raise RuntimeError("requests não disponível")
    ultimo = None
    for n in range(tentativas):
        try:
            r = requests.get(URL, timeout=timeout, headers={"User-Agent": UA})
            r.raise_for_status()
            if "aposta" not in r.text.lower():  # casca/anti-bot intermitente
                raise ValueError("resposta sem conteúdo (anti-bot/cache)")
            p = parse_precos(r.text)
            p["fonte"] = URL
            p["atualizado_em"] = dt.datetime.now().isoformat(timespec="seconds")
            if verbose:
                print(f"[precos] online OK (tentativa {n+1}): "
                      f"unit R$ {p['preco_unitario']:.2f}, teto {p['max_combos']}")
            return p
        except Exception as e:  # noqa: BLE001
            ultimo = e
            if verbose:
                print(f"[precos] tentativa {n+1} falhou: {e}")
    raise ultimo if ultimo else RuntimeError("falha desconhecida")


def _ler_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _salvar_cache(p):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CACHE_PATH)


def _fresco(p, ttl_horas):
    ts = (p or {}).get("atualizado_em")
    if not ts:
        return False
    try:
        quando = dt.datetime.fromisoformat(ts)
    except ValueError:
        return False
    return (dt.datetime.now() - quando) <= dt.timedelta(hours=ttl_horas)


def obter_precos(checar=True, forcar=False, ttl_horas=24, verbose=False):
    """Resolve os preços oficiais com cache + fallback.

    checar=False  -> não toca a rede (usa cache fresco ou o fallback embutido).
    forcar=True   -> ignora o cache e rebusca online.
    Sempre retorna um dict utilizável; nunca lança.
    """
    cache = _ler_cache()

    if not checar:
        if cache and cache.get("preco_unitario"):
            return cache
        return dict(FALLBACK)

    if not forcar and _fresco(cache, ttl_horas):
        if verbose:
            print(f"[precos] cache fresco ({cache.get('atualizado_em')})")
        return cache

    try:
        p = fetch_precos_online(verbose=verbose)
        antes = cache or FALLBACK
        if abs(p["preco_unitario"] - antes.get("preco_unitario", -1)) > 1e-9 \
                or p["max_combos"] != antes.get("max_combos"):
            if verbose:
                print(f"[precos] MUDOU vs anterior: "
                      f"unit {antes.get('preco_unitario')}->{p['preco_unitario']}, "
                      f"teto {antes.get('max_combos')}->{p['max_combos']}")
        _salvar_cache(p)
        return p
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"[precos] online falhou ({e}); usando "
                  f"{'cache' if cache else 'fallback embutido'}")
        if cache and cache.get("preco_unitario"):
            return cache
        return dict(FALLBACK)


def main():
    ap = argparse.ArgumentParser(description="Preços oficiais da Loteca (Caixa).")
    ap.add_argument("--forcar", action="store_true", help="ignora cache e rebusca")
    ap.add_argument("--offline", action="store_true", help="não toca a rede")
    ap.add_argument("--tabela", action="store_true",
                    help="mostra a tabela (D,T) verificada linha a linha")
    a = ap.parse_args()
    p = obter_precos(checar=not a.offline, forcar=a.forcar, verbose=True)

    tabela = p.get("tabela_dt") or []
    if a.tabela and tabela:
        unit = p["preco_unitario"]
        print(f"\nTabela (D,T) — {len(tabela)} pares, fórmula 2^D·3^T validada:")
        print(f"{'Duplos':>6} {'Triplos':>7} {'2^D·3^T':>8} {'R$':>10}")
        for ln in tabela:
            print(f"{ln['d']:>6} {ln['t']:>7} {ln['combos']:>8} "
                  f"{ln['combos']*unit:>10.2f}")

    resumo = {k: v for k, v in p.items() if k != "tabela_dt"}
    resumo["tabela_dt"] = f"<{len(tabela)} pares (use --tabela p/ listar)>"
    print(json.dumps(resumo, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
