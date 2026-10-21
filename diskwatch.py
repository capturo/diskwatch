#!/usr/bin/env python3
"""diskwatch - find out what wakes up the NAS hard drives and what reads from them.

Runs in a privileged container that shares the host PID namespace. It watches
file activity on the HDD volume with fanotify and polls /proc/diskstats once a
second. It records two kinds of reports:

  * wakeups: HDD activity after a quiet period, with every process that touched
    the volume shortly before and after;
  * bursts: sustained heavy HDD I/O, with the processes that did the most I/O
    and the files/folders they used on the volume.

Results are served on a small web page and appended to DATA_DIR/*.jsonl.
The monitor never reads from or writes to the watched volume itself: it only
reads procfs/sysfs and writes to DATA_DIR (which must live on the SSD).
"""
import ctypes
import http.client
import http.server
import html
import json
import os
import re
import socket
import struct
import threading
import time
from collections import Counter, deque

VOLUME = os.environ.get("WATCH_VOLUME", "/volume1")
QUIET_SECONDS = int(os.environ.get("QUIET_SECONDS", "300"))
WINDOW_BEFORE = int(os.environ.get("WINDOW_BEFORE", "60"))
WINDOW_AFTER = int(os.environ.get("WINDOW_AFTER", "30"))
BURST_MB_S = float(os.environ.get("BURST_MB_S", "20"))
BURST_AVG_SECONDS = int(os.environ.get("BURST_AVG_SECONDS", "60"))
BURST_MIN_SECONDS = int(os.environ.get("BURST_MIN_SECONDS", "120"))
BURST_CHUNK_SECONDS = int(os.environ.get("BURST_CHUNK_SECONDS", "3600"))
PORT = int(os.environ.get("PORT", "8099"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
TRACE_READS = os.environ.get("TRACE_READS", "0") == "1"
DOCKER_SOCK = "/var/run/docker.sock"
HOST_ROOT = "/proc/1/root"
RETRY_SECONDS = 30

FAN_CLOEXEC = 0x00000001
FAN_CLASS_NOTIF = 0x00000000
FAN_MARK_ADD = 0x00000001
FAN_MARK_MOUNT = 0x00000010
FAN_MARK_FILESYSTEM = 0x00000100
FAN_ACCESS = 0x00000001
FAN_MODIFY = 0x00000002
FAN_CLOSE_WRITE = 0x00000008
FAN_OPEN = 0x00000020
FAN_OPEN_EXEC = 0x00001000
FAN_Q_OVERFLOW = 0x00004000
FAN_ONDIR = 0x40000000
AT_FDCWD = -100
EVENT_FMT = "=IBBHQii"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

lock = threading.Lock()
events = deque(maxlen=50000)      # (ts, pid, comm, who, cmd, op, path)
wakes = deque(maxlen=300)         # finalized wake reports, oldest first
bursts = deque(maxlen=300)        # finalized burst reports, oldest first
burst_agg = None                  # per-process file activity while a burst is active
watch = {"fd": None, "source": None}
status = {
    "started": time.time(),
    "mode": "starting",
    "marks": 0,
    "events_seen": 0,
    "event_errors": 0,
    "overflows": 0,
    "hdds": [],
    "last_hdd_activity": None,
    "read_mb_s": 0.0,
    "write_mb_s": 0.0,
    "burst_since": None,
    "error": None,
}


def clean(s):
    """Make a string safe to encode as UTF-8 (paths can contain arbitrary bytes)."""
    return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


# ---------------------------------------------------------------- fanotify

try:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
    libc.fanotify_init.restype = ctypes.c_int
    libc.fanotify_mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64,
                                   ctypes.c_int, ctypes.c_char_p]
    libc.fanotify_mark.restype = ctypes.c_int
except (OSError, AttributeError):
    libc = None


class NotMounted(Exception):
    pass


def fan_mark(fd, flags, mask, path):
    if libc.fanotify_mark(fd, FAN_MARK_ADD | flags, mask, AT_FDCWD, path.encode()) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path)


