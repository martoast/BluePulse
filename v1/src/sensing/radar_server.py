"""
RuView — Bluetooth presence detection.

Scans for nearby phones and devices via BLE advertisements.
Distance estimated from RSSI using log-distance path-loss model.
Every number is real — RSSI from CoreBluetooth, no fake heuristics.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

HOST = "localhost"
PORT = 8765
TICK = 0.5


class BLECollector:
    """Scans for nearby BLE devices using a compiled Swift CoreBluetooth utility."""

    STALE = 30  # drop devices not seen in 30s

    def __init__(self):
        self.devices: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None
        base = os.path.dirname(os.path.abspath(__file__))
        self.src = os.path.join(base, "ble_scanner.swift")
        self.bin = os.path.join(base, "ble_scanner")

    def start(self):
        logger.info("Compiling ble_scanner.swift...")
        try:
            subprocess.run(["swiftc", "-O", "-o", self.bin, self.src],
                           check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            logger.error("BLE compile failed: %s",
                         e.stderr.decode("utf-8", "replace"))
            return
        except FileNotFoundError:
            logger.error("swiftc not found — install Xcode CLI tools")
            return

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                         name="ble")
        self._thread.start()
        logger.info("BLE scanning started")

    def stop(self):
        self._running = False
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self):
        while self._running:
            try:
                self._process = subprocess.Popen(
                    [self.bin], stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,  # don't buffer stderr — blocks the process
                    text=True, bufsize=1)

                while self._running and self._process and self._process.poll() is None:
                    try:
                        line = self._process.stdout.readline().strip()
                        if not line or not line.startswith("{"):
                            continue
                        data = json.loads(line)
                        msg_type = data.get("type")
                        if msg_type == "status":
                            logger.info("BLE: %s", data.get("state"))
                            continue
                        if msg_type == "error":
                            logger.warning("BLE: %s", data.get("message"))
                            continue
                        if msg_type != "ble":
                            continue
                        self._ingest(data)
                    except Exception:
                        time.sleep(0.1)

                if self._running:
                    logger.warning("BLE scanner died — restarting in 3s")
                    time.sleep(3)
            except Exception:
                logger.exception("BLE loop error")
                if self._running:
                    time.sleep(5)

    # Exclude these — everything else (including unknown) is treated as a phone
    NOT_PHONE = {"laptop", "desktop", "tablet", "watch", "earbuds", "tv"}

    @staticmethod
    def _is_phone(d: dict) -> bool:
        """Return True unless we know it's NOT a phone."""
        cls = d.get("cls")
        return cls not in BLECollector.NOT_PHONE

    def _ingest(self, data: dict):
        uuid = data["uuid"]
        rssi = data["rssi"]
        now = data.get("ts", time.time())
        cls = data.get("class")  # phone, laptop, tablet, watch, earbuds, tv, or None

        with self._lock:
            if uuid in self.devices:
                d = self.devices[uuid]
                d["rssi"] = rssi
                d["ts"] = now
                d["hist"].append(rssi)  # deque auto-evicts oldest
                if data.get("name"):
                    d["name"] = data["name"]
                if data.get("vendor"):
                    d["vendor"] = data["vendor"]
                if cls:
                    d["cls"] = cls
            else:
                self.devices[uuid] = {
                    "uuid": uuid, "rssi": rssi,
                    "name": data.get("name"), "vendor": data.get("vendor"),
                    "cls": cls,
                    "ts": now, "first": now,
                    "hist": deque([rssi], maxlen=20),
                }

    def get_devices(self) -> List[dict]:
        now = time.time()
        with self._lock:
            # Prune stale
            stale = [u for u, d in self.devices.items()
                     if now - d["ts"] > self.STALE]
            for u in stale:
                del self.devices[u]

            out = []
            for d in self.devices.values():
                # Only include phones — skip laptops, watches, earbuds, etc.
                if not self._is_phone(d):
                    continue
                rssi = d["rssi"]
                # Path-loss distance: d = 10^((P_ref - RSSI) / (10*n))
                # P_ref = -59 dBm (typical BLE at 1m), n = 2.5 (indoor)
                dist = round(min(30.0, max(0.3,
                    10 ** ((-59 - rssi) / 25.0))), 1)
                hist = list(d.get("hist", []))
                out.append({
                    "uuid": d["uuid"][:8],
                    "uuid_full": d["uuid"],
                    "name": d.get("name"),
                    "vendor": d.get("vendor"),
                    "rssi": rssi,
                    "dist": dist,
                    "age": round(now - d["first"]),
                    "seen": round(now - d["ts"], 1),
                    "history": hist,
                    "rssi_min": min(hist) if hist else rssi,
                    "rssi_max": max(hist) if hist else rssi,
                    "rssi_avg": round(sum(hist) / len(hist), 1) if hist else rssi,
                    "samples": len(hist),
                })
            out.sort(key=lambda x: x["rssi"], reverse=True)
            return out


class Server:
    """WebSocket server broadcasting BLE presence data."""

    def __init__(self):
        self.clients: Set = set()
        self.ble = BLECollector()
        self.timeline: deque = deque(maxlen=600)
        self._running = False
        self.t0 = time.time()
        self._tl_t = 0.0

    async def _handler(self, ws):
        self.clients.add(ws)
        logger.info("Client connected (%d)", len(self.clients))
        try:
            async for _ in ws:
                pass
        finally:
            self.clients.discard(ws)

    async def _broadcast(self, msg: str):
        dead = set()
        for ws in self.clients:
            try:
                await ws.send(msg)
            except Exception:
                dead.add(ws)
        self.clients -= dead

    async def _tick(self):
        while self._running:
            try:
                devs = self.ble.get_devices()
                now = time.time()
                close = sum(1 for d in devs if d["rssi"] > -70)

                if now - self._tl_t >= 1.0:
                    self._tl_t = now
                    self.timeline.append({
                        "t": round(now, 1),
                        "n": len(devs),
                        "c": close,
                    })

                if self.clients:
                    await self._broadcast(json.dumps({
                        "type": "ble_update",
                        "ts": now,
                        "uptime": round(now - self.t0, 1),
                        "count": len(devs),
                        "close": close,
                        "devices": devs,
                        "tl": list(self.timeline)[-120:],
                    }))
            except Exception:
                logger.exception("Tick error")
            await asyncio.sleep(TICK)

    async def run(self):
        try:
            import websockets
        except ImportError:
            print("pip install websockets"); sys.exit(1)

        self.ble.start()
        self._running = True

        print(f"\n  RuView Bluetooth Presence — ws://{HOST}:{PORT}")
        print(f"  Open http://localhost:4000\n")

        async with websockets.serve(self._handler, HOST, PORT):
            await self._tick()

    def stop(self):
        self._running = False
        self.ble.stop()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    srv = Server()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    signal.signal(signal.SIGINT, lambda *_: (srv.stop(), loop.stop()))
    try:
        loop.run_until_complete(srv.run())
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
        loop.close()


if __name__ == "__main__":
    main()
