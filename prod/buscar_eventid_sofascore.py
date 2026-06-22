#!/usr/bin/env python3
"""
Descobre o EVENT_ID do Sofascore para um jogo, dado a DATA e os NOMES dos times.

Como funciona (resumo):
  1. Sobe um Chrome REAL headful via nodriver (CDP direto; o mais difícil de
     fingerprintar). Opcionalmente roteia por proxy Webshare (rotativo ou IP
     fixo de um país).
  2. Abre a HOME do Sofascore (não precisa abrir a página de nenhum jogo) e
     captura o token de sessão `X-Requested-With` de qualquer chamada
     `/api/v1/...` que a própria home dispara.
  3. Com o token, faz um fetch IN-PAGE (mesma origem + cookies + proxy) em
     `/api/v1/sport/football/scheduled-events/<AAAA-MM-DD>` (a data pedida e os
     vizinhos até ±janela-dias, por fuso — preguiçoso: só abre o próximo vizinho
     se ainda não casou) e casa os times informados contra a agenda.
  4. FALLBACK (se a agenda não casar): procura cada time em `/api/v1/search/all`,
     pega a lista de confrontos do time-âncora em `/api/v1/team/<id>/events/
     last|next/<pag>` e escolhe o confronto entre os dois na data MAIS PRÓXIMA
     da pedida, dentro de uma janela de +/- N dias (`--janela-dias`, default 2).
  5. Imprime o melhor match (event_id + metadados) como JSON. O campo `metodo`
     diz se veio da "agenda" ou da "lista-do-time", e `dias_diferenca` informa a
     distância (em dias) entre a data encontrada e a pedida.

Por que a home basta: o `X-Requested-With` é por SESSÃO de browser — qualquer
`/api/v1/` da home carrega o mesmo token, e ele serve pra qualquer event_id.

IMPORTANTE (geo): o Sofascore só serve a agenda/odds em países onde opera
apostas. Rodando SEM proxy de um IP "sujo", ou com IP de país não-permitido,
as chamadas `/api/v1/` podem dar 403/timeout. Prefira `--proxy fixo --country BR`.

Uso (CLI):
    # sem proxy (usa o IP da máquina):
    python3 buscar_eventid_sofascore.py 2026-06-01 "Sao Paulo" "Palmeiras"

    # proxy rotativo (troca país a cada execução — só se país não importa):
    python3 buscar_eventid_sofascore.py 2026-06-01 "Sao Paulo" "Palmeiras" --proxy rotativo

    # proxy com IP FIXO de um país (recomendado p/ Sofascore: BR):
    python3 buscar_eventid_sofascore.py 2026-06-01 "Sao Paulo" "Palmeiras" --proxy fixo --country BR

    # aceita confronto em data próxima (ex.: +/- 10 dias) se a data exata não casar:
    python3 buscar_eventid_sofascore.py 2026-05-28 "Palmeiras" "Flamengo" --janela-dias 10

Uso (import):
    from buscar_eventid_sofascore import buscar_event_id
    res = buscar_event_id("2026-06-01", "Sao Paulo", "Palmeiras",
                          proxy="fixo", country="BR")
    print(res["event_id"])

Pré-requisitos: nodriver instalado; Chrome + Xvfb (já vêm no ambiente
RSCode/JupyterLab). Proxy vem do HubService (env HUB_API_KEY / HUB_SERVICE_URL).
"""
import os
import re
import sys
import fcntl
import json
import random
import asyncio
import argparse
import unicodedata
import datetime as dt
from difflib import SequenceMatcher

import requests
import nodriver as uc
from nodriver import cdp

ORIGIN = "https://www.sofascore.com"
HOME = ORIGIN + "/pt/"
MATCH_THRESHOLD = 0.55

HUB = os.environ.get("HUB_SERVICE_URL", "http://hub:8788")
HUB_KEY = os.environ.get("HUB_API_KEY")
GATEWAY_HOST = "p.webshare.io"          # gateway Webshare (rotativo e backbone)
GATEWAY_ROTATING_PORT = 80              # :80 = IP (e país) muda a cada conexão


# --------------------------------------------------------------------------- #
# Normalização / matching de nomes de times
# --------------------------------------------------------------------------- #
_STOP = {"fc", "ec", "sc", "ac", "cf", "afc", "futebol", "clube", "de", "do",
         "da", "the", "esporte", "esportivo", "club", "cd", "ca", "saf"}


def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def normalize(s):
    s = _strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s):
    return [t for t in normalize(s).split() if t not in _STOP]


def name_score(a, b):
    """0..1 — quão parecidos são dois nomes de time (robusto a sufixos/acentos)."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    base = SequenceMatcher(None, na, nb).ratio()
    if na in nb or nb in na:
        base = max(base, 0.9)
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if ta and tb:
        base = max(base, len(ta & tb) / len(ta | tb))
    return base


# --------------------------------------------------------------------------- #
# Apelidos (de-para de nomes) + pistas geográficas (UF / país) p/ desempate
# --------------------------------------------------------------------------- #
_APELIDOS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "apelidos_loteca_sofascore.json")


def _carregar_apelidos(path=_APELIDOS_PATH):
    """Lê apelidos.json. -> (times, paises, uf), tudo já normalizado."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}, {}, {}
    times = {normalize(k): v for k, v in (d.get("times") or {}).items()}
    paises = {k.strip().upper(): (v or "").strip().upper()
              for k, v in (d.get("paises") or {}).items()}
    uf = {k.strip().upper(): [normalize(t) for t in (v or [])]
          for k, v in (d.get("uf") or {}).items()}
    return times, paises, uf


_APELIDOS_TIMES, _PAISES, _UF_TERMOS = _carregar_apelidos()


def _canonico(nome):
    """Aplica a tabela de apelidos: nome (Loteca) -> nome canônico (Sofascore).

    Antes de consultar, descarta um sufixo de desambiguação que a Loteca às
    vezes anexa com barra (ESCOCIA/SCT, RACING/ARG, SERVIA/SER): fica só a parte
    antes da '/' — que também melhora o name_score quando não há apelido."""
    base = (nome or "").split("/", 1)[0].strip()
    return _APELIDOS_TIMES.get(normalize(base), base or nome)


def _pista(uf, pais):
    """Pista geográfica de um time da Loteca p/ desempate. -> dict ou None.

    país: a Loteca usa ~ISO alpha-3 (BRA, ARG, ESP) com quirks (ING=Inglaterra,
    COR=Coreia) e às vezes lixo (AR, MG, SP0). Só aceito código de 3 letras;
    mapeio p/ alpha-2 via apelidos.json e guardo também o alpha-3 cru (o
    Sofascore expõe os dois). UF não existe no Sofascore -> vira sinal textual.
    """
    uf = (uf or "").strip().upper()
    cod = (pais or "").strip().upper()
    a3 = cod if (len(cod) == 3 and cod.isalpha()) else None
    a2 = _PAISES.get(cod) if a3 else None
    if not uf and not a2 and not a3:
        return None
    return {"uf": uf or None, "alpha2": a2, "alpha3": a3}


def _pais_casa(country, pista):
    """True se o país da Loteca (pista) bate com o country{} do Sofascore."""
    if not pista or not country:
        return False
    a2 = (country.get("alpha2") or "").upper()
    a3 = (country.get("alpha3") or "").upper()
    return bool((pista.get("alpha2") and pista["alpha2"] == a2) or
                (pista.get("alpha3") and pista["alpha3"] == a3))


