# diskwatch

Find out what keeps waking up the hard drives in a NAS, and what reads so much data from them.

diskwatch runs as a Docker container on the NAS and shows its results on a small web page. It records two kinds of reports:

- **Wakeups:** when the HDDs become active after a quiet period, every process that touched the HDD volume in the 60 s before and 30 s after.
- **Heavy I/O bursts:** when the HDDs together read or write at 20 MB/s or more for at least 2 minutes, the processes that did the most I/O and the files and folders they used.

It was written for a UGREEN NAS running UGOS Pro, with the HDDs on `/volume1` and an NVMe SSD on `/volume2`. It should work on any Linux NAS with Docker if you adjust the paths.

## How it works

- The kernel's fanotify feature reports file activity on the HDD volume and the process responsible. Docker processes are shown with a container ID prefix by default.
- `/proc/diskstats` is read once a second to see when rotational disks are active and how much they read and write. These counter reads do not access disk contents.
- `/proc/<pid>/io` gives each process's read and write totals during a burst.
- Reports are kept on the web page and appended to `data/wakes.jsonl` and `data/bursts.jsonl`.

### Safety and limitations

The code does not intentionally read file contents, change NAS settings, or write to the watched volume. It reads kernel and process information, receives file-event descriptors, and writes reports under `/data`. The supplied container has a read-only root filesystem; **make sure `/data` is on an SSD or another always-on filesystem** before starting it. Creating the watch may touch volume metadata during setup, so do not promise that installing it will never wake a sleeping pool.

- The fanotify watch uses notification events, not permission events. It cannot approve or deny a file operation, but processing events consumes CPU and kernel resources. With `TRACE_READS=1`, a busy copy or stream can produce many events and some may be dropped; start with the Compose default of `0`.
- The container is **privileged** and shares the host PID namespace. Docker says a privileged container can take control of its host. Run only code you trust, keep the NAS and Docker updated, and stop the stack after collecting enough evidence. The read-only root filesystem and `no-new-privileges` limit accidental damage by this script but do not contain a compromised privileged process.
- The Docker socket is **not mounted** by default. Mounting it read-only does not make Docker API access read-only: a process can still send write requests to the daemon. Without it, container IDs appear instead of friendly names.
- The web page and JSON API require a **password** (`DISKWATCH_PASSWORD`). After 5 wrong attempts, a client is locked out for 15 minutes. Sessions last 12 hours and end when the container restarts. The Compose file publishes port 8099 only on the NAS LAN address. Reports include paths and process command lines, which can contain sensitive information.
- The page is served over **plain HTTP**. The password and session cookie are not encrypted, so anyone who can capture traffic on your network could read them. Use a password you don't use anywhere else, only open the page from trusted networks, and **never forward the port to the internet**. For HTTPS, put it behind a reverse proxy that you already trust.
- Report files grow without automatic rotation. Check free space on the report volume, especially if you leave the stack running for days. If monitoring crashes, the script stops observing or retries; there is no mechanism in this code to alter stored files. A busy or failing NAS can still experience monitoring overhead.
- A wakeup means disk activity after `QUIET_SECONDS` of no observed I/O, **not proof that the drives had spun down**. A listed process is a lead, not proof of causation; metadata access, existing open files, filesystem tasks, and activity on other rotational `sd*` disks can be missed or misattributed.

## Setup

The image is built from the `Dockerfile` in this repo, which copies `diskwatch.py` into a `python:3.12-slim` image. The Compose file stores reports at `/volume2/docker/diskwatch/data` on the NAS. **Create that directory yourself and verify `/volume2` is the SSD mount before deploying.** Also check that the UGOS Docker app's image/container storage and the Project folder are on `/volume2`; otherwise Docker itself may access the HDD pool. The Compose bind mount is configured to fail if the directory is absent, preventing Docker from silently creating it on the wrong filesystem. If your NAS Compose implementation rejects `create_host_path`, update it rather than removing that safeguard.

If your volumes are laid out differently, change the `/volume2/docker/diskwatch/data` path and `WATCH_VOLUME` in `docker-compose.yml`.

The web page listens only on the address in `DISKWATCH_BIND_IP`. Set it to the NAS's LAN address. Deploying fails with an error if it isn't set. If the NAS gets its address from DHCP, **reserve that address for the NAS in your router**. If the NAS's address changes, the container fails to start, because Docker can't listen on an address the NAS no longer has.

Choose a password of at least 12 characters for `DISKWATCH_PASSWORD`. Don't put it in `docker-compose.yml`, which is in Git. Set it in Portainer or in a `.env` file as described below. If it isn't set, diskwatch generates a random password at each start and prints it in the container log.

### With Portainer (recommended)

Portainer builds and runs the stack straight from this repository, so nothing needs to be uploaded to the NAS.

1. In Portainer, go to **Stacks → Add stack**.
2. **Name:** `diskwatch`
3. **Build method:** **Repository**.
   - Repository URL: `https://github.com/capturo/diskwatch`
   - Repository reference: `refs/heads/main`
   - Compose path: `docker-compose.yml`
   - If the repository is private, turn on **Authentication** and enter your GitHub username and a personal access token with read access to the repo.
4. Under **Environment variables**, add:
   - `DISKWATCH_PASSWORD`: your password
   - `DISKWATCH_BIND_IP`: the NAS LAN address