def watch_mask():
    mask = FAN_OPEN | FAN_OPEN_EXEC | FAN_CLOSE_WRITE | FAN_ONDIR
    if TRACE_READS:
        mask |= FAN_ACCESS | FAN_MODIFY
    return mask


def parse_mountinfo(path):
    """Yield (mount_id, mount_point, source) for each line of a mountinfo file."""
    for line in read_text(path).splitlines():
        left, _, right = line.partition(" - ")
        fields = left.split()
        rfields = right.split()
        if len(fields) >= 5 and len(rfields) >= 2:
            yield fields[0], fields[4].replace("\\040", " "), rfields[1]


def volume_source():
    for _, mp, source in parse_mountinfo("/proc/1/mountinfo"):
        if mp == VOLUME:
            return source
    raise NotMounted(f"{VOLUME} is not mounted yet")


marked_mounts = set()


def mark_all_mounts(fd, source):
    """Fallback: mark every mount of the volume in every mount namespace
    (host, Docker containers, ...). Called again periodically for new ones."""
    seen_ns = set()
    added = 0
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            ns = os.readlink(f"/proc/{pid}/ns/mnt")
            if ns in seen_ns:
                continue
            seen_ns.add(ns)
            for mount_id, mp, src in parse_mountinfo(f"/proc/{pid}/mountinfo"):
                if src != source or (ns, mount_id) in marked_mounts:
                    continue
                try:
                    fan_mark(fd, FAN_MARK_MOUNT, watch_mask(), f"/proc/{pid}/root{mp}")
                    marked_mounts.add((ns, mount_id))
                    added += 1
                except OSError:
                    pass
        except OSError:
            continue
    return added


def setup_fanotify():
    if libc is None or not hasattr(libc, "fanotify_init"):
        raise OSError(0, "fanotify is not available on this system")
    source = volume_source()
    flags = os.O_RDONLY | getattr(os, "O_LARGEFILE", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = libc.fanotify_init(FAN_CLOEXEC | FAN_CLASS_NOTIF, flags)
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"fanotify_init failed: {os.strerror(err)} (is the container privileged?)")
    marked_mounts.clear()
    try:
        # One mark covers every mount of the filesystem, including Docker bind mounts.
        fan_mark(fd, FAN_MARK_FILESYSTEM, watch_mask(), HOST_ROOT + VOLUME)
        return fd, None, "whole filesystem", 1
    except OSError as e:
        marks = mark_all_mounts(fd, source)
        if not marks:
            os.close(fd)
            raise OSError(e.errno, f"could not watch {VOLUME}: {e.strerror}")
        return fd, source, f"per-mount (filesystem mark unavailable: {e.strerror})", marks


# ---------------------------------------------------------- process info

proc_cache = {}
container_names = {}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path):
        super().__init__("localhost", timeout=2)
        self.sock_path = sock_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(self.sock_path)
        self.sock = s


def container_name(cid):
    if cid in container_names:
        return container_names[cid]
    name = cid[:12]
    if os.path.exists(DOCKER_SOCK):
        try:
            conn = UnixHTTPConnection(DOCKER_SOCK)
            conn.request("GET", f"/containers/{cid}/json")
            resp = conn.getresponse()
            if resp.status == 200:
                name = json.loads(resp.read()).get("Name", name).lstrip("/")
            conn.close()
        except (OSError, ValueError, http.client.HTTPException):
            pass
    container_names[cid] = name
    return name


def describe_cgroup(cgroup):
    m = re.search(r"([0-9a-f]{64})", cgroup)
    if m:
        return "container: " + container_name(m.group(1))
    parts = [p for p in cgroup.split("/") if p]
    if not parts:
        return "host"
    for p in reversed(parts):
        if p.endswith(".service") or p.startswith("com.ugreen"):
            return p
    return "/".join(parts[-2:])


