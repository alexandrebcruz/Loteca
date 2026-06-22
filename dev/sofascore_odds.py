#!/usr/bin/env python3
"""
Baixa as odds CRUAS de um jogo no Sofascore (todas as casas, /odds/1/all) e salva.

METODO (adaptado do notebook do usuario que comprovadamente funciona:
Projeto_Loteca_v2/bkp_codigos/01.2_Scraping_Odds_1X2.ipynb):

  Dirige um Chrome REAL (Selenium, headless) e usa o truque da "outra aba":
  estando numa pagina do proprio Sofascore, abre a URL da API em uma NOVA ABA
  com window.open(...). Isso faz a requisicao no contexto SAME-ORIGIN (com
  Referer do Sofascore) e PASSA pelo anti-bot -- enquanto um driver.get()/fetch()
  manual manda Sec-Fetch-Site:none e leva 403 {"reason":"challenge"}.

  Fluxo para um jogo:
    1. acha o eventId (+slug/customId) casando os times via scheduled-events
    2. driver.get() na PAGINA DO JOGO  (aquece a origem)
    3. window.open() em /event/{id}/odds/1/all  -> le innerText -> JSON
    4. salva o JSON cru (idempotente: nao rebaixa se ja existe)

IMPORTANTE: precisa rodar de um IP "limpo" (sua maquina fora de cooldown, ou um
Pod CPU no RunPod). De um IP penalizado ate o HTML retorna 403.

Pre-requisitos (uma vez):
    pip install selenium
    # Chrome/Chromium instalado; o Selenium Manager baixa o chromedriver sozinho.
    # (no Pod RunPod com google-chrome ja vem pronto)

Uso (CLI):
    python3 sofascore_odds.py 2026-06-01 "Sao Paulo" "Palmeiras"
    python3 sofascore_odds.py 2026-06-01 "Sao Paulo" "Palmeiras" --force
    python3 sofascore_odds.py 2026-06-01 "x" "y" --event-id 12345678
    python3 sofascore_odds.py ... --proxy http://user:pass@host:porta   # proxy residencial

Uso (import):
    from sofascore_odds import baixar_odds
    baixar_odds("2026-06-01", "Sao Paulo", "Palmeiras")

Saidas (idempotente):
    data/odds_sofascore/{data}_{home}_vs_{away}.json        <- odds CRUAS (/odds/1/all)
    data/odds_sofascore/{data}_{home}_vs_{away}.meta.json   <- proveniencia
"""
import os
import re
import sys
import json
import time
import random
import argparse
import unicodedata
import datetime as dt
from difflib import SequenceMatcher

OUT_DIR = "data/odds_sofascore"
ORIGIN = "https://www.sofascore.com"
TIMEOUT = 20
MATCH_THRESHOLD = 0.55

# Pausas (segundos). Aleatorias para nao parecer robo; backoff cresce a cada falha.
SLEEP_API = (0.8, 2.5)     # antes de cada chamada de API (era fixo em 1s)
SLEEP_NAV = (1.5, 3.5)     # entre navegacoes/jogos (warm de paginas, loop)
BACKOFF_BASE = 1.5         # multiplicador do backoff por tentativa que falha


def _dorme(faixa):
    """Pausa por um tempo aleatorio dentro de (min, max)."""
    a, b = faixa
    time.sleep(random.uniform(a, b))


# --------------------------------------------------------------------------- #
# Normalizacao / matching de nomes
# --------------------------------------------------------------------------- #
_STOP = {"fc", "ec", "sc", "ac", "cf", "afc", "futebol", "clube", "de", "do",
         "da", "the", "esporte", "esportivo", "club"}


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
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    base = SequenceMatcher(None, na, nb).ratio()
    if na in nb or nb in na:
        base = max(base, 0.9)
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if ta and tb:
        jac = len(ta & tb) / len(ta | tb)
        base = max(base, jac)
    return base


def slug(s):
    return re.sub(r"-+", "-", normalize(s).replace(" ", "-")).strip("-") or "x"