def _uf_no_texto(texto_norm, uf):
    """True se a UF aparece em `texto_norm` (já normalizado): como gentílico/nome
    do estado OU como o código de 2 letras isolado — que é o sufixo que o
    Sofascore usa p/ desambiguar homônimos ("Vitória-ES" -> token "es",
    "América-RN" -> "rn"). Guarda contra UF-lixo: só aceita as 27 siglas reais."""
    if not uf:
        return False
    u = uf.strip().upper()
    if u not in _UF_TERMOS:                      # não é UF real (lixo) -> ignora
        return False
    if u.lower() in texto_norm.split():          # sufixo "-ES" -> token "es"
        return True
    return any(t and t in texto_norm for t in _UF_TERMOS[u])


def _geo_bonus(team, ev, pista):
    """Bônus pequeno (SÓ desempate) se país/UF da Loteca batem com o candidato.

    Não entra no gate do threshold nem no score reportado — só desempata a
    ESCOLHA entre candidatos de nome parecido (ex.: dois 'América', Barcelona
    ESP x ECU). País: match no alpha-2/alpha-3. UF: o Sofascore não tem estado,
    então procuro o gentílico/nome do estado no nome do time ou no torneio.
    """
    if not pista:
        return 0.0
    b = 0.06 if _pais_casa(team.get("country") or {}, pista) else 0.0
    alvo = normalize((team.get("name") or "") + " " +
                     ((ev.get("tournament") or {}).get("name") or ""))
    if _uf_no_texto(alvo, pista.get("uf")):
        b += 0.04
    return b


def _datas(date, janela_dias=1):
    """A data pedida + vizinhos por fuso, em ordem de prioridade (0, +1, -1, +2,
    -2, …) até ±`janela_dias`. A agenda do Sofascore lista um jogo no dia vizinho
    quando o horário cai do outro lado da meia-noite UTC; alongar a janela (a
    mesma `--janela-dias` do fallback) cobre confrontos deslocados/remarcados além
    de ±1 — igual ao feed do buscar_odds_flashscore. Default ±1 (compat.)."""
    d = dt.date.fromisoformat(date)
    offs = [0] + [s * k for k in range(1, max(janela_dias, 0) + 1) for s in (1, -1)]
    return [(d + dt.timedelta(days=k)).isoformat() for k in offs]


# --------------------------------------------------------------------------- #
# Resolução do proxy (via HubService)
# --------------------------------------------------------------------------- #
def _hub_get(path, **params):
    if not HUB_KEY:
        raise RuntimeError("HUB_API_KEY não está no ambiente — necessário p/ proxy.")
    r = requests.get(f"{HUB}{path}", params=params,
                     headers={"Authorization": f"Bearer {HUB_KEY}"}, timeout=25)
    r.raise_for_status()
    return r.json()


def resolver_proxy(modo, country=None):
    """Retorna (host, port, user, pw) ou None (sem proxy).

    modo: None/"none" -> sem proxy
          "rotativo"   -> gateway :80 (IP e país mudam a cada conexão)
          "fixo"       -> uma porta backbone do país (IP estável daquele país)
    """
    if not modo or modo == "none":
        return None

    cfg = _hub_get("/proxy/config", provider="webshare")
    user, pw = cfg["username"], cfg["password"]

    if modo == "rotativo":
        return GATEWAY_HOST, GATEWAY_ROTATING_PORT, user, pw

    if modo == "fixo":
        cc = (country or "BR").upper()
        lst = _hub_get("/proxy/list", provider="webshare",
                       mode="backbone", country_code=cc, page_size=50)
        proxies = [p for p in lst.get("proxies", []) if p.get("valid", True)]
        if not proxies:
            raise RuntimeError(f"Sem proxies backbone válidos p/ {cc}.")
        # sorteia uma porta -> um IP estável daquele país (rotaciona dentro do país
        # entre execuções, mas é sticky dentro da mesma sessão de browser).
        p = random.choice(proxies)
        # cada entrada traz sua PRÓPRIA porta + username (sufixo -CC-N); o host é
        # sempre o gateway. Senha pode vir na entrada ou herdar a do config.
        return GATEWAY_HOST, p["port"], p["username"], p.get("password") or pw

    raise RuntimeError(f"modo de proxy desconhecido: {modo}")


# --------------------------------------------------------------------------- #
# Browser (nodriver) + proxy auth + navegação
# --------------------------------------------------------------------------- #
async def _goto(tab, url, timeout=30):
    """Navega via CDP e espera readyState=complete.

    NÃO usar tab.get(): com o Fetch de proxy ativo ele trava esperando um
    'network idle' que nunca chega em páginas pesadas.
    """
    await tab.send(cdp.page.navigate(url))
    for _ in range(timeout * 2):
        await asyncio.sleep(0.5)
        try:
            if await tab.evaluate("document.readyState") == "complete":
                return tab
        except Exception:
            pass
    return tab


async def _capturar_token(tab, timeout=20, tentativas=3):
    """Captura o X-Requested-With na home, esperando ATIVAMENTE até ele aparecer.

    A home dispara ~27 chamadas www.sofascore.com/api/v1 (TODAS carregam o
    token), a 1a em ~0.6s. Em vez de um sleep fixo que pode desistir cedo, usa
    espera orientada a evento (retorna no instante que o token aparece) e, se um
    carregamento falhar e nada vier dentro do timeout, RECARREGA a home e tenta
    de novo (até `tentativas`). Filtra só o host www.sofascore.com porque as
    chamadas do CDN de imagens (img.sofascore.com/api/v1) NÃO carregam o token.
    """
    tok = {"v": None}
    achou = asyncio.Event()

    def on_send(ev):
        if tok["v"] or "www.sofascore.com/api/v1/" not in ev.request.url:
            return
        h = ev.request.headers
        d = h.to_json() if hasattr(h, "to_json") else dict(h)
        for k, v in d.items():
            if k.lower() == "x-requested-with" and v:
                tok["v"] = v
                achou.set()
                return

    tab.add_handler(cdp.network.RequestWillBeSent, on_send)
    await tab.send(cdp.network.enable())

    for i in range(tentativas):
        if i == 0:
            await _goto(tab, HOME)
        else:
            print(f"[token] não veio em {timeout}s; recarregando a home "
                  f"(tentativa {i + 1}/{tentativas})…", file=sys.stderr)
            await _goto(tab, HOME)         # re-navegar re-dispara as chamadas /api/v1
        try:
            await asyncio.wait_for(achou.wait(), timeout=timeout)
            return tok["v"]
        except asyncio.TimeoutError:
            continue
    return tok["v"]


# Contador de requests por tipo — p/ comparar o custo de rede entre estratégias
# (fuzzy x search-first). Chame _reset_req() antes de cada medição.
_REQ = {"agenda": 0, "search": 0, "team": 0, "outro": 0}


def _classificar_req(path):
    if path.startswith("sport/"):
        return "agenda"
    if path.startswith("search/"):
        return "search"
    if path.startswith("team/"):
        return "team"
    return "outro"


def _reset_req():
    for k in _REQ:
        _REQ[k] = 0


