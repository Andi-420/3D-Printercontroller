#!/usr/bin/env python3
"""
Bambu Lab Status-Monitor (nur lesen)

Verbindet sich lokal per MQTT mit dem Drucker und zeigt alle Daten, die der
Drucker meldet, in einer Web-Oberfläche an. Funktioniert auch im normalen
Cloud-Modus, die Bambu Handy App bleibt also nutzbar.

Das Programm sendet KEINE Steuerbefehle. Die einzigen Anfragen an den Drucker
sind "pushall" (vollständigen Status senden) und "get_version"
(Firmware-Versionen), beide nur lesend.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DEMO_FILE = BASE_DIR / "demo" / "p2s_beispiel.json"
MQTT_PORT = 8883
MQTT_USER = "bblp"
PUSHALL_COOLDOWN_S = 60  # pushall nicht zu oft anfordern, das belastet den Drucker


# --------------------------------------------------------------------------- #
# Zustand
# --------------------------------------------------------------------------- #

def _is_id_list(value) -> bool:
    return bool(value) and all(isinstance(i, dict) and "id" in i for i in value)


def deep_merge(dst: dict, src: dict) -> dict:
    """Merged ein Teil-Update in den bisherigen Zustand.

    Bambu-Drucker senden meist nur geänderte Felder. Dicts werden rekursiv
    zusammengeführt, Listen von Objekten mit "id" (z. B. AMS-Einheiten und
    Spulen) werden anhand der id zusammengeführt, alles andere wird ersetzt.
    """
    for key, value in src.items():
        old = dst.get(key)
        if isinstance(value, dict) and isinstance(old, dict):
            deep_merge(old, value)
        elif isinstance(value, list) and isinstance(old, list) and _is_id_list(value) and _is_id_list(old):
            by_id = {item["id"]: item for item in old}
            for item in value:
                if item["id"] in by_id:
                    deep_merge(by_id[item["id"]], item)
                else:
                    old.append(copy.deepcopy(item))
        else:
            dst[key] = copy.deepcopy(value)
    return dst


class PrinterState:
    def __init__(self, host: str, serial: str, demo: bool):
        self._cond = threading.Condition()
        self.data: dict = {}
        self.version = 0
        self.meta = {
            "host": host,
            "serial": serial,
            "demo": demo,
            "connected": False,
            "error": None,
            "messages": 0,
            "last_message": None,
            "last_pushall": None,
        }

    def merge(self, payload: dict) -> None:
        with self._cond:
            deep_merge(self.data, payload)
            self.meta["messages"] += 1
            self.meta["last_message"] = time.time()
            self._bump()

    def set_status(self, connected: bool, error: str | None = None) -> None:
        with self._cond:
            self.meta["connected"] = connected
            self.meta["error"] = error
            self._bump()

    def set_meta(self, **kwargs) -> None:
        with self._cond:
            self.meta.update(kwargs)
            self._bump()

    def _bump(self) -> None:
        self.version += 1
        self._cond.notify_all()

    def snapshot(self) -> dict:
        with self._cond:
            return {"version": self.version, "meta": dict(self.meta), "state": copy.deepcopy(self.data)}

    def wait_for_change(self, since: int, timeout: float) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: self.version != since, timeout=timeout)


# --------------------------------------------------------------------------- #
# MQTT-Verbindung zum Drucker
# --------------------------------------------------------------------------- #

class BambuReader:
    def __init__(self, host: str, serial: str, access_code: str, state: PrinterState, log_path: Path | None):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            sys.exit(
                "Fehler: Die Bibliothek 'paho-mqtt' fehlt für dieses Python.\n"
                "Installiere sie mit demselben Befehl, mit dem du das Programm startest:\n"
                f'  "{sys.executable}" -m pip install -r requirements.txt'
            )

        self.state = state
        self.serial = serial
        self.topic_report = f"device/{serial}/report"
        self.topic_request = f"device/{serial}/request"
        self._log_lock = threading.Lock()
        self._log = open(log_path, "a", encoding="utf-8") if log_path else None
        self._last_pushall = 0.0

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambu-monitor-{random.randint(0, 1 << 30):08x}",
            protocol=mqtt.MQTTv311,
        )
        self.client.username_pw_set(MQTT_USER, access_code)
        # Der Drucker nutzt ein Zertifikat der Bambu-eigenen CA, das auf die
        # Seriennummer ausgestellt ist. Wir verbinden nur im eigenen LAN und
        # prüfen das Zertifikat daher nicht.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.client.tls_set_context(ctx)
        self.client.reconnect_delay_set(min_delay=2, max_delay=30)
        self.client.on_connect = self._on_connect
        self.client.on_connect_fail = self._on_connect_fail
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.host = host

    def start(self) -> None:
        self.client.connect_async(self.host, MQTT_PORT, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()
        if self._log:
            self._log.close()

    # -- Callbacks ---------------------------------------------------------- #

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            hint = ""
            if reason_code.value in (4, 5, 134, 135):
                hint = " (Access Code falsch? Er ändert sich z. B. nach einem Werksreset.)"
            self.state.set_status(False, f"Verbindung abgelehnt: {reason_code}{hint}")
            return
        print(f"[mqtt] verbunden mit {self.host}, abonniere {self.topic_report}")
        self.state.set_status(True)
        client.subscribe(self.topic_report)
        self._publish({"info": {"sequence_id": "0", "command": "get_version"}})
        self.request_pushall(force=True)

    def _on_connect_fail(self, client, userdata):
        print(f"[mqtt] {self.host}:{MQTT_PORT} nicht erreichbar, neuer Versuch läuft ...")
        self.state.set_status(
            False,
            f"Drucker unter {self.host}:{MQTT_PORT} nicht erreichbar. Ist er eingeschaltet, "
            "im selben Netz und stimmt die IP-Adresse? Neuer Versuch läuft ...",
        )

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        print(f"[mqtt] getrennt: {reason_code}")
        if not self.state.meta["connected"] and self.state.meta["error"]:
            return  # Fehler aus on_connect (z. B. falscher Access Code) nicht überschreiben
        msg = None if reason_code.value == 0 else f"Verbindung getrennt: {reason_code}, neuer Versuch läuft ..."
        self.state.set_status(False, msg)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            print(f"[mqtt] konnte Nachricht nicht lesen ({len(msg.payload)} Bytes)")
            return
        if not isinstance(payload, dict):
            return
        self.state.merge(payload)
        if self._log:
            with self._log_lock:
                self._log.write(json.dumps({"ts": time.time(), "topic": msg.topic, "payload": payload}, ensure_ascii=False) + "\n")
                self._log.flush()

    # -- Anfragen (nur lesend) ---------------------------------------------- #

    def _publish(self, payload: dict) -> None:
        self.client.publish(self.topic_request, json.dumps(payload))

    def request_pushall(self, force: bool = False) -> tuple[bool, str]:
        now = time.time()
        wait = PUSHALL_COOLDOWN_S - (now - self._last_pushall)
        if not force and wait > 0:
            return False, f"Bitte noch {math.ceil(wait)} s warten."
        if not self.state.meta["connected"]:
            return False, "Nicht mit dem Drucker verbunden."
        self._last_pushall = now
        self._publish({"pushing": {"sequence_id": "0", "command": "pushall", "version": 1, "push_target": 1}})
        self.state.set_meta(last_pushall=now)
        return True, "Vollständiger Status angefordert."


# --------------------------------------------------------------------------- #
# Demo-Modus (ohne Drucker)
# --------------------------------------------------------------------------- #

class DemoReader:
    """Spielt Beispieldaten ab, damit man die Oberfläche ohne Drucker sieht.

    Grundlage ist ein echter, anonymisierter Status eines P2S (demo/p2s_beispiel.json).
    Temperaturen, Fortschritt und Trocknung werden leicht animiert.
    """

    def __init__(self, state: PrinterState):
        self.state = state
        self._stop = threading.Event()

    def start(self) -> None:
        self.state.set_status(True)
        self.state.merge(self._full())
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def request_pushall(self, force: bool = False) -> tuple[bool, str]:
        self.state.merge(self._full())
        self.state.set_meta(last_pushall=time.time())
        return True, "Demo: vollständiger Status neu geladen."

    @staticmethod
    def _full() -> dict:
        return json.loads(DEMO_FILE.read_text(encoding="utf-8"))

    @staticmethod
    def _packed(current: float, target: int) -> int:
        """Neuere Bambu-Firmware packt Temperaturen als (Soll << 16) | Ist."""
        return (int(target) << 16) | int(round(current))

    def _loop(self) -> None:
        base = self._full()["print"]
        layer, total = base["layer_num"], base["total_layer_num"]
        remaining = base["mc_remaining_time"]
        dry = base["ams"]["ams"][0].get("dry_time", 0)
        t = 0
        while not self._stop.wait(1.5):
            t += 1
            nozzle = 220 + math.sin(t / 3) * 0.6
            bed = 55 + math.sin(t / 5) * 0.3
            chamber = 27 + (t // 20) % 3
            update = {
                "print": {
                    "nozzle_temper": round(nozzle, 1),
                    "bed_temper": round(bed, 1),
                    "big_fan1_speed": str(13 + t % 3),
                    "device": {
                        "extruder": {"info": [{"id": 0, "temp": self._packed(nozzle, 220)}]},
                        "bed": {"info": {"temp": self._packed(bed, 55)}},
                        "bed_temp": self._packed(bed, 55),
                        "ctc": {"info": {"temp": chamber}},
                    },
                    "info": {"temp": chamber},
                }
            }
            if t % 8 == 0:
                layer = min(total, layer + 1)
                remaining = max(0, remaining - 1)
                pct = round(layer / total * 100)
                update["print"].update(
                    layer_num=layer, mc_remaining_time=remaining, remain_time=remaining,
                    mc_percent=pct, percent=pct, **{"3D": {"layer_num": layer}},
                )
            if t % 40 == 0 and dry:
                dry = max(0, dry - 1)
                update["print"]["ams"] = {"ams": [{"id": "0", "dry_time": dry}]}
            if t % 30 == 0:
                update["print"]["wifi_signal"] = f"-{random.randint(62, 72)}dBm"
            self.state.merge(update)


# --------------------------------------------------------------------------- #
# Web-Oberfläche
# --------------------------------------------------------------------------- #

def make_handler(state: PrinterState, reader):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BambuMonitor/1.0"

        def log_message(self, fmt, *args):  # ruhigere Konsole
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(200, state.snapshot())
            elif path == "/events":
                self._events()
            else:
                self._send(404, b"Not found", "text/plain")

        def do_POST(self):
            if self.path == "/api/pushall":
                ok, message = reader.request_pushall()
                self._json(200 if ok else 429, {"ok": ok, "message": message})
            else:
                self._send(404, b"Not found", "text/plain")

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            version = -1
            try:
                while True:
                    if state.wait_for_change(version, timeout=15):
                        snap = state.snapshot()
                        version = snap["version"]
                        data = json.dumps(snap, ensure_ascii=False)
                        self.wfile.write(f"data: {data}\n\n".encode())
                    else:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    time.sleep(0.25)  # Updates leicht bündeln
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    return Handler


# --------------------------------------------------------------------------- #
# Start
# --------------------------------------------------------------------------- #

def load_env_file(path: Path) -> None:
    """Liest eine einfache .env-Datei (KEY=VALUE) ohne Zusatzpaket."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_args(argv=None):
    load_env_file(BASE_DIR / ".env")
    p = argparse.ArgumentParser(description="Bambu Lab Status-Monitor (nur lesen)")
    p.add_argument("--host", default=os.environ.get("BAMBU_HOST"), help="IP-Adresse des Druckers (oder BAMBU_HOST)")
    p.add_argument("--serial", default=os.environ.get("BAMBU_SERIAL"), help="Seriennummer des Druckers (oder BAMBU_SERIAL)")
    p.add_argument("--access-code", default=os.environ.get("BAMBU_ACCESS_CODE"), help="Access Code vom Druckerdisplay (oder BAMBU_ACCESS_CODE)")
    p.add_argument("--web-host", default=os.environ.get("WEB_HOST", "127.0.0.1"), help="Adresse der Web-Oberfläche, 0.0.0.0 = im ganzen LAN erreichbar (Standard: 127.0.0.1)")
    p.add_argument("--web-port", type=int, default=int(os.environ.get("WEB_PORT", "8080")), help="Port der Web-Oberfläche (Standard: 8080)")
    p.add_argument("--log", type=Path, default=os.environ.get("BAMBU_LOG") or None, help="Alle Rohnachrichten zusätzlich als JSON-Lines in diese Datei schreiben")
    p.add_argument("--demo", action="store_true", help="Ohne Drucker mit Beispieldaten starten")
    args = p.parse_args(argv)
    if not args.demo:
        missing = [n for n, v in (("--host", args.host), ("--serial", args.serial), ("--access-code", args.access_code)) if not v]
        if missing:
            p.error(f"fehlt: {', '.join(missing)} (oder in .env eintragen, siehe .env.example; ohne Drucker: --demo)")
        args.serial = args.serial.strip().upper()
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    state = PrinterState(host=args.host or "demo", serial=args.serial or "DEMO", demo=args.demo)
    reader = DemoReader(state) if args.demo else BambuReader(args.host, args.serial, args.access_code, state, args.log)
    reader.start()

    server = ThreadingHTTPServer((args.web_host, args.web_port), make_handler(state, reader))
    server.daemon_threads = True
    shown = "localhost" if args.web_host in ("127.0.0.1", "0.0.0.0") else args.web_host
    print(f"Web-Oberfläche: http://{shown}:{args.web_port}  (Beenden mit Strg+C)")
    if args.log:
        print(f"Rohdaten werden mitgeschrieben in: {args.log}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBeende ...")
    finally:
        server.server_close()
        reader.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
