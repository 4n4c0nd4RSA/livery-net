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
VERSION = "1.0.4"
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 9239

STEAM_LOGINUSERS = r"C:\Program Files (x86)\Steam\config\loginusers.vdf"

CHUNK_SIZE = 256 * 1024  # 256KB
ALLOWED_EXTS = {".png", ".dds", ".json"}

CONFLICT_SUFFIX = "__CONFLICT__"

# If local mtime is newer by more than this, refuse overwrite.
# (Small tolerance helps avoid 1-second resolution quirks / copy2 behavior.)
MTIME_TOLERANCE_SECONDS = 1.0

def find_liveries_root() -> str:
    """
    Attempts to find the ACC Liveries folder in standard Documents or OneDrive.
    """
    standard_docs = os.path.join(os.path.expanduser("~"), "Documents")
    
    # 2. Check OneDrive environment variables (common on Windows)
    # OneDrive (Personal) or OneDriveConsumer
    onedrive_path = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")

    candidates = [standard_docs]
    if onedrive_path:
        candidates.insert(0, os.path.join(onedrive_path, "Documents"))

    sub_path = os.path.join("Assetto Corsa Competizione", "Customs", "Liveries")

    for base in candidates:
        full_path = os.path.join(base, sub_path)
        if os.path.isdir(full_path):
            return full_path

    # Fallback to standard if none exist yet
    return os.path.join(standard_docs, sub_path)

LIVERIES_ROOT = find_liveries_root()

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
    return rel.replace("\\", "/")


def _normalize_rel(rel: str) -> str:
    return rel.replace("\\", "/").strip()


def _is_allowed_rel(rel: str) -> bool:
    """
    Strict validation for any relative path received from the network or built locally.

    Rules:
    - must be a non-empty string
    - must not be absolute
    - must not contain parent traversal segments
    - must not contain Windows drive prefixes
    - must not contain NUL bytes
    - must not contain conflict-tagged names
    - extension must be in ALLOWED_EXTS
    """
    if not isinstance(rel, str):
        return False

    rp = _normalize_rel(rel)
    if not rp:
        return False

    if "\x00" in rp:
        return False

    if rp.startswith("/") or rp.startswith("\\"):
        return False

    drive, _ = os.path.splitdrive(rp)
    if drive:
        return False

    parts = [p for p in rp.split("/") if p not in ("", ".")]
    if not parts:
        return False

    if ".." in parts:
        return False

    if "__CONFLICT__" in rp:
        return False

    if "Copy." in rp:
        return False

    _b, ext = os.path.splitext(rp)
    return ext.lower() in ALLOWED_EXTS


def _safe_join_under_root(root: str, rel: str) -> str:
    """
    Join rel under root and verify the result remains inside root.
    Raises ValueError if unsafe.
    """
    if not _is_allowed_rel(rel):
        raise ValueError(f"Unsafe relative path: {rel!r}")

    root_abs = os.path.abspath(root)
    joined = os.path.abspath(os.path.join(root_abs, _normalize_rel(rel).replace("/", os.sep)))

    try:
        common = os.path.commonpath([root_abs, joined])
    except ValueError:
        raise ValueError(f"Unsafe path outside root: {rel!r}")

    if common != root_abs:
        raise ValueError(f"Unsafe path outside root: {rel!r}")

    return joined


def _conflict_path_for(final_path: str, incoming_sha256: str) -> str:
    base, ext = os.path.splitext(final_path)
    short = (incoming_sha256 or "unknown")[:8]
    candidate = f"{base}{CONFLICT_SUFFIX}{short}{ext}"
    # Ensure uniqueness if needed
    i = 1
    while os.path.exists(candidate):
        candidate = f"{base}{CONFLICT_SUFFIX}{short}_{i}{ext}"
        i += 1
    return candidate