async def _fetch_json(tab, path, token):
    """Faz um fetch IN-PAGE (same-origin) de /api/v1/<path> com o token. -> dict."""
    _REQ[_classificar_req(path)] += 1
    js = """(async () => {
        const r = await fetch(%s, { headers: { 'X-Requested-With': %s } });
        return JSON.stringify({ status: r.status, body: await r.text() });
    })()""" % (json.dumps(ORIGIN + "/api/v1/" + path), json.dumps(token or ""))
    out = json.loads(await tab.evaluate(js, await_promise=True))
    if out["status"] != 200:
        raise RuntimeError(f"/api/v1/{path}: HTTP {out['status']} {out['body'][:120]}")
    return json.loads(out["body"])


async def _egress_ip(tab):
    try:
        await _goto(tab, "https://api.ipify.org?format=json", timeout=15)
        m = re.search(r'"ip":"([^"]+)"', await tab.get_content())
        return m.group(1) if m else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Núcleo: achar o evento
# --------------------------------------------------------------------------- #
def _score_evento(ev, home, away, ph=None, pa=None):
    """-> (base, bonus, invertido).

    base  = score de nome (0..1); usado no gate do threshold e reportado.
    bonus = desempate geográfico (UF/país); só influi na ESCOLHA, nunca no gate.
    ph/pa = pistas (UF/país) do time mandante/visitante da Loteca.
    """
    h = ev.get("homeTeam") or {}
    a = ev.get("awayTeam") or {}
    hn, an = h.get("name", ""), a.get("name", "")
    s_dir = min(name_score(home, hn), name_score(away, an))   # mandante/visitante
    s_inv = min(name_score(home, an), name_score(away, hn))   # invertidos
    inv = s_inv > s_dir
    base = s_inv if inv else s_dir
    th, ta = (a, h) if inv else (h, a)      # th ~ loteca-home, ta ~ loteca-away
    bonus = _geo_bonus(th, ev, ph) + _geo_bonus(ta, ev, pa)
    return base, bonus, inv


def _melhor_evento(eventos, home, away, ph=None, pa=None):
    """Casa (home, away) contra a lista. -> (evento, base_score, invertido).
    Ranqueia por base+bonus (desempate geográfico); reporta o base puro."""
    melhor, melhor_rank, melhor_base, melhor_inv = None, -1.0, 0.0, False
    for ev in eventos:
        base, bonus, inv = _score_evento(ev, home, away, ph, pa)
        rank = base + bonus
        if rank > melhor_rank:
            melhor, melhor_rank, melhor_base, melhor_inv = ev, rank, base, inv
    return melhor, melhor_base, melhor_inv


def _ev_date(ev):
    ts = ev.get("startTimestamp")
    return dt.datetime.utcfromtimestamp(ts).date() if ts else None


def _montar_resultado(ev, date, home, away, score, invertido, metodo, dias_diff=0):
    ts = ev.get("startTimestamp")
    return {
        "event_id": ev.get("id"),
        "home_sofascore": ev.get("homeTeam", {}).get("name"),
        "away_sofascore": ev.get("awayTeam", {}).get("name"),
        "invertido": invertido,          # True = os times vieram trocados na entrada
        "score": round(score, 3),
        "metodo": metodo,                # "agenda" ou "lista-do-time"
        "data_encontrada": _ev_date(ev).isoformat() if _ev_date(ev) else None,
        "dias_diferenca": dias_diff,     # |data encontrada - data pedida|, em dias
        "torneio": ev.get("tournament", {}).get("name"),
        "inicio_utc": (dt.datetime.utcfromtimestamp(ts).isoformat() + "Z"
                       if ts else None),
        "status": ev.get("status", {}).get("type"),
        "slug": ev.get("slug"),
        "custom_id": ev.get("customId"),
        "url": (f"{ORIGIN}/pt/football/match/{ev.get('slug')}/"
                f"{ev.get('customId')}#id:{ev.get('id')}"),
        "consulta": {"date": date, "home": home, "away": away},
    }


# --------------------------------------------------------------------------- #
# Fallback: lista de confrontos de cada time (quando a agenda da data não casa)
# --------------------------------------------------------------------------- #
async def _buscar_time(tab, nome, token, pista=None):
    """Procura o time pelo nome. -> (team_id, nome_sofascore, score) ou None.
    Se houver pista de país, usa um pequeno bônus p/ desempatar homônimos."""
    try:
        data = await _fetch_json(
            tab, "search/all?q=" + requests.utils.quote(nome), token)
    except Exception as e:
        print(f"[aviso] search '{nome}': {e}", file=sys.stderr)
        return None
    melhor = None       # (id, nome, base, rank)
    for r in data.get("results", []):
        if r.get("type") != "team":
            continue
        ent = r.get("entity", {})
        if (ent.get("sport") or {}).get("name") not in (None, "Football"):
            continue
        base = name_score(nome, ent.get("name", ""))
        rank = base + (0.06 if _pais_casa(ent.get("country") or {}, pista) else 0.0)
        # UF explícita no nome (ex.: "Vitória-ES") é um desempate FORTE p/ escolher
        # o time certo entre homônimos de um clube grande (EC Vitória/BA x Vitória-
        # ES): supera a vantagem de base que o nome "limpo" do clube grande teria.
        if pista and _uf_no_texto(normalize(ent.get("name", "")), pista.get("uf")):
            rank += 0.15
        if melhor is None or rank > melhor[3]:
            melhor = (ent.get("id"), ent.get("name"), base, rank)
    return melhor[:3] if melhor else None


async def _eventos_do_time(tab, team_id, token, alvo=None, janela_dias=2):
    """Junta os confrontos do time (passados + futuros), paginando SÓ o
    necessário em vez de um número fixo de páginas.

    Os endpoints `team/<id>/events/last/<p>` (passados, página 0 = mais recentes)
    e `.../next/<p>` (futuros) devolvem ~30 jogos por página. Com `alvo` (a data
    pedida), aprofunda o histórico até a página já ter alcançado ANTES de
    `alvo - janela` (e o futuro até passar de `alvo + janela`), parando assim que
    a data buscada ficou coberta. Sem trava de páginas: a paginação termina
    naturalmente quando cobre o alvo OU quando o histórico acaba (página vazia) —
    então até um confronto de muitos anos atrás é alcançado. Sem `alvo`, mantém o
    comportamento antigo (3 last + 2 next).

    Por que importa: times ativos jogam ~2x/semana, então 3 páginas (~90 jogos)
    cobrem poucos meses; um confronto de >~1 mês atrás "rolava para fora" das
    páginas fixas e o fallback não o via.
    """
    eventos, vistos = [], set()

    async def _coletar(path):
        try:
            data = await _fetch_json(tab, path, token)
        except Exception:
            return None
        evs = data.get("events", [])
        for ev in evs:
            if ev.get("id") not in vistos:
                vistos.add(ev.get("id"))
                eventos.append(ev)
        return evs

    if alvo is None:
        for path in ([f"team/{team_id}/events/last/{p}" for p in range(3)] +
                     [f"team/{team_id}/events/next/{p}" for p in range(2)]):
            await _coletar(path)
        return eventos

    lo = alvo - dt.timedelta(days=janela_dias)   # limite inferior (no passado)
    hi = alvo + dt.timedelta(days=janela_dias)   # limite superior (no futuro)

    async def _paginar(tipo, parar):
        pg = 0
        while True:
            evs = await _coletar(f"team/{team_id}/events/{tipo}/{pg}")
            if evs is None or not evs:        # erro ou acabou o histórico -> para
                break
            datas = [d for d in (_ev_date(e) for e in evs) if d]
            if datas and parar(datas):        # já cobriu a data alvo -> para
                break
            pg += 1

    # last vai do recente p/ o antigo: para quando o jogo mais ANTIGO da página
    # já é anterior a `lo` (logo a data pedida, se existir, já passou).
    await _paginar("last", lambda ds: min(ds) < lo)
    # next vai do recente p/ o futuro: para quando o mais NOVO da página passa `hi`.
    await _paginar("next", lambda ds: max(ds) > hi)
    return eventos


