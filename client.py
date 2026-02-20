# client.py
import asyncio
import base64
import hashlib
import json
import os
import shutil
import struct
import time
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple


# -----------------------------
# Config
# -----------------------------
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 9239

STEAM_LOGINUSERS = r"C:\Program Files (x86)\Steam\config\loginusers.vdf"

LIVERIES_ROOT = os.path.join(
    os.path.expanduser("~"),
    "Documents",
    "Assetto Corsa Competizione",
    "Customs",
    "Liveries",
)

CHUNK_SIZE = 256 * 1024  # 256KB


# -----------------------------
# Framing (len-prefixed JSON)
# -----------------------------
async def read_msg(reader: asyncio.StreamReader) -> Dict[str, Any]:
    hdr = await reader.readexactly(4)
    (n,) = struct.unpack(">I", hdr)
    payload = await reader.readexactly(n)
    return json.loads(payload.decode("utf-8"))


async def send_msg(writer: asyncio.StreamWriter, msg: Dict[str, Any]) -> None:
    data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    writer.write(struct.pack(">I", len(data)) + data)
    await writer.drain()


# -----------------------------
# Helpers
# -----------------------------
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def canonical_relpath(root: str, full_path: str) -> str:
    rel = os.path.relpath(full_path, root)
    rel = rel.replace("\\", "/")
    return rel