def proc_info(pid):
    now = time.time()
    cached = proc_cache.get(pid)
    if cached and now - cached[0] < 60:
        return cached[1]
    comm, who, cmd = "?", "?", ""
    try:
        comm = read_text(f"/proc/{pid}/comm").strip()
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()[:200]
        cg = read_text(f"/proc/{pid}/cgroup").strip().splitlines()
        who = describe_cgroup(cg[-1].split(":", 2)[-1] if cg else "")
    except OSError:
        pass
    info = (comm, who, cmd)
    proc_cache[pid] = (now, info)
    if len(proc_cache) > 5000:
        for k in [k for k, v in proc_cache.items() if now - v[0] > 60]:
            del proc_cache[k]
    return info


def io_snapshot():
    """Bytes each process has read/written from block devices so far."""
    snap = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            d = dict(line.split(": ", 1) for line in read_text(f"/proc/{pid}/io").splitlines())
            snap[int(pid)] = (int(d["read_bytes"]), int(d["write_bytes"]))
        except (OSError, ValueError, KeyError):
            pass
    return snap


def op_name(mask):
    if mask & FAN_CLOSE_WRITE:
        return "write"
    if mask & FAN_MODIFY:
        return "modify"
    if mask & FAN_OPEN_EXEC:
        return "exec"
    if mask & FAN_ONDIR:
        return "list dir"
    if mask & FAN_OPEN:
        return "open"
    if mask & FAN_ACCESS:
        return "read"
    return hex(mask)


def add_to_burst(agg, pid, comm, who, cmd, op, path):
    g = agg.get((who, comm))
    if g is None:
        if len(agg) >= 500:
            return
        g = agg[(who, comm)] = {"cmd": cmd, "count": 0, "ops": set(), "pids": set(),
                                "paths": [], "dirs": Counter()}
    g["count"] += 1
    g["ops"].add(op)
    if len(g["pids"]) < 20:
        g["pids"].add(pid)
    if len(g["paths"]) < 8 and path not in g["paths"]:
        g["paths"].append(path)
    folder = os.path.dirname(path)
    if folder in g["dirs"] or len(g["dirs"]) < 300:
        g["dirs"][folder] += 1


def handle_event(mask, efd, pid, my_pid):
    path = "?"
    if efd >= 0:
        try:
            path = clean(os.readlink(f"/proc/self/fd/{efd}"))
        except OSError:
            pass
        finally:
            os.close(efd)
    if pid == my_pid:
        return
    comm, who, cmd = proc_info(pid)
    op = op_name(mask)
    with lock:
        events.append((time.time(), pid, comm, who, cmd, op, path))
        status["events_seen"] += 1
        if burst_agg is not None:
            add_to_burst(burst_agg, pid, comm, who, cmd, op, path)


def fanotify_reader(fd):
    my_pid = os.getpid()
    while True:
        data = os.read(fd, 256 * 1024)
        if not data:
            raise EOFError("fanotify descriptor closed")
        off = 0
        while off + EVENT_SIZE <= len(data):
            ev_len, _, _, _, mask, efd, pid = struct.unpack_from(EVENT_FMT, data, off)
            off += ev_len if ev_len else EVENT_SIZE
            if mask & FAN_Q_OVERFLOW:
                status["overflows"] += 1
                continue
            try:
                handle_event(mask, efd, pid, my_pid)
            except Exception:  # one odd event must never stop the watcher
                status["event_errors"] += 1


def file_watch_loop():
    """Set up file watching (waiting for the volume to be mounted) and keep it running."""
    while True:
        try:
            fd, source, mode, marks = setup_fanotify()
        except NotMounted as e:
            status["mode"] = f"waiting: {e}"
            time.sleep(RETRY_SECONDS)
            continue
        except OSError as e:
            status["mode"] = "disk activity only"
            status["error"] = f"File watching unavailable, retrying every {RETRY_SECONDS}s: {e.strerror}"
            time.sleep(RETRY_SECONDS)
            continue
        watch["fd"], watch["source"] = fd, source
        status.update(mode=mode, marks=marks, error=None)
        try:
            fanotify_reader(fd)
        except Exception as e:
            status["error"] = f"File watcher stopped ({e}); restarting"
        watch["fd"], watch["source"] = None, None
        try:
            os.close(fd)
        except OSError:
            pass
        time.sleep(RETRY_SECONDS)