async def _fallback_por_time(tab, date, home, away, token, janela_dias,
                             ph=None, pa=None):
    """Acha o confronto home x away na data MAIS PRÓXIMA da pedida, via a lista
    de jogos de um dos times. Retorna o dict do resultado ou levanta."""
    alvo = dt.date.fromisoformat(date)

    # acha os dois times; usa o de MAIOR confiança como âncora (lista a buscar).
    t_home = await _buscar_time(tab, home, token, ph)
    t_away = await _buscar_time(tab, away, token, pa)
    achados = [t for t in (t_home, t_away) if t]
    if not achados:
        raise RuntimeError(f"não achei nenhum dos times no Sofascore "
                           f"('{home}', '{away}').")
    ancora = max(achados, key=lambda t: t[2])
    tid, tnome, _ = ancora
    print(f"[fallback] usando lista de confrontos de '{tnome}' (id {tid})",
          file=sys.stderr)

    eventos = await _eventos_do_time(tab, tid, token,
                                     alvo=alvo, janela_dias=janela_dias)
    if not eventos:
        raise RuntimeError(f"a lista de jogos de '{tnome}' veio vazia.")

    # entre os jogos que casam home x away, escolhe o de data mais próxima.
    candidatos = []
    for ev in eventos:
        base, bonus, inv = _score_evento(ev, home, away, ph, pa)
        d = _ev_date(ev)
        if base >= MATCH_THRESHOLD and d is not None:
            candidatos.append((abs((d - alvo).days), base + bonus, base, ev, inv))
    if not candidatos:
        raise RuntimeError(
            f"'{tnome}' não tem confronto contra o outro time nos jogos "
            f"listados (passados/futuros).")

    # mais perto da data; em empate de dias, maior rank (base + desempate geo).
    candidatos.sort(key=lambda c: (c[0], -c[1]))
    dias, _rank, score, ev, inv = candidatos[0]
    if dias > janela_dias:
        raise RuntimeError(
            f"confronto mais próximo está a {dias} dias de {date} "
            f"(em {_ev_date(ev)}), fora da janela de ±{janela_dias} dias. "
            f"Rode de novo com essa data ou aumente --janela-dias.")
    return _montar_resultado(ev, date, home, away, score, inv,
                             metodo="lista-do-time", dias_diff=dias)


# --------------------------------------------------------------------------- #
# Search-first: resolve cada time -> team_id (search/all) e casa o jogo por ID
# --------------------------------------------------------------------------- #
def _ids_evento(ev):
    return ((ev.get("homeTeam") or {}).get("id"),
            (ev.get("awayTeam") or {}).get("id"))


def _match_por_id(eventos, home_id, away_id):
    """Acha o evento cujos times batem por ID, em qualquer ordem.
    -> (evento, invertido) ou (None, False)."""
    if home_id is None or away_id is None:
        return None, False
    for ev in eventos:
        h, a = _ids_evento(ev)
        if h == home_id and a == away_id:
            return ev, False
        if h == away_id and a == home_id:
            return ev, True
    return None, False


async def _resolver_time(tab, nome, token, pista=None, cache=None):
    """Resolve um nome da Loteca p/ (team_id, nome_sofascore, score) usando a
    PRÓPRIA search/all do Sofascore como matcher geral — ela já faz tradução
    (Alemanha->Germany), expande sigla (PSG, CRB) e tolera truncamento, então o
    de-para de nomes vira quase desnecessário.

    Diferenças p/ _buscar_time (usado no fallback fuzzy):
      * Ranqueia pela ORDEM da search (sinal primário que resolve siglas sem
        de-para); geo (UF/país) pode PROMOVER um homônimo melhor; name_score só
        desempata — assim a sigla "CRB" não é punida por divergir do nome longo.
      * `cache`: dict opcional compartilhado entre jogos de um lote — evita
        repetir a busca do MESMO time (URT, CRB aparecem em vários concursos). A
        chave inclui a pista (UF/país muda qual homônimo escolher).
      * Só recorre ao apelidos.json se a search não devolver NENHUM time (ex.:
        "Coreia do Sul"): de-para como EXCEÇÃO, não como regra.
    """
    p = pista or {}
    chave = (normalize(nome), p.get("uf"), p.get("alpha2"), p.get("alpha3"))
    if cache is not None and chave in cache:
        return cache[chave]

    async def _uma(q):
        try:
            data = await _fetch_json(
                tab, "search/all?q=" + requests.utils.quote(q), token)
        except Exception as e:
            print(f"[aviso] search '{q}': {e}", file=sys.stderr)
            return None
        melhor, pos = None, 0      # (id, nome, base, rank)
        for r in data.get("results", []):
            if r.get("type") != "team":
                continue
            ent = r.get("entity", {})
            if (ent.get("sport") or {}).get("name") not in (None, "Football"):
                continue
            base = name_score(q, ent.get("name", ""))
            rank = -pos * 0.1 + base * 0.05      # ordem da search domina
            if _pais_casa(ent.get("country") or {}, pista):
                rank += 0.06
            if pista and _uf_no_texto(normalize(ent.get("name", "")),
                                      pista.get("uf")):
                rank += 0.15
            if melhor is None or rank > melhor[3]:
                melhor = (ent.get("id"), ent.get("name"), base, rank)
            pos += 1
        return melhor

    melhor = await _uma(nome)
    if melhor is None:                            # search falhou -> tenta apelido
        canon = _canonico(nome)
        if normalize(canon) != normalize(nome):
            melhor = await _uma(canon)
    resultado = melhor[:3] if melhor else None
    if cache is not None:
        cache[chave] = resultado
    return resultado


