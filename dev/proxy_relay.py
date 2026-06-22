"""Relay HTTP local SEM auth -> proxy upstream Webshare COM auth.
Chrome aponta para 127.0.0.1:PORT (sem credenciais) e nos injetamos o
Proxy-Authorization no upstream. Lida com CONNECT (https) e requests http.
"""
import asyncio, base64, sys

UP_HOST = "p.webshare.io"
UP_PORT = 10002
UP_USER = "ffeunouk-BR-3"
UP_PASS = "8iwhyogequpc"
LOCAL_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899

AUTH = "Basic " + base64.b64encode(f"{UP_USER}:{UP_PASS}".encode()).decode()

async def pipe(r, w):
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass

async def handle(creader, cwriter):
    try:
        # le a linha+headers da request inicial do cliente
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = await creader.read(4096)
            if not chunk:
                cwriter.close(); return
            header += chunk
        head, _, rest = header.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        # remove qualquer Proxy-Authorization do cliente e injeta a nossa
        lines = [l for l in lines if not l.lower().startswith(b"proxy-authorization:")]
        lines.insert(1, b"Proxy-Authorization: " + AUTH.encode())
        new_head = b"\r\n".join(lines) + b"\r\n\r\n"

        ureader, uwriter = await asyncio.open_connection(UP_HOST, UP_PORT)
        uwriter.write(new_head + rest)
        await uwriter.drain()

        await asyncio.gather(pipe(creader, uwriter), pipe(ureader, cwriter))
    except Exception as e:
        try:
            cwriter.close()
        except Exception:
            pass

async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", LOCAL_PORT)
    print(f"relay em 127.0.0.1:{LOCAL_PORT} -> {UP_HOST}:{UP_PORT}", flush=True)
    async with server:
        await server.serve_forever()

asyncio.run(main())