def build_manifest(root: str) -> Tuple[List[Dict[str, Any]], str]:
    """
    Manifest items include true last modified time:
      - mtime: os.path.getmtime(full) (UNIX seconds)
    """
    items: List[Dict[str, Any]] = []
    if not os.path.isdir(root):
        ensure_dir(root)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in (".backup", ".tmp")]

        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if not os.path.isfile(full):
                continue

            rel = canonical_relpath(root, full)
            if not _is_allowed_rel(rel):
                continue

            try:
                size = os.path.getsize(full)
                mtime = float(os.path.getmtime(full))
                h = sha256_file(full)
            except Exception:
                continue

            items.append({"rel_path": rel, "size": size, "sha256": h, "mtime": mtime})

    items.sort(key=lambda x: x["rel_path"])
    blob = json.dumps(
        [{"rel_path": i["rel_path"], "size": i["size"], "sha256": i["sha256"]} for i in items],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
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


def safe_set_mtime(path: str, mtime: Optional[float]) -> None:
    if not mtime:
        return
    try:
        os.utime(path, (float(mtime), float(mtime)))
    except Exception:
        pass


def safe_get_mtime(path: str) -> float:
    try:
        return float(os.path.getmtime(path))
    except Exception:
        return 0.0


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
        self._rx_mtime: Optional[float] = None
        self._rx_sync_id: Optional[str] = None

        self._sync_done_task: Optional[asyncio.Task] = None

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

        await send_msg(self.writer, {
            "type": "MANIFEST_FULL",
            "sync_id": "",
            "manifest": self.manifest,
        })

        print(f"[CONNECTED] client_id={self.client_id} steam_id={self.steam_id}")
        print(f"[STATE] Liveries={LIVERIES_ROOT}")
        print(f"[STATE] manifest_hash={self.manifest_hash} files={len(self.manifest)}")

    def _manifest_map(self, manifest: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for m in manifest:
            rel = m.get("rel_path")
            sh = m.get("sha256")
            if not isinstance(rel, str) or not isinstance(sh, str):
                continue
            out[rel] = {
                "sha256": sh,
                "size": int(m.get("size", 0)),
                "mtime": float(m.get("mtime", 0.0) or 0.0),
            }
        return out

    def _schedule_sync_done(self, sync_id: str) -> None:
        # cancel prior debounce task
        if self._sync_done_task and not self._sync_done_task.done():
            self._sync_done_task.cancel()
        self._sync_done_task = asyncio.create_task(self._debounced_sync_done(sync_id))

    def _reset_rx_state(self) -> None:
        self._rx_tmp_path = None
        self._rx_sha256 = None
        self._rx_rel = None
        self._rx_mtime = None
        self._rx_sync_id = None

    async def _report_error(self, sync_id: str, message: str) -> None:
        print(f"[ERROR] {message}")
        try:
            await send_msg(self.writer, {
                "type": "ERROR",
                "sync_id": sync_id,
                "message": message,
            })
        except Exception:
            pass

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

                elif mtype == "SEND_FILES":
                    sync_id = str(msg.get("sync_id", ""))
                    paths = msg.get("paths", [])
                    if not isinstance(paths, list):
                        continue

                    self.manifest, self.manifest_hash = build_manifest(LIVERIES_ROOT)
                    src_map = self._manifest_map(self.manifest)

                    print(f"[SYNC] SOURCE sending {len(paths)} files for sync_id={sync_id}")

                    for rel in paths:
                        if not isinstance(rel, str):
                            continue
                        rel_norm = _normalize_rel(rel)
                        if not _is_allowed_rel(rel_norm):
                            print(f"[WARN] Refusing to send unsafe path from server request: {rel!r}")
                            continue

                        try:
                            full = _safe_join_under_root(LIVERIES_ROOT, rel_norm)
                        except ValueError:
                            print(f"[WARN] Refusing to send path outside root: {rel!r}")
                            continue

                        if not os.path.isfile(full):
                            continue

                        meta = src_map.get(rel_norm)
                        expected_hash = (meta or {}).get("sha256") or sha256_file(full)
                        size = os.path.getsize(full)
                        mtime = safe_get_mtime(full)

                        await send_msg(self.writer, {
                            "type": "FILE_BEGIN",
                            "sync_id": sync_id,
                            "rel_path": rel_norm,
                            "size": size,
                            "sha256": expected_hash,
                            "mtime": mtime,
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
                    rel = str(msg.get("rel_path", ""))
                    sh = str(msg.get("sha256", ""))
                    sync_id = str(msg.get("sync_id", ""))
                    mt = msg.get("mtime", 0)

                    if not _is_allowed_rel(rel):
                        await self._report_error(sync_id, f"Rejected unsafe rel_path in FILE_BEGIN: {rel!r}")
                        self._reset_rx_state()
                        continue

                    try:
                        final_path = _safe_join_under_root(LIVERIES_ROOT, rel)
                    except ValueError as e:
                        await self._report_error(sync_id, f"Rejected path outside root in FILE_BEGIN: {e}")
                        self._reset_rx_state()
                        continue

                    self._rx_rel = rel
                    self._rx_sha256 = sh
                    self._rx_sync_id = sync_id
                    try:
                        self._rx_mtime = float(mt)
                    except Exception:
                        self._rx_mtime = 0.0

                    tmp_dir = os.path.join(LIVERIES_ROOT, ".tmp")
                    ensure_dir(tmp_dir)
                    tmp_name = f"rx_{int(time.time()*1000)}_{os.path.basename(rel).replace('/', '_')}.tmp"
                    self._rx_tmp_path = os.path.join(tmp_dir, tmp_name)

                    ensure_dir(os.path.dirname(final_path))

                    with open(self._rx_tmp_path, "wb") as _:
                        pass

                elif mtype == "FILE_CHUNK":
                    rel = str(msg.get("rel_path", ""))
                    sync_id = str(msg.get("sync_id", ""))

                    if not _is_allowed_rel(rel):
                        await self._report_error(sync_id, f"Rejected unsafe rel_path in FILE_CHUNK: {rel!r}")
                        continue

                    if not self._rx_tmp_path or rel != self._rx_rel:
                        continue

                    data_b64 = msg.get("data_b64", "")
                    if not isinstance(data_b64, str):
                        await self._report_error(sync_id, f"Rejected invalid chunk payload for {rel}")
                        continue

                    try:
                        chunk = base64.b64decode(data_b64.encode("ascii"), validate=True)
                    except Exception:
                        await self._report_error(sync_id, f"Rejected invalid base64 chunk for {rel}")
                        continue

                    with open(self._rx_tmp_path, "ab") as f:
                        f.write(chunk)

                elif mtype == "FILE_END":
                    sync_id = str(msg.get("sync_id", ""))
                    rel = str(msg.get("rel_path", ""))

                    if not _is_allowed_rel(rel):
                        await self._report_error(sync_id, f"Rejected unsafe rel_path in FILE_END: {rel!r}")
                        continue

                    if not self._rx_tmp_path or rel != self._rx_rel:
                        continue

                    try:
                        final_path = _safe_join_under_root(LIVERIES_ROOT, rel)
                    except ValueError as e:
                        await self._report_error(sync_id, f"Rejected path outside root in FILE_END: {e}")
                        try:
                            os.remove(self._rx_tmp_path)
                        except Exception:
                            pass
                        self._reset_rx_state()
                        continue

                    try:
                        got = sha256_file(self._rx_tmp_path)
                    except Exception:
                        got = ""

                    if self._rx_sha256 and got.lower() != self._rx_sha256.lower():
                        await self._report_error(
                            sync_id,
                            f"Hash mismatch for {rel} expected={self._rx_sha256} got={got}",
                        )
                        try:
                            os.remove(self._rx_tmp_path)
                        except Exception:
                            pass
                        self._reset_rx_state()
                        continue

                    incoming_mtime = float(self._rx_mtime or 0.0)
                    local_exists = os.path.isfile(final_path)
                    local_mtime = safe_get_mtime(final_path) if local_exists else 0.0

                    if local_exists and (local_mtime > incoming_mtime + MTIME_TOLERANCE_SECONDS):
                        conflict_path = _conflict_path_for(final_path, got)
                        backup_existing(conflict_path, LIVERIES_ROOT)
                        os.replace(self._rx_tmp_path, conflict_path)
                        safe_set_mtime(conflict_path, incoming_mtime)

                        print(
                            f"[MTIME] Kept LOCAL newer file for {rel} "
                            f"(local={local_mtime:.3f} > incoming={incoming_mtime:.3f}); "
                            f"saved incoming as {os.path.relpath(conflict_path, LIVERIES_ROOT)}"
                        )
                    else:
                        if local_exists:
                            backup_existing(final_path, LIVERIES_ROOT)
                        os.replace(self._rx_tmp_path, final_path)
                        safe_set_mtime(final_path, incoming_mtime)
                        print(
                            f"[MTIME] Applied INCOMING for {rel} "
                            f"(incoming={incoming_mtime:.3f} >= local={local_mtime:.3f})"
                        )

                    self._reset_rx_state()
                    self._schedule_sync_done(sync_id)

                elif mtype == "SYNC_DONE_ACK":
                    print(f"[SYNC] Server ack sync_id={msg.get('sync_id')} new_hash={msg.get('new_manifest_hash')}")

                elif mtype == "PONG":
                    pass

        except (asyncio.IncompleteReadError, ConnectionResetError):
            print("[DISCONNECTED] server closed connection")
        except asyncio.CancelledError:
            pass
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

    ap = argparse.ArgumentParser(description=f"ACC Liveries Sync Client v{VERSION} (SAFER PATH VALIDATION)")
    ap.add_argument("--host", default=DEFAULT_SERVER_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT)
    ap.add_argument("--name", default=socket.gethostname())
    args = ap.parse_args()

    c = SyncClient(args.host, args.port, args.name)

    async def main():
        await c.connect()
        await c.run()

    asyncio.run(main())