async def _buscar_por_id(tab, date, home, away, token, janela_dias,
                         ph=None, pa=None, cache=None):
    """Estratégia search-first: resolve cada time p/ um team_id e casa o confronto
    por ID (não por string) — primeiro na agenda da data, senão na lista de
    confrontos da âncora. Casar por ID dispensa o de-para de nomes."""
    alvo = dt.date.fromisoformat(date)
    t_home = await _resolver_time(tab, home, token, ph, cache=cache)
    t_away = await _resolver_time(tab, away, token, pa, cache=cache)
    faltam = [n for n, t in ((home, t_home), (away, t_away)) if not t]
    if faltam:
        raise RuntimeError(f"search/all não resolveu p/ ID: {faltam}")
    hid, hnome, _ = t_home
    aid, anome, _ = t_away
    print(f"[id] '{home}' -> {hnome} ({hid}) | '{away}' -> {anome} ({aid})",
          file=sys.stderr)

    # 1) agenda da data pedida (+ vizinhos ±janela_dias por fuso): casa por ID.
    vistos, cands = set(), []
    for d in _datas(date, janela_dias):
        try:
            data = await _fetch_json(
                tab, f"sport/football/scheduled-events/{d}", token)
        except Exception as e:
            print(f"[aviso] agenda {d}: {e}", file=sys.stderr)
            continue
        for ev in data.get("events", []):
            if ev.get("id") not in vistos:
                vistos.add(ev.get("id"))
                cands.append(ev)
    ev, inv = _match_por_id(cands, hid, aid)
    if ev:
        d = _ev_date(ev)
        dias = abs((d - alvo).days) if d else 0
        return _montar_resultado(ev, date, home, away, 1.0, inv,
                                 metodo="agenda-id", dias_diff=dias)

    # 2) fallback: lista de confrontos da âncora (maior confiança), casa por ID.
    ancora = max((t_home, t_away), key=lambda t: t[2])
    outro = t_away if ancora is t_home else t_home
    eventos = await _eventos_do_time(tab, ancora[0], token,
                                     alvo=alvo, janela_dias=janela_dias)
    cands = []
    for ev in eventos:
        m, e_inv = _match_por_id([ev], hid, aid)
        d = _ev_date(ev)
        if m is not None and d is not None:
            cands.append((abs((d - alvo).days), ev, e_inv))
    if not cands:
        raise RuntimeError(f"'{ancora[1]}' não tem confronto contra "
                           f"'{outro[1]}' nos jogos listados.")
    cands.sort(key=lambda c: c[0])
    dias, ev, inv = cands[0]
    if dias > janela_dias:
        raise RuntimeError(
            f"confronto por ID mais próximo a {dias} dias de {date} "
            f"(em {_ev_date(ev)}), fora da janela ±{janela_dias}.")
    return _montar_resultado(ev, date, home, away, 1.0, inv,
                             metodo="lista-do-time-id", dias_diff=dias)


# --------------------------------------------------------------------------- #
# Auditoria por LLM (Claude via Hub) — etapa final OPCIONAL, p/ UM jogo:
#   (1) valida se o match achado é plausível — pega o falso-positivo que passa no
#       threshold quando a agenda do dia não tem o jogo verdadeiro e o fuzzy casa
#       com outro jogo de nome parecido (homônimo/país/torneio errado);
#   (2) se NÃO achou, tenta resgatar: o LLM escolhe entre candidatos REAIS da
#       search/all (aterrado em fatos) e o confronto por ID é VERIFICADO contra o
#       Sofascore antes de confiar — e sugere a entrada de apelido a adicionar.
# O LLM nunca decide sozinho: na validação é consultivo (não altera o match); no
# resgate, só propõe uma escolha entre IDs reais, confirmada por confronto.
# --------------------------------------------------------------------------- #
_LLM_SYS = (
    "Voce e um especialista em futebol que casa nomes de times da LOTECA "
    "(caixa-alta, truncados em ~17 chars, com siglas e sufixo de UF brasileira) "
    "aos nomes canonicos do Sofascore. Conhece clubes de todas as divisoes do "
    "Brasil e do mundo, selecoes (em ingles no Sofascore) e siglas (CRB, URT, "
    "ASA, PSG). Responda SEMPRE so com um objeto JSON, sem texto fora dele."
)

# Vocabulário histórico da Loteca: universo de times que JÁ apareceram em algum
# concurso (data/raw/*.json). Ancora o LLM da auditoria — um nome desta lista é um
# time legítimo/recorrente da Loteca, mesmo coincidindo com homônimo estrangeiro
# (ex.: "ATHLETIC CLUB" aqui é o clube brasileiro da Série B, não o Bilbao). Pequeno
# (~900 nomes, ~9 KB) e cacheado no módulo; ausência da lista é inofensiva.
_RAW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "data", "raw")
_FREQ_CACHE = None


def _freq_loteca(raw_dir=_RAW_DIR):
    """Frequência histórica de cada time da Loteca (cacheada). Lê os raw da Caixa;
    tolera arquivos ausentes/corrompidos. -> {nome_normalizado: (nome_exibido, n)}."""
    global _FREQ_CACHE
    if _FREQ_CACHE is not None:
        return _FREQ_CACHE
    freq = {}
    try:
        arquivos = [os.path.join(raw_dir, f) for f in os.listdir(raw_dir)
                    if f.endswith(".json")]
    except OSError:
        arquivos = []
    for f in arquivos:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        for j in d.get("listaResultadoEquipeEsportiva") or []:
            for k in ("nomeEquipeUm", "nomeEquipeDois"):
                v = (j.get(k) or "").strip()
                if not v:
                    continue
                key = normalize(v)
                disp, n = freq.get(key, (v, 0))
                freq[key] = (disp, n + 1)
    _FREQ_CACHE = freq
    return _FREQ_CACHE


def _vocab_loteca(raw_dir=_RAW_DIR):
    """Lista ordenada dos nomes de time distintos no histórico da Loteca."""
    return sorted(d for d, _ in _freq_loteca(raw_dir).values())


def _loteca_contexto(*nomes):
    """Fato DETERMINÍSTICO p/ ancorar a auditoria do LLM: para cada nome consultado,
    diz quantas vezes ele já apareceu no histórico de concursos da Loteca. Um nome
    presente é um time LEGÍTIMO da Loteca (mata o falso-positivo de homônimo —
    'ATHLETIC CLUB' é o clube brasileiro, não o Bilbao). Vazio se não houver dados."""
    freq = _freq_loteca()
    if not freq:
        return ""
    linhas = []
    for nm in nomes:
        if not nm:
            continue
        disp, n = freq.get(normalize(nm), (None, 0))
        if n:
            linhas.append(f"- '{nm}' JA APARECEU {n}x na Loteca (como '{disp}') "
                          "-> time legitimo e recorrente; NAO marque como suspeito "
                          "so por coincidir com um homonimo estrangeiro mais famoso.")
        else:
            linhas.append(f"- '{nm}' nao consta no historico da Loteca (pode ser "
                          "time novo; ausencia NAO e indicio de erro).")
    return ("CONTEXTO LOTECA (frequencia no historico de concursos, fato verificado "
            "nos dados oficiais da Caixa):\n" + "\n".join(linhas) + "\n\n")


def _sys_loteca(base):
    """Anexa ao system prompt o universo de times da Loteca, p/ o LLM tratar nomes
    conhecidos como legítimos. Se não houver dados (data/raw vazio), devolve o `base`
    intacto. O sinal forte é o `_loteca_contexto` por consulta; esta lista é o pano
    de fundo amplo."""
    vocab = _vocab_loteca()
    if not vocab:
        return base
    return (
        base + "\n\n"
        f"TIMES QUE JA APARECERAM NA LOTECA ({len(vocab)} nomes distintos do "
        "historico de concursos, no mesmo formato caixa-alta/truncado das consultas). "
        "Um nome que esta nesta lista e um time LEGITIMO e recorrente da Loteca: NAO o "
        "trate como suspeito so por coincidir com um homonimo estrangeiro mais famoso "
        "(ex.: 'ATHLETIC CLUB' aqui e o clube brasileiro, nao o Athletic Bilbao). "
        "Ausencia da lista NAO e indicio de erro (a Loteca inclui times novos). Lista:\n"
        + ", ".join(vocab)
    )


