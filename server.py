# server.py
import asyncio
import json
import struct
import time
import uuid
import hashlib
import os
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

    # Client-local manifest summary
    manifest_hash: str = ""
    manifest_count: int = 0
    manifest_bytes: int = 0

    # Client full manifest (list of {rel_path,size,sha256})
    manifest_items: Optional[List[Dict[str, Any]]] = None
    manifest_items_at: float = 0.0


@dataclass
class SyncSession:
    sync_id: str
    source_id: str
    target_id: str
    paths: List[str]  # paths requested from source to target
    union_hash: str
    created_at: float = field(default_factory=time.time)


class SyncServer:
    """
    UNION MERGE SERVER:

    - Collect full manifests from all clients
    - Compute global union manifest (with conflict renaming)
    - For each client, compute missing items vs union
    - Schedule sync sessions SOURCE -> TARGET for missing files
      (multi-source; each session only contains files a given source has)
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9239):
        self.host = host
        self.port = port

        self.clients: Dict[str, ClientConn] = {}
        self.syncs: Dict[str, SyncSession] = {}

        self._lock = asyncio.Lock()

        # Scheduling knobs
        self.MAX_FILES_PER_SESSION = 50           # keep sessions reasonably small
        self.MANIFEST_STALE_SECONDS = 15.0        # if manifest older than this, ask refresh
        self.SCHEDULER_TICK_SECONDS = 1.0         # periodic scheduler
        self.CONFLICT_SUFFIX = "__CONFLICT__"     # conflict marker

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
        # Deterministic hash over sorted (rel_path, sha256, size)
        norm = [
            {"rel_path": str(i.get("rel_path", "")),
             "sha256": str(i.get("sha256", "")),
             "size": int(i.get("size", 0))}
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

    def _add_conflict_name(self, rel_path: str, sha256: str) -> str:
        base, ext = self._split_ext(rel_path)
        short = (sha256 or "")[:8] if sha256 else "unknown"
        return f"{base}{self.CONFLICT_SUFFIX}{short}{ext}"

    def _build_union(self) -> Tuple[List[Dict[str, Any]], str, Dict[str, List[Tuple[str, Dict[str, Any]]]]]:
        """
        Returns:
          union_items: list of dict {rel_path,size,sha256}
          union_hash: sha256 over union_items
          providers: mapping rel_path -> list of (client_id, item)
                     (for conflict-renamed entries too)
        """
        # Collect all items from all clients with manifest_items
        all_entries: List[Tuple[str, Dict[str, Any]]] = []
        for cid, c in self.clients.items():
            if not c.manifest_items:
                continue
            for it in c.manifest_items:
                rel = it.get("rel_path")
                sh = it.get("sha256")
                if not isinstance(rel, str) or not rel:
                    continue
                if not isinstance(sh, str) or not sh:
                    continue
                size = int(it.get("size", 0))
                all_entries.append((cid, {"rel_path": rel.replace("\\", "/"), "sha256": sh, "size": size}))

        # Union by rel_path, but keep conflicts by renaming later ones
        union_map: Dict[str, Dict[str, Any]] = {}
        providers: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}

        # Stable iteration order for reproducibility
        all_entries.sort(key=lambda x: (x[1]["rel_path"], x[1]["sha256"], x[0]))

        for cid, it in all_entries:
            rel = it["rel_path"]
            sh = it["sha256"]

            if rel not in union_map:
                union_map[rel] = it
                providers.setdefault(rel, []).append((cid, it))
                continue

            # Same rel_path exists
            if union_map[rel]["sha256"] == sh:
                # exact duplicate content; still record provider
                providers.setdefault(rel, []).append((cid, it))
                continue

            # Conflict: same rel_path but different content
            conflict_rel = self._add_conflict_name(rel, sh)
            # Ensure no collision with existing union entry names
            while conflict_rel in union_map:
                conflict_rel = self._add_conflict_name(conflict_rel, sh)

            it2 = {"rel_path": conflict_rel, "sha256": sh, "size": it.get("size", 0)}
            union_map[conflict_rel] = it2
            providers.setdefault(conflict_rel, []).append((cid, it2))

        union_items = list(union_map.values())
        union_items.sort(key=lambda x: x["rel_path"])
        union_hash = self._hash_manifest_items(union_items)
        return union_items, union_hash, providers

    # -----------------------------
    # Diff: what a client is missing vs union
    # -----------------------------
    @staticmethod
    def _manifest_map(items: List[Dict[str, Any]]) -> Dict[str, str]:
        return {
            str(i.get("rel_path")): str(i.get("sha256"))
            for i in items
            if i.get("rel_path") and i.get("sha256")
        }

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
    # Picking a source for a given rel_path
    # -----------------------------
    def _pick_source_for_path(self, rel_path: str, providers: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> Optional[str]:
        # Prefer earliest connected provider that isn't currently syncing
        cand: List[ClientConn] = []
        for cid, _it in providers.get(rel_path, []):
            c = self.clients.get(cid)
            if not c or c.syncing:
                continue
            cand.append(c)
        if not cand:
            return None
        cand.sort(key=lambda c: c.connected_at)
        return cand[0].client_id

    # -----------------------------
    # Scheduling union syncs
    # -----------------------------
    async def _ensure_fresh_manifests(self) -> None:
        """
        Ask clients for MANIFEST_FULL if missing or stale.
        """
        now = time.time()
        for cid, c in list(self.clients.items()):
            if not c.writer:
                continue
            need = (c.manifest_items is None) or ((now - c.manifest_items_at) > self.MANIFEST_STALE_SECONDS)
            if need and not c.syncing:
                await self._safe_send(cid, {"type": "REQUEST_MANIFEST", "sync_id": ""})

    async def maybe_schedule_union_syncs(self) -> None:
        async with self._lock:
            # Need manifests first
            have_any = any(c.manifest_items for c in self.clients.values())
            if not have_any:
                return

            union_items, union_hash, providers = self._build_union()

            # Build missing lists for each target
            targets: List[Tuple[ClientConn, List[str]]] = []
            for c in self.clients.values():
                if c.syncing:
                    continue
                if not c.manifest_items:
                    # treat as missing all
                    missing = [u["rel_path"] for u in union_items]
                else:
                    missing = self._client_missing(c, union_items)
                if missing:
                    targets.append((c, missing))

            if not targets:
                return

            # Prioritize earliest connected targets (or could do most-missing first)
            targets.sort(key=lambda t: t[0].connected_at)

            # Create sessions until we can't
            for target_conn, missing in targets:
                if target_conn.syncing:
                    continue

                # Group missing by chosen source
                per_source: Dict[str, List[str]] = {}
                for rel in missing:
                    src = self._pick_source_for_path(rel, providers)
                    if not src:
                        continue
                    if src == target_conn.client_id:
                        continue
                    per_source.setdefault(src, []).append(rel)

                # For each source, schedule a session (limited)
                for src_id, paths in per_source.items():
                    src_conn = self.clients.get(src_id)
                    if not src_conn or src_conn.syncing or target_conn.syncing:
                        continue

                    # Chunk to MAX_FILES_PER_SESSION
                    chunk = paths[: self.MAX_FILES_PER_SESSION]
                    if not chunk:
                        continue

                    # Lock source + target
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

                    # Notify both of session
                    # (role names kept for client compatibility)
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

                    # Ask source to send these files
                    asyncio.create_task(self._safe_send(src_id, {
                        "type": "SEND_FILES",
                        "sync_id": sync_id,
                        "paths": chunk,
                    }))

                    print(f"[UNION] Scheduled {sync_id}: SOURCE={src_conn.client_name or src_conn.client_id} -> "
                          f"TARGET={target_conn.client_name or target_conn.client_id} "
                          f"files={len(chunk)} union={union_hash}")

                    # Schedule one session at a time (avoid starving others)
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

                    # Tell them current union hash (if available)
                    union_items, union_hash, _providers = self._build_union()
                    current = union_hash if union_items else (conn.manifest_hash or "")
                    await send_msg(writer, {"type": "SERVER_STATE", "current_hash": current})

                    # Ask for full manifest immediately (union needs it)
                    await send_msg(writer, {"type": "REQUEST_MANIFEST", "sync_id": ""})

                elif mtype == "MANIFEST_SUMMARY":
                    conn.manifest_hash = str(msg.get("manifest_hash", conn.manifest_hash))
                    conn.manifest_count = int(msg.get("file_count", conn.manifest_count))
                    conn.manifest_bytes = int(msg.get("total_bytes", conn.manifest_bytes))

                elif mtype == "MANIFEST_FULL":
                    manifest = msg.get("manifest")
                    if not isinstance(manifest, list):
                        continue

                    # Store client's manifest
                    conn.manifest_items = manifest
                    conn.manifest_items_at = time.time()

                    # Update summary hash if present
                    # (client computes its own hash; we accept it)
                    sid = str(msg.get("sync_id", ""))  # may be "" in union mode

                    # If this MANIFEST_FULL belongs to a specific sync request, optionally forward to target
                    if sid and sid in self.syncs:
                        sess = self.syncs.get(sid)
                        if sess and sess.source_id == client_id:
                            await self._safe_send(sess.target_id, {
                                "type": "MANIFEST_FULL",
                                "sync_id": sid,
                                "manifest": manifest,
                            })

                    # After receiving manifest, attempt scheduling
                    await self.maybe_schedule_union_syncs()

                elif mtype in ("FILE_BEGIN", "FILE_CHUNK", "FILE_END"):
                    sync_id = str(msg.get("sync_id", ""))
                    sess = self.syncs.get(sync_id)
                    if not sess:
                        continue
                    # Relay only from SOURCE -> TARGET
                    if client_id != sess.source_id:
                        continue
                    await self._safe_send(sess.target_id, msg)

                elif mtype == "SYNC_DONE":
                    sync_id = str(msg.get("sync_id", ""))
                    new_hash = str(msg.get("new_manifest_hash", ""))

                    sess = self.syncs.get(sync_id)
                    if not sess:
                        continue

                    # Only target can finish session
                    if sess.target_id != client_id:
                        continue

                    # Update target summary hash; full manifest will be refreshed shortly
                    conn.manifest_hash = new_hash

                    # Ack source
                    await self._safe_send(sess.source_id, {
                        "type": "SYNC_DONE_ACK",
                        "sync_id": sync_id,
                        "new_manifest_hash": new_hash,
                    })

                    await self._end_sync(sync_id, reason="completed")

                    # Ask target for fresh manifest so union stays accurate
                    await self._safe_send(client_id, {"type": "REQUEST_MANIFEST", "sync_id": ""})

                    # Try schedule more
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
            # Cleanup
            async with self._lock:
                # end any sync involving this client
                for sid, sess in list(self.syncs.items()):
                    if sess.source_id == client_id or sess.target_id == client_id:
                        await self._end_sync(sid, reason="peer disconnected")
                self.clients.pop(client_id, None)

            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

            print(f"[DISCONNECT] {client_id}")

            # Attempt scheduling after disconnect
            await self.maybe_schedule_union_syncs()

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self._ensure_fresh_manifests()
                await self.maybe_schedule_union_syncs()
            except Exception:
                pass
            await asyncio.sleep(self.SCHEDULER_TICK_SECONDS)

    async def run(self) -> None:
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        addr = ", ".join(str(s.getsockname()) for s in server.sockets or [])
        print(f"[START] Listening on {addr}")

        # Start background scheduler loop
        asyncio.create_task(self._scheduler_loop())

        async with server:
            await server.serve_forever()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="ACC Liveries Sync Server (UNION MERGE)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9239)
    args = ap.parse_args()

    asyncio.run(SyncServer(args.host, args.port).run())