import asyncio
import socket
import struct
import time
from collections import deque
from typing import Optional

_cache: dict = {}  # key: (ip, port) -> {"data": ..., "timestamp": ...}
_history: dict = {}  # key: (ip, port) -> deque of (ts, player_count)
CACHE_TTL = 30
HISTORY_INTERVAL = 300  # record every 5 min
HISTORY_MAX = 288       # 24h at 5-min intervals


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
            if len(data) >= 5 and data[4] == 0x41:
                challenge = data[5:9]
                sock2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock2.settimeout(3.0)
                sock2.sendto(b"\xFF\xFF\xFF\xFFTSource Engine Query\x00" + challenge, (ip, port))
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
        offset += 2  # app_id
        players = data[offset]; offset += 1
        max_players = data[offset]; offset += 1
        offset += 1  # bots
        offset += 1  # server_type
        offset += 1  # environment
        offset += 1  # visibility
        vac = data[offset]; offset += 1
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


async def query_players(ip: str, port: int) -> list:
    loop = asyncio.get_event_loop()

    def _udp_query() -> Optional[bytes]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(3.0)
            sock.sendto(b"\xFF\xFF\xFF\xFF\x55\xFF\xFF\xFF\xFF", (ip, port))
            data, _ = sock.recvfrom(4096)
            sock.close()
            if len(data) >= 5 and data[4] == 0x41:
                challenge = data[5:9]
                sock2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock2.settimeout(3.0)
                sock2.sendto(b"\xFF\xFF\xFF\xFF\x55" + challenge, (ip, port))
                data, _ = sock2.recvfrom(4096)
                sock2.close()
            return data
        except Exception:
            return None

    data = await loop.run_in_executor(None, _udp_query)
    if data is None or len(data) < 6 or data[4] != 0x44:
        return []

    try:
        count = data[5]
        offset = 6
        players = []
        for _ in range(count):
            offset += 1  # index byte
            name, offset = _read_string(data, offset)
            score = struct.unpack_from("<i", data, offset)[0]; offset += 4
            duration = struct.unpack_from("<f", data, offset)[0]; offset += 4
            players.append({"name": name, "score": score, "duration": int(duration)})
        return players
    except Exception:
        return []


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.index(b"\x00", offset)
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


async def get_server_status(ip: str, port: int) -> dict:
    now = time.time()
    key = (ip, port)
    entry = _cache.get(key)
    if entry and entry["data"] is not None and (now - entry["timestamp"]) < CACHE_TTL:
        return entry["data"]

    result = await query_server(ip, port)
    if result is None:
        status = {
            "online": False, "name": "Unknown", "players": 0,
            "max_players": 0, "version": "N/A", "map": "N/A",
            "vac": False, "players_list": [],
        }
    else:
        players_list = await query_players(ip, port)
        status = {**result, "players_list": players_list}

    _cache[key] = {"data": status, "timestamp": now}

    # record history snapshot every HISTORY_INTERVAL seconds
    hist = _history.setdefault(key, deque(maxlen=HISTORY_MAX))
    if not hist or (now - hist[-1][0]) >= HISTORY_INTERVAL:
        hist.append((now, status["players"]))

    return status


def get_history(ip: str, port: int) -> list:
    key = (ip, port)
    return [{"ts": int(ts), "players": p} for ts, p in _history.get(key, [])]