def _extrair_json(texto):
    """Extrai o primeiro objeto JSON de um texto (tolera cercas/prosa em volta)."""
    t = (texto or "").strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError(f"sem JSON na resposta do LLM: {t[:160]}")
    return json.loads(t[i:j + 1])


def _hub_generate(prompt, model="claude-sonnet-4-5", system=None, max_tokens=700):
    """Chama o Hub (POST /generate, provider claude). -> texto."""
    if not HUB_KEY:
        raise RuntimeError("HUB_API_KEY não está no ambiente — necessário p/ LLM.")
    params = {"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]}
    if system:
        params["system"] = system
    r = requests.post(f"{HUB}/generate",
                      json={"provider": "claude", "params": params},
                      headers={"Authorization": f"Bearer {HUB_KEY}"}, timeout=120)
    r.raise_for_status()
    return r.json().get("text", "")


async def _llm_json(prompt, model, system=_LLM_SYS, max_tokens=700):
    """Roda o LLM (requests sync) fora do event loop e parseia o JSON. O system
    ganha o vocabulário histórico da Loteca (ancora contra falso-positivo de
    homônimo)."""
    system = _sys_loteca(system)
    loop = asyncio.get_event_loop()
    txt = await loop.run_in_executor(
        None, lambda: _hub_generate(prompt, model, system, max_tokens))
    return _extrair_json(txt)


def _pista_txt(pista):
    if not pista:
        return "sem pista"
    return (f"UF={pista.get('uf') or '-'}, "
            f"pais={pista.get('alpha3') or pista.get('alpha2') or '-'}")


async def _auditar_match(res, date, home_in, away_in, ph, pa, ev, model):
    """Fase 1: o LLM julga se o match é o jogo certo. Consultivo (não altera o
    match). -> dict {veredito, confianca, motivo, alias_sugerido}."""
    hc = ((ev.get("homeTeam") or {}).get("country") or {}).get("name") if ev else None
    ac = ((ev.get("awayTeam") or {}).get("country") or {}).get("name") if ev else None
    prompt = (
        "Confira se o jogo casado pelo sistema e REALMENTE o jogo da Loteca.\n\n"
        + _loteca_contexto(home_in, away_in) +
        f"LOTECA: '{home_in}' x '{away_in}' em {date} "
        f"(mandante: {_pista_txt(ph)}; visitante: {_pista_txt(pa)}).\n"
        f"CASOU COM (Sofascore): '{res['home_sofascore']}' x "
        f"'{res['away_sofascore']}' em {res.get('data_encontrada')} "
        f"(+-{res.get('dias_diferenca')}d), torneio '{res.get('torneio')}', "
        f"pais dos times: {hc} / {ac}, metodo={res.get('metodo')}, "
        f"invertido={res.get('invertido')}.\n\n"
        "Considere: time errado/homonimo (ex.: Atletico-PR x Atletico Madrid), "
        "pais ou divisao incompativel, torneio que nao bate, data distante. "
        "Diferenca de grafia/idioma/sigla e NORMAL e nao e erro.\n"
        'Responda JSON: {"veredito":"ok"|"suspeito","confianca":0..1,'
        '"motivo":"...","alias_sugerido":{"de":"<nome loteca>",'
        '"para":"<nome sofascore>"}|null}.'
    )
    try:
        out = await _llm_json(prompt, model, max_tokens=400)
        out["_ok"] = True
        print(f"[auditoria] veredito={out.get('veredito')} "
              f"({out.get('confianca')}): {out.get('motivo')}", file=sys.stderr)
        return out
    except Exception as e:
        print(f"[auditoria] falhou: {e}", file=sys.stderr)
        return {"_ok": False, "erro": str(e)[:200]}


async def _candidatos_time(tab, nome, token, top=6):
    """Lista os melhores times reais da search/all p/ um nome — fatos p/ o LLM
    escolher (id, nome, pais)."""
    try:
        data = await _fetch_json(
            tab, "search/all?q=" + requests.utils.quote(nome), token)
    except Exception as e:
        print(f"[aviso] search '{nome}': {e}", file=sys.stderr)
        return []
    out = []
    for r in data.get("results", []):
        if r.get("type") != "team":
            continue
        ent = r.get("entity", {})
        if (ent.get("sport") or {}).get("name") not in (None, "Football"):
            continue
        out.append({"id": ent.get("id"), "nome": ent.get("name"),
                    "pais": (ent.get("country") or {}).get("name")})
        if len(out) >= top:
            break
    return out


async def _resgatar_llm(tab, date, home_in, away_in, token, ph, pa,
                        janela_dias, erro_fb, model):
    """Fase 2: jogo NÃO achado. O LLM usa conhecimento de mundo p/ dar o NOME
    CANONICO de cada time (resolve apelido coloquial que a search nao indexa —
    GALO->Atletico Mineiro, TIMAO->Corinthians); EU busco esse nome no Sofascore
    (search + geo) e VERIFICO o confronto por ID antes de confiar. -> res (metodo
    'llm-resgate' + apelido sugerido) ou levanta.

    Por que pelo NOME e nao por id de candidato: a search do Sofascore NAO conhece
    apelidos (busca "GALO" devolve "Galo Maringa", nao o Atletico-MG), entao dar a
    lista de candidatos crus ao LLM nao resgata esses casos. O conhecimento de
    mundo do LLM traduz o apelido; o Sofascore fornece o id; o confronto e o gate.
    """
    cand_h = await _candidatos_time(tab, home_in, token)
    cand_a = await _candidatos_time(tab, away_in, token)

    def _fmt(c):
        return ("; ".join(f"{x['nome']} ({x['pais']})" for x in c)
                or "(a busca nao trouxe nada)")
    prompt = (
        "O jogo da Loteca abaixo NAO foi encontrado. Diga, para cada time, o "
        "NOME CANONICO exato pelo qual ele aparece no Sofascore, para eu buscar. "
        "Use seu conhecimento de futebol (apelidos como GALO=Atletico Mineiro, "
        "TIMAO=Corinthians; siglas; nomes de selecao em ingles) e o contexto "
        "(UF/pais, divisao, o adversario). A lista que minha busca ja retornou "
        "serve so p/ voce ver a grafia do Sofascore (pode estar errada).\n\n"
        f"LOTECA: '{home_in}' x '{away_in}' em {date} "
        f"(mandante: {_pista_txt(ph)}; visitante: {_pista_txt(pa)}).\n"
        f"Busca p/ '{home_in}' retornou: {_fmt(cand_h)}\n"
        f"Busca p/ '{away_in}' retornou: {_fmt(cand_a)}\n\n"
        "Responda JSON: "
        '{"home_busca":"<nome p/ buscar no sofascore>",'
        '"away_busca":"<nome p/ buscar no sofascore>","motivo":"...",'
        '"alias_sugerido":{"de":"<nome loteca>","para":"<nome sofascore>"}|null}.'
    )
    escolha = await _llm_json(prompt, model, max_tokens=400)
    hq, aq = escolha.get("home_busca"), escolha.get("away_busca")
    print(f"[resgate] LLM sugere buscar '{hq}' x '{aq}': "
          f"{escolha.get('motivo')}", file=sys.stderr)
    if not hq or not aq:
        raise RuntimeError(f"{erro_fb or 'não achei o jogo'}; LLM não sugeriu "
                           f"nomes de busca.")

    # resolve os nomes sugeridos no Sofascore (search + desempate geo) -> ids reais
    th = await _resolver_time(tab, hq, token, ph)
    ta = await _resolver_time(tab, aq, token, pa)
    if not th or not ta:
        raise RuntimeError(f"{erro_fb or 'não achei o jogo'}; nomes do LLM "
                           f"('{hq}', '{aq}') não acharam time no Sofascore.")
    hid, aid = th[0], ta[0]

    # VERIFICA contra o Sofascore: existe confronto entre esses IDs na janela?
    alvo = dt.date.fromisoformat(date)
    eventos = await _eventos_do_time(tab, hid, token, alvo=alvo,
                                     janela_dias=janela_dias)
    melhor, mdias, minv = None, 10 ** 9, False
    for ev in eventos:
        m, e_inv = _match_por_id([ev], hid, aid)
        d = _ev_date(ev)
        if m is not None and d is not None:
            dd = abs((d - alvo).days)
            if dd < mdias:
                melhor, mdias, minv = ev, dd, e_inv
    if melhor is None or mdias > janela_dias:
        raise RuntimeError(
            f"{erro_fb or 'não achei o jogo'}; resgate do LLM "
            f"('{th[1]}' x '{ta[1]}') NÃO tem confronto verificavel em "
            f"+-{janela_dias}d de {date} — descartado (anti-alucinacao).")
    res = _montar_resultado(melhor, date, home_in, away_in, 1.0, minv,
                            metodo="llm-resgate", dias_diff=mdias)
    res["apelido_sugerido"] = escolha.get("alias_sugerido")
    print(f"[resgate] VERIFICADO: {res['home_sofascore']} x "
          f"{res['away_sofascore']} ({res['data_encontrada']}). "
          f"Apelido sugerido: {res.get('apelido_sugerido')}", file=sys.stderr)
    return res


# --------------------------------------------------------------------------- #
# Gravação do apelido sugerido de volta no apelidos.json (opcional, --aplicar-apelido)
# --------------------------------------------------------------------------- #
def _sugestao_apelido(res):
    """Deriva (de, para) FUNDAMENTADO do resultado p/ gravar no apelidos.json:
    'de' precisa ser um dos nomes da Loteca consultados, e 'para' é o nome REAL do
    Sofascore daquele lado (NÃO o texto livre do LLM — anti-alucinação no valor).
    -> (de, para) ou None se não houver sugestão confiável."""
    cons = res.get("consulta") or {}
    home_in, away_in = cons.get("home") or "", cons.get("away") or ""
    inv = bool(res.get("invertido"))
    hs, asof = res.get("home_sofascore"), res.get("away_sofascore")
    para_home = asof if inv else hs        # nome sofascore do mandante da Loteca
    para_away = hs if inv else asof        # nome sofascore do visitante da Loteca

    sug = res.get("apelido_sugerido")      # resgate (fase 2) tem prioridade
    if not sug:
        aud = res.get("auditoria") or {}
        if aud.get("veredito") == "ok":    # validação (fase 1) só se o LLM aprovou
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
        return None                        # 'de' não bate com nenhum lado -> não confio
    if not para or normalize(de) == normalize(para):
        return None                        # sem 'para' real, ou alias trivial (de==para)
    return de, para


def _aplicar_apelido(de, para, path=_APELIDOS_PATH):
    """Acrescenta {de: para} à seção 'times' do apelidos.json. -> 'adicionado' |
    'ja-existe' | 'erro: ...'. Reflete também em memória p/ este processo.

    Concorrência: todo o read-modify-write roda sob um lock de arquivo EXCLUSIVO
    (fcntl.flock em <path>.lock), serializando processos paralelos — sem isso, dois
    `--aplicar-apelido` simultâneos poderiam ler a mesma versão e um sobrescrever a
    entrada do outro (lost update). A escrita em si é atômica (.tmp + os.replace)."""
    try:
        lock_f = open(path + ".lock", "w")
    except Exception as e:
        return f"erro: não abri o lock ({e})"
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX)        # bloqueia até obter o lock
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return f"erro: não li o apelidos.json ({e})"
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
            return f"erro: não gravei o apelidos.json ({e})"
        _APELIDOS_TIMES[normalize(de)] = para
        return "adicionado"
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