def caminho_arquivo(date, home, away):
    return os.path.join(OUT_DIR, f"{date}_{slug(home)}_vs_{slug(away)}.json")


def caminho_meta(date, home, away):
    return caminho_arquivo(date, home, away)[:-5] + ".meta.json"


def _datas(date):
    """A data dada e os vizinhos (+1/-1) por causa de fuso na agenda do Sofascore."""
    d = dt.date.fromisoformat(date)
    return [(d + dt.timedelta(days=k)).isoformat() for k in (0, 1, -1)]


# --------------------------------------------------------------------------- #
# Browser (Selenium) + truque da "outra aba"
# --------------------------------------------------------------------------- #
def _inicia_relay_proxy(upstream):
    """Sobe um relay TCP local que injeta a auth no proxy upstream autenticado.

    O Chrome ignora user:pass na URL do --proxy-server (abre popup que trava o
    headless), e a extensao MV3 de auth nao roda confiavel em headless. Solucao
    robusta: o Chrome aponta pra ESTE relay local (sem auth) e o relay so faz o
    tunel CONNECT pro upstream com o header Proxy-Authorization. Como e
    passthrough puro de TCP, o TLS do Chrome fica INTACTO (essencial pro
    anti-bot). Retorna 'host:port' local pra usar no --proxy-server.
    """
    import socket
    import base64
    import threading
    from urllib.parse import urlparse
    u = urlparse(upstream)
    up_host, up_port = u.hostname, (u.port or 80)
    cred = base64.b64encode(
        f"{u.username}:{u.password or ''}".encode()).decode()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(128)
    local_port = srv.getsockname()[1]

    def _pipe(a, b):
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                b.sendall(d)
        except Exception:                 # noqa: BLE001
            pass
        finally:
            for s in (a, b):
                try:
                    s.close()
                except Exception:         # noqa: BLE001
                    pass

    def _handle(client):
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                d = client.recv(4096)
                if not d:
                    client.close()
                    return
                buf += d
            linha = buf.split(b"\r\n", 1)[0].decode("latin1")
            metodo, alvo = (linha.split(" ") + ["", ""])[:2]
            up = socket.create_connection((up_host, up_port), timeout=30)
            if metodo.upper() == "CONNECT":
                req = (f"CONNECT {alvo} HTTP/1.1\r\nHost: {alvo}\r\n"
                       f"Proxy-Authorization: Basic {cred}\r\n"
                       f"Proxy-Connection: Keep-Alive\r\n\r\n").encode()
                up.sendall(req)
                resp = b""
                while b"\r\n\r\n" not in resp:
                    d = up.recv(4096)
                    if not d:
                        break
                    resp += d
                cab, _, rest = resp.partition(b"\r\n\r\n")
                client.sendall(cab + b"\r\n\r\n")
                if rest:
                    client.sendall(rest)
            else:                          # HTTP simples: injeta auth no header
                cab, _, corpo = buf.partition(b"\r\n\r\n")
                cab += f"\r\nProxy-Authorization: Basic {cred}".encode()
                up.sendall(cab + b"\r\n\r\n" + corpo)
            threading.Thread(target=_pipe, args=(client, up),
                             daemon=True).start()
            _pipe(up, client)
        except Exception:                 # noqa: BLE001
            try:
                client.close()
            except Exception:             # noqa: BLE001
                pass

    def _serve():
        while True:
            try:
                c, _ = srv.accept()
            except Exception:             # noqa: BLE001
                break
            threading.Thread(target=_handle, args=(c,), daemon=True).start()

    threading.Thread(target=_serve, daemon=True).start()
    return f"127.0.0.1:{local_port}"


