# server.py
import asyncio
import json
import struct
import time
import uuid
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple, Set


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
# Server state
# -----------------------------
@dataclass
class ClientConn:
    client_id: str
    steam_id: str = ""
    client_name: str = ""
    connected_at: float = field(default_factory=time.time)
    syncing: bool = False
    reader: asyncio.StreamReader = None  # type: ignore
    writer: asyncio.StreamWriter = None  # type: ignore

    manifest_hash: str = ""
    manifest_count: int = 0
    manifest_bytes: int = 0

    manifest_items: Optional[List[Dict[str, Any]]] = None
    manifest_items_at: float = 0.0

    manifest_request_inflight: bool = False
    manifest_last_request_at: float = 0.0
    manifest_refresh_pending: bool = False


@dataclass
class SyncSession:
    sync_id: str
    source_id: str
    target_id: str
    paths: List[str]
    union_hash: str
    created_at: float = field(default_factory=time.time)


class SyncServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 9239):
        self.host = host
        self.port = port

        self.clients: Dict[str, ClientConn] = {}
        self.syncs: Dict[str, SyncSession] = {}

        self._lock = asyncio.Lock()

        self.MAX_FILES_PER_SESSION = 50
        self.MANIFEST_STALE_SECONDS = 15.0
        self.SCHEDULER_TICK_SECONDS = 1.0
        self.CONFLICT_SUFFIX = "__CONFLICT__"
        self.MANIFEST_REQUEST_MIN_INTERVAL = 3.0

        # ONLY allow these:
        self.ALLOWED_EXTS: Set[str] = {".png", ".dds", ".json"}
        self._sha256_re = re.compile(r"^[0-9a-fA-F]{64}$")

        # Whitelist (root/whitelist.txt)
        self.WHITELIST_FILE = os.path.join(os.getcwd(), "whitelist.txt")
        self.whitelist_names: Set[str] = set()
        self._whitelist_mtime: float = 0.0
        self._load_whitelist(force=True)

    # -----------------------------
    # Whitelist
    # -----------------------------
    def _load_whitelist(self, force: bool = False) -> None:
        try:
            if not os.path.isfile(self.WHITELIST_FILE):
                if force:
                    self.whitelist_names = set()
                    self._whitelist_mtime = 0.0
                return

            st = os.stat(self.WHITELIST_FILE)
            mtime = float(st.st_mtime)
            if (not force) and (mtime == self._whitelist_mtime):
                return

            names: Set[str] = set()
            with open(self.WHITELIST_FILE, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    s = s.replace("\\", "/").strip().strip("/")
                    if s:
                        names.add(s)

            self.whitelist_names = names
            self._whitelist_mtime = mtime

            if names:
                print(f"[WHITELIST] Loaded {len(names)} names from whitelist.txt")
            else:
                print("[WHITELIST] whitelist.txt present but empty -> allowing ALL liveries")
        except Exception as e:
            print(f"[WHITELIST] Failed to load whitelist.txt ({e}) -> allowing ALL liveries")
            self.whitelist_names = set()
            self._whitelist_mtime = 0.0

    def _is_whitelisted_path(self, rel_path: str) -> bool:
        if not self.whitelist_names:
            return True
        if not isinstance(rel_path, str) or not rel_path:
            return False
        rp = rel_path.replace("\\", "/").lstrip("/")
        first = rp.split("/", 1)[0].strip()
        if not first:
            return False
        return first in self.whitelist_names

    # -----------------------------
    # Validation helpers
    # -----------------------------
    def _is_allowed_path(self, rel_path: str) -> bool:
        if not isinstance(rel_path, str) or not rel_path:
            return False
        rp = rel_path.replace("\\", "/")
        if rp.startswith("/") or rp.startswith("\\"):
            return False
        if ".." in rp.split("/"):
            return False
        _base, ext = os.path.splitext(rp)
        if ext.lower() not in self.ALLOWED_EXTS:
            return False
        if not self._is_whitelisted_path(rp):
            return False
        return True

    def _is_valid_sha256(self, sha256: str) -> bool:
        if not isinstance(sha256, str) or not sha256:
            return False
        return bool(self._sha256_re.match(sha256))

    def _sanitize_manifest(self, manifest: Any) -> List[Dict[str, Any]]:
        """
        Accepts items with:
          - rel_path (str)
          - sha256 (str)
          - size (int)
          - mtime (float|int)  <-- UNIX seconds (last modified)
        """
        if not isinstance(manifest, list):
            return []

        out: List[Dict[str, Any]] = []
        for it in manifest:
            if not isinstance(it, dict):
                continue

            rel = it.get("rel_path")
            sh = it.get("sha256")
            if not isinstance(rel, str) or not isinstance(sh, str):
                continue

            rel_norm = rel.replace("\\", "/").strip()
            sh_norm = sh.strip()

            if not self._is_allowed_path(rel_norm):
                continue
            if not self._is_valid_sha256(sh_norm):
                continue

            try:
                size = int(it.get("size", 0))
            except Exception:
                continue
            if size < 0:
                continue

            mtime_val = it.get("mtime", 0)
            try:
                mtime_f = float(mtime_val)
            except Exception:
                mtime_f = 0.0
            if mtime_f < 0:
                mtime_f = 0.0

            out.append({"rel_path": rel_norm, "sha256": sh_norm, "size": size, "mtime": mtime_f})

        out.sort(key=lambda x: (x["rel_path"], x["sha256"], x["size"], x.get("mtime", 0.0)))
        return out

    # -----------------------------
    # Utility: safe send
    # -----------------------------
    async def _safe_send(self, client_id: str, msg: Dict[str, Any]) -> None:
        c = self.clients.get(client_id)
        if not c:
            return
        try:
            await send_msg(c.writer, msg)
        except Exception:
            pass

    # -----------------------------
    # Union manifest computation
    # -----------------------------
    @staticmethod
    def _hash_manifest_items(items: List[Dict[str, Any]]) -> str:
        # Union hash should be based on content identity, not mtime.
        norm = [
            {
                "rel_path": str(i.get("rel_path", "")),
                "sha256": str(i.get("sha256", "")),
                "size": int(i.get("size", 0)),
            }
            for i in items
            if i.get("rel_path") and i.get("sha256")
        ]
        norm.sort(key=lambda x: (x["rel_path"], x["sha256"], x["size"]))
        blob = json.dumps(norm, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    @staticmethod
    def _split_ext(rel_path: str) -> Tuple[str, str]:
        base, ext = os.path.splitext(rel_path)
        return base, ext

    def _conflict_name(self, rel_path: str, sha256: str, attempt: int = 0) -> str:
        base, ext = self._split_ext(rel_path)
        short = (sha256 or "")[:8] if sha256 else "unknown"
        if attempt <= 0:
            return f"{base}{self.CONFLICT_SUFFIX}{short}{ext}"
        return f"{base}{self.CONFLICT_SUFFIX}{short}_{attempt}{ext}"

    def _build_union(self) -> Tuple[List[Dict[str, Any]], str, Dict[str, List[Tuple[str, Dict[str, Any]]]]]:
        """
        CONFLICT RULE:
          - For same rel_path with different sha256:
              pick canonical sha by newest mtime (max mtime)
              if tie: fall back to earliest connected_at
          - Non-canonical hashes get stored as __CONFLICT__ files.
        """
        by_rel: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}

        for cid, c in self.clients.items():
            if c.manifest_refresh_pending:
                continue
            if not c.manifest_items:
                continue
            for it in c.manifest_items:
                rel = it.get("rel_path")
                sh = it.get("sha256")
                if not isinstance(rel, str) or not rel:
                    continue
                if not isinstance(sh, str) or not sh:
                    continue

                reln = rel.replace("\\", "/")
                if not self._is_allowed_path(reln):
                    continue
                if not self._is_valid_sha256(sh):
                    continue

                try:
                    size = int(it.get("size", 0))
                except Exception:
                    continue
                if size < 0:
                    continue

                try:
                    mtime_f = float(it.get("mtime", 0.0))
                except Exception:
                    mtime_f = 0.0
                if mtime_f < 0:
                    mtime_f = 0.0

                by_rel.setdefault(reln, []).append(
                    (cid, {"rel_path": reln, "sha256": sh, "size": size, "mtime": mtime_f})
                )

        union_map: Dict[str, Dict[str, Any]] = {}
        providers: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}

        for rel in sorted(by_rel.keys()):
            entries = by_rel[rel]

            sha_groups: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
            for cid, it in entries:
                sha_groups.setdefault(it["sha256"], []).append((cid, it))

            def connected_key(pair: Tuple[str, Dict[str, Any]]) -> Tuple[float, str]:
                cid, _it = pair
                c = self.clients.get(cid)
                return ((c.connected_at if c else float("inf")), cid)

            sha_fresh: List[Tuple[str, float, Tuple[str, Dict[str, Any]]]] = []
            for sh, group in sha_groups.items():
                group_mtime = 0.0
                for _cid, it in group:
                    try:
                        mt = float(it.get("mtime", 0.0))
                    except Exception:
                        mt = 0.0
                    if mt > group_mtime:
                        group_mtime = mt

                rep = min(group, key=connected_key)
                sha_fresh.append((sh, group_mtime, rep))

            if not sha_fresh:
                continue

            sha_fresh.sort(key=lambda x: (-x[1], connected_key(x[2])))
            canonical_sha, _canonical_mtime, canonical_provider = sha_fresh[0]

            _canon_cid, canon_item = canonical_provider
            canon_entry = {
                "rel_path": rel,
                "sha256": canon_item["sha256"],
                "size": int(canon_item["size"]),
                "mtime": float(canon_item.get("mtime", 0.0)),
            }

            if rel not in union_map:
                union_map[rel] = canon_entry

            for cid, _it in sha_groups[canonical_sha]:
                providers.setdefault(rel, []).append((cid, canon_entry))

            for sh, group in sorted(sha_groups.items(), key=lambda kv: kv[0]):
                if sh == canonical_sha:
                    continue

                attempt = 0
                conflict_rel = self._conflict_name(rel, sh, attempt=attempt)
                while conflict_rel in union_map:
                    attempt += 1
                    conflict_rel = self._conflict_name(rel, sh, attempt=attempt)

                rep_cid, rep_it = min(group, key=connected_key)
                conflict_entry = {
                    "rel_path": conflict_rel,
                    "sha256": sh,
                    "size": int(rep_it.get("size", 0)),
                    "mtime": float(rep_it.get("mtime", 0.0) or 0.0),
                }
                union_map[conflict_rel] = conflict_entry

                for cid, _it in group:
                    providers.setdefault(conflict_rel, []).append((cid, conflict_entry))

        union_items = list(union_map.values())
        union_items.sort(key=lambda x: x["rel_path"])
        union_hash = self._hash_manifest_items(union_items) if union_items else ""
        return union_items, union_hash, providers

    # -----------------------------
    # Diff
    # -----------------------------
    @staticmethod
    def _manifest_map(items: List[Dict[str, Any]]) -> Dict[str, str]:
        return {str(i.get("rel_path")): str(i.get("sha256")) for i in items if i.get("rel_path") and i.get("sha256")}

    def _client_missing(self, client: ClientConn, union_items: List[Dict[str, Any]]) -> List[str]:
        if not client.manifest_items:
            return [str(u["rel_path"]) for u in union_items]

        local_map = self._manifest_map(client.manifest_items)
        missing: List[str] = []
        for u in union_items:
            rel = str(u["rel_path"])
            sh = str(u["sha256"])
            if local_map.get(rel) != sh:
                missing.append(rel)
        return missing

    # -----------------------------
    # Source selection
    # -----------------------------
    def _pick_source_for_path(
        self, rel_path: str, providers: Dict[str, List[Tuple[str, Dict[str, Any]]]]
    ) -> Optional[str]:
        cand: List[ClientConn] = []
        for cid, _it in providers.get(rel_path, []):
            c = self.clients.get(cid)
            if not c or c.syncing or c.manifest_refresh_pending:
                continue
            cand.append(c)
        if not cand:
            return None
        cand.sort(key=lambda c: c.connected_at)
        return cand[0].client_id

    # -----------------------------
    # Manifest request control
    # -----------------------------
    async def _request_manifest_if_needed(self, c: ClientConn, force: bool = False) -> None:
        if not c.writer or c.syncing:
            return
        now = time.time()

        if c.manifest_request_inflight and not force:
            return
        if not force and (now - c.manifest_last_request_at) < self.MANIFEST_REQUEST_MIN_INTERVAL:
            return

        c.manifest_request_inflight = True
        c.manifest_last_request_at = now
        await self._safe_send(c.client_id, {"type": "REQUEST_MANIFEST", "sync_id": ""})

    async def _ensure_fresh_manifests(self) -> None:
        now = time.time()
        for c in list(self.clients.values()):
            if not c.writer:
                continue
            if c.syncing:
                continue

            stale = (c.manifest_items is None) or ((now - c.manifest_items_at) > self.MANIFEST_STALE_SECONDS)

            if c.manifest_refresh_pending:
                await self._request_manifest_if_needed(c, force=False)
                continue

            if stale:
                await self._request_manifest_if_needed(c, force=False)

    # -----------------------------
    # Scheduling union syncs
    # -----------------------------
    async def maybe_schedule_union_syncs(self) -> None:
        async with self._lock:
            union_items, union_hash, providers = self._build_union()
            if not union_items:
                return

            targets: List[Tuple[ClientConn, List[str]]] = []
            for c in self.clients.values():
                if c.syncing:
                    continue
                if c.manifest_refresh_pending:
                    continue
                missing = self._client_missing(c, union_items)
                if missing:
                    targets.append((c, missing))

            if not targets:
                return

            targets.sort(key=lambda t: (-len(t[1]), t[0].connected_at))

            for target_conn, missing in targets:
                if target_conn.syncing or target_conn.manifest_refresh_pending:
                    continue

                per_source: Dict[str, List[str]] = {}
                for rel in missing:
                    if not self._is_allowed_path(rel):
                        continue
                    src = self._pick_source_for_path(rel, providers)
                    if not src:
                        continue
                    if src == target_conn.client_id:
                        continue
                    per_source.setdefault(src, []).append(rel)

                source_order = sorted(per_source.items(), key=lambda kv: -len(kv[1]))

                for src_id, paths in source_order:
                    src_conn = self.clients.get(src_id)
                    if not src_conn or src_conn.syncing or src_conn.manifest_refresh_pending or target_conn.syncing:
                        continue

                    chunk = paths[: self.MAX_FILES_PER_SESSION]
                    if not chunk:
                        continue

                    src_conn.syncing = True
                    target_conn.syncing = True

                    sync_id = str(uuid.uuid4())
                    self.syncs[sync_id] = SyncSession(
                        sync_id=sync_id,
                        source_id=src_id,
                        target_id=target_conn.client_id,
                        paths=chunk,
                        union_hash=union_hash,
                    )

                    asyncio.create_task(self._safe_send(src_id, {
                        "type": "SYNC_OFFER",
                        "sync_id": sync_id,
                        "role": "SOURCE",
                        "current_hash": union_hash,
                        "source_client_id": src_id,
                        "target_client_id": target_conn.client_id,
                    }))
                    asyncio.create_task(self._safe_send(target_conn.client_id, {
                        "type": "SYNC_OFFER",
                        "sync_id": sync_id,
                        "role": "TARGET",
                        "current_hash": union_hash,
                        "source_client_id": src_id,
                        "target_client_id": target_conn.client_id,
                    }))

                    asyncio.create_task(self._safe_send(src_id, {
                        "type": "SEND_FILES",
                        "sync_id": sync_id,
                        "paths": chunk,
                    }))

                    print(f"[UNION] Scheduled {sync_id}: SOURCE={src_conn.client_name or src_conn.client_id} -> "
                          f"TARGET={target_conn.client_name or target_conn.client_id} "
                          f"files={len(chunk)} union={union_hash}")

                    return

    async def _end_sync(self, sync_id: str, reason: str = "") -> None:
        async with self._lock:
            sess = self.syncs.pop(sync_id, None)
            if not sess:
                return
            src = self.clients.get(sess.source_id)
            tgt = self.clients.get(sess.target_id)
            if src:
                src.syncing = False
            if tgt:
                tgt.syncing = False

        if reason:
            print(f"[SYNC] Ended {sync_id}: {reason}")

    # -----------------------------
    # Client handling
    # -----------------------------
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client_id = str(uuid.uuid4())[:8]
        conn = ClientConn(client_id=client_id, reader=reader, writer=writer)

        async with self._lock:
            self.clients[client_id] = conn

        peer = writer.get_extra_info("peername")
        print(f"[CONNECT] {client_id} from {peer}")

        try:
            await send_msg(writer, {"type": "WELCOME", "client_id": client_id})

            while True:
                msg = await read_msg(reader)
                mtype = msg.get("type")
                if not mtype:
                    continue

                if mtype == "HELLO":
                    conn.steam_id = str(msg.get("steam_id", ""))
                    conn.client_name = str(msg.get("client_name", ""))
                    conn.manifest_hash = str(msg.get("manifest_hash", ""))

                    union_items, union_hash, _providers = self._build_union()
                    current = union_hash if union_items else (conn.manifest_hash or "")
                    await send_msg(writer, {"type": "SERVER_STATE", "current_hash": current})

                    await self._request_manifest_if_needed(conn, force=True)
                    await self.maybe_schedule_union_syncs()

                elif mtype == "MANIFEST_SUMMARY":
                    conn.manifest_hash = str(msg.get("manifest_hash", conn.manifest_hash))
                    conn.manifest_count = int(msg.get("file_count", conn.manifest_count))
                    conn.manifest_bytes = int(msg.get("total_bytes", conn.manifest_bytes))

                elif mtype == "MANIFEST_FULL":
                    manifest = msg.get("manifest")
                    sanitized = self._sanitize_manifest(manifest)

                    conn.manifest_items = sanitized
                    conn.manifest_items_at = time.time()
                    conn.manifest_request_inflight = False
                    conn.manifest_refresh_pending = False

                    await self.maybe_schedule_union_syncs()

                elif mtype in ("FILE_BEGIN", "FILE_CHUNK", "FILE_END"):
                    sync_id = str(msg.get("sync_id", ""))
                    sess = self.syncs.get(sync_id)
                    if not sess:
                        continue
                    if client_id != sess.source_id:
                        continue

                    rel_path = msg.get("rel_path")
                    if isinstance(rel_path, str) and rel_path:
                        rel_norm = rel_path.replace("\\", "/")
                        if (rel_norm not in sess.paths) or (not self._is_allowed_path(rel_norm)):
                            continue

                    await self._safe_send(sess.target_id, msg)

                elif mtype == "SYNC_DONE":
                    sync_id = str(msg.get("sync_id", ""))
                    new_hash = str(msg.get("new_manifest_hash", ""))

                    sess = self.syncs.get(sync_id)
                    if not sess:
                        continue
                    if sess.target_id != client_id:
                        continue

                    conn.manifest_hash = new_hash

                    conn.manifest_refresh_pending = True
                    conn.manifest_request_inflight = False
                    conn.manifest_items_at = 0.0

                    await self._safe_send(sess.source_id, {
                        "type": "SYNC_DONE_ACK",
                        "sync_id": sync_id,
                        "new_manifest_hash": new_hash,
                    })

                    await self._end_sync(sync_id, reason="completed")
                    await self._request_manifest_if_needed(conn, force=True)
                    await self.maybe_schedule_union_syncs()

                elif mtype == "ERROR":
                    sync_id = str(msg.get("sync_id", ""))
                    err = str(msg.get("message", ""))
                    print(f"[ERROR] from {client_id}: {err}")
                    if sync_id:
                        await self._end_sync(sync_id, reason=f"error: {err}")
                        await self.maybe_schedule_union_syncs()

                elif mtype == "PING":
                    await send_msg(writer, {"type": "PONG"})

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception as e:
            print(f"[EXCEPTION] client {client_id}: {e}")
        finally:
            sync_ids_to_end: List[str] = []

            async with self._lock:
                for sid, sess in list(self.syncs.items()):
                    if sess.source_id == client_id or sess.target_id == client_id:
                        sync_ids_to_end.append(sid)
                self.clients.pop(client_id, None)

            for sid in sync_ids_to_end:
                await self._end_sync(sid, reason="peer disconnected")

            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

            print(f"[DISCONNECT] {client_id}")
            await self.maybe_schedule_union_syncs()

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                self._load_whitelist(force=False)
                await self._ensure_fresh_manifests()
                await self.maybe_schedule_union_syncs()
            except Exception:
                pass
            await asyncio.sleep(self.SCHEDULER_TICK_SECONDS)

    async def run(self) -> None:
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        addr = ", ".join(str(s.getsockname()) for s in server.sockets or [])
        print(f"[START] Listening on {addr}")

        asyncio.create_task(self._scheduler_loop())

        async with server:
            await server.serve_forever()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="ACC Liveries Sync Server (UNION MERGE, MTIME-WINS)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9239)
    args = ap.parse_args()

    asyncio.run(SyncServer(args.host, args.port).run())