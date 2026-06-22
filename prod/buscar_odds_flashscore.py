#!/usr/bin/env python3
"""
Coletor de odds MULTI-CASA do Flashscore.com.br (fonte Livesport).

Por quê: o Sofascore só devolve ~2 casas por jogo (no mercado BR: bet365 +
Superbet). O Flashscore.com.br, sobre o MESMO backend da Livesport que alimenta
o BetExplorer, lista ~20-24 casas brasileiras por jogo no mercado 1X2 — de longe
a melhor cobertura de NÚMERO de casas para a Loteca (que é só 1X2). O preço é que
não há API JSON pública limpa: a página renderiza via SPA e exige aceitar o
consent de cookies antes de mostrar conteúdo.

Como funciona:
  1. Sobe um Chrome REAL headful (nodriver; o mais difícil de fingerprintar),
     opcionalmente por proxy Webshare (reusa `resolver_proxy` do buscar_eventid_sofascore).
  2. Abre a home do flashscore.com.br e ACEITA O CONSENT (sem isso a página vem
     vazia).
  3. RESOLVE o jogo a partir de (home, away, data):
       a. Busca o time na API JSONP pública `s.flashscore.com/search` -> team_id
          (id Livesport, universal) + slug.
       b. Abre `/equipe/{slug}/{team_id}/` (futuros) e `/resultados/` (passados),
          raspa as linhas `.event__match` (id do elemento = `g_<sport>_<MID>`,
          times em `.event__homeParticipant/awayParticipant`, e a URL canônica
          do jogo no `a.eventRowLink`).
       c. Casa o confronto pelo ADVERSÁRIO (fuzzy) + DATA (DD.MM.).
  4. Abre a página de odds 1X2 do jogo (URL canônica + `odds/1x2-odds/
     tempo-regulamentar/`) e raspa TODAS as casas: nome
     (`[data-analytics-element="ODDS_COMPARISONS_BOOKMAKER_CELL"] a[title]`),
     bookmaker_id (`data-analytics-bookmaker-id`) e as 3 cotações
     (`a.oddsCell__odd` -> [1, X, 2]).

IMPORTANTE (geo): igual ao Sofascore/BetExplorer, o Flashscore FILTRA as casas
pelo IP/locale. Do BR (ou via `--proxy fixo --country BR`) saem as casas BR.

IDs: o match-id do Flashscore É o mesmo do BetExplorer (ambos Livesport), mas
NÃO é o event_id do Sofascore. Por isso este coletor resolve o jogo por
nome+data, e não reaproveita o event_id do `buscar_eventid_sofascore.py`.

Uso (CLI) — MESMA interface do buscar_eventid_sofascore.py (data home away + flags):
    # resolve por nome + data e coleta as odds (IP da máquina):
    python3 buscar_odds_flashscore.py 2026-06-20 "Alemanha" "Costa do Marfim"

    # search-first (resolve a página do time e casa por adversário+data):
    python3 buscar_odds_flashscore.py 2026-06-20 "Alemanha" "Costa do Marfim" --modo id

    # proxy BR fixo (p/ IP sujo):
    python3 buscar_odds_flashscore.py 2026-06-20 "Alemanha" "Costa do Marfim" --proxy fixo --country BR

Uso (import):
    from buscar_odds_flashscore import buscar_odds_flashscore
    res = buscar_odds_flashscore("2026-06-20", "Alemanha", "Costa do Marfim")
    print(res["n_casas"], res["odds_por_casa"][0])

Pré-requisitos: nodriver + Chrome/Xvfb (ambiente RSCode). Proxy via HubService.
"""
import re
import os
import sys
import json
import fcntl
import asyncio
import datetime
import argparse
import unicodedata

import nodriver as uc
from nodriver import cdp

# Reusa do buscar_eventid_sofascore.py: o resolvedor de proxy E todo o miolo de casamento
# de nomes (funções puras sobre listas de eventos) — assim a lógica de match fica
# em UM lugar só. O Flashscore só precisa ADAPTAR o evento do feed pro formato que
# essas funções esperam ({homeTeam,awayTeam,tournament,startTimestamp}).
# Também reusa os helpers GENÉRICOS da camada LLM (chamada ao Hub, parse de JSON,
# formatação de pista) — só o de-para de nomes (_canonico) e o system prompt são
# locais, porque o Flashscore.com.br é PT (≠ Sofascore, em inglês).
from buscar_eventid_sofascore import (
    resolver_proxy, _egress_ip,
    normalize, name_score, _pista, _melhor_evento, _ev_date,
    MATCH_THRESHOLD,
    _hub_generate, _extrair_json, _pista_txt, _sys_loteca, _loteca_contexto,
)

ORIGIN = "https://www.flashscore.com.br"
SEARCH_URL = ("https://s.flashscore.com/search/?q={q}&l=4&s=1&f=1%3B1&pid=2&sid=1")

# Feed de AGENDA por data (uso INTERNO: alimenta o casamento por nome+data, como o
# buscar_eventid_sofascore faz com a agenda do Sofascore — não há modo de listagem standalone).
# Uma requisição devolve TODOS os jogos de futebol da janela da data, com mid +
# times + liga + horário. offset: 0=hoje, -1=ontem, +1=amanhã (relativo a "hoje").
# Precisa do header x-fsign (valor estático público do app web da Livesport).
FEED_URL = "https://global.flashscore.ninja/401/x/feed/f_1_{offset}_0_pt-br_1"
FSIGN = "SW9D1eZo"

CONSENT_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "button#didomi-notice-agree-button",
    ".css-47sehv",
]


# --------------------------------------------------------------------------- #
# Apelidos DEDICADOS ao Flashscore (de-para Loteca -> Flashscore.com.br, em PT).
# Arquivo SEPARADO do apelidos_loteca_sofascore.json: a tabela do Sofascore tem
# seleções em INGLÊS (Alemanha->Germany), que quebrariam o match aqui (Flashscore
# é PT). Por isso o casamento é RAW-FIRST e só cai neste de-para se ficar abaixo
# do threshold. O `paises`/`uf` é o mesmo esquema (reusa a maquinaria geo do
# buscar_eventid_sofascore via `_pista`); aqui só sobrescrevemos o de-para de `times`.
# --------------------------------------------------------------------------- #
_APELIDOS_FS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "apelidos_loteca_flashscore.json")