# ------------------------------------------------------------ disk polling

def find_hdds():
    hdds = []
    for dev in sorted(os.listdir("/sys/block")):
        if not dev.startswith("sd"):
            continue
        try:
            if read_text(f"/sys/block/{dev}/queue/rotational").strip() == "1":
                hdds.append(dev)
        except OSError:
            pass
    return hdds


def read_diskstats(hdds):
    stats = {}
    for line in read_text("/proc/diskstats").splitlines():
        p = line.split()
        if len(p) >= 12 and p[2] in hdds:
            # reads, sectors read, writes, sectors written, in flight
            stats[p[2]] = (int(p[3]), int(p[5]), int(p[7]), int(p[9]), int(p[11]))
    return stats


def save(name, record):
    try:
        with open(os.path.join(DATA_DIR, name), "a") as f:
            f.write(json.dumps(record) + "\n")
    except (OSError, ValueError) as e:
        status["error"] = f"could not save {name}: {e}"


def summarize(window, t0):
    groups = {}
    for ts, pid, comm, who, cmd, op, path in window:
        g = groups.setdefault((who, comm), {
            "who": who, "comm": comm, "cmd": cmd, "pids": set(), "count": 0,
            "first": ts - t0, "ops": set(), "paths": [],
        })
        g["pids"].add(pid)
        g["count"] += 1
        g["ops"].add(op)
        if len(g["paths"]) < 8 and path not in g["paths"]:
            g["paths"].append(path)
    out = []
    for g in sorted(groups.values(), key=lambda g: g["first"]):
        g["pids"] = sorted(g["pids"])
        g["ops"] = sorted(g["ops"])
        g["first"] = round(g["first"], 1)
        out.append(g)
    return out


def finalize_wake(wake):
    t0 = wake["time"]
    with lock:
        window = [e for e in events if t0 - WINDOW_BEFORE <= e[0] <= t0 + WINDOW_AFTER]
    wake["groups"] = summarize(window, t0)
    wake["events_in_window"] = len(window)
    with lock:
        wakes.append(wake)
    save("wakes.jsonl", wake)


def start_burst(now):
    global burst_agg
    with lock:
        burst_agg = {}
    status["burst_since"] = now
    return {"start": now, "read_kb": 0, "write_kb": 0, "peak_mb_s": 0.0, "io": io_snapshot()}


def finish_burst(b, now, continues):
    """Turn the collected burst data into a report: processes ranked by I/O,
    merged with what they touched on the volume."""
    global burst_agg
    with lock:
        agg, burst_agg = burst_agg or {}, None
    status["burst_since"] = None
    if now - b["start"] < BURST_MIN_SECONDS:
        return
    rows = {}
    for pid, (r, w) in io_snapshot().items():
        r0, w0 = b["io"].get(pid, (0, 0))
        dr, dw = max(r - r0, 0), max(w - w0, 0)
        if dr + dw < 1024 * 1024:
            continue
        comm, who, cmd = proc_info(pid)
        row = rows.setdefault((who, comm), {"who": who, "comm": comm, "cmd": cmd,
                                            "read_mb": 0, "write_mb": 0, "pids": []})
        row["read_mb"] += dr // (1024 * 1024)
        row["write_mb"] += dw // (1024 * 1024)
        if len(row["pids"]) < 20:
            row["pids"].append(pid)
    for (who, comm), g in agg.items():
        row = rows.setdefault((who, comm), {"who": who, "comm": comm, "cmd": g["cmd"],
                                            "read_mb": 0, "write_mb": 0, "pids": []})
        row.update(events=g["count"], ops=sorted(g["ops"]), paths=g["paths"],
                   dirs=[[d, n] for d, n in g["dirs"].most_common(5)])
        row["pids"] = sorted(set(row["pids"]) | g["pids"])[:20]
    ranked = sorted(rows.values(), key=lambda r: (r.get("events", 0) > 0, r["read_mb"] + r["write_mb"],
                                                   r.get("events", 0)), reverse=True)
    report = {"start": b["start"], "end": now, "read_mb": b["read_kb"] // 1024,
              "write_mb": b["write_kb"] // 1024, "peak_mb_s": round(b["peak_mb_s"], 1),
              "continues": continues, "rows": ranked[:15]}
    with lock:
        bursts.append(report)
    save("bursts.jsonl", report)