def build_manifest(root: str) -> Tuple[List[Dict[str, Any]], str]:
    items: List[Dict[str, Any]] = []
    if not os.path.isdir(root):
        ensure_dir(root)

    for dirpath, dirnames, filenames in os.walk(root):
        # Skip backups and temp
        dirnames[:] = [d for d in dirnames if d.lower() not in (".backup", ".tmp")]

        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if not os.path.isfile(full):
                continue
            rel = canonical_relpath(root, full)
            try:
                size = os.path.getsize(full)
                h = sha256_file(full)
            except Exception:
                continue
            items.append({"rel_path": rel, "size": size, "sha256": h})

    items.sort(key=lambda x: x["rel_path"])
    blob = json.dumps(items, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    mh = hashlib.sha256(blob).hexdigest()
    return items, mh


def read_steam_id_from_loginusers(path: str = STEAM_LOGINUSERS) -> str:
    if not os.path.isfile(path):
        return ""

    steam_ids: List[str] = []
    most_recent: Optional[str] = None
    current_id: Optional[str] = None
    in_users = False

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue

            if s.lower().strip('"') == "users":
                in_users = True
                continue

            if in_users and s.startswith('"') and s.count('"') >= 2:
                token = s.split('"')[1]
                if token.isdigit() and len(token) >= 15:
                    current_id = token
                    if current_id not in steam_ids:
                        steam_ids.append(current_id)
                    continue

            if current_id and '"MostRecent"' in s and '"1"' in s.replace(" ", ""):
                most_recent = current_id

    if most_recent:
        return most_recent
    return steam_ids[0] if steam_ids else ""


def backup_existing(target_file: str, root: str) -> None:
    if not os.path.isfile(target_file):
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = os.path.join(root, ".backup", ts)
    rel = canonical_relpath(root, target_file)
    backup_path = os.path.join(backup_root, rel.replace("/", os.sep))
    ensure_dir(os.path.dirname(backup_path))
    shutil.copy2(target_file, backup_path)


# -----------------------------
# Client
# -----------------------------
class SyncClient:
    def __init__(self, host: str, port: int, client_name: str):
        self.host = host
        self.port = port
        self.client_name = client_name

        self.reader: asyncio.StreamReader = None  # type: ignore
        self.writer: asyncio.StreamWriter = None  # type: ignore
        self.client_id: str = ""
        self.steam_id: str = ""

        self.manifest: List[Dict[str, Any]] = []
        self.manifest_hash: str = ""

        # Receive state
        self._rx_tmp_path: Optional[str] = None
        self._rx_sha256: Optional[str] = None
        self._rx_rel: Optional[str] = None

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)

        welcome = await read_msg(self.reader)
        if welcome.get("type") != "WELCOME":
            raise RuntimeError("Bad server welcome")
        self.client_id = str(welcome.get("client_id", ""))

        self.steam_id = read_steam_id_from_loginusers() or "UNKNOWN"
        self.manifest, self.manifest_hash = build_manifest(LIVERIES_ROOT)

        await send_msg(self.writer, {
            "type": "HELLO",
            "steam_id": self.steam_id,
            "client_name": self.client_name,
            "manifest_hash": self.manifest_hash,
        })

        await send_msg(self.writer, {
            "type": "MANIFEST_SUMMARY",
            "manifest_hash": self.manifest_hash,
            "file_count": len(self.manifest),
            "total_bytes": sum(int(x["size"]) for x in self.manifest),
        })

        # UNION MODE: proactively send full manifest once on connect
        await send_msg(self.writer, {
            "type": "MANIFEST_FULL",
            "sync_id": "",
            "manifest": self.manifest,
        })

        print(f"[CONNECTED] client_id={self.client_id} steam_id={self.steam_id}")
        print(f"[STATE] Liveries={LIVERIES_ROOT}")
        print(f"[STATE] manifest_hash={self.manifest_hash} files={len(self.manifest)}")

    def _manifest_map(self, manifest: List[Dict[str, Any]]) -> Dict[str, str]:
        return {m["rel_path"]: m["sha256"] for m in manifest if "rel_path" in m and "sha256" in m}

    async def run(self) -> None:
        try:
            while True:
                msg = await read_msg(self.reader)
                mtype = msg.get("type")

                if mtype == "SERVER_STATE":
                    print(f"[SERVER] union_hash={msg.get('current_hash')}")

                elif mtype == "SYNC_OFFER":
                    print(f"[SYNC] Offer sync_id={msg.get('sync_id')} role={msg.get('role')} union={msg.get('current_hash')}")

                elif mtype == "REQUEST_MANIFEST":
                    # Rebuild manifest and send full (server uses it for union)
                    self.manifest, self.manifest_hash = build_manifest(LIVERIES_ROOT)
                    await send_msg(self.writer, {
                        "type": "MANIFEST_FULL",
                        "sync_id": str(msg.get("sync_id", "")),
                        "manifest": self.manifest,
                    })
                    await send_msg(self.writer, {
                        "type": "MANIFEST_SUMMARY",
                        "manifest_hash": self.manifest_hash,
                        "file_count": len(self.manifest),
                        "total_bytes": sum(int(x["size"]) for x in self.manifest),
                    })
                    print("[MANIFEST] Sent MANIFEST_FULL + SUMMARY")

                elif mtype == "SEND_FILES":
                    # We are SOURCE: stream requested files
                    sync_id = str(msg.get("sync_id", ""))
                    paths = msg.get("paths", [])
                    if not isinstance(paths, list):
                        continue

                    # Ensure manifest is fresh
                    self.manifest, self.manifest_hash = build_manifest(LIVERIES_ROOT)
                    src_map = self._manifest_map(self.manifest)

                    print(f"[SYNC] SOURCE sending {len(paths)} files for sync_id={sync_id}")

                    for rel in paths:
                        if not isinstance(rel, str):
                            continue
                        rel_norm = rel.replace("\\", "/")
                        full = os.path.join(LIVERIES_ROOT, rel_norm.replace("/", os.sep))
                        if not os.path.isfile(full):
                            continue

                        expected_hash = src_map.get(rel_norm) or sha256_file(full)
                        size = os.path.getsize(full)

                        await send_msg(self.writer, {
                            "type": "FILE_BEGIN",
                            "sync_id": sync_id,
                            "rel_path": rel_norm,
                            "size": size,
                            "sha256": expected_hash,
                        })

                        with open(full, "rb") as f:
                            offset = 0
                            while True:
                                chunk = f.read(CHUNK_SIZE)
                                if not chunk:
                                    break
                                b64 = base64.b64encode(chunk).decode("ascii")
                                await send_msg(self.writer, {
                                    "type": "FILE_CHUNK",
                                    "sync_id": sync_id,
                                    "rel_path": rel_norm,
                                    "offset": offset,
                                    "data_b64": b64,
                                })
                                offset += len(chunk)

                        await send_msg(self.writer, {
                            "type": "FILE_END",
                            "sync_id": sync_id,
                            "rel_path": rel_norm,
                        })

                    print(f"[SYNC] SOURCE done streaming sync_id={sync_id}")

                elif mtype == "FILE_BEGIN":
                    # We are TARGET: prepare to receive
                    rel = str(msg.get("rel_path", ""))
                    sh = str(msg.get("sha256", ""))

                    self._rx_rel = rel
                    self._rx_sha256 = sh

                    tmp_dir = os.path.join(LIVERIES_ROOT, ".tmp")
                    ensure_dir(tmp_dir)
                    tmp_name = f"rx_{int(time.time()*1000)}_{os.path.basename(rel).replace('/', '_')}.tmp"
                    self._rx_tmp_path = os.path.join(tmp_dir, tmp_name)

                    with open(self._rx_tmp_path, "wb") as _:
                        pass

                elif mtype == "FILE_CHUNK":
                    rel = str(msg.get("rel_path", ""))
                    if not self._rx_tmp_path or rel != self._rx_rel:
                        continue
                    data_b64 = msg.get("data_b64", "")
                    if not isinstance(data_b64, str):
                        continue
                    chunk = base64.b64decode(data_b64.encode("ascii"))

                    with open(self._rx_tmp_path, "ab") as f:
                        f.write(chunk)

                elif mtype == "FILE_END":
                    sync_id = str(msg.get("sync_id", ""))
                    rel = str(msg.get("rel_path", ""))
                    if not self._rx_tmp_path or rel != self._rx_rel:
                        continue

                    # Verify hash
                    try:
                        got = sha256_file(self._rx_tmp_path)
                    except Exception:
                        got = ""

                    if self._rx_sha256 and got.lower() != self._rx_sha256.lower():
                        await send_msg(self.writer, {
                            "type": "ERROR",
                            "sync_id": sync_id,
                            "message": f"Hash mismatch for {rel} expected={self._rx_sha256} got={got}",
                        })
                        try:
                            os.remove(self._rx_tmp_path)
                        except Exception:
                            pass
                        self._rx_tmp_path = None
                        self._rx_rel = None
                        self._rx_sha256 = None
                        continue

                    # Move into place (backup if overwriting)
                    final_path = os.path.join(LIVERIES_ROOT, rel.replace("/", os.sep))
                    ensure_dir(os.path.dirname(final_path))
                    backup_existing(final_path, LIVERIES_ROOT)

                    os.replace(self._rx_tmp_path, final_path)

                    self._rx_tmp_path = None
                    self._rx_rel = None
                    self._rx_sha256 = None

                    # Debounced done signal for this sync session
                    asyncio.create_task(self._debounced_sync_done(sync_id))

                elif mtype == "SYNC_DONE_ACK":
                    print(f"[SYNC] Server ack sync_id={msg.get('sync_id')} new_hash={msg.get('new_manifest_hash')}")

                elif mtype == "PONG":
                    pass

        except (asyncio.IncompleteReadError, ConnectionResetError):
            print("[DISCONNECTED] server closed connection")
        except Exception as e:
            print(f"[ERROR] {e}")

    async def _debounced_sync_done(self, sync_id: str) -> None:
        await asyncio.sleep(1.0)

        # If still mid-file, don't finalize
        if self._rx_tmp_path is not None:
            return

        # Recompute manifest/hash and report done
        self.manifest, self.manifest_hash = build_manifest(LIVERIES_ROOT)

        await send_msg(self.writer, {
            "type": "MANIFEST_SUMMARY",
            "manifest_hash": self.manifest_hash,
            "file_count": len(self.manifest),
            "total_bytes": sum(int(x["size"]) for x in self.manifest),
        })

        # Also send full manifest so server union stays accurate
        await send_msg(self.writer, {
            "type": "MANIFEST_FULL",
            "sync_id": "",
            "manifest": self.manifest,
        })

        await send_msg(self.writer, {
            "type": "SYNC_DONE",
            "sync_id": sync_id,
            "new_manifest_hash": self.manifest_hash,
        })

        print(f"[SYNC] TARGET reported SYNC_DONE sync_id={sync_id} new_hash={self.manifest_hash}")


if __name__ == "__main__":
    import argparse
    import socket

    ap = argparse.ArgumentParser(description="ACC Liveries Sync Client (UNION MERGE)")
    ap.add_argument("--host", default=DEFAULT_SERVER_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT)
    ap.add_argument("--name", default=socket.gethostname())
    args = ap.parse_args()

    c = SyncClient(args.host, args.port, args.name)

    async def main():
        await c.connect()
        await c.run()

    asyncio.run(main())