def _carregar_apelidos_fs(path=_APELIDOS_FS_PATH):
    """Lê só a seção 'times' do apelidos do Flashscore. -> dict normalizado."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}
    return {normalize(k): v for k, v in (d.get("times") or {}).items()}


_FS_TIMES = _carregar_apelidos_fs()


def _canonico(nome):
    """De-para Loteca -> Flashscore (PT). Igual ao do buscar_eventid_sofascore, mas sobre a
    tabela do FLASHSCORE. Descarta sufixo de desambiguação com barra (RACING/ARG)."""
    base = (nome or "").split("/", 1)[0].strip()
    return _FS_TIMES.get(normalize(base), base or nome)


# --------------------------------------------------------------------------- #
# Helpers de navegação / página
# --------------------------------------------------------------------------- #
async def _goto(tab, url, timeout=40):
    """Navega via CDP e espera readyState=complete (não usa tab.get())."""
    await tab.send(cdp.page.navigate(url))
    for _ in range(timeout * 2):
        await asyncio.sleep(0.5)
        try:
            if await tab.evaluate("document.readyState") == "complete":
                return tab
        except Exception:
            pass
    return tab


async def _consent(tab):
    """Aceita o banner de cookies (sem isso o conteúdo não renderiza)."""
    for sel in CONSENT_SELECTORS:
        try:
            await tab.evaluate(
                f"(document.querySelector({sel!r})||{{}}).click "
                f"&& document.querySelector({sel!r}).click()")
        except Exception:
            pass


def _jval(o):
    """Desembrulha o retorno do tab.evaluate (string JSON) -> objeto Python."""
    s = getattr(o, "value", o)
    try:
        return json.loads(s)
    except Exception:
        return None


async def _eval_json(tab, expr):
    """Avalia uma expr JS que retorna JSON.stringify(...) e desserializa."""
    return _jval(await tab.evaluate(expr))


def _unwrap(o):
    """Desembrulha retorno do tab.evaluate (objeto c/ .value OU primitivo cru)."""
    return getattr(o, "value", o)


async def _esperar(tab, css, tentativas=10, intervalo=2.5):
    """Espera ativa por >=1 elemento `css` (SPA carrega via feed assíncrono)."""
    for _ in range(tentativas):
        await asyncio.sleep(intervalo)
        try:
            n = _unwrap(await tab.evaluate(
                f"document.querySelectorAll({css!r}).length"))
            n = int(n)
        except Exception:
            n = 0
        if n > 0:
            return n
    return 0


def _norm(s):
    """lower + sem acento + só alfanumérico/espaço (p/ casar nomes)."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", _norm(s)).strip("-")


# --------------------------------------------------------------------------- #
# Resolução do jogo (busca JSONP -> página do time -> casa por adversário+data)
# --------------------------------------------------------------------------- #
async def _buscar_time(tab, nome, tentativas=3):
    """Busca um time na API JSONP pública. Retorna [{id, slug, title}] (futebol).

    A `s.flashscore.com/search` é intermitente (responde vazio sob throttle), por
    isso tenta algumas vezes. OBS: pode não indexar bem nomes em PT — se vier
    vazio, use `--team-url` (página /equipe/ do time).
    """
    url = SEARCH_URL.format(q=re.sub(r"\s+", "%20", nome.strip()))
    expr = """
        (async () => {
          try {
            const r = await fetch(%r);
            return await r.text();
          } catch (e) { return ""; }
        })()
    """ % url
    for _ in range(tentativas):
        txt = _unwrap(await tab.evaluate(expr, await_promise=True)) or ""
        m = re.search(r"\((\{.*\})\)\s*;?\s*$", txt.strip(), re.S)
        if m:
            try:
                data = json.loads(m.group(1))
            except Exception:
                data = {}
            out = []
            for r in data.get("results", []):
                if r.get("type") == "participants" and r.get("sport_id") == 1 and r.get("id"):
                    out.append({"id": r["id"], "slug": r.get("url") or "",
                                "title": r.get("title") or ""})
            if out:
                return out
        await asyncio.sleep(1.5)
    return []


def _parse_team_url(team_url):
    """Extrai (slug, team_id) de uma URL /equipe/{slug}/{id}/ (ou /team/...)."""
    m = re.search(r"/(?:equipe|team)/([^/]+)/([A-Za-z0-9]{6,})/", team_url)
    if not m:
        raise RuntimeError(f"--team-url inválida (esperado /equipe/SLUG/ID/): {team_url}")
    return m.group(1), m.group(2)


def _melhor_time(cands, nome):
    """Escolhe o participante cujo título melhor casa com `nome`."""
    alvo = _norm(nome)
    best, score = None, -1
    for c in cands:
        t = _norm(c["title"])
        if t == alvo:
            return c
        s = 0
        if alvo in t or t in alvo:
            s = 2
        elif set(alvo.split()) & set(t.split()):
            s = 1
        if s > score:
            best, score = c, s
    return best


async def _eventos_time(tab, slug, team_id):
    """Raspa jogos (futuros + passados) da página do time. -> [{mid, home, away, data, url}]."""
    eventos = []
    base = f"{ORIGIN}/equipe/{slug or 'x'}/{team_id}"
    for sub in ("", "/resultados"):
        await _goto(tab, f"{base}{sub}/")
        if not await _esperar(tab, ".event__match"):
            continue
        rows = await _eval_json(tab, r"""JSON.stringify((()=>{
            const out=[];
            document.querySelectorAll('.event__match').forEach(e=>{
              const mid=(e.id||'').replace(/^g_\d+_/,'');
              const home=(e.querySelector('.event__homeParticipant, [class*="homeParticipant"]')||{}).innerText||'';
              const away=(e.querySelector('.event__awayParticipant, [class*="awayParticipant"]')||{}).innerText||'';
              const dt=(e.querySelector('.event__time, [class*="event__time"]')||{}).innerText||'';
              const a=e.querySelector('a.eventRowLink, a[href*="/jogo/"]');
              out.push({mid, home:home.trim(), away:away.trim(),
                        data:dt.trim(), url:a?a.getAttribute('href'):null});
            });
            return out;
        })())""") or []
        eventos.extend(rows)
    # dedup por mid
    vistos, uniq = set(), []
    for e in eventos:
        if e.get("mid") and e["mid"] not in vistos:
            vistos.add(e["mid"]); uniq.append(e)
    return uniq