def disk_poller():
    hdds = find_hdds()
    status["hdds"] = hdds
    prev = read_diskstats(hdds)
    last_activity = time.time()
    status["last_hdd_activity"] = last_activity
    pending_wake = None
    burst = None
    rates = deque(maxlen=BURST_AVG_SECONDS)
    last_rescan = time.time()
    while True:
        time.sleep(1)
        now = time.time()
        cur = read_diskstats(hdds)
        read_kb = write_kb = 0
        for d in hdds:
            a, b = prev.get(d), cur.get(d)
            if a and b:
                read_kb += max(b[1] - a[1], 0) // 2
                write_kb += max(b[3] - a[3], 0) // 2
        rates.append((read_kb, write_kb))
        avg_read = sum(r for r, _ in rates) / len(rates) / 1024
        avg_write = sum(w for _, w in rates) / len(rates) / 1024
        status["read_mb_s"], status["write_mb_s"] = round(avg_read, 1), round(avg_write, 1)

        active = [d for d in hdds if cur.get(d) != prev.get(d) or cur.get(d, (0,) * 5)[4] > 0]
        if active:
            if pending_wake is None and now - last_activity >= QUIET_SECONDS:
                pending_wake = {"time": now, "idle_seconds": round(now - last_activity),
                                "disks": active, "read_kb": 0, "write_kb": 0}
            last_activity = now
            status["last_hdd_activity"] = now
        if pending_wake is not None:
            pending_wake["read_kb"] += read_kb
            pending_wake["write_kb"] += write_kb
            if now - pending_wake["time"] >= WINDOW_AFTER:
                finalize_wake(pending_wake)
                pending_wake = None

        heavy = len(rates) == BURST_AVG_SECONDS and avg_read + avg_write >= BURST_MB_S
        if burst is None and heavy:
            burst = start_burst(now)
        elif burst is not None:
            burst["read_kb"] += read_kb
            burst["write_kb"] += write_kb
            burst["peak_mb_s"] = max(burst["peak_mb_s"], avg_read + avg_write)
            if not heavy:
                finish_burst(burst, now, continues=False)
                burst = None
            elif now - burst["start"] >= BURST_CHUNK_SECONDS:
                finish_burst(burst, now, continues=True)
                burst = start_burst(now)

        prev = cur
        if watch["source"] and watch["fd"] is not None and now - last_rescan > 60:
            status["marks"] += mark_all_mounts(watch["fd"], watch["source"])
            last_rescan = now


def disk_poller_loop():
    while True:
        try:
            disk_poller()
        except Exception as e:
            status["error"] = f"Disk monitor restarted after error: {e}"
            time.sleep(RETRY_SECONDS)


# ------------------------------------------------------------------- web

