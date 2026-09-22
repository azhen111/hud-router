#!/usr/bin/env python3
"""G2 镜片文字推送通道（Phase 2 / Milestone 3）。

WebSocket 服务：把一行文本以 {"text": "..."} 推给所有已连接的 Even Hub 插件。
不接麦克风 / ASR / router / LLM；不做 TTL / 预算 / 去重（那是 M4）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
from pathlib import Path
from typing import Any

try:
    from websockets.asyncio.server import serve
except ImportError:  # websockets < 13
    from websockets.server import serve  # type: ignore[no-redef]


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8766
DEFAULT_INTERVAL_S = 3.0


def unescape_line(raw: str) -> str:
    """把文件里的字面量 \\n / \\t / \\\\ 展开为真实控制字符。"""
    out: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(raw[i])
        i += 1
    return "".join(out)


def iter_fixture_lines(path: Path) -> list[str]:
    """读取 UTF-8 文本：跳过空行与 # 注释，展开转义。"""
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(unescape_line(raw.rstrip("\r\n")))
    return lines


def client_addr(ws: Any) -> str:
    try:
        remote = ws.remote_address
        if remote is None:
            return "?"
        if isinstance(remote, tuple) and len(remote) >= 2:
            return f"{remote[0]}:{remote[1]}"
        return str(remote)
    except Exception:
        return "?"


class DisplayHub:
    def __init__(self) -> None:
        self.clients: set[Any] = set()

    def status_line(self) -> str:
        if not self.clients:
            return "clients=0 status=no_clients"
        addrs = ", ".join(client_addr(ws) for ws in self.clients)
        return f"clients={len(self.clients)} status=connected addrs=[{addrs}]"

    async def handler(self, websocket: Any, path: Any = None) -> None:
        self.clients.add(websocket)
        print(f"[conn] + {client_addr(websocket)}  {self.status_line()}", flush=True)
        try:
            await websocket.wait_closed()
        finally:
            self.clients.discard(websocket)
            print(f"[conn] - {client_addr(websocket)}  {self.status_line()}", flush=True)

    async def push(self, text: str) -> None:
        payload = json.dumps({"text": text}, ensure_ascii=False)
        n = len(text)
        print(f"[push] chars={n}  {self.status_line()}  text={text!r}", flush=True)
        if not self.clients:
            return
        stale: list[Any] = []
        for ws in list(self.clients):
            try:
                await ws.send(payload)
            except Exception as exc:
                print(f"[push] send_fail {client_addr(ws)}: {exc}", flush=True)
                stale.append(ws)
        for ws in stale:
            self.clients.discard(ws)


async def stdin_loop(hub: DisplayHub) -> None:
    print("交互模式：输入一行回车即推送。Ctrl-D / Ctrl-C 退出。", flush=True)
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if line == "":
            print("[stdin] EOF", flush=True)
            return
        text = unescape_line(line.rstrip("\r\n"))
        if text == "":
            continue
        await hub.push(text)


async def wait_for_first_client(hub: DisplayHub) -> None:
    print("[file] 等待至少一个客户端连接后再推送…", flush=True)
    while not hub.clients:
        await asyncio.sleep(0.05)
    print(f"[file] 已连接，开始推送  {hub.status_line()}", flush=True)


async def wait_enter_or_skip() -> bool:
    """--pause：回车继续，输入 skip（或 EOF）结束剩余。True=继续。"""
    print("[pause] Enter=下一条  skip=结束剩余", flush=True)
    line = await asyncio.to_thread(sys.stdin.readline)
    if line == "":
        print("[pause] EOF — 结束剩余", flush=True)
        return False
    if line.strip().lower() == "skip":
        print("[pause] skip — 结束剩余", flush=True)
        return False
    return True


async def file_loop(
    hub: DisplayHub,
    path: Path,
    interval_s: float,
    pause: bool,
) -> None:
    lines = iter_fixture_lines(path)
    pacing = "pause(Enter/skip)" if pause else f"interval={interval_s}s"
    print(f"[file] {path}  {len(lines)} lines  {pacing}", flush=True)
    if not lines:
        print("[file] 没有可推送的行（全是空行或注释）", flush=True)
        return
    await wait_for_first_client(hub)
    for i, text in enumerate(lines, start=1):
        print(f"[file] {i}/{len(lines)}", flush=True)
        await hub.push(text)
        if i >= len(lines):
            break
        if pause:
            if not await wait_enter_or_skip():
                print(f"[file] 已跳过剩余 {len(lines) - i} 条", flush=True)
                break
        else:
            await asyncio.sleep(interval_s)
    print("[file] done — 服务继续运行，等待客户端。Ctrl-C 退出。", flush=True)


def build_ssl(cert: Path | None, key: Path | None) -> ssl.SSLContext | None:
    if cert is None and key is None:
        return None
    if cert is None or key is None:
        raise SystemExit("--cert 与 --key 必须同时提供才能启用 wss")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return ctx


async def main_async(args: argparse.Namespace) -> None:
    hub = DisplayHub()
    ssl_ctx = build_ssl(
        Path(args.cert) if args.cert else None,
        Path(args.key) if args.key else None,
    )
    scheme = "wss" if ssl_ctx else "ws"
    print(
        f"[listen] {scheme}://{args.host}:{args.port}  "
        f"(bind {args.host})",
        flush=True,
    )

    async with serve(hub.handler, args.host, args.port, ssl=ssl_ctx):
        if args.file:
            await file_loop(hub, Path(args.file), args.interval, args.pause)
            await asyncio.Future()
        else:
            await stdin_loop(hub)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="把文本以 JSON {\"text\": \"...\"} 推到 Even Hub 插件",
    )
    p.add_argument("--host", default=DEFAULT_HOST, help="监听地址，默认 0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 8766")
    p.add_argument(
        "--file",
        metavar="PATH",
        help="按行推送文件（等首个客户端连上后，每行一条；默认间隔 --interval 秒）；省略则走终端交互",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_S,
        help="--file 且未加 --pause 时的行间隔秒数，默认 3",
    )
    p.add_argument(
        "--pause",
        action="store_true",
        help="--file 模式下每条推送后等 stdin 回车再下一条；输入 skip 结束剩余",
    )
    p.add_argument("--cert", help="TLS 证书（启用 wss）")
    p.add_argument("--key", help="TLS 私钥（启用 wss）")
    return p.parse_args(argv)


def main() -> None:
    args = parse_args()
    if args.pause and not args.file:
        raise SystemExit("--pause 仅用于 --file 模式")
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[exit] KeyboardInterrupt", flush=True)


if __name__ == "__main__":
    main()