async def _resolver_por_time(tab, home, away, data, team_url=None):
    """Via por time (search-first): resolve a página /equipe/ (busca JSONP ou
    `--team-url`) e casa o confronto por ADVERSÁRIO + DATA. -> dict do jogo (com
    mid + url, metodo='lista-do-time'). Levanta se não achar.

    É o análogo do `_fallback_por_time`/`_buscar_por_id` do buscar_eventid_sofascore: serve
    tanto de FALLBACK determinístico (modo fuzzy, quando a agenda não casa) quanto
    de caminho PRIMÁRIO (modo id / `--team-url`)."""
    if team_url:
        slug, team_id = _parse_team_url(team_url)
        time = {"slug": slug, "id": team_id, "title": slug}
    else:
        cands = await _buscar_time(tab, home)
        if not cands:  # tenta pelo visitante como âncora
            cands = await _buscar_time(tab, away)
            home, away = away, home
        if not cands:
            raise RuntimeError(
                f"busca não encontrou time p/ '{home}' nem '{away}' "
                f"(a busca do Flashscore é instável/PT). Use --team-url.")
        time = _melhor_time(cands, home)

    eventos = await _eventos_time(tab, time["slug"], time["id"])
    if not eventos:
        raise RuntimeError(f"sem jogos na página do time {time['title']} ({time['id']}).")

    # alvo de data em DD.MM. (formato exibido pelo Flashscore)
    dd_mm = None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", data or "")
    if m:
        dd_mm = f"{m.group(3)}.{m.group(2)}."
    adv = _norm(away)

    melhor, pont = None, -1
    for e in eventos:
        blob = _norm(e.get("home", "") + " " + e.get("away", ""))
        adv_ok = adv in blob or bool(set(adv.split()) & set(blob.split()))
        if not adv_ok:
            continue
        p = 1 + (1 if (dd_mm and dd_mm in (e.get("data") or "")) else 0)
        if p > pont:
            melhor, pont = e, p
    if not melhor:
        raise RuntimeError(
            f"não casei '{away}' (data {data}) nos jogos de {time['title']}. "
            f"Passe --team-url do mandante.")
    out = dict(melhor)
    out["metodo"] = "lista-do-time"      # espelha o metodo homônimo do buscar_eventid_sofascore
    if out.get("url") and not out["url"].startswith("http"):
        out["url"] = ORIGIN + out["url"]
    elif not out.get("url") and out.get("mid"):
        out["url"] = await _canonical_from_mid(tab, out["mid"])
    return out


async def _resolver_jogo(tab, home, away, data, team_url=None,
                         ph=None, pa=None, janela_dias=2, modo="fuzzy",
                         auditar=False, llm_model="claude-sonnet-4-5"):
    """Resolve o jogo -> dict do evento (com mid + url). Levanta se não achar.

    Espelha a ORQUESTRAÇÃO do `_buscar` do buscar_eventid_sofascore (MESMA ordem de etapas):
      - `modo id` / `--team-url`: vai DIRETO pela via por time (sem agenda, sem
        LLM) — análogo do ramo `if modo == "id": ... return` do buscar_eventid_sofascore.
      - `modo fuzzy` (default): (1) agenda da DATA (feed da Livesport) + apelidos +
        geo + invertido; (2) se NÃO casa, FALLBACK determinístico por time
        (lista-do-time) ANTES do LLM — igual ao buscar_eventid_sofascore; (3) auditoria por
        LLM: VALIDA o achado (consultivo, anexa `auditoria`) OU, se nada casou,
        RESGATA na agenda do dia (LLM dá o nome canônico PT -> re-casa no feed).
    """
    # modo id / team_url: caminho primário por time, sem agenda nem LLM.
    if modo == "id" or team_url:
        return await _resolver_por_time(tab, home, away, data, team_url=team_url)

    # modo fuzzy: 1) agenda da DATA (feed) com o miolo reusado do buscar_eventid_sofascore.
    res, erro_fb = None, None
    ev, jogos = await _resolver_via_feed(tab, home, away, data, ph=ph, pa=pa,
                                         janela_dias=janela_dias)
    if ev:
        res = ev
    else:
        # 2) fallback determinístico por time ANTES do LLM (igual ao buscar_eventid_sofascore).
        print(f"[info] não casou na agenda de {data}; tentando a via por time…",
              file=sys.stderr)
        try:
            res = await _resolver_por_time(tab, home, away, data)
        except Exception as e:
            erro_fb = str(e)
            print(f"[info] via por time não achou: {erro_fb}", file=sys.stderr)

    # 3) auditoria por LLM: valida o achado OU resgata o não-achado na agenda do dia.
    if auditar:
        if res is not None:
            res["auditoria"] = await _auditar_match_fs(
                res, data, home, away, ph, pa, llm_model)
        elif jogos:
            res = await _resgatar_llm_fs(tab, data, home, away, jogos,
                                         ph, pa, llm_model)

    if res is None:
        raise RuntimeError(erro_fb or "não foi possível resolver o jogo.")
    return res


# --------------------------------------------------------------------------- #
# Agenda por DATA (feed da Livesport) — INTERNA, insumo do casamento por nome+data
# --------------------------------------------------------------------------- #
def _parse_feed(txt):
    """Parseia o feed delimitado da Livesport -> [{mid, home, away, ...}].

    Formato: blocos separados por '~'; dentro, pares 'CHAVE÷VALOR' por '¬'.
    Cabeçalho de liga tem 'ZA' (nome) e 'ZY' (país); jogo tem 'AA' (match-id),
    'AE'/'AF' (times), 'AD' (timestamp unix), 'AG'/'AH' (placar), 'AB' (status).
    """
    jogos, liga, pais = [], None, None
    for bloco in (txt or "").split("~"):
        d = {}
        for par in bloco.split("¬"):
            if "÷" in par:
                k, v = par.split("÷", 1)
                d[k] = v
        if "ZA" in d:                       # cabeçalho de liga
            liga, pais = d.get("ZA"), d.get("ZY")
        if "AA" in d:                       # registro de jogo
            try:
                dt = datetime.datetime.fromtimestamp(int(d["AD"])) if d.get("AD") else None
            except Exception:
                dt = None
            jogos.append({
                "mid": d.get("AA"),
                "home": (d.get("AE") or "").strip(),
                "away": (d.get("AF") or "").strip(),
                "placar": ((d.get("AG") or "") + "-" + (d.get("AH") or "")
                           if d.get("AG") is not None and d.get("AH") is not None else None),
                "status": d.get("AB"),
                "dt": dt,
                "data": dt.date().isoformat() if dt else None,
                "hora": dt.strftime("%H:%M") if dt else None,
                "liga": liga, "pais": pais,
            })
    return jogos


async def _feed_text(tab, offset):
    """Busca o feed de agenda do offset (in-page fetch com o header x-fsign)."""
    url = FEED_URL.format(offset=offset)
    expr = """
        (async () => {
          try {
            const r = await fetch(%r, {headers: {"x-fsign": %r}});
            return await r.text();
          } catch (e) { return ""; }
        })()
    """ % (url, FSIGN)
    return _unwrap(await tab.evaluate(expr, await_promise=True)) or ""


