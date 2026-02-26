# ACC Livery-Net

A high-performance peer-assisted livery synchronization system for
Assetto Corsa Competizione (ACC) that keeps custom liveries synchronized across multiple clients.

Designed for leagues like Abomination Racing League (ARL) to ensure every driver automatically receives missing liveries.

---

## Table of Contents

1. [Overview](#overview)
2. [Requirements](#requirements)
3. [Installation & Running](#installation--running)
4. [How It Works](#how-it-works)
   - [Connection & Handshake](#connection--handshake)
   - [Manifests](#manifests)
   - [Union Manifest & Conflict Resolution](#union-manifest--conflict-resolution)
   - [File Transfer Protocol](#file-transfer-protocol)
   - [mtime-Based Overwrite Logic](#mtime-based-overwrite-logic)
   - [Backup System](#backup-system)
5. [Architecture](#architecture)
6. [Message Protocol Reference](#message-protocol-reference)
7. [Configuration](#configuration)
   - [Server Configuration](#server-configuration)
   - [Client Configuration](#client-configuration)
   - [Whitelist](#whitelist)
8. [File & Directory Layout](#file--directory-layout)
9. [Security Considerations](#security-considerations)

---

## Overview

When multiple sim racers share a server, each driver typically needs identical livery files (`.png`, `.dds`, `.json`) to see everyone else's custom skins. Distributing these files manually is tedious. ACC Liveries Sync automates the process:

- **One machine runs `server.py`** — it acts as a relay and sync coordinator.
- **Every machine that needs liveries runs `client.py`** — it connects to the server, exchanges a file manifest, and either pushes or receives livery files as directed by the server.

After an initial convergence pass, every connected client ends up with the **union** of all liveries held by any peer. Subsequent connections are incremental — only missing or changed files are transferred.

---

## Requirements

- Python 3.10+
- No third-party dependencies — only standard library modules (`asyncio`, `hashlib`, `struct`, `json`, `base64`, `shutil`, `uuid`, etc.)
- **Server**: any OS (Linux recommended for headless hosting)
- **Client**: Windows (Steam ID detection is Windows-specific; the rest is cross-platform)

---

## Installation & Running

### Server

```bash
# Clone or copy server.py to your server machine
python server.py --host 0.0.0.0 --port 9239
```

The server will print its listening address and log every connection, manifest exchange, and sync session.

### Client

```bash
# Run on each PC that needs to sync liveries
python client.py --host 192.168.1.100 --port 9239 --name MyPC
```

`--name` defaults to the machine's hostname. It is used only for logging on the server side.

Both scripts can be wrapped in a `.bat` file or a `systemd` service for automatic launch.

---


## How It Works

### Connection & Handshake

1. The client opens a TCP connection to the server.
2. The server immediately sends a `WELCOME` message containing a short UUID `client_id`.
3. The client reads its Steam ID from `C:\Program Files (x86)\Steam\config\loginusers.vdf` (Windows only, falls back to `"UNKNOWN"`), builds its local manifest, then sends:
   - `HELLO` — announces itself with `steam_id`, `client_name`, and current `manifest_hash`.
   - `MANIFEST_SUMMARY` — file count and total bytes.
   - `MANIFEST_FULL` — the complete list of every livery file with its relative path, size, SHA-256 hash, and last-modified timestamp.
4. The server responds with `SERVER_STATE` containing the current **union hash** of all connected clients' liveries.

### Manifests

A **manifest** is the canonical description of a client's livery folder. Each entry contains:

| Field | Type | Description |
|---|---|---|
| `rel_path` | `str` | Forward-slash-separated path relative to `LIVERIES_ROOT`, e.g. `MyTeam/skin.png` |
| `sha256` | `str` | Lowercase hex SHA-256 of the file content |
| `size` | `int` | File size in bytes |
| `mtime` | `float` | UNIX timestamp of the file's last modification time |

The **manifest hash** is the SHA-256 of a deterministically serialised JSON array of `{rel_path, size, sha256}` entries (mtime is excluded from the hash so that copying a file without changing content does not trigger a re-sync).

The server requests a fresh manifest from each client every **15 seconds** (configurable via `MANIFEST_STALE_SECONDS`) and after every completed sync. A rate-limit of 3 seconds between requests (`MANIFEST_REQUEST_MIN_INTERVAL`) prevents flooding.

### Union Manifest & Conflict Resolution

Every scheduler tick (default 1 second), the server builds a **union manifest** — the set of all files that should exist on every client:

1. All manifest items from all connected clients are collected and grouped by `rel_path`.
2. For each path, if multiple clients hold different hashes (i.e. the same filename but different file content), the server runs conflict resolution:
   - The version with the **highest `mtime`** across all holders is elected **canonical**.
   - If two hashes share the same mtime, the tie is broken by the client's `connected_at` time (earliest connection wins).
3. Non-canonical versions are renamed to `<base>__CONFLICT__<sha8><ext>` in the union manifest. These conflict variants are propagated to all peers just like normal files, so no data is lost.
4. A **union hash** (SHA-256 of the sorted canonical entries) is computed. As long as every client's local manifest matches the union hash, no sync is scheduled.

### File Transfer Protocol

When the server detects a client is missing one or more union files, it orchestrates a sync session:

1. The server picks the best **source** client for each missing file — preferring the earliest-connected, non-busy peer that holds the file.
2. The server creates a `SyncSession` and sends a `SYNC_OFFER` to both the source (`role: SOURCE`) and target (`role: TARGET`) clients.
3. The server sends `SEND_FILES` to the source listing the paths to stream.
4. The source opens each file and streams it in **256 KB base64-encoded chunks** via `FILE_BEGIN` → `FILE_CHUNK` × N → `FILE_END` messages.
5. The server routes each message verbatim to the target (validating that the source is authorised and each path is in the session's path list).
6. Once the target has received and verified `FILE_END`, it sends `SYNC_DONE` back to the server, which sends `SYNC_DONE_ACK` to the source and ends the session.
7. A 1-second debounce allows multiple files in one session to finish before the client recomputes its manifest and sends the updated `MANIFEST_FULL`.

Sessions are capped at **50 files** (`MAX_FILES_PER_SESSION`). Larger diffs are handled across multiple sequential sessions.

### mtime-Based Overwrite Logic

When a target client receives a complete file, it compares timestamps before writing:

```
incoming_mtime  vs  local_mtime
```

- If the **incoming file is newer** (or the local file doesn't exist), the local copy is replaced atomically using `os.replace()` and the mtime is preserved using `os.utime()`.
- If the **local file is newer** by more than 1 second (`MTIME_TOLERANCE_SECONDS`), the incoming version is saved as a `__CONFLICT__` file instead. The local canonical file is left untouched.

This tolerance avoids false conflicts from filesystem 1-second mtime resolution and `shutil.copy2` behaviour.

### Backup System

Before overwriting any existing file, the client backs it up to:

```
<LIVERIES_ROOT>/.backup/<YYYYMMDD_HHMMSS>/<original relative path>
```

The `.backup` and `.tmp` directories are excluded from manifest scanning.

---

## Architecture

```
  Client A  ←──────────────┐
                            │  TCP (JSON framing)
  Client B  ←──── server.py (relay + scheduler)
                            │
  Client C  ←──────────────┘
```

The server is **stateless regarding file content** — it never stores livery data itself. It only:

1. Collects and merges file manifests from all clients.
2. Determines which client has which files and which clients are missing files.
3. Routes file data verbatim from a source client to a target client through the relay.

---

## Message Protocol Reference

All messages are framed with a 4-byte big-endian unsigned integer length prefix followed by a UTF-8 JSON payload.

### Server → Client

| Message type | Key fields | Description |
|---|---|---|
| `WELCOME` | `client_id` | Sent immediately on connect |
| `SERVER_STATE` | `current_hash` | Union manifest hash at handshake time |
| `REQUEST_MANIFEST` | `sync_id` | Server requests a fresh manifest upload |
| `SYNC_OFFER` | `sync_id`, `role`, `current_hash`, `source_client_id`, `target_client_id` | Announces a pending sync; role is `SOURCE` or `TARGET` |
| `SEND_FILES` | `sync_id`, `paths` | Instructs source to stream the listed files |
| `FILE_BEGIN` | `sync_id`, `rel_path`, `size`, `sha256`, `mtime` | Relayed from source to target |
| `FILE_CHUNK` | `sync_id`, `rel_path`, `offset`, `data_b64` | Relayed chunk (base64) |
| `FILE_END` | `sync_id`, `rel_path` | Signals end of file transfer |
| `SYNC_DONE_ACK` | `sync_id`, `new_manifest_hash` | Confirms sync completion to source |
| `PONG` | — | Keepalive reply |

### Client → Server

| Message type | Key fields | Description |
|---|---|---|
| `HELLO` | `steam_id`, `client_name`, `manifest_hash` | Identity + initial hash |
| `MANIFEST_SUMMARY` | `manifest_hash`, `file_count`, `total_bytes` | Lightweight summary |
| `MANIFEST_FULL` | `sync_id`, `manifest` | Full item array |
| `FILE_BEGIN` | `sync_id`, `rel_path`, `size`, `sha256`, `mtime` | Start of file upload |
| `FILE_CHUNK` | `sync_id`, `rel_path`, `offset`, `data_b64` | Data chunk |
| `FILE_END` | `sync_id`, `rel_path` | End of file upload |
| `SYNC_DONE` | `sync_id`, `new_manifest_hash` | Target signals completion |
| `ERROR` | `sync_id`, `message` | Reports an error (e.g. hash mismatch) |
| `PING` | — | Keepalive probe |

---

## Configuration

### Server Configuration

| Constant | Default | Description |
|---|---|---|
| `host` | `0.0.0.0` | Bind address |
| `port` | `9239` | TCP port |
| `MAX_FILES_PER_SESSION` | `50` | Files streamed per sync session |
| `MANIFEST_STALE_SECONDS` | `15.0` | How old a manifest can be before re-requesting |
| `SCHEDULER_TICK_SECONDS` | `1.0` | How often the sync scheduler runs |
| `MANIFEST_REQUEST_MIN_INTERVAL` | `3.0` | Minimum seconds between manifest requests per client |
| `ALLOWED_EXTS` | `.png .dds .json` | File extensions permitted in manifests and transfers |
| `WHITELIST_FILE` | `./whitelist.txt` | Path to optional livery name whitelist |

### Client Configuration

| Constant | Default | Description |
|---|---|---|
| `DEFAULT_SERVER_HOST` | `127.0.0.1` | Server address |
| `DEFAULT_SERVER_PORT` | `9239` | Server TCP port |
| `LIVERIES_ROOT` | `~/Documents/Assetto Corsa Competizione/Customs/Liveries` | Where liveries are read and written |
| `STEAM_LOGINUSERS` | `C:\Program Files (x86)\Steam\config\loginusers.vdf` | Path to Steam's login file for Steam ID detection |
| `CHUNK_SIZE` | `262144` (256 KB) | Transfer chunk size |
| `ALLOWED_EXTS` | `.png .dds .json` | Extensions scanned for manifest |
| `MTIME_TOLERANCE_SECONDS` | `1.0` | Grace window for mtime comparison |
| `CONFLICT_SUFFIX` | `__CONFLICT__` | Suffix used for conflict copies |

All values can be overridden at the top of each file. CLI arguments also exist for `--host`, `--port`, and `--name`.

### Whitelist

Create a file called `whitelist.txt` in the same directory as `server.py`. Add one livery folder name per line (the top-level directory under `LIVERIES_ROOT`). Lines starting with `#` are comments.

```
# Only sync these liveries
McLaren_720S_GT3
Ferrari_488_GT3
```

- If the file is **absent or empty**, all livery folders are allowed.
- If the file contains entries, only those folders are synced; all others are silently ignored.
- The whitelist is **hot-reloaded** every scheduler tick without restarting the server.

---

## File & Directory Layout

```
# Server side
server.py
whitelist.txt           ← optional; hot-reloaded

# Client side (per machine)
client.py

~/Documents/Assetto Corsa Competizione/Customs/Liveries/
├── SomeLivery/
│   ├── skin.json
│   ├── decals.png
│   └── decals.dds
├── AnotherLivery/
│   └── ...
├── .backup/            ← pre-overwrite backups (timestamped)
│   └── 20240501_143022/
│       └── SomeLivery/skin.json
└── .tmp/               ← in-progress downloads (cleaned up automatically)
    └── rx_1714567890123_skin.json.tmp
```

---

## Security Considerations

- **No authentication** — any TCP client can connect. Deploy on a trusted LAN or behind a VPN/firewall. Do not expose port 9239 to the public internet.
- **Path traversal protection** — the server rejects any `rel_path` containing `..` components or absolute path prefixes. Only files with `.png`, `.dds`, or `.json` extensions are accepted.
- **Hash verification** — every received file's SHA-256 is verified before it is placed on disk. A mismatch triggers an `ERROR` message and the temporary file is deleted.
- **Session scoping** — the server only relays `FILE_*` messages from the declared source client for the declared sync session's file list. A client cannot push arbitrary files by forging a `FILE_BEGIN` message.
- **Whitelist** — the server-side whitelist limits which livery directories can be synced, providing an additional layer of control over what gets distributed.
- **Steam ID** is read locally and sent to the server for logging only; it is never validated or used for access control.