def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def fmt_duration(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h {sec % 3600 // 60}m"


def fmt_mb(mb):
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{int(mb)} MB"


def render_page():
    e = lambda s: html.escape(str(s))
    now = time.time()
    with lock:
        wake_list = list(wakes)[::-1]
        burst_list = list(bursts)[::-1]
        recent = list(events)[-60:][::-1]
    quiet = now - status["last_hdd_activity"] if status["last_hdd_activity"] else 0
    burst_now = (f'since {fmt_time(status["burst_since"])}' if status["burst_since"] else "no")
    parts = [f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30"><title>Disk Watch</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#677084;--line:#e3e6ec;--accent:#2f6fde;--warn:#b5651d}}
@media (prefers-color-scheme:dark){{:root{{--bg:#14171c;--card:#1d2128;--fg:#e4e7ec;--muted:#959cab;--line:#2c313a;--accent:#6ea0ff;--warn:#e0a060}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:20px 16px}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 10px}}
.muted{{color:var(--muted)}} .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}}
.stats{{display:flex;flex-wrap:wrap;gap:8px 24px}} .stats b{{display:block;font-size:16px}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}}
th{{color:var(--muted);font-weight:500;border-top:none}} code{{font-size:12px;word-break:break-all}}
.t{{white-space:nowrap;font-variant-numeric:tabular-nums}} .neg{{color:var(--accent)}} .pos{{color:var(--warn)}}
.scroll{{overflow-x:auto}}
</style></head><body><main>
<h1>Disk Watch</h1><div class="muted">What wakes up and reads from the hard drives behind {e(VOLUME)}. Refreshes every 30 s.</div>
<div class="card stats">
<div><span class="muted">HDDs</span><b>{e(", ".join(status["hdds"]) or "none found")}</b></div>
<div><span class="muted">HDD I/O quiet for</span><b>{fmt_duration(quiet)}</b></div>
<div><span class="muted">HDD read / write (1-min avg)</span><b>{status["read_mb_s"]} / {status["write_mb_s"]} MB/s</b></div>
<div><span class="muted">Heavy I/O now</span><b>{e(burst_now)}</b></div>
<div><span class="muted">Wakeups / bursts recorded</span><b>{len(wake_list)} / {len(burst_list)}</b></div>
<div><span class="muted">File events seen</span><b>{status["events_seen"]}</b></div>
<div><span class="muted">Watch mode</span><b>{e(status["mode"])}{" + reads" if TRACE_READS else ""}</b></div>
<div><span class="muted">Running since</span><b>{fmt_time(status["started"])}</b></div>
</div>"""]
    if status["error"]:
        parts.append(f'<div class="card pos">{e(status["error"])}</div>')
    if status["overflows"]:
        parts.append(f'<div class="card pos">Event queue overflowed {status["overflows"]} times; some accesses were missed.</div>')

    parts.append(f"<h2>Heavy I/O bursts (≥ {BURST_MB_S:g} MB/s for ≥ {fmt_duration(BURST_MIN_SECONDS)})</h2>")
    parts.append('<div class="muted">Processes ranked by how much they read/wrote during the burst. '
                 'Rows without /volume1 activity did their I/O elsewhere (usually the SSD). '
                 'Processes that started and exited within the burst may be missing from the byte counts.</div>')
    if not burst_list:
        parts.append('<div class="card muted">No heavy I/O recorded yet.</div>')
    for b in burst_list:
        cont = " · continues in next report" if b.get("continues") else ""
        parts.append(f"""<div class="card"><b>{fmt_time(b["start"])} → {time.strftime("%H:%M:%S", time.localtime(b["end"]))}</b>
<span class="muted"> · {fmt_duration(b["end"] - b["start"])} · {fmt_mb(b["read_mb"])} read, {fmt_mb(b["write_mb"])} written
(all HDDs combined) · peak {b["peak_mb_s"]} MB/s{cont}</span>
<div class="scroll"><table><tr><th>Process</th><th>Where</th><th>Read / written</th><th>/volume1 activity</th><th>Busiest folders</th></tr>""")
        for r in b["rows"]:
            if r.get("events"):
                act = f'{r["events"]} · {e(", ".join(r["ops"]))}<br>' + "<br>".join(
                    f"<code>{e(p)}</code>" for p in r["paths"][:3])
            else:
                act = '<span class="muted">none seen</span>'
            dirs = "<br>".join(f"<code>{e(d)}</code> <span class='muted'>×{n}</span>" for d, n in r.get("dirs", []))
            parts.append(f"""<tr><td><b>{e(r["comm"])}</b> <span class="muted">pid {e(", ".join(map(str, r["pids"][:4])))}</span><br>
<code class="muted">{e(r["cmd"][:120])}</code></td><td>{e(r["who"])}</td>
<td class="t">{fmt_mb(r["read_mb"])} / {fmt_mb(r["write_mb"])}</td><td>{act}</td><td>{dirs}</td></tr>""")
        parts.append("</table></div></div>")

    parts.append(f"<h2>Wakeups (HDD activity after ≥ {fmt_duration(QUIET_SECONDS)} of silence)</h2>")
    parts.append('<div class="muted">Offsets are relative to when the drives became active. '
                 'The cause is usually the earliest entry; entries at +5…+15 s are often the request '
                 'that had to wait for the drives to spin up.</div>')
    if not wake_list:
        parts.append('<div class="card muted">Nothing yet. Leave it running until the drives sleep and wake again.</div>')
    for w in wake_list:
        parts.append(f"""<div class="card"><b>{fmt_time(w["time"])}</b>
<span class="muted"> · after {fmt_duration(w["idle_seconds"])} idle · {e(", ".join(w["disks"]))}
· {w["read_kb"] // 1024} MB read, {w["write_kb"] // 1024} MB written in first {WINDOW_AFTER}s</span>""")
        if not w["groups"]:
            parts.append('<p class="muted">No file activity on the volume in this window. Possible causes: '
                         'metadata-only access (stat/ls of folders no longer cached), a process that '
                         'already had files open, or RAID/filesystem housekeeping.</p></div>')
            continue
        parts.append('<div class="scroll"><table><tr><th>First</th><th>Process</th><th>Where</th>'
                     '<th>Events</th><th>Files / folders</th></tr>')
        for g in w["groups"]:
            cls = "neg" if g["first"] < 0 else "pos"
            paths = "<br>".join(f"<code>{e(p)}</code>" for p in g["paths"])
            parts.append(f"""<tr><td class="t {cls}">{g["first"]:+.1f}s</td>
<td><b>{e(g["comm"])}</b> <span class="muted">pid {e(", ".join(map(str, g["pids"][:4])))}</span><br>
<code class="muted">{e(g["cmd"][:120])}</code></td><td>{e(g["who"])}</td>
<td class="t">{g["count"]} · {e(", ".join(g["ops"]))}</td><td>{paths}</td></tr>""")
        parts.append("</table></div></div>")

    parts.append(f"<h2>Latest activity on {e(VOLUME)}</h2>")
    parts.append('<div class="card scroll"><table><tr><th>Time</th><th>Process</th><th>Where</th><th>Op</th><th>Path</th></tr>')
    for ts, pid, comm, who, cmd, op, path in recent:
        parts.append(f'<tr><td class="t">{time.strftime("%H:%M:%S", time.localtime(ts))}</td>'
                     f'<td>{e(comm)} <span class="muted">{pid}</span></td><td>{e(who)}</td>'
                     f'<td>{e(op)}</td><td><code>{e(path)}</code></td></tr>')
    parts.append("</table></div></main></body></html>")
    return "".join(parts)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path.startswith("/api/"):
                with lock:
                    data = {"wakes": list(wakes), "bursts": list(bursts)}
                body = json.dumps(data).encode()
                ctype = "application/json"
            else:
                body = render_page().encode("utf-8", "replace")
                ctype = "text/html; charset=utf-8"
            code = 200
        except Exception as e:
            body = f"Disk Watch page error: {e}".encode("utf-8", "replace")
            ctype, code = "text/plain; charset=utf-8", 500
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def load_history(name, target):
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    target.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    load_history("wakes.jsonl", wakes)
    load_history("bursts.jsonl", bursts)
    threading.Thread(target=file_watch_loop, daemon=True).start()
    threading.Thread(target=disk_poller_loop, daemon=True).start()
    print(f"diskwatch: watching {VOLUME}, HDDs {find_hdds()}, web on :{PORT}", flush=True)
    http.server.ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