async def listar_jogos_data(tab, data, hoje=None):
    """Busca TODOS os jogos de futebol de uma data (AAAA-MM-DD). -> [dict]. INTERNA.

    Uma requisição ao feed da Livesport devolve o dia inteiro (todas as ligas/
    países visíveis do locale), cada jogo já com o `mid` que alimenta a coleta de
    odds — é o insumo do casamento por nome+data (`_resolver_via_feed`), não um
    modo de listagem exposto. Filtra pela data exata; se vier vazio (drift de fuso
    no offset), tenta os offsets vizinhos.
    """
    alvo = datetime.date.fromisoformat(data)
    base = hoje or datetime.date.today()
    offset = (alvo - base).days
    vistos, jogos = set(), []
    for off in (offset, offset - 1, offset + 1):
        txt = await _feed_text(tab, off)
        for j in _parse_feed(txt):
            if j.get("data") == data and j.get("mid") and j["mid"] not in vistos:
                vistos.add(j["mid"])
                jogos.append(j)
        if jogos:            # o feed central já traz a data completa
            break
    jogos.sort(key=lambda j: (j.get("hora") or "", j.get("liga") or ""))
    return jogos


async def _canonical_from_mid(tab, mid):
    """Resolve a URL canônica do jogo a partir do mid (/jogo/{mid}/ redireciona)."""
    await _goto(tab, f"{ORIGIN}/jogo/{mid}/")
    for _ in range(8):
        href = _unwrap(await tab.evaluate("location.href"))
        if href and "/jogo/futebol/" in href:
            return href
        await asyncio.sleep(0.5)
    return _unwrap(await tab.evaluate("location.href"))


def _feed_ev(j):
    """Adapta um jogo do feed pro formato que os matchers do buscar_eventid_sofascore
    esperam ({homeTeam,awayTeam,tournament,startTimestamp}). O país vem por NOME
    (o feed não dá alpha-2/3), então o bônus de país não dispara — mas o de UF
    (textual, no nome do time/liga) sim. Guarda o jogo cru em `_feed`."""
    ts = int(j["dt"].timestamp()) if j.get("dt") else None
    pais_nome = j.get("pais") or ""
    return {
        "id": j.get("mid"),
        "homeTeam": {"name": j.get("home", ""), "country": {"name": pais_nome}},
        "awayTeam": {"name": j.get("away", ""), "country": {"name": pais_nome}},
        "tournament": {"name": j.get("liga") or ""},
        "startTimestamp": ts,
        "_feed": j,
    }


def _casar_no_feed(jogos, home, away, ph, pa):
    """Roda o matcher reusado sobre uma lista de jogos do feed. RAW-FIRST: casa
    com os nomes crus (Flashscore é PT, igual à Loteca) e só cai no nome canônico
    do apelidos.json se ficar abaixo do threshold (a tabela é Sofascore-flavored,
    com seleções em inglês). -> (jogo_cru, score, invertido) ou (None, 0, False).
    """
    if not jogos:
        return None, 0.0, False
    eventos = [_feed_ev(j) for j in jogos]
    ev, score, inv = _melhor_evento(eventos, home, away, ph, pa)
    if not ev or score < MATCH_THRESHOLD:
        hc, ac = _canonico(home), _canonico(away)
        if (normalize(hc), normalize(ac)) != (normalize(home), normalize(away)):
            ev2, score2, inv2 = _melhor_evento(eventos, hc, ac, ph, pa)
            if ev2 and score2 > score:
                ev, score, inv = ev2, score2, inv2
    if not ev or score < MATCH_THRESHOLD:
        return None, score, inv
    return ev["_feed"], score, inv


async def _resolver_via_feed(tab, home, away, data, ph=None, pa=None,
                             janela_dias=2):
    """Resolve um confronto pela agenda da data, reusando o `_melhor_evento` do
    buscar_eventid_sofascore (name_score forte + teste invertido + gate por threshold +
    desempate geográfico). -> (dict do jogo c/ `url` canônica | None, jogos).

    Janela PREGUIÇOSA: tenta só a data exata primeiro (caminho rápido); só abre os
    vizinhos ±`janela_dias` (uma requisição de feed a mais cada) se não casar — em
    geral por fuso, quando o jogo aparece no dia adjacente. O 2º retorno é a agenda
    ACUMULADA (toda a janela quando NÃO casou) — insumo p/ o resgate por LLM.
    """
    alvo = datetime.date.fromisoformat(data)
    vistos = set()
    # offsets em ordem de prioridade: 0, +1, -1, +2, -2, ...
    offs = [0] + [s * k for k in range(1, max(janela_dias, 0) + 1) for s in (1, -1)]
    melhor, melhor_sc, melhor_inv = None, 0.0, False
    jogos = []
    for k in offs:
        d = (alvo + datetime.timedelta(days=k)).isoformat()
        novos = [j for j in await listar_jogos_data(tab, d)
                 if j.get("mid") and j["mid"] not in vistos]
        for j in novos:
            vistos.add(j["mid"])
        jogos.extend(novos)
        j, sc, inv = _casar_no_feed(jogos, home, away, ph, pa)
        if j and sc > melhor_sc:
            melhor, melhor_sc, melhor_inv = j, sc, inv
        if melhor:                     # já casou -> não abre mais vizinhos
            break

    if not melhor:
        return None, jogos
    out = dict(melhor)
    out["score"] = round(melhor_sc, 3)
    out["invertido"] = melhor_inv
    out["metodo"] = "agenda"
    out["url"] = await _canonical_from_mid(tab, out["mid"])
    return out, jogos


# --------------------------------------------------------------------------- #
# Auditoria por LLM (Claude via Hub) — etapa final OPCIONAL, p/ UM jogo. Espelha
# a do buscar_eventid_sofascore, mas adaptada ao Flashscore:
#   (1) valida se o match da agenda é plausível (pega o falso-positivo que passa no
#       threshold quando o jogo verdadeiro não está no feed e o fuzzy casa com um
#       homônimo);
#   (2) se NÃO casou, RESGATA — e aqui é MAIS SIMPLES que no Sofascore: o LLM dá o
#       nome canônico PT de cada time e eu re-caso na AGENDA DO DIA INTEIRO (que já
#       tenho na mão). Não precisa resolver team_id nem verificar confronto por id:
#       o gate anti-alucinação é o próprio feed — o nome do LLM só vale se cair num
#       jogo REAL da agenda.
# O LLM nunca decide sozinho: na validação é consultivo; no resgate, só propõe
# nomes, confirmados por um match real no feed.
# --------------------------------------------------------------------------- #
_LLM_SYS_FS = (
    "Voce e um especialista em futebol que casa nomes de times da LOTECA "
    "(caixa-alta, truncados em ~17 chars, com siglas e sufixo de UF brasileira) "
    "aos nomes como aparecem no Flashscore.com.br, em PORTUGUES (selecoes e clubes "
    "em pt-br: Alemanha, Atletico-MG, Sao Paulo, Costa do Marfim — NAO em ingles). "
    "Conhece clubes de todas as divisoes do Brasil e do mundo. Responda SEMPRE so "
    "com um objeto JSON, sem texto fora dele."
)


