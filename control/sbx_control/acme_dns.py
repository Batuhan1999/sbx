"""A tiny authoritative DNS server for one name: _acme-challenge.<domain>.

The operator adds one NS record at their DNS provider,
    _acme-challenge.sbx.example.com.  NS  sbx.example.com.
and from then on certbot's DNS-01 challenge is answered by this host
itself: no DNS API credential, at any provider, renewals included. It
answers TXT for that name (the challenge tokens certbot wrote to
/run/sbx/acme/*.txt), SOA and NS for it, TXT for a running port 53 probe's
name under it (probe()), NXDOMAIN for any other name under it, and REFUSED
for anything else. No recursion, nothing cached, UDP and TCP on port 53
(sbx-acme-dns.service).

  python -m sbx_control.acme_dns serve            the server
  python -m sbx_control.acme_dns set TOKEN        certbot's auth hook: add a token
  python -m sbx_control.acme_dns clear            certbot's cleanup hook
  python -m sbx_control.acme_dns probe            whether a public resolver reaches this server on 53 (exit 1: no)
"""
from __future__ import annotations

import asyncio
import re
import secrets
import struct
import subprocess
import sys
import time

from . import paths, settings



def token_dir():
    """Where the certificate's pending challenge answers are."""
    return paths.run() / "acme"


def probe_dir():
    """A running port 53 probe's answer: one file per probe name (probe())."""
    return token_dir() / "probe"


TTL = 30
PROBE_LABEL = re.compile(r"sbx-probe-[0-9a-f]{8}")
# Public resolvers, asked in turn: Let's Encrypt looks the challenge up from the open internet too.
RESOLVERS = ("1.1.1.1", "8.8.8.8")
PORT_53_FIX = ("allow TCP and UDP port 53 in your provider's firewall, or use a DNS API token "
               "(sbx setup address --dns cloudflare)")


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
    host = settings.current().domain.name.rstrip(".")
    if qname.endswith(f".{mine}") and qclass == 1:   # a name under the zone: only a running probe's has records
        found = _probe_answer(qname[:-len(mine) - 1]) if qtype in (16, 255) else None
        if found is not None:
            return (struct.pack("!HHHHHH", ident, header_flags, 1, 1, 0, 0) + question
                    + _rr(qname, 16, bytes([len(found)]) + found.encode()))
        return struct.pack("!HHHHHH", ident, header_flags | 3, 1, 0, 1, 0) + question + _soa(mine, host)  # NXDOMAIN
    if qname != mine or qclass != 1:
        return struct.pack("!HHHHHH", ident, header_flags | 5, 1, 0, 0, 0) + question  # REFUSED
    records = []
    if qtype in (16, 255):  # TXT, ANY
        for token in tokens():
            data = bytes([len(token)]) + token.encode()
            records.append(_rr(mine, 16, data))
    if qtype in (2, 255):  # NS
        records.append(_rr(mine, 2, _labels(host)))
    if qtype in (6, 255) or not records:  # SOA (also as the authority of an empty answer)
        records.append(_soa(mine, host))
    body = b"".join(records)
    return struct.pack("!HHHHHH", ident, header_flags, 1, len(records), 0, 0) + question + body


def _rr(name: str, rtype: int, data: bytes) -> bytes:
    return _labels(name) + struct.pack("!HHIH", rtype, 1, TTL, len(data)) + data


def _soa(zone_name: str, host: str) -> bytes:
    data = _labels(host) + _labels(f"hostmaster.{host}") + struct.pack(
        "!IIIII", int(time.time()) % 2**32, 300, 60, 86400, TTL)
    return _rr(zone_name, 6, data)


def _probe_answer(label: str) -> str | None:
    """The value a running probe published for label, else None."""
    if not PROBE_LABEL.fullmatch(label):
        return None
    try:
        return (probe_dir() / label).read_text().strip() or None
    except OSError:
        return None


def _txt(server: str, name: str, timeout: int) -> list[str]:
    """The TXT values server answers for name (empty: none, or no answer in time)."""
    try:
        out = subprocess.run(["dig", f"@{server}", f"+time={timeout}", "+tries=1", "+short", "TXT", name],
                             capture_output=True, text=True, timeout=timeout + 5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip().strip('"') for line in out.splitlines() if line.startswith('"')]


def probe(timeout: int = 5) -> tuple[bool, str, str]:
    """Whether the internet reaches this server's responder on port 53, as
    Let's Encrypt must: publish a random TXT value at a random name under
    the delegated zone, ask this host's responder for it, then a public
    resolver (RESOLVERS in turn). Returns (ok, detail, fix); the value is
    gone again afterwards, and the name is never asked for twice."""
    if not settings.current().domain.name:
        return False, "no domain yet", ""
    label = f"sbx-probe-{secrets.token_hex(4)}"
    value = secrets.token_hex(12)
    name = f"{label}.{zone()}"
    probe_dir().mkdir(parents=True, exist_ok=True)
    path = probe_dir() / label
    path.write_text(value + "\n")
    try:
        local = primary_address()
        if local and value not in _txt(local, name, timeout):
            return (False, f"sbx's DNS responder does not answer on {local}:53",
                    "sudo systemctl restart sbx-acme-dns, or sudo sbx-apply tls, which starts it")
        for resolver in RESOLVERS:
            if value in _txt(resolver, name, timeout):
                return True, f"a public resolver ({resolver}) reached this server on port 53", ""
        return (False, f"no public resolver ({', '.join(RESOLVERS)}) reached this server on port 53 within {timeout} s",
                PORT_53_FIX)
    finally:
        path.unlink(missing_ok=True)


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
    elif argv[:1] == ["probe"]:   # tls.sh, before it asks Let's Encrypt: what this prints is the certificate's error
        ok, detail, fix = probe()
        print(f"port 53: {detail}" + (f"; fix: {fix}" if fix else ""))
        return 0 if ok else 1
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
