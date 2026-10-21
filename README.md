# diskwatch

Find out what keeps waking up the hard drives in a NAS, and what reads so much data from them.

diskwatch runs as a Docker container on the NAS and shows its results on a small web page. It records two kinds of reports:

- **Wakeups:** when the HDDs become active after a quiet period, every process that touched the HDD volume in the 60 s before and 30 s after.
- **Heavy I/O bursts:** when the HDDs together read or write at 20 MB/s or more for at least 2 minutes, the processes that did the most I/O and the files and folders they used.

It was written for a UGREEN NAS running UGOS Pro, with the HDDs on `/volume1` and an NVMe SSD on `/volume2`. It should work on any Linux NAS with Docker if you adjust the paths.

## How it works

- The kernel's fanotify feature reports every file and folder opened on the HDD volume, with the process and Docker container that opened it.
- `/proc/diskstats` is read once a second to see when the HDDs are active and how much they read and write. Reading it never spins the drives up.
- `/proc/<pid>/io` gives each process's read and write totals during a burst.
- Reports are kept on the web page and appended to `data/wakes.jsonl` and `data/bursts.jsonl`.

### Is it safe?

- It never reads or writes your files. It only reads system information from `/proc` and `/sys`. The only thing it writes is its own report files, in the data folder on the SSD.
- fanotify runs in notify-only mode, so diskwatch can't block or delay any file access.
- It doesn't wake the drives, and it doesn't keep `/volume1` busy, so it won't stop the pool from unmounting or the NAS from shutting down.
- If it crashes, monitoring stops and nothing else is affected.

Things to be aware of:

- The container runs **privileged** and shares the host's process list. It needs that to watch files and see other processes. It also mounts the Docker socket read-only, but only to look up container names.
- The web page has **no password**. Anyone on your network can see file and folder names on the HDD volume. Stop it once you've found what you were looking for.
- With `TRACE_READS=1`, every read on the volume becomes an event. During a large copy or a 4K stream, that can use a noticeable share of one CPU core.

## Setup (UGOS Pro, no SSH needed)

1. **Create the folders.** In UGOS **File Manager**, open the **docker** shared folder on `/volume2`. Create a folder named `diskwatch`, and inside it a folder named `data`.
2. **Upload the script.** Upload `diskwatch.py` into `docker/diskwatch` (not into `data`). You should end up with:
   ```
   docker/
     diskwatch/
       diskwatch.py
       data/
   ```
   The folder has to be on the SSD volume (`/volume2`), so the monitor never touches the HDDs.
3. **Create the project.** In the **Docker** app, go to **Project → Create**.
   - Name: `diskwatch`
   - Path: `docker/diskwatch`
   - Compose configuration: paste in the contents of `docker-compose.yml`, or upload the file.
4. **Start it.** Click **Deploy**, or **Create** and then **Start**. The first start downloads the `python:3.12-slim` image (about 50 MB).
5. **Check that it works.**
   - In the Docker app, open **Container → diskwatch → Log**. You should see a line like:
     ```
     diskwatch: watching /volume1, HDDs ['sda', 'sdb', 'sdc', 'sdd'], web on :8099
     ```
   - Open `http://<NAS-IP>:8099` in a browser, using the same IP as the UGOS web interface.
   - **HDDs** should list your hard drives.
   - **Watch mode** should say `whole filesystem` or `per-mount ...`.
   - **File events seen** should go up when you open a folder on `/volume1`.

   If the page shows a red error box, the message says what went wrong. "waiting: /volume1 is not mounted yet" right after a reboot is normal and clears by itself.

If your volumes are laid out differently, change the `/volume2/docker/diskwatch/...` paths and `WATCH_VOLUME` in `docker-compose.yml`.

## Reading the results

The page refreshes every 30 seconds. Let it run for a few days, at least until the drives have slept and woken again.

**Wakeups.** Times are relative to the moment the drives became active.
- Entries with negative times (for example `-12.0s`) touched the volume before the drives woke. The earliest one is usually the cause.
- Entries at about `+5` to `+15 s` are often the request that had to wait for the drives to spin up.
- If a wakeup shows no file activity, the cause was probably a metadata-only access (a folder listing that was no longer cached), a process with files already open, or RAID or filesystem housekeeping.

**Bursts.** Processes are ranked by how much they read and wrote during the burst.
- The **/volume1 activity** column shows whether a process actually touched the HDD volume, and the busiest folders it used.
- Rows marked "none seen" did their I/O elsewhere, usually on the SSD. Linux counts each process's reads across all drives.
- A process that started and exited during the burst may be missing from the byte counts, but still appears in the file activity.
- A long burst is split into one report per hour.

**Recognising the culprit:**
- A Docker container (Immich, Jellyfin, ...) shows up as `container: <name>`, with paths as they look inside that container.
- A computer on the network using a shared folder shows up as `smbd`, with the file path.
- The NAS's own apps show up by their service name, for example `com.ugreen...`.
- The antivirus and search indexer often appear right after another process writes a file. They're reacting to that write, not causing it.

Raw reports are in `docker/diskwatch/data/wakes.jsonl` and `bursts.jsonl`. They're also available as JSON from `http://<NAS-IP>:8099/api/`.

## Configuration

These are set as environment variables in `docker-compose.yml`. Restart the project after changing them.

| Variable | Default | Meaning |
|---|---|---|
| `WATCH_VOLUME` | `/volume1` | The HDD volume to watch |
| `QUIET_SECONDS` | `300` | HDD silence that counts as asleep before a wakeup |
| `WINDOW_BEFORE` / `WINDOW_AFTER` | `60` / `30` | Seconds of file activity to include before and after a wakeup |
| `TRACE_READS` | `0` (set to `1` in the compose file) | `1` also records reads and writes on files that were already open |
| `BURST_MB_S` | `20` | Combined HDD read+write rate that counts as heavy I/O |
| `BURST_AVG_SECONDS` | `60` | Window the rate is averaged over |
| `BURST_MIN_SECONDS` | `120` | Shortest burst that gets reported |
| `BURST_CHUNK_SECONDS` | `3600` | Long bursts are split into reports of this length |
| `PORT` | `8099` | Web page port. If you change it, change the port mapping too. |
| `DATA_DIR` | `/data` | Where reports are saved inside the container |

## Stopping

In the Docker app, go to **Project → diskwatch** and click **Stop**, or delete the project. The data folder keeps the reports until you delete it.