async def _llm_json_fs(prompt, model, max_tokens=400):
    """Roda o LLM (requests sync, reusa `_hub_generate`) fora do loop e parseia. O
    system ganha o vocabulário histórico da Loteca (ancora contra falso-positivo de
    homônimo) — mesmo bloco usado no buscar_eventid_sofascore."""
    system = _sys_loteca(_LLM_SYS_FS)
    loop = asyncio.get_event_loop()
    txt = await loop.run_in_executor(
        None, lambda: _hub_generate(prompt, model, system, max_tokens))
    return _extrair_json(txt)


def _dias_diff(ev, data):
    try:
        return abs((datetime.date.fromisoformat(ev.get("data"))
                    - datetime.date.fromisoformat(data)).days)
    except Exception:
        return None


async def _auditar_match_fs(ev, data, home_in, away_in, ph, pa, model):
    """Fase 1: o LLM julga se o match da agenda é o jogo certo. Consultivo (não
    altera o match). -> dict {veredito, confianca, motivo, alias_sugerido}."""
    prompt = (
        "Confira se o jogo casado pelo sistema e REALMENTE o jogo da Loteca.\n\n"
        + _loteca_contexto(home_in, away_in) +
        f"LOTECA: '{home_in}' x '{away_in}' em {data} "
        f"(mandante: {_pista_txt(ph)}; visitante: {_pista_txt(pa)}).\n"
        f"CASOU COM (Flashscore): '{ev.get('home')}' x '{ev.get('away')}' em "
        f"{ev.get('data')} (+-{_dias_diff(ev, data)}d), liga '{ev.get('liga')}', "
        f"pais '{ev.get('pais')}', invertido={ev.get('invertido')}.\n\n"
        "Considere: time errado/homonimo (ex.: Atletico-PR x Atletico Madrid), "
        "pais ou divisao incompativel, liga que nao bate, data distante. "
        "Diferenca de grafia/abreviacao/sigla e NORMAL e nao e erro.\n"
        'Responda JSON: {"veredito":"ok"|"suspeito","confianca":0..1,'
        '"motivo":"...","alias_sugerido":{"de":"<nome loteca>",'
        '"para":"<nome flashscore>"}|null}.'
    )
    try:
        out = await _llm_json_fs(prompt, model)
        out["_ok"] = True
        print(f"[auditoria] veredito={out.get('veredito')} "
              f"({out.get('confianca')}): {out.get('motivo')}", file=sys.stderr)
        return out
    except Exception as e:
        print(f"[auditoria] falhou: {e}", file=sys.stderr)
        return {"_ok": False, "erro": str(e)[:200]}


def _candidatos_feed(jogos, nome, top=8):
    """Times da agenda cujo nome compartilha ao menos um token com `nome` — fatos
    p/ o LLM ver a grafia EXATA do Flashscore (id implícito: o próprio nome casa no
    feed depois). -> ['Nome (liga, pais)', ...]."""
    toks = set(_norm(nome).split())
    if not toks:
        return []
    out, vistos = [], set()
    for j in jogos:
        for lado in ("home", "away"):
            t = _norm(j.get(lado, ""))
            if t and t not in vistos and (toks & set(t.split())):
                vistos.add(t)
                out.append(f"{j.get(lado)} ({j.get('liga')}, {j.get('pais')})")
                if len(out) >= top:
                    return out
    return out


async def _resgatar_llm_fs(tab, data, home_in, away_in, jogos, ph, pa, model):
    """Fase 2: jogo NÃO casou na agenda. O LLM traduz apelido/sigla -> nome
    canônico PT (GALO->Atletico-MG, TIMAO->Corinthians) e EU re-caso na AGENDA do
    dia. Gate anti-alucinação: o nome do LLM só vale se cair num jogo REAL do feed.
    -> ev (metodo 'llm-resgate' + apelido sugerido + url) ou None."""
    if not jogos:
        return None

    def _fmt(c):
        return "; ".join(c) or "(nada parecido na agenda do dia)"
    prompt = (
        "O jogo da Loteca abaixo NAO casou na agenda do dia do Flashscore. Diga, "
        "para cada time, o NOME exato pelo qual ele aparece no Flashscore.com.br "
        "(em PORTUGUES), para eu re-buscar na agenda. Use seu conhecimento de "
        "futebol (apelidos: GALO=Atletico-MG, TIMAO=Corinthians; siglas; selecoes "
        "em pt-br) e o contexto (UF/pais, divisao, o adversario). A lista abaixo "
        "sao times reais da agenda do dia parecidos com a busca — use p/ ver a "
        "grafia (pode nao conter o time certo).\n\n"
        f"LOTECA: '{home_in}' x '{away_in}' em {data} "
        f"(mandante: {_pista_txt(ph)}; visitante: {_pista_txt(pa)}).\n"
        f"Agenda parecida c/ '{home_in}': {_fmt(_candidatos_feed(jogos, home_in))}\n"
        f"Agenda parecida c/ '{away_in}': {_fmt(_candidatos_feed(jogos, away_in))}\n\n"
        "Responda JSON: "
        '{"home_busca":"<nome p/ re-buscar>","away_busca":"<nome p/ re-buscar>",'
        '"motivo":"...","alias_sugerido":{"de":"<nome loteca>",'
        '"para":"<nome flashscore>"}|null}.'
    )
    try:
        escolha = await _llm_json_fs(prompt, model)
    except Exception as e:
        print(f"[resgate] falhou: {e}", file=sys.stderr)
        return None
    hq, aq = escolha.get("home_busca"), escolha.get("away_busca")
    print(f"[resgate] LLM sugere re-buscar '{hq}' x '{aq}': "
          f"{escolha.get('motivo')}", file=sys.stderr)
    if not hq or not aq:
        return None

    j, sc, inv = _casar_no_feed(jogos, hq, aq, ph, pa)
    if not j:
        print("[resgate] nomes do LLM NÃO casaram em nenhum jogo real da agenda "
              "— descartado (anti-alucinacao).", file=sys.stderr)
        return None
    out = dict(j)
    out["score"] = round(sc, 3)
    out["invertido"] = inv
    out["metodo"] = "llm-resgate"
    out["apelido_sugerido"] = escolha.get("alias_sugerido")
    out["url"] = await _canonical_from_mid(tab, out["mid"])
    print(f"[resgate] VERIFICADO na agenda: {out.get('home')} x {out.get('away')} "
          f"({out.get('data')}). Apelido sugerido: {out.get('apelido_sugerido')}",
          file=sys.stderr)
    return out


