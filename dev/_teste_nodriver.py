import asyncio, nodriver as uc

ORIGIN = "https://www.sofascore.com"

async def main():
    browser = await uc.start(
        browser_args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled",
                      "--window-size=1366,900", "--lang=pt-BR"],
    )
    try:
        # 1) home / futebol -> aquece a origem
        page = await browser.get(f"{ORIGIN}/pt/futebol")
        await page.sleep(5)
        title = await page.evaluate("document.title")
        body = await page.evaluate("document.body.innerText.slice(0,300)")
        print("=== /pt/futebol ===")
        print("title:", title)
        print("body[:300]:", repr(body))

        # 2) tenta a API de scheduled-events na mesma origem (same-origin fetch)
        js = (
            "(async () => { try {"
            " const r = await fetch('/api/v1/sport/football/scheduled-events/2026-06-20');"
            " const t = await r.text();"
            " return r.status + ' :: ' + t.slice(0,200);"
            " } catch(e) { return 'ERR ' + e; } })()"
        )
        res = await page.evaluate(js, await_promise=True)
        print("=== scheduled-events ===")
        print(res)
    finally:
        browser.stop()

uc.loop().run_until_complete(main())
