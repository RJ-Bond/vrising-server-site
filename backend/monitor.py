import asyncio
import socket
import struct
import time
import os
from typing import Optional

_cache: dict = {"data": None, "timestamp": 0}
CACHE_TTL = 30


async def query_server(ip: str, port: int) -> Optional[dict]:
    A2S_INFO = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"
    loop = asyncio.get_event_loop()

    def _udp_query() -> Optional[bytes]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(3.0)
            sock.sendto(A2S_INFO, (ip, port))
            data, _ = sock.recvfrom(4096)
            sock.close()
            # Handle challenge response (0x41)
            if len(data) >= 5 and data[4] == 0x41:
                challenge = data[5:9]
                A2S_INFO_CHALLENGE = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00" + challenge
                sock2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock2.settimeout(3.0)
                sock2.sendto(A2S_INFO_CHALLENGE, (ip, port))
                data, _ = sock2.recvfrom(4096)
                sock2.close()
            return data
        except Exception:
            return None

    data = await loop.run_in_executor(None, _udp_query)
    if data is None:
        return None

    try:
        offset = 5
        name, offset = _read_string(data, offset)
        map_name, offset = _read_string(data, offset)
        folder, offset = _read_string(data, offset)
        game, offset = _read_string(data, offset)
        app_id = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        players = data[offset]
        offset += 1
        max_players = data[offset]
        offset += 1
        bots = data[offset]
        offset += 1
        server_type = chr(data[offset])
        offset += 1
        environment = chr(data[offset])
        offset += 1
        visibility = data[offset]
        offset += 1
        vac = data[offset]
        offset += 1
        version, offset = _read_string(data, offset)
        return {
            "online": True,
            "name": name,
            "map": map_name,
            "players": players,
            "max_players": max_players,
            "game": game,
            "version": version,
            "vac": bool(vac),
        }
    except Exception:
        return None


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.index(b"\x00", offset)
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


async def get_server_status(ip: str, port: int) -> dict:
    now = time.time()
    if _cache["data"] is not None and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["data"]

    result = await query_server(ip, port)
    if result is None:
        status = {"online": False, "name": "Unknown", "players": 0, "max_players": 0, "version": "N/A", "map": "N/A"}
    else:
        status = result

    _cache["data"] = status
    _cache["timestamp"] = now
    return status