# --------------------------------------------------------------------------- #
# Gravação do apelido sugerido no apelidos_loteca_flashscore.json (--aplicar-apelido)
# --------------------------------------------------------------------------- #
def _sugestao_apelido_fs(ev, home_in, away_in):
    """Deriva (de, para) FUNDAMENTADO p/ gravar: 'de' é um dos nomes da Loteca
    consultados; 'para' é o nome REAL do Flashscore daquele lado (não o texto livre
    do LLM — anti-alucinação no valor). -> (de, para) ou None."""
    inv = bool(ev.get("invertido"))
    fh, fa = ev.get("home"), ev.get("away")
    para_home = fa if inv else fh          # nome Flashscore do mandante da Loteca
    para_away = fh if inv else fa          # nome Flashscore do visitante da Loteca

    sug = ev.get("apelido_sugerido")       # resgate (fase 2) tem prioridade
    if not sug:
        aud = ev.get("auditoria") or {}
        if aud.get("veredito") == "ok":    # validação (fase 1) só se aprovou
            sug = aud.get("alias_sugerido")
    if not isinstance(sug, dict):
        return None
    de = (sug.get("de") or "").strip()
    if not de:
        return None
    nd = normalize(de)
    if nd == normalize(home_in):
        para = para_home
    elif nd == normalize(away_in):
        para = para_away
    else:
        return None                        # 'de' não bate com nenhum lado
    if not para or normalize(de) == normalize(para):
        return None                        # sem 'para' real, ou alias trivial
    return de, para


def _aplicar_apelido_fs(de, para, path=_APELIDOS_FS_PATH):
    """Acrescenta {de: para} à seção 'times' do apelidos do Flashscore, sob lock
    exclusivo + escrita atômica (igual ao buscar_eventid_sofascore). -> 'adicionado' |
    'ja-existe' | 'erro: ...'. Reflete em memória (_FS_TIMES) p/ este processo."""
    try:
        lock_f = open(path + ".lock", "w")
    except Exception as e:
        return f"erro: não abri o lock ({e})"
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return f"erro: não li o apelidos ({e})"
        times = data.setdefault("times", {})
        if normalize(de) in {normalize(k) for k in times}:
            return "ja-existe"
        times[de] = para
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        except Exception as e:
            return f"erro: não gravei o apelidos ({e})"
        _FS_TIMES[normalize(de)] = para
        return "adicionado"
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


# --------------------------------------------------------------------------- #
# Coleta das odds (página 1X2 -> todas as casas)
# --------------------------------------------------------------------------- #
def _odds_url(match_url):
    """Da URL canônica do jogo monta a URL de odds 1X2 (tempo regulamentar)."""
    base, _, query = match_url.partition("?")
    if not base.endswith("/"):
        base += "/"
    url = base + "odds/1x2-odds/tempo-regulamentar/"
    if query:
        url += "?" + query
    return url


async def coletar_odds(tab, match_url):
    """Abre a página de odds 1X2 e raspa todas as casas. -> dict."""
    url = _odds_url(match_url if match_url.startswith("http") else ORIGIN + match_url)
    await _goto(tab, url)
    await _esperar(tab, '[data-analytics-element="ODDS_COMPARISONS_BOOKMAKER_CELL"]')

    dados = await _eval_json(tab, r"""JSON.stringify((()=>{
        const casas=[];
        document.querySelectorAll('.ui-table__row').forEach(row=>{
          const cell=row.querySelector('[data-analytics-element="ODDS_COMPARISONS_BOOKMAKER_CELL"]');
          if(!cell) return;
          const a=cell.querySelector('a[title]');
          const nome=a?a.getAttribute('title').trim():null;
          const bid=cell.getAttribute('data-analytics-bookmaker-id');
          const odds=[...row.querySelectorAll('a.oddsCell__odd, [class*="oddsCell__odd"]')]
            .map(e=>(e.textContent||'').trim()).filter(t=>/^\d+(\.\d+)?$/.test(t));
          if(nome) casas.push({casa:nome, bookmaker_id:bid, odds});
        });
        // dedup por bookmaker_id
        const seen={}, out=[];
        casas.forEach(c=>{ if(!seen[c.bookmaker_id]){seen[c.bookmaker_id]=1; out.push(c);} });
        const jogo=(document.querySelector('.duelParticipant, [class*="duelParticipant"]')||{}).innerText||'';
        return {casas:out, jogo:jogo.replace(/\s+/g,' ').trim(), url:location.href};
    })())""") or {}

    casas = []
    for c in (dados.get("casas") or []):
        o = c.get("odds") or []
        casas.append({
            "casa": c.get("casa"),
            "bookmaker_id": c.get("bookmaker_id"),
            "odds_1x2": {
                "casa": float(o[0]) if len(o) > 0 else None,
                "empate": float(o[1]) if len(o) > 1 else None,
                "fora": float(o[2]) if len(o) > 2 else None,
            },
        })
    if not casas:
        raise RuntimeError(f"nenhuma casa raspada em {dados.get('url') or url} "
                           f"(consent? jogo sem odds?).")
    return {
        "url": dados.get("url") or url,
        "jogo_label": dados.get("jogo"),
        "n_casas": len(casas),
        "casas_disponiveis": [c["casa"] for c in casas],
        "odds_por_casa": casas,
    }


# --------------------------------------------------------------------------- #
# Odds AO VIVO (intragame) — canal GraphQL `findOddsByEventId` da Livesport.
#
# Por quê um caminho SEPARADO de coletar_odds(): a página HTML de comparação
# (odds/1x2-odds/) mostra a linha de FECHAMENTO pré-jogo e NÃO atualiza com a bola
# rolando. As cotações que ticam in-play vêm de um GraphQL limpo (JSON, sem
# x-fsign): global.ds.lsapp.eu/odds/pq_graphql. Lá, cada mercado é por
# casa × tipo (HOME_DRAW_AWAY = 1X2) × tempo (FULL_TIME), com `value` (cotação
# corrente) + `opening` e a flag `hasLiveBettingOffers` — o sinal real de que a
# casa está precificando AO VIVO. Cobertura é fina: na maioria dos jogos só um
# punhado de casas oferece live (em liga grande, mais). Devolve no MESMO formato
# de coletar_odds() (odds_por_casa/odds_1x2 em orientação FLASHSCORE: casa=mandante,
# empate, fora=visitante), pra reusar estimar_prob(..., invertido=...) sem adaptar.
# --------------------------------------------------------------------------- #
_GQL_ODDS = ("https://global.ds.lsapp.eu/odds/pq_graphql?_hash=oce&eventId={mid}"
             "&projectId=401&geoIpCode={cc}&geoIpSubdivisionCode={sub}")