def _boota(binario):
    """Testa se o binario REALMENTE sobe em headless neste ambiente.

    `--version` nao serve de prova: passa ate quando o headless da SIGTRAP
    (caso do chromium cheio em WSL/containers restritos). Aqui rodamos um
    --dump-dom de verdade e exigimos saida + exit 0. Custo: ~1-2s, so na
    deteccao inicial.
    """
    import subprocess
    try:
        r = subprocess.run(
            [binario, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
             "--disable-gpu", "--dump-dom", "about:blank"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        return r.returncode == 0 and b"<html" in r.stdout.lower()
    except Exception:                         # noqa: BLE001
        return False


def _acha_chrome_bin():
    """Acha um binario do Chrome que SOBE neste ambiente, preferindo o mais furtivo.

    Ordem de preferencia: chromium COMPLETO (Playwright/sistema) primeiro -- o
    fingerprint dele passa melhor no challenge da app do Sofascore que o
    `chrome-headless-shell`. Mas em WSL/containers restritos o chromium cheio
    da SIGTRAP (core dump) em headless, enquanto o headless-shell sobe normal.
    Por isso testamos cada candidato com _boota() e devolvemos o 1o que de fato
    inicia: local -> headless-shell; RunPod -> chromium completo.
    """
    import glob
    home = os.path.expanduser("~")
    raizes = [r for r in (os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
                          os.path.join(home, ".cache", "ms-playwright")) if r]
    completos, shells = [], []
    for raiz in raizes:
        completos += glob.glob(os.path.join(raiz, "chromium-*", "*", "chrome"))
    completos += ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                  "/usr/bin/chromium", "/usr/bin/chromium-browser"]
    for raiz in raizes:
        shells += glob.glob(os.path.join(
            raiz, "chromium_headless_shell-*", "*", "chrome-headless-shell"))

    candidatos = [p for p in completos + shells if p and os.path.exists(p)]
    for p in candidatos:                      # 1o que sobe de verdade vence
        if _boota(p):
            return p
    return candidatos[0] if candidatos else None


def _chrome(headless=True, proxy=None, chrome_bin=None):
    from urllib.parse import urlparse
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    o = Options()
    if headless:
        o.add_argument("--headless=new")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_argument("--disable-blink-features=AutomationControlled")
    o.add_argument("--window-size=1366,900")
    o.add_argument("--lang=pt-BR")
    if proxy:
        u = urlparse(proxy)
        if u.username:                    # proxy com auth -> relay local
            alvo = _inicia_relay_proxy(proxy)
            o.add_argument(f"--proxy-server=http://{alvo}")
        else:
            host_port = u.hostname + (f":{u.port}" if u.port else "")
            o.add_argument(f"--proxy-server={u.scheme or 'http'}://{host_port}")
    binario = chrome_bin or os.environ.get("CHROME_BIN") or _acha_chrome_bin()
    if binario:
        o.binary_location = binario
    return webdriver.Chrome(options=o)


def _wait_ready(driver):
    from selenium.webdriver.support.ui import WebDriverWait
    WebDriverWait(driver, TIMEOUT).until(
        lambda d: d.execute_script("return document.readyState") == "complete")


def executa_api_em_outra_aba(driver, url_api, tentativas=10):
    """Abre a URL da API em NOVA ABA (same-origin), le o JSON do body e volta.

    Esse e o cerne do metodo: window.open a partir de uma pagina do Sofascore
    -> requisicao same-origin com Referer -> passa o anti-bot.
    """
    ultimo = None
    for n in range(tentativas):
        try:
            # 1a tentativa: pausa aleatoria curta. Tentativas seguintes: backoff
            # crescente (espaca mais a cada falha, evitando martelar o anti-bot).
            if n == 0:
                _dorme(SLEEP_API)
            else:
                time.sleep(random.uniform(*SLEEP_API) + BACKOFF_BASE * n)
            driver.execute_script("window.open(arguments[0], '_blank');", url_api)
            driver.switch_to.window(driver.window_handles[-1])
            _wait_ready(driver)
            raw = driver.execute_script("return document.body.innerText")
            data = json.loads(raw)            # valida; corpo de erro tb e JSON
            if isinstance(data, dict) and "error" in data and len(data) == 1:
                ultimo = data["error"]        # ex.: {"code":403,"reason":"challenge"}
                raise RuntimeError(f"challenge: {ultimo}")
            return raw, data
        except Exception as e:                # noqa: BLE001
            ultimo = ultimo or str(e)
        finally:
            # garante que sobrou so a aba principal
            try:
                while len(driver.window_handles) > 1:
                    driver.switch_to.window(driver.window_handles[-1])
                    driver.close()
                driver.switch_to.window(driver.window_handles[0])
            except Exception:                 # noqa: BLE001
                pass
    raise RuntimeError(f"falhou apos {tentativas} tentativas (ultimo: {ultimo})")


def _api(driver, path):
    """GET de um endpoint da API via outra aba. Retorna (raw_text, obj) ou levanta."""
    return executa_api_em_outra_aba(driver, f"{ORIGIN}/api/v1/{path}")


# --------------------------------------------------------------------------- #
# Descoberta do jogo (eventId, slug, customId) casando os times
# --------------------------------------------------------------------------- #
class BloqueioAntiBot(RuntimeError):
    """Levantada quando o Sofascore barra o acesso (IP penalizado / challenge)."""


def achar_jogo(driver, date, home, away):
    """Aquece na pagina da data e usa scheduled-events para casar os times."""
    vistos = {}
    chamadas, falhas = 0, 0
    for i, dd in enumerate(_datas(date)):
        if i:                                 # pausa entre datas (nao antes da 1a)
            _dorme(SLEEP_NAV)
        driver.get(f"{ORIGIN}/pt/futebol/{dd}")
        _wait_ready(driver)
        for sufixo in ("", "/inverse"):
            chamadas += 1
            try:
                _raw, obj = _api(driver, f"sport/football/scheduled-events/{dd}{sufixo}")
            except Exception:                 # noqa: BLE001
                falhas += 1
                continue
            for e in obj.get("events", []):
                vistos[e.get("id")] = e

    # Se NENHUMA chamada trouxe eventos, quase certo que e bloqueio, nao "sem jogo".
    if not vistos:
        raise BloqueioAntiBot(
            f"0 eventos retornados em {chamadas} chamadas ({falhas} falharam). "
            "Provavel bloqueio anti-bot: rode de um IP limpo (sua maquina fora de "
            "cooldown, outro Pod/regiao, ou proxy residencial).")

    melhor, melhor_s, invertido = None, 0.0, False
    for e in vistos.values():
        h = e.get("homeTeam", {}).get("name", "")
        a = e.get("awayTeam", {}).get("name", "")
        s_dir = min(name_score(home, h), name_score(away, a))
        s_inv = min(name_score(home, a), name_score(away, h))
        s = max(s_dir, s_inv)
        if s > melhor_s:
            melhor_s, melhor, invertido = s, e, (s_inv > s_dir)
    if melhor and melhor_s >= MATCH_THRESHOLD:
        return melhor, melhor_s, invertido
    print(f"[aviso] {len(vistos)} jogos vistos em {date}, mas nenhum casou "
          f"'{home}' x '{away}' (melhor score={melhor_s:.2f}).")
    return None, melhor_s, False


def _url_jogo(ev):
    return (f"{ORIGIN}/pt/football/match/{ev.get('slug')}/"
            f"{ev.get('customId')}#id:{ev.get('id')}")


def capturar_odds(driver, ev):
    """Entra na pagina do jogo e pega /event/{id}/odds/1/all pela outra aba."""
    _dorme(SLEEP_NAV)                     # espaca o warm da pagina do jogo
    driver.get(_url_jogo(ev))             # <- "entra na pagina do jogo"
    _wait_ready(driver)
    raw, obj = _api(driver, f"event/{ev['id']}/odds/1/all")   # <- odds na outra aba
    return raw, obj


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def baixar_odds(date, home, away, force=False, event_id=None,
                headless=True, proxy=None, chrome_bin=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    dest = caminho_arquivo(date, home, away)

    if not force and os.path.exists(dest) and os.path.getsize(dest) > 0:
        try:
            with open(dest, "rb") as f:
                json.loads(f.read())
            print(f"[cache] ja existe, pulando: {dest}")
            return dest
        except Exception:                     # noqa: BLE001
            pass

    try:
        import selenium  # noqa: F401
    except Exception:                         # noqa: BLE001
        raise SystemExit("[erro] instale: pip install selenium (e tenha Chrome instalado)")

    driver = _chrome(headless=headless, proxy=proxy, chrome_bin=chrome_bin)
    try:
        if event_id is not None:
            driver.get(f"{ORIGIN}/pt/futebol")     # aquece origem
            _wait_ready(driver)
            _raw, ev = _api(driver, f"event/{event_id}")
            ev = ev.get("event", ev)
            score, invertido = 1.0, False
            print(f"[match] eventId={event_id} (informado): "
                  f"{ev.get('homeTeam',{}).get('name')} x {ev.get('awayTeam',{}).get('name')}")
        else:
            ev, score, invertido = achar_jogo(driver, date, home, away)
            if not ev:
                raise SystemExit(f"[erro] nao achei '{home}' x '{away}' em {date} "
                                 f"(melhor score={score:.2f}).")
            print(f"[match] eventId={ev['id']} score={score:.2f}"
                  f"{' (ordem invertida)' if invertido else ''}: "
                  f"{ev.get('homeTeam',{}).get('name')} x {ev.get('awayTeam',{}).get('name')}")

        raw, obj = capturar_odds(driver, ev)
        n_mkts = len(obj.get("markets", [])) if isinstance(obj, dict) else 0
        if n_mkts == 0:
            print(f"[aviso] eventId={ev['id']}: 0 markets "
                  f"(jogo pode nao ter odds disponiveis).")

        with open(dest, "wb") as f:
            f.write(raw.encode("utf-8"))      # grava o JSON CRU lido do body
        meta = {
            "consulta": {"date": date, "home": home, "away": away},
            "eventId": ev.get("id"),
            "slug": ev.get("slug"),
            "customId": ev.get("customId"),
            "match_score": round(float(score), 3),
            "ordem_invertida": bool(invertido),
            "times_sofascore": {
                "home": ev.get("homeTeam", {}).get("name"),
                "away": ev.get("awayTeam", {}).get("name"),
            },
            "n_markets": n_mkts,
            "fonte": f"{ORIGIN}/api/v1/event/{ev.get('id')}/odds/1/all",
            "metodo": "selenium-window.open-outra-aba",
            "baixado_em": dt.datetime.now().isoformat(timespec="seconds"),
        }
        with open(caminho_meta(date, home, away), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"[ok] salvo: {dest} ({len(raw)} chars, {n_mkts} markets)")
        return dest
    except BloqueioAntiBot as e:
        raise SystemExit(f"[bloqueado] {e}")
    except RuntimeError as e:
        msg = str(e)
        if any(t in msg for t in ("challenge", "Forbidden", "falhou apos")):
            raise SystemExit(f"[bloqueado] anti-bot barrou ({msg}). Rode de um IP limpo.")
        raise
    finally:
        try:
            driver.quit()
        except Exception:                     # noqa: BLE001
            pass


def main():
    ap = argparse.ArgumentParser(
        description="Baixa odds /odds/1/all de um jogo no Sofascore (raw, via Selenium + outra aba).")
    ap.add_argument("date", help="data do jogo AAAA-MM-DD")
    ap.add_argument("home", help="time mandante")
    ap.add_argument("away", help="time visitante")
    ap.add_argument("--event-id", type=int, default=None, help="pula a busca e usa este eventId")
    ap.add_argument("--force", action="store_true", help="rebaixa mesmo se ja existir")
    ap.add_argument("--no-headless", action="store_true", help="abre o navegador visivel (debug)")
    ap.add_argument("--proxy", default=None, help="proxy (ex.: http://user:pass@host:porta)")
    ap.add_argument("--chrome-bin", default=None, help="caminho do binario do Chrome")
    a = ap.parse_args()
    baixar_odds(a.date, a.home, a.away, force=a.force, event_id=a.event_id,
                headless=not a.no_headless, proxy=a.proxy, chrome_bin=a.chrome_bin)


if __name__ == "__main__":
    main()
