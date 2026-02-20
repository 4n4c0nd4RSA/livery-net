# ACC Liveries Sync (Union Merge)

A high-performance peer-assisted livery synchronization system for
Assetto Corsa Competizione (ACC) that keeps custom liveries synchronized across multiple clients.

Designed for leagues like Abomination Racing League (ARL) to ensure every driver automatically receives missing liveries.

---

## Features

* Automatic livery synchronization between clients
* Central union-merge server
* Chunked streaming (256 KB) for large files
* SHA-256 integrity verification
* Conflict detection and automatic renaming
* Incremental sync (only missing files transfer)
* Safe overwrite with automatic backup
* Async high-performance networking
* Multi-source distribution (not single-point bottleneck)

---

## Architecture

```
Clients (ACC PCs)
        │
        │ manifests + files
        ▼
   Union Sync Server
        │
        │ schedules sessions
        ▼
Clients receive only missing liveries
```

### Flow

1. Client connects
2. Client sends manifest
3. Server builds global union
4. Server detects missing files per client
5. Server schedules SOURCE → TARGET sync
6. Files stream in chunks
7. Target verifies hash and installs
8. Server updates union state

---

## Project Structure

```
.
├── server.py   # Union merge sync server
├── client.py   # ACC livery sync client
└── README.md
```

---

## Requirements

* Python 3.10+
* Windows (client path assumes ACC Windows install)
* Assetto Corsa Competizione installed

No external Python dependencies required (stdlib only).

---

## Server Setup

### Run server

```bash
python server.py --host 0.0.0.0 --port 9239
```

Server defaults:

* Host: `0.0.0.0`
* Port: `9239`

---

### Server Responsibilities

The server:

* collects client manifests
* computes global union
* resolves filename conflicts
* schedules sync sessions
* relays file streams
* prevents sync collisions

---

## Client Setup

### Default ACC livery path

The client automatically scans:

```
Documents/Assetto Corsa Competizione/Customs/Liveries
```

From code: 

---

### Run client

```bash
python client.py --host SERVER_IP --port 9239 --name MyRig
```

Example:

```bash
python client.py --host 192.168.1.50 --name ARL-Driver01
```

---

## Integrity and Safety

### Hash Verification

Every file transfer is validated using:

* SHA-256 checksum
* size verification
* chunked transfer validation

If mismatch occurs:

* file is rejected
* temp file deleted
* error reported to server

---

### Automatic Backup

Before overwriting an existing livery, the client creates:

```
Liveries/.backup/<timestamp>/...
```

So no liveries are ever lost.

---

## Conflict Handling

If two clients provide different files with the same name:

Server automatically renames using:

```
<name>__CONFLICT__<hash>.ext
```

This guarantees:

* no data loss
* deterministic union
* full league coverage

---

## Sync Strategy

The system uses union merge logic:

* Every unique livery across all clients becomes part of the global set
* Each client downloads only what it is missing
* Multiple sources can serve files
* Sessions are capped (`MAX_FILES_PER_SESSION = 50`) for stability 

---

## Smart Scheduling

Server continuously:

* refreshes stale manifests
* detects missing files
* picks optimal source
* avoids busy clients
* prevents sync storms

Background scheduler runs every 1 second. 

---

## Network Protocol

Transport:

* TCP
* length-prefixed JSON frames

Key message types:

* `HELLO`
* `MANIFEST_FULL`
* `SEND_FILES`
* `FILE_BEGIN`
* `FILE_CHUNK`
* `FILE_END`
* `SYNC_DONE`

---

## Configuration Knobs (Server)

Inside `SyncServer`:

```python
MAX_FILES_PER_SESSION = 50
MANIFEST_STALE_SECONDS = 15.0
SCHEDULER_TICK_SECONDS = 1.0
CONFLICT_SUFFIX = "__CONFLICT__"
```

Tune these for large leagues.

---

## Testing Locally

### Terminal 1

```bash
python server.py
```

### Terminal 2+

```bash
python client.py --name ClientA
python client.py --name ClientB
```

Drop test files into one client’s liveries folder — they should propagate automatically.