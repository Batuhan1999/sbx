"""A tiny authoritative DNS server for one name: _acme-challenge.<domain>.

The operator adds one NS record at their DNS provider,
    _acme-challenge.sbx.example.com.  NS  sbx.example.com.
and from then on certbot's DNS-01 challenge is answered by this host
itself: no DNS API credential, at any provider, renewals included. It
answers TXT for that name (the challenge tokens certbot wrote to
/run/sbx/acme/*.txt), SOA and NS for it, and REFUSED for anything else.
No recursion, nothing cached, UDP and TCP on port 53 (sbx-acme-dns.service).

  python -m sbx_control.acme_dns serve            the server
  python -m sbx_control.acme_dns set TOKEN        certbot's auth hook: add a token
  python -m sbx_control.acme_dns clear            certbot's cleanup hook
"""
from __future__ import annotations

import asyncio
import secrets
import struct
import sys
import time

from . import paths, settings



def token_dir():
    """Where the certificate's pending challenge answers are."""
    return paths.run() / "acme"
TTL = 30


def zone() -> str:
    return f"_acme-challenge.{settings.current().domain.name}".lower().rstrip(".")


def _labels(name: str) -> bytes:
    out = b"".join(bytes([len(p)]) + p.encode() for p in name.strip(".").split(".") if p)
    return out + b"\x00"


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    parts = []
    jumped, end = False, offset
    for _ in range(64):
        length = data[offset]
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack("!H", data[offset:offset + 2])[0] & 0x3FFF
            if not jumped:
                end = offset + 2
            offset, jumped = pointer, True
            continue
        if length == 0:
            if not jumped:
                end = offset + 1
            break
        parts.append(data[offset + 1:offset + 1 + length].decode(errors="replace"))
        offset += 1 + length
    return ".".join(parts).lower(), end


def tokens() -> list[str]:
    if not token_dir().is_dir():
        return []
    return sorted(p.read_text().strip() for p in token_dir().glob("*.txt") if p.read_text().strip())


def answer(query: bytes) -> bytes | None:
    """The response to one DNS message, or None for garbage."""
    if len(query) < 12:
        return None
    ident, flags, qd = struct.unpack("!HHH", query[:6])
    if flags & 0x8000 or qd != 1:
        return None
    qname, offset = _read_name(query, 12)
    qtype, qclass = struct.unpack("!HH", query[offset:offset + 4])
    question = query[12:offset + 4]
    mine = zone()
    header_flags = 0x8400  # response, authoritative
    if qname != mine or qclass != 1:
        return struct.pack("!HHHHHH", ident, header_flags | 5, 1, 0, 0, 0) + question  # REFUSED
    records = []
    host = settings.current().domain.name.rstrip(".")
    if qtype in (16, 255):  # TXT, ANY
        for token in tokens():
            data = bytes([len(token)]) + token.encode()
            records.append(_rr(mine, 16, data))
    if qtype in (2, 255):  # NS
        records.append(_rr(mine, 2, _labels(host)))
    if qtype in (6, 255) or not records:  # SOA (also as the authority of an empty answer)
        soa = _labels(host) + _labels(f"hostmaster.{host}") + struct.pack("!IIIII", int(time.time()) % 2**32, 300, 60, 86400, TTL)
        records.append(_rr(mine, 6, soa))
    body = b"".join(records)
    return struct.pack("!HHHHHH", ident, header_flags, 1, len(records), 0, 0) + question + body


def _rr(name: str, rtype: int, data: bytes) -> bytes:
    return _labels(name) + struct.pack("!HHIH", rtype, 1, TTL, len(data)) + data


class _UDP(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        reply = answer(data)
        if reply:
            self.transport.sendto(reply, addr)


async def _tcp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        length = struct.unpack("!H", await asyncio.wait_for(reader.readexactly(2), 5))[0]
        reply = answer(await asyncio.wait_for(reader.readexactly(length), 5))
        if reply:
            writer.write(struct.pack("!H", len(reply)) + reply)
            await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()


def primary_address() -> str:
    """The address of the interface the host's default route leaves by: its
    public address, or behind a cloud's NAT the private one the public one
    forwards to; "" when there is no route."""
    import subprocess
    try:
        out = subprocess.run(["ip", "-4", "route", "get", "1.1.1.1"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    words = out.split()
    return words[words.index("src") + 1] if "src" in words[:-1] else ""


def _addresses() -> list[str]:
    """systemd-resolved holds 127.0.0.53:53, so 0.0.0.0 would not bind; the
    public interface does."""
    return [primary_address() or "0.0.0.0"]


async def serve(port: int = 53, addresses: list[str] | None = None):
    loop = asyncio.get_running_loop()
    servers = []
    for address in addresses or _addresses():
        await loop.create_datagram_endpoint(_UDP, local_addr=(address, port))
        servers.append(await asyncio.start_server(_tcp, address, port))
    print(f"acme-dns: authoritative for {zone()} on {', '.join(addresses or _addresses())}:{port}", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


def main(argv: list) -> int:
    if argv[:1] == ["serve"]:  # serve [PORT] [ADDRESS ...]
        asyncio.run(serve(int(argv[1]) if argv[1:] else 53, argv[2:] or None))
    elif argv[:1] == ["set"] and argv[1:]:
        token_dir().mkdir(parents=True, exist_ok=True)
        (token_dir() / f"{secrets.token_hex(4)}.txt").write_text(argv[1] + "\n")
        time.sleep(2)  # let the resolver's cache of an empty answer age
    elif argv[:1] == ["clear"]:
        for p in token_dir().glob("*.txt"):
            p.unlink()
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
