"""Lightweight local dev server: static UI + health endpoints + stub WS."""
import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone

app = FastAPI(title="RuView Local Dev")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_now = lambda: datetime.now(timezone.utc).isoformat()

# ── Health ─────────────────────────────────────────────────────
@app.get("/health/live")
@app.get("/health/health")
@app.get("/health/ready")
async def health():
    return {"status": "healthy", "timestamp": _now()}

# ── API info / status / metrics ────────────────────────────────
@app.get("/api/v1/info")
async def info():
    return {
        "name": "wifi-densepose",
        "version": "2.0.0-local",
        "mode": "sensing-only",
        "features": ["presence", "motion", "rssi"],
    }

@app.get("/api/v1/status")
async def status():
    return {"status": "ok", "services": {"sensing_ws": "running"}}

@app.get("/api/v1/metrics")
async def metrics():
    return {"uptime_s": 0, "ws_clients": 0}

# ── Pose stubs (all endpoints the UI calls) ────────────────────
@app.get("/api/v1/pose/current")
async def pose_current():
    return {"persons": [], "timestamp": _now()}

@app.get("/api/v1/pose/zones/summary")
async def zones_summary():
    return {"zones": []}

@app.get("/api/v1/pose/zones/{zone_id}/occupancy")
async def zone_occupancy(zone_id: str):
    return {"zone_id": zone_id, "count": 0}

@app.get("/api/v1/pose/stats")
async def pose_stats(hours: int = 24):
    return {"hours": hours, "total_detections": 0, "avg_confidence": 0}

@app.get("/api/v1/pose/historical")
async def pose_historical():
    return {"frames": []}

@app.get("/api/v1/pose/activities")
async def pose_activities():
    return {"activities": []}

@app.post("/api/v1/pose/analyze")
async def pose_analyze():
    return {"persons": [], "timestamp": _now()}

@app.post("/api/v1/pose/calibrate")
async def pose_calibrate():
    return {"status": "ok"}

@app.get("/api/v1/pose/calibration/status")
async def calibration_status():
    return {"calibrated": False}

# ── Stream stubs ───────────────────────────────────────────────
@app.get("/api/v1/stream/status")
async def stream_status():
    return {"active": True, "clients": 0}

@app.get("/api/v1/stream/metrics")
async def stream_metrics():
    return {"messages_sent": 0, "clients": 0}

# ── WebSocket stubs ────────────────────────────────────────────
@app.websocket("/api/v1/stream/pose")
async def ws_pose(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            # Keep-alive only — real pose data comes via sensing WS
            await asyncio.sleep(30)
            await ws.send_json({"type": "heartbeat"})
    except (WebSocketDisconnect, Exception):
        pass

@app.websocket("/api/v1/stream/events")
async def ws_events(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await asyncio.sleep(5)
            await ws.send_json({"type": "heartbeat"})
    except (WebSocketDisconnect, Exception):
        pass

@app.websocket("/ws/sensing")
async def ws_sensing_stub(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await asyncio.sleep(5)
            await ws.send_json({"type": "heartbeat"})
    except (WebSocketDisconnect, Exception):
        pass

# ── Custom dashboard ────────────────────────────────────────────
from fastapi.responses import FileResponse

@app.get("/")
async def root():
    return FileResponse("dashboard.html")

# ── Static UI (old UI still accessible at /ui/) ────────────────
app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=4000)