def _team_ids_da_url(match_url):
    """Da URL canônica /jogo/futebol/<slug>-<homeId>/<slug>-<awayId>/... extrai
    (home_id, away_id). O 1º participante do caminho é o mandante. -> (str, str)|(None,None)."""
    m = re.search(r"/jogo/[^/]+/[^/]*?-([A-Za-z0-9]{6,})/[^/]*?-([A-Za-z0-9]{6,})/", match_url or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def _map_1x2(odds_items, home_id, away_id):
    """Mapeia os 3 EventOddsItem -> {casa, empate, fora} (orientação Flashscore).
    Empate = item com eventParticipantId nulo; mandante/visitante por id (com
    fallback posicional [mandante, visitante] na ordem do array)."""
    casa = empate = fora = None
    sobra = []
    for it in odds_items or []:
        pid = it.get("eventParticipantId")
        try:
            v = float(it.get("value")) if it.get("value") is not None else None
        except (TypeError, ValueError):
            v = None
        if pid is None:
            empate = v
        elif home_id and pid == home_id:
            casa = v
        elif away_id and pid == away_id:
            fora = v
        else:
            sobra.append(v)
    # fallback posicional p/ os participantes não casados por id
    if casa is None and sobra:
        casa = sobra.pop(0)
    if fora is None and sobra:
        fora = sobra.pop(0)
    return {"casa": casa, "empate": empate, "fora": fora}


async def buscar_odds_live_flashscore(tab, mid, match_url=None,
                                      cc="BR", sub="BRSP", so_live=True):
    """Odds 1X2 AO VIVO (intragame) de um jogo, via GraphQL da Livesport.

    Usa uma `tab` já aberta (browser/consent já resolvidos pelo chamador) e faz um
    fetch in-page do GraphQL findOddsByEventId. Filtra HOME_DRAW_AWAY + FULL_TIME.

    so_live=True (default): devolve só as casas com hasLiveBettingOffers=True (as
    que realmente precificam in-play). so_live=False: devolve todas as casas com
    1X2 FT (útil pré-jogo), marcando quais são live.

    -> dict no formato de coletar_odds():
       {url, mid, n_casas, n_casas_live, casas_disponiveis, odds_por_casa:[
          {casa, bookmaker_id, live(bool), odds_1x2:{casa,empate,fora},
           opening_1x2:{casa,empate,fora}} ]}
       Levanta RuntimeError se o GraphQL não responder; odds_por_casa pode vir
       vazia (jogo sem oferta live) — o chamador trata."""
    if not match_url:
        match_url = await _canonical_from_mid(tab, mid)
    home_id, away_id = _team_ids_da_url(match_url)

    url = _GQL_ODDS.format(mid=mid, cc=cc, sub=sub)
    expr = ("(async()=>{try{const r=await fetch(%r);return await r.text();}"
            "catch(e){return '';}})()" % url)
    raw = _unwrap(await tab.evaluate(expr, await_promise=True)) or ""
    try:
        node = json.loads(raw)["data"]["findOddsByEventId"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError(f"GraphQL de odds live não retornou JSON p/ mid={mid}")

    # de-para bookmakerId -> nome (settings.bookmakers)
    nomes = {}
    for pb in (((node.get("settings") or {}).get("bookmakers")) or []):
        bk = pb.get("bookmaker") or {}
        if bk.get("id") is not None:
            nomes[bk["id"]] = bk.get("name")

    casas = []
    for m in (node.get("odds") or []):
        if m.get("bettingType") != "HOME_DRAW_AWAY" or m.get("bettingScope") != "FULL_TIME":
            continue
        live = bool(m.get("hasLiveBettingOffers"))
        if so_live and not live:
            continue
        bid = m.get("bookmakerId")
        valores = _map_1x2(m.get("odds"), home_id, away_id)
        if None in (valores["casa"], valores["empate"], valores["fora"]):
            continue
        abert = _map_1x2(
            [{"eventParticipantId": it.get("eventParticipantId"),
              "value": it.get("opening")} for it in (m.get("odds") or [])],
            home_id, away_id)
        casas.append({
            "casa": nomes.get(bid) or (f"bk{bid}" if bid is not None else None),
            "bookmaker_id": bid,
            "live": live,
            "odds_1x2": valores,
            "opening_1x2": abert,
        })

    # dedup por bookmaker_id (mantém a 1ª ocorrência)
    seen, uniq = set(), []
    for c in casas:
        if c["bookmaker_id"] not in seen:
            seen.add(c["bookmaker_id"]); uniq.append(c)

    return {
        "url": match_url,
        "mid": mid,
        "fonte": "flashscore-live-graphql",
        "n_casas": len(uniq),
        "n_casas_live": sum(1 for c in uniq if c["live"]),
        "casas_disponiveis": [c["casa"] for c in uniq],
        "odds_por_casa": uniq,
    }


# --------------------------------------------------------------------------- #
# Orquestração
# --------------------------------------------------------------------------- #
async def _run(data=None, home=None, away=None, proxy=None, country=None,
               janela_dias=2, home_uf=None, away_uf=None, home_pais=None,
               away_pais=None, modo="fuzzy", auditar=True,
               llm_model="claude-sonnet-4-5", aplicar_apelido=False,
               team_url=None, verbose=True):
    if aplicar_apelido:
        auditar = True          # gravar exige a sugestão produzida pela auditoria
    pr = resolver_proxy(proxy, country)
    args = ["--no-sandbox"]
    if pr:
        host, port, user, pw = pr
        args.append(f"--proxy-server=http://{host}:{port}")
        if verbose:
            print(f"[proxy] {proxy}/{(country or '').upper()} via {host}:{port}", file=sys.stderr)

    browser = await uc.start(browser_args=args, sandbox=False)
    try:
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

        # home + consent (uma vez por sessão)
        await _goto(tab, ORIGIN + "/")
        await asyncio.sleep(3)
        await _consent(tab)
        await asyncio.sleep(1)

        ph, pa = _pista(home_uf, home_pais), _pista(away_uf, away_pais)
        if verbose:
            via = ("team-url" if team_url
                   else "search-first" if modo == "id" else "agenda/feed")
            print(f"[resolve] ({via}) '{home}' x '{away}' ({data})...", file=sys.stderr)
        ev = await _resolver_jogo(tab, home, away, data, team_url=team_url,
                                  ph=ph, pa=pa, janela_dias=janela_dias, modo=modo,
                                  auditar=auditar, llm_model=llm_model)
        url = ev["url"]
        jogo = {"home": ev.get("home"), "away": ev.get("away"),
                "data_exibida": ev.get("data"), "mid": ev.get("mid"),
                "metodo": ev.get("metodo"), "score": ev.get("score"),
                "invertido": ev.get("invertido")}
        if ev.get("auditoria") is not None:
            jogo["auditoria"] = ev.get("auditoria")
        if ev.get("apelido_sugerido") is not None:
            jogo["apelido_sugerido"] = ev.get("apelido_sugerido")
        if verbose:
            print(f"[resolve] mid={ev.get('mid')} ({ev.get('metodo')}) "
                  f"-> {url}", file=sys.stderr)
        # grava o apelido sugerido no apelidos_loteca_flashscore.json
        if aplicar_apelido:
            sug = _sugestao_apelido_fs(ev, home, away)
            if sug:
                estado = _aplicar_apelido_fs(*sug)
                jogo["apelido_aplicado"] = {"de": sug[0], "para": sug[1],
                                            "estado": estado}
                if verbose:
                    print(f"[apelido] {sug[0]!r} -> {sug[1]!r}: {estado}",
                          file=sys.stderr)
            elif verbose:
                print("[apelido] sem sugestão fundamentada p/ gravar.",
                      file=sys.stderr)

        res = await coletar_odds(tab, url)
        res["jogo"] = jogo
        m = re.search(r"mid=([A-Za-z0-9]+)", url) or re.search(r"/([A-Za-z0-9]{6,})/?$", url)
        res["mid"] = jogo.get("mid") or (m.group(1) if m else None)
        res["fonte"] = "flashscore.com.br"
        return res
    finally:
        try:
            browser.stop()
        except Exception:
            pass


def buscar_odds_flashscore(data=None, home=None, away=None, proxy=None,
                           country=None, janela_dias=2, home_uf=None,
                           away_uf=None, home_pais=None, away_pais=None,
                           modo="fuzzy", auditar=True,
                           llm_model="claude-sonnet-4-5", aplicar_apelido=False,
                           team_url=None, verbose=True):
    """API síncrona, espelha buscar_event_id(date, home, away, ...): mesma ordem de
    parâmetros (date->data). Extras do Flashscore: `team_url` (escape p/ a via por
    time). Retorna o dict de odds multi-casa (ver _run)."""
    return asyncio.run(_run(data, home, away, proxy=proxy, country=country,
                            janela_dias=janela_dias, home_uf=home_uf,
                            away_uf=away_uf, home_pais=home_pais,
                            away_pais=away_pais, modo=modo, auditar=auditar,
                            llm_model=llm_model, aplicar_apelido=aplicar_apelido,
                            team_url=team_url, verbose=verbose))


def main():
    # CLI espelha o buscar_eventid_sofascore.py: MESMOS inputs, MESMA ordem (date->data).
    ap = argparse.ArgumentParser(
        description="Coleta odds multi-casa (1X2) de um jogo no Flashscore.com.br "
                    "(mesma interface do buscar_eventid_sofascore.py: data home away + flags).")
    ap.add_argument("data", help="data do jogo, AAAA-MM-DD")
    ap.add_argument("home", help="time mandante (nome livre)")
    ap.add_argument("away", help="time visitante (nome livre)")
    # desempate geográfico (UF/país da Loteca)
    ap.add_argument("--home-uf", default=None, dest="home_uf",
                    help="UF do mandante (Loteca siglaUF) p/ desempate de homônimos")
    ap.add_argument("--away-uf", default=None, dest="away_uf",
                    help="UF do visitante (Loteca siglaUF) p/ desempate de homônimos")
    ap.add_argument("--home-pais", default=None, dest="home_pais",
                    help="país do mandante (Loteca siglaPais, ~ISO alpha-3)")
    ap.add_argument("--away-pais", default=None, dest="away_pais",
                    help="país do visitante (Loteca siglaPais, ~ISO alpha-3)")
    # estratégia de resolução (análogo do --modo do buscar_eventid_sofascore)
    ap.add_argument("--modo", choices=["fuzzy", "id"], default="fuzzy",
                    help="fuzzy: agenda(feed)+apelidos (offline). id: search-first, "
                         "resolve a página do time (/equipe/) e casa por adversário+data")
    ap.add_argument("--janela-dias", type=int, default=2, dest="janela_dias",
                    help="janela (em dias) p/ tolerar confronto em data próxima "
                         "(fuso); default: 2")
    # auditoria por LLM (LIGADA por padrão)
    ap.add_argument("--sem-auditar", action="store_false", dest="auditar",
                    help="DESLIGA a auditoria por LLM (Claude via Hub), que roda "
                         "por padrão: valida o match achado e resgata o não-achado")
    ap.add_argument("--llm-model", default="claude-sonnet-4-5", dest="llm_model",
                    help="modelo do Hub p/ a auditoria (default: claude-sonnet-4-5)")
    ap.add_argument("--aplicar-apelido", action="store_true", dest="aplicar_apelido",
                    help="grava no apelidos_loteca_flashscore.json o apelido sugerido "
                         "pela auditoria (implica auditoria; só grava o fundamentado)")
    ap.add_argument("--proxy", nargs="?", choices=["none", "rotativo", "fixo"],
                    const="fixo", default="none",
                    help="sem o flag: sem proxy. --proxy sozinho: fixo BR. "
                         "Tb aceita none, rotativo (IP/país muda) ou fixo (IP de um país)")
    ap.add_argument("--country", default="BR",
                    help="ISO-2 do país p/ --proxy fixo (default: BR)")
    # extras do Flashscore (não existem no buscar_eventid_sofascore)
    ap.add_argument("--team-url", default=None, dest="team_url",
                    help="[extra] URL /equipe/SLUG/ID/ do mandante: força a via por "
                         "time (search-first manual) em qualquer modo; 'home' é ignorado")
    ap.add_argument("--quiet", action="store_true", help="[extra] sem logs em stderr")
    a = ap.parse_args()

    try:
        res = buscar_odds_flashscore(a.data, a.home, a.away, proxy=a.proxy,
                                     country=a.country, janela_dias=a.janela_dias,
                                     home_uf=a.home_uf, away_uf=a.away_uf,
                                     home_pais=a.home_pais, away_pais=a.away_pais,
                                     modo=a.modo, auditar=a.auditar,
                                     llm_model=a.llm_model,
                                     aplicar_apelido=a.aplicar_apelido,
                                     team_url=a.team_url, verbose=not a.quiet)
    except Exception as e:
        print(f"[erro] {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