5. Click **Deploy the stack**. The first deploy downloads the Python base image and builds the image. Repository deployment uses the version pushed to GitHub; local edits here are not included until you push them.

To update after new commits, open the stack, click **Pull and redeploy**, and turn on the option to re-pull the image. If the page still shows the old version, delete the `diskwatch:local` image under **Images** and redeploy.

### With the UGOS Docker app

1. In UGOS **File Manager**, open the **docker** shared folder on `/volume2` and create `diskwatch/data`.
2. Upload `diskwatch.py`, `Dockerfile` and `docker-compose.yml` into it. Also create a file named `.env` there, containing:
   ```
   DISKWATCH_PASSWORD=<your password>
   DISKWATCH_BIND_IP=<NAS LAN address>
   ```
   If the UGOS Docker app ignores `.env`, deploying fails with "Set DISKWATCH_BIND_IP to the NAS LAN IP". In that case, use Portainer, or replace `${DISKWATCH_BIND_IP:?...}` in your uploaded copy of `docker-compose.yml` with the address.
3. In the **Docker** app, go to **Project → Create**.
   - Name: `diskwatch`
   - Path: `docker/diskwatch`
   - Compose configuration: use the uploaded `docker-compose.yml`.
4. Click **Deploy**, or **Create** and then **Start**.

### Check that it works

- Open the container's log in Portainer or the Docker app. You should see a line like:
  ```
  diskwatch: watching /volume1, HDDs ['sda', 'sdb', 'sdc', 'sdd'], web on :8099
  ```
- On your laptop, open `http://<DISKWATCH_BIND_IP>:8099` and sign in with `DISKWATCH_PASSWORD`.
- **HDDs** should list your hard drives.
- **Watch mode** should say `whole filesystem` or `per-mount ...`.
- **File events seen** should go up when you open a folder on `/volume1`.

If the page shows a red error box, the message says what went wrong. "waiting: /volume1 is not mounted yet" right after a reboot is normal and clears by itself.

## Reading the results

The page refreshes every 30 seconds. Let it run for a few days, at least until the drives have slept and woken again.

**Wakeups.** Times are relative to the moment the drives became active.
- Entries with negative times (for example `-12.0s`) touched the volume before the drives became active. Investigate the earliest entries first, but they may not be the cause.
- Entries at about `+5` to `+15 s` are often the request that had to wait for the drives to spin up.
- If a wakeup shows no file activity, the cause was probably a metadata-only access (a folder listing that was no longer cached), a process with files already open, or RAID or filesystem housekeeping.

**Bursts.** Processes are ranked by how much they read and wrote during the burst.
- The **/volume1 activity** column shows whether a process actually touched the HDD volume, and the busiest folders it used.
- Rows marked "none seen" did their I/O elsewhere, usually on the SSD. Linux counts each process's reads across all drives.
- A process that started and exited during the burst may be missing from the byte counts, but still appears in the file activity.
- A long burst is split into one report per hour.

**Recognising the culprit:**
- A Docker container (Immich, Jellyfin, ...) shows up as `container: <ID prefix>`, with paths as they look inside that container. Match the ID in Portainer or the Docker app.
- A computer on the network using a shared folder shows up as `smbd`, with the file path.
- The NAS's own apps show up by their service name, for example `com.ugreen...`.
- The antivirus and search indexer often appear right after another process writes a file. They're reacting to that write, not causing it.

Raw reports are in `docker/diskwatch/data/wakes.jsonl` and `bursts.jsonl`. They're also available as JSON at `/api/` on the same address as the web page.

## Configuration

`DISKWATCH_PASSWORD` and `DISKWATCH_BIND_IP` are set as stack environment variables in Portainer, or in `.env`. The others are set in `docker-compose.yml`. Redeploy after changing them.

| Variable | Default | Meaning |
|---|---|---|
| `DISKWATCH_PASSWORD` | random, printed to the log | Password for the web page and API. Set it in Portainer or `.env`, not in the compose file. |
| `DISKWATCH_BIND_IP` | none, required | NAS LAN address the web page listens on. Used by the compose port mapping. |
| `SESSION_HOURS` | `12` | How long a sign-in lasts |
| `WATCH_VOLUME` | `/volume1` | The HDD volume to watch |
| `QUIET_SECONDS` | `300` | No observed HDD I/O required before an activity report |
| `WINDOW_BEFORE` / `WINDOW_AFTER` | `60` / `30` | Seconds of file activity to include before and after a wakeup |
| `TRACE_READS` | `0` | `1` also records reads and writes on files that were already open, at a higher overhead |
| `BURST_MB_S` | `20` | Combined HDD read+write rate that counts as heavy I/O |
| `BURST_AVG_SECONDS` | `60` | Window the rate is averaged over |
| `BURST_MIN_SECONDS` | `120` | Shortest burst that gets reported |
| `BURST_CHUNK_SECONDS` | `3600` | Long bursts are split into reports of this length |
| `PORT` | `8099` | Web page port. If you change it, change the port mapping too. |
| `DATA_DIR` | `/data` | Where reports are saved inside the container |

## Stopping

In Portainer, open **Stacks → diskwatch** and click **Stop this stack**, or delete it. In the UGOS Docker app, go to **Project → diskwatch** and click **Stop**, or delete the project. Either way, `/volume2/docker/diskwatch/data` keeps the reports until you delete it.

## License

Licensed under the [Apache License 2.0](LICENSE).