async def _buscar(tab, date, home_in, away_in, janela_dias=2,
                  home_uf=None, away_uf=None, home_pais=None, away_pais=None,
                  modo="fuzzy", cache_ids=None, auditar=True,
                  llm_model="claude-sonnet-4-5", aplicar_apelido=False):
    if aplicar_apelido:
        auditar = True          # aplicar exige a sugestão produzida pela auditoria
    # pistas geográficas (UF/país) p/ desempate de homônimos
    ph0, pa0 = _pista(home_uf, home_pais), _pista(away_uf, away_pais)

    # modo "id" (search-first): resolve cada time -> team_id e casa por ID.
    if modo == "id":
        token = await _capturar_token(tab)
        if not token:
            raise RuntimeError("não capturei o X-Requested-With na home "
                               "(IP bloqueado/país sem cobertura?).")
        res = await _buscar_por_id(tab, date, home_in, away_in, token,
                                   janela_dias, ph0, pa0, cache=cache_ids)
        res["consulta"] = {"date": date, "home": home_in, "away": away_in,
                           "home_uf": home_uf, "away_uf": away_uf,
                           "home_pais": home_pais, "away_pais": away_pais,
                           "modo": "id"}
        return res

    # modo "fuzzy" (original): apelidos -> agenda -> fallback por lista-de-time.
    # apelidos: nome da Loteca -> nome canônico do Sofascore (ex.: ALEMANHA -> Germany)
    home, away = _canonico(home_in), _canonico(away_in)
    if (home, away) != (home_in, away_in):
        print(f"[apelido] '{home_in}' -> '{home}' | '{away_in}' -> '{away}'",
              file=sys.stderr)
    # pistas geográficas (UF/país) p/ desempate de homônimos
    ph, pa = _pista(home_uf, home_pais), _pista(away_uf, away_pais)

    token = await _capturar_token(tab)
    if not token:
        raise RuntimeError("não capturei o X-Requested-With na home "
                           "(IP bloqueado/país sem cobertura?).")

    # 1) caminho rápido: agenda da data pedida e vizinhos por fuso. PREGUIÇOSO,
    # igual ao feed do buscar_odds_flashscore: tenta a data exata primeiro e só
    # abre os vizinhos ±janela_dias (0, +1, -1, +2, -2, …) se ainda não casou —
    # poupa uma requisição de agenda no caso comum.
    vistos, candidatos = set(), []
    ev, score, invertido = None, 0.0, False
    for d in _datas(date, janela_dias):
        try:
            data = await _fetch_json(
                tab, f"sport/football/scheduled-events/{d}", token)
        except Exception as e:
            print(f"[aviso] agenda {d}: {e}", file=sys.stderr)
            continue
        for e2 in data.get("events", []):
            if e2.get("id") not in vistos:
                vistos.add(e2.get("id"))
                candidatos.append(e2)
        ev, score, invertido = _melhor_evento(candidatos, home, away, ph, pa)
        if ev and score >= MATCH_THRESHOLD:
            break               # já casou -> não abre mais vizinhos

    res, ev_match, erro_fb = None, None, None
    if ev and score >= MATCH_THRESHOLD:
        d = _ev_date(ev)
        dias = abs((d - dt.date.fromisoformat(date)).days) if d else 0
        res = _montar_resultado(ev, date, home, away, score, invertido,
                                metodo="agenda", dias_diff=dias)
        ev_match = ev
    else:
        # 2) fallback: lista de confrontos do time, data mais próxima na janela.
        print(f"[info] não casou na agenda de {date} "
              f"(melhor score={score:.2f}); tentando lista de confrontos do time…",
              file=sys.stderr)
        try:
            res = await _fallback_por_time(tab, date, home, away, token,
                                           janela_dias, ph, pa)
        except Exception as e:
            erro_fb = str(e)
            print(f"[info] fallback não achou: {erro_fb}", file=sys.stderr)

    # 3) auditoria por LLM (opcional): valida o achado OU resgata o não-achado.
    if auditar:
        if res is not None:
            res["auditoria"] = await _auditar_match(
                res, date, home_in, away_in, ph, pa, ev_match, llm_model)
        else:
            res = await _resgatar_llm(tab, date, home_in, away_in, token,
                                      ph, pa, janela_dias, erro_fb, llm_model)

    if res is None:
        raise RuntimeError(erro_fb or "não foi possível resolver o jogo.")

    # registra a entrada crua + o que foi efetivamente usado na busca.
    res["consulta"] = {
        "date": date, "home": home_in, "away": away_in,
        "home_query": home, "away_query": away,
        "home_uf": home_uf, "away_uf": away_uf,
        "home_pais": home_pais, "away_pais": away_pais,
    }

    # 4) grava no apelidos.json a sugestão fundamentada da auditoria, se pedido.
    if aplicar_apelido:
        sug = _sugestao_apelido(res)
        if sug:
            de, para = sug
            st = _aplicar_apelido(de, para)
            res["apelido_aplicado"] = {"de": de, "para": para, "status": st}
            print(f"[apelido] {st}: '{de}' -> '{para}'", file=sys.stderr)
        else:
            print("[apelido] nenhuma sugestão aplicável (sem alias, não "
                  "fundamentado, ou trivial).", file=sys.stderr)
    return res


