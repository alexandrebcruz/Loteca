import nodriver as uc

ORIGIN = "https://www.sofascore.com"

async def carrega(tab, url, espera=10):
    await tab.get(url)
    await tab.sleep(espera)
    cur = await tab.evaluate("location.href")
    title = await tab.evaluate("document.title")
    body = await tab.evaluate("document.body.innerText.slice(0,300)")
    print(f"url={cur}\ntitle={title!r}\nbody={body!r}\n", flush=True)

async def main():
    browser = await uc.start(
        browser_args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1366,900", "--lang=pt-BR",
            "--proxy-server=http://127.0.0.1:8899",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ],
    )
    try:
        tab = await browser.get("https://api.ipify.org?format=json")
        await tab.sleep(3)
        print("egress:", await tab.evaluate("document.body.innerText"), "\n", flush=True)

        print("--- HOME ---", flush=True)
        await carrega(tab, f"{ORIGIN}/pt/futebol", 12)

        print("--- JOGO ---", flush=True)
        await carrega(tab, f"{ORIGIN}/pt/football/match/flamengo-palmeiras/OR#id:12436889", 12)

        await tab.save_screenshot("/mnt/d/Projetos/Loteca/_game.png")
        print("screenshot: _game.png", flush=True)
    finally:
        browser.stop()

uc.loop().run_until_complete(main())