# --------------------------------------------------------------------------- #
# Orquestração (sobe browser, aplica proxy, retorna o resultado)
# --------------------------------------------------------------------------- #
async def _run(date, home, away, proxy=None, country=None, janela_dias=2,
               home_uf=None, away_uf=None, home_pais=None, away_pais=None,
               modo="fuzzy", cache_ids=None, auditar=True,
               llm_model="claude-sonnet-4-5", aplicar_apelido=False,
               verbose=True):
    pr = resolver_proxy(proxy, country)
    args = ["--no-sandbox"]
    if pr:
        host, port, user, pw = pr
        args.append(f"--proxy-server=http://{host}:{port}")
        if verbose:
            tag = f"{proxy}" + (f"/{country.upper()}" if country else "")
            print(f"[proxy] {tag} via {host}:{port} (user={user})", file=sys.stderr)

    browser = await uc.start(browser_args=args, sandbox=False)
    try:
        tab = await browser.get("about:blank")

        if pr:   # responde o desafio de auth do proxy via CDP (user:pass)
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

        return await _buscar(tab, date, home, away, janela_dias=janela_dias,
                             home_uf=home_uf, away_uf=away_uf,
                             home_pais=home_pais, away_pais=away_pais,
                             modo=modo, cache_ids=cache_ids, auditar=auditar,
                             llm_model=llm_model, aplicar_apelido=aplicar_apelido)
    finally:
        try:
            browser.stop()
        except Exception:
            pass


def buscar_event_id(date, home, away, proxy=None, country=None, janela_dias=2,
                    home_uf=None, away_uf=None, home_pais=None, away_pais=None,
                    modo="fuzzy", auditar=True, llm_model="claude-sonnet-4-5",
                    aplicar_apelido=False, verbose=True):
    """API síncrona. Retorna o dict do match (ver _buscar). Levanta em falha.

    home_uf/away_uf  : sigla do estado BR (Loteca siglaUF*) — desempata homônimos.
    home_pais/away_pais : código de país da Loteca (siglaPais*, ~ISO alpha-3)."""
    return uc.loop().run_until_complete(
        _run(date, home, away, proxy=proxy, country=country,
             janela_dias=janela_dias, home_uf=home_uf, away_uf=away_uf,
             home_pais=home_pais, away_pais=away_pais, modo=modo,
             auditar=auditar, llm_model=llm_model,
             aplicar_apelido=aplicar_apelido, verbose=verbose))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Descobre o EVENT_ID do Sofascore por data + nomes dos times.")
    ap.add_argument("date", help="data do jogo, AAAA-MM-DD")
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
    # estratégia de resolução
    ap.add_argument("--modo", choices=["fuzzy", "id"], default="fuzzy",
                    help="fuzzy: agenda+apelidos (offline). id: search-first, "
                         "resolve team_id e casa por ID (de-para vira exceção)")
    ap.add_argument("--janela-dias", type=int, default=2, dest="janela_dias",
                    help="janela (em dias) p/ o fallback aceitar um confronto "
                         "em data próxima da pedida (default: 2)")
    # auditoria por LLM (LIGADA por padrão)
    ap.add_argument("--sem-auditar", action="store_false", dest="auditar",
                    help="DESLIGA a auditoria por LLM (Claude via Hub), que roda "
                         "por padrão: valida o match achado e resgata+verifica o "
                         "não-achado")
    ap.add_argument("--llm-model", default="claude-sonnet-4-5", dest="llm_model",
                    help="modelo do Hub p/ a auditoria (default: claude-sonnet-4-5)")
    ap.add_argument("--aplicar-apelido", action="store_true", dest="aplicar_apelido",
                    help="grava no apelidos_loteca_sofascore.json o apelido sugerido pela "
                         "auditoria (implica auditoria; só grava o fundamentado)")
    # rede / proxy
    ap.add_argument("--proxy", nargs="?", choices=["none", "rotativo", "fixo"],
                    const="fixo", default="none",
                    help="sem o flag: sem proxy. --proxy sozinho: fixo BR. "
                         "Tb aceita none, rotativo (IP/país muda) ou fixo (IP de um país)")
    ap.add_argument("--country", default="BR",
                    help="ISO-2 do país p/ --proxy fixo (default: BR)")
    a = ap.parse_args()

    try:
        res = buscar_event_id(a.date, a.home, a.away, proxy=a.proxy,
                              country=a.country, janela_dias=a.janela_dias,
                              home_uf=a.home_uf, away_uf=a.away_uf,
                              home_pais=a.home_pais, away_pais=a.away_pais,
                              modo=a.modo, auditar=a.auditar,
                              llm_model=a.llm_model,
                              aplicar_apelido=a.aplicar_apelido)
    except Exception as e:
        print(f"[erro] {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
