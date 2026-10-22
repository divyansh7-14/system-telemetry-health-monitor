"""End-to-end checks: run the real agent and compare it with independent OS tools.

Unit tests fake psutil; this script does not. It runs ``python -m sysmon`` on the
actual machine (a GitHub Actions Windows/Linux runner in CI) and cross-checks the
numbers against PowerShell/CIM on Windows or /proc and df on Linux.

Results are printed and also emitted as GitHub annotations so they can be read
from the run summary. Exit code 1 if any check fails.

Usage:  python scripts/e2e_check.py phase1 [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IS_WINDOWS = platform.system() == "Windows"
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"


class Report:
    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.lines: list[str] = []
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str) -> None:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}: {detail}"
        print(line, flush=True)
        self.lines.append(line)
        if not ok:
            self.failures.append(line)

    def info(self, text: str) -> None:
        print(f"[INFO] {text}", flush=True)
        self.lines.append(f"[INFO] {text}")

    def finish(self) -> int:
        osname = f"{platform.system()} {platform.release()} py{platform.python_version()}"
        summary = f"{len(self.lines) - len(self.failures)} ok / {len(self.failures)} failed"
        if IN_CI:
            # Annotations are readable from the API even when raw logs are not.
            body = "%0A".join(s.replace("%", "%25") for s in self.lines)
            kind = "error" if self.failures else "notice"
            print(f"::{kind} title=e2e {self.phase} on {osname} ({summary})::{body}")
        print(f"\n{self.phase}: {summary}")
        return 1 if self.failures else 0


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)


def powershell_json(command: str):
    res = run(["powershell", "-NoProfile", "-Command", f"{command} | ConvertTo-Json -Depth 3"])
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())
    return json.loads(res.stdout)


def within(a: float, b: float, rel: float = 0.0, absolute: float = 0.0) -> bool:
    return abs(a - b) <= max(absolute, rel * max(abs(a), abs(b)))


def _burn(seconds: float) -> None:
    end = time.monotonic() + seconds
    x = 0
    while time.monotonic() < end:
        x += 1


# --------------------------------------------------------------------- phase 1
def independent_host_facts() -> dict:
    """Facts gathered without psutil or platform, for cross-checking."""
    if IS_WINDOWS:
        os_ = powershell_json(
            "Get-CimInstance Win32_OperatingSystem | "
            "Select-Object Caption, Version, BuildNumber, LastBootUpTime, OSArchitecture"
        )
        cs = powershell_json(
            "Get-CimInstance Win32_ComputerSystem | "
            "Select-Object TotalPhysicalMemory, NumberOfLogicalProcessors, Name"
        )
        cores = powershell_json(
            "(Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum"
        )
        up = powershell_json(
            "[int]((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime)"
            ".TotalSeconds"
        )
        disk = powershell_json(
            "Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='C:'\" | "
            "Select-Object Size, FreeSpace"
        )
        return {
            "caption": os_["Caption"],
            "build": int(os_["BuildNumber"]),
            "total_memory": int(cs["TotalPhysicalMemory"]),
            "logical": int(cs["NumberOfLogicalProcessors"]),
            "physical": int(cores),
            "uptime": int(up),
            "disk_key": "C:\\",
            "disk_total": int(disk["Size"]),
            "disk_free": int(disk["FreeSpace"]),
        }
    meminfo = Path("/proc/meminfo").read_text().split("\n")
    mem_kb = next(int(line.split()[1]) for line in meminfo if line.startswith("MemTotal"))
    df = run(["df", "-B1", "--output=size,avail", "/"]).stdout.split("\n")[1].split()
    return {
        "caption": run(["uname", "-sr"]).stdout.strip(),
        "build": None,
        "total_memory": mem_kb * 1024,
        "logical": int(run(["nproc", "--all"]).stdout),
        "physical": None,
        "uptime": int(float(Path("/proc/uptime").read_text().split()[0])),
        "disk_key": "/",
        "disk_total": int(df[0]),
        "disk_free": int(df[1]),
    }


def phase1(out: Path, rep: Report) -> None:
    res = run([sys.executable, "-m", "sysmon", "--once", "--output-dir", str(out)])
    rep.check("agent --once exits 0", res.returncode == 0, f"exit={res.returncode}")
    if res.returncode != 0:
        rep.info(res.stderr[-500:])
        return
    console = res.stdout
    bad = run([sys.executable, "-m", "sysmon", "--interval", "5"])
    rep.check("bad flag -> exit 2 (usage)", bad.returncode == 2, f"exit={bad.returncode}")
    blocker = out / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    unw = run([sys.executable, "-m", "sysmon", "--once", "--output-dir", str(blocker / "sub")])
    rep.check(
        "unwritable output dir -> exit 3, no traceback",
        unw.returncode == 3 and "Traceback" not in unw.stderr,
        f"exit={unw.returncode} stderr={unw.stderr.strip()[:120]}",
    )
    rep.check(
        "console sections",
        all(x in console for x in ("== Host ==", "== Network ==", "[#1 ")),
        "Host, Network and cycle #1 printed",
    )
    once_lines = (out / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    rep.check("--once writes exactly one metrics line", len(once_lines) == 1, f"{len(once_lines)}")
    host = json.loads((out / "host_info.json").read_text(encoding="utf-8"))
    ref = independent_host_facts()
    rep.info(f"reference OS: {ref['caption']} build={ref['build']}")
    rep.info(f"agent OS: {host['os_name']} {host['os_release']} ({host['os_version']})")

    if IS_WINDOWS:
        caption = ref["caption"]
        ok = f"Windows {host['os_release']}" in caption
        rep.check("OS name matches CIM caption", ok, f"'{host['os_release']}' vs '{caption}'")
        rep.check(
            "OS build matches CIM",
            str(ref["build"]) in host["os_version"],
            f"{host['os_version']} vs {ref['build']}",
        )
    else:
        rep.check(
            "kernel release matches uname",
            host["os_release"] in ref["caption"],
            f"{host['os_release']} vs {ref['caption']}",
        )

    rep.check(
        "total RAM matches",
        within(host["total_memory_bytes"], ref["total_memory"], rel=0.01),
        f"{host['total_memory_bytes']} vs {ref['total_memory']}",
    )
    rep.check(
        "logical cores match",
        host["cpu_logical_cores"] == ref["logical"],
        f"{host['cpu_logical_cores']} vs {ref['logical']}",
    )
    if ref["physical"] is not None:
        rep.check(
            "physical cores match",
            host["cpu_physical_cores"] == ref["physical"],
            f"{host['cpu_physical_cores']} vs {ref['physical']}",
        )
    rep.check(
        "uptime matches (+/-120 s)",
        within(host["uptime_seconds"], ref["uptime"], absolute=120),
        f"{host['uptime_seconds']} vs {ref['uptime']}",
    )

    # Memory: used % vs the OS's own free-memory figure (both read within ~1 s).
    from sysmon.collectors.resources import collect_memory

    mem = collect_memory()
    if IS_WINDOWS:
        m = powershell_json(
            "Get-CimInstance Win32_OperatingSystem | "
            "Select-Object TotalVisibleMemorySize, FreePhysicalMemory"
        )
        ref_pct = 100 * (1 - int(m["FreePhysicalMemory"]) / int(m["TotalVisibleMemorySize"]))
    else:
        info = {
            ln.split(":")[0]: int(ln.split()[1])
            for ln in Path("/proc/meminfo").read_text().splitlines()
            if ln
        }
        ref_pct = 100 * (1 - info["MemAvailable"] / info["MemTotal"])
    rep.check(
        "memory used % matches OS (+/-5 pts)",
        within(mem.percent, ref_pct, absolute=5),
        f"{mem.percent} vs {ref_pct:.1f}",
    )

    if IS_WINDOWS:
        # Swap = page file on Windows.
        pf = powershell_json(
            "@(Get-CimInstance Win32_PageFileUsage | Select-Object AllocatedBaseSize, CurrentUsage)"
        )
        pf = pf if isinstance(pf, list) else [pf]
        pf_total = sum(int(p["AllocatedBaseSize"]) for p in pf) * 1024**2
        pf_used = sum(int(p["CurrentUsage"]) for p in pf) * 1024**2
        rep.info(
            f"swap: agent total={mem.swap_total_bytes} used={mem.swap_used_bytes} "
            f"({mem.swap_percent}%) | CIM pagefile total={pf_total} used={pf_used}"
        )
        # Every fixed drive Windows knows about must be reported.
        fixed = powershell_json(
            "@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | "
            "Select-Object -ExpandProperty DeviceID)"
        )
        fixed = fixed if isinstance(fixed, list) else [fixed]
        from sysmon.collectors.resources import collect_disks as _cd

        agent_drives = sorted(d.mountpoint.rstrip("\\") for d in _cd())
        rep.check(
            "all fixed drives reported",
            sorted(fixed) == agent_drives,
            f"agent={agent_drives} CIM={sorted(fixed)}",
        )

    # Disk: the agent's reading of the system drive vs CIM (Windows) or df (Linux).
    from sysmon.collectors.resources import collect_disks

    disks = {d.mountpoint: d for d in collect_disks()}
    d = disks.get(ref["disk_key"])
    rep.check("system drive reported", d is not None, f"mounts={list(disks)}")
    if d:
        rep.check(
            "disk total matches (+/-1%)",
            within(d.total_bytes, ref["disk_total"], rel=0.01),
            f"{d.total_bytes} vs {ref['disk_total']}",
        )
        rep.check(
            "disk free matches (+/-2%)",
            within(d.free_bytes, ref["disk_free"], rel=0.02),
            f"{d.free_bytes} vs {ref['disk_free']}",
        )
        rep.check(
            "used% + free% = 100",
            abs(d.used_percent + d.free_percent - 100) < 0.2,
            f"{d.used_percent} + {d.free_percent}",
        )

    # CPU: idle-ish reading, then with every core busy the agent must see it.
    n = os.cpu_count() or 1
    procs = [mp.Process(target=_burn, args=(6.0,)) for _ in range(n)]
    for p in procs:
        p.start()
    time.sleep(1.5)
    busy_dir = out / "busy"
    run([sys.executable, "-m", "sysmon", "--once", "--output-dir", str(busy_dir)])
    for p in procs:
        p.join()
    try:
        snap = json.loads((busy_dir / "metrics.jsonl").read_text(encoding="utf-8"))
        cpu = float(snap["cpu"]["percent"])
    except (OSError, ValueError, KeyError, TypeError):
        cpu = -1.0
    rep.check("CPU % rises under full load (> 60%)", cpu > 60, f"{cpu}% with {n} busy procs")


# --------------------------------------------------------------------- phase 2
HOG_BYTES = 1024**3  # 1 GiB
DOWNLOAD_URL = "https://files.pythonhosted.org/packages/source/n/numpy/numpy-2.1.0.tar.gz"


def _hog(size: int, ready) -> None:
    buf = bytearray(size)
    for i in range(0, size, 4096):  # touch every page so it is really resident
        buf[i] = 1
    ready.set()
    time.sleep(600)


def _read_snapshots(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def _wait_cycles(path: Path, n: int, timeout: float) -> list[dict]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        snaps = _read_snapshots(path)
        if len(snaps) >= n:
            return snaps
        time.sleep(0.2)
    return _read_snapshots(path)


def os_network_reference() -> dict:
    """Default-route interface, its IPv4 + MAC, and its byte counters, from OS tools."""
    if IS_WINDOWS:
        route = powershell_json(
            "Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric, "
            "InterfaceMetric | Select-Object -First 1 InterfaceAlias"
        )
        alias = route["InterfaceAlias"]
        ips = powershell_json(
            "@(Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress, InterfaceAlias)"
        )
        ad = powershell_json(f"Get-NetAdapter -Name '{alias}' | Select-Object MacAddress")
        st = powershell_json(
            f"Get-NetAdapterStatistics -Name '{alias}' | Select-Object ReceivedBytes, SentBytes"
        )
        return {
            "iface": alias,
            "ipv4": [x["IPAddress"] for x in ips if x["InterfaceAlias"] == alias],
            "all_ipv4": sorted(
                x["IPAddress"] for x in ips if not x["IPAddress"].startswith("127.")
            ),
            "mac": ad["MacAddress"].replace("-", ":").upper(),
            "rx": int(st["ReceivedBytes"]),
            "tx": int(st["SentBytes"]),
        }
    dev = json.loads(run(["ip", "-j", "route", "show", "default"]).stdout)[0]["dev"]
    addrs = json.loads(run(["ip", "-j", "addr"]).stdout)
    me = next(a for a in addrs if a["ifname"] == dev)
    stats = Path(f"/sys/class/net/{dev}/statistics")
    return {
        "iface": dev,
        "ipv4": [x["local"] for x in me["addr_info"] if x["family"] == "inet"],
        "all_ipv4": sorted(
            x["local"]
            for a in addrs
            for x in a["addr_info"]
            if x["family"] == "inet" and not x["local"].startswith("127.")
        ),
        "mac": me["address"].upper(),
        "rx": int((stats / "rx_bytes").read_text()),
        "tx": int((stats / "tx_bytes").read_text()),
    }


def phase2(out: Path, rep: Report) -> None:
    import urllib.request

    import psutil

    d = out / "phase2"
    d.mkdir(exist_ok=True)
    metrics = d / "metrics.jsonl"
    cores = psutil.cpu_count(logical=True) or 1

    busy = mp.Process(target=_burn, args=(120.0,), daemon=True)
    ready = mp.Event()
    hog = mp.Process(target=_hog, args=(HOG_BYTES, ready), daemon=True)
    busy.start()
    hog.start()
    ready.wait(60)

    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    stdout_f = open(d / "stdout.txt", "w", encoding="utf-8")  # noqa: SIM115
    stderr_f = open(d / "stderr.txt", "w", encoding="utf-8")  # noqa: SIM115
    interval = 10
    agent = subprocess.Popen(
        [sys.executable, "-m", "sysmon", "--interval", str(interval), "--top-n", "5"]
        + ["--output-dir", str(d)],
        stdout=stdout_f,
        stderr=stderr_f,
        creationflags=flags,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        _wait_cycles(metrics, 2, 40)
        downloaded, dl_secs = 0, 0.0
        try:
            t0 = time.monotonic()
            with urllib.request.urlopen(DOWNLOAD_URL, timeout=60) as resp:
                while chunk := resp.read(1 << 16):
                    downloaded += len(chunk)
            dl_secs = time.monotonic() - t0
        except OSError as exc:
            rep.info(f"download skipped ({exc}); rate check not possible")
        dl_done_at_cycle = len(_read_snapshots(metrics))
        _wait_cycles(metrics, max(4, dl_done_at_cycle + 2), 60)

        proc = psutil.Process(agent.pid)
        with proc.oneshot():
            ct = proc.cpu_times()
            age = time.time() - proc.create_time()
            rss = proc.memory_info().rss
        agent_cpu = 100 * (ct.user + ct.system) / max(age, 1e-6)

        net_ref = os_network_reference()
        from sysmon.collectors.network import collect_interfaces

        mine = {i.name: i for i in collect_interfaces()}
    finally:
        sig = signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGINT
        t_stop = time.monotonic()
        agent.send_signal(sig)
        try:
            code = agent.wait(timeout=15)
        except subprocess.TimeoutExpired:
            agent.kill()
            code = "killed"
        stop_secs = time.monotonic() - t_stop
        stdout_f.close()
        stderr_f.close()
        busy.terminate()
        hog.terminate()

    stdout = (d / "stdout.txt").read_text(encoding="utf-8", errors="replace")
    stderr = (d / "stderr.txt").read_text(encoding="utf-8", errors="replace")
    final = _read_snapshots(metrics)

    # --- loop, file format, shutdown
    rep.check("ran >= 4 cycles", len(final) >= 4, f"{len(final)} cycles")
    rep.check(
        "cycle numbers consecutive",
        [s["cycle"] for s in final] == list(range(1, len(final) + 1)),
        str([s["cycle"] for s in final]),
    )
    stamps = [datetime.fromisoformat(s["timestamp"]) for s in final]
    gaps = [round((b - a).total_seconds()) for a, b in zip(stamps, stamps[1:], strict=False)]
    rep.check(
        f"cycles {interval}s apart (+/-1 s)",
        bool(gaps) and all(abs(g - interval) <= 1 for g in gaps),
        str(gaps),
    )
    rep.check("no collector errors", all(not s["errors"] for s in final), "")
    rep.check(
        "each cycle < 2 s",
        all(s["duration_ms"] < 2000 for s in final),
        str([s["duration_ms"] for s in final]),
    )
    rep.check(
        f"{'Ctrl+Break' if IS_WINDOWS else 'Ctrl+C'} stops within 2 s, exit 0",
        code == 0 and stop_secs <= 2,
        f"exit={code} after {stop_secs:.2f}s",
    )
    rep.check(
        "clean shutdown output",
        "Traceback" not in stderr and "stopped after" in stdout,
        stderr.strip()[-200:] or "no stderr",
    )
    rep.check(
        "agent overhead: CPU < 5% of a core, RSS < 60 MB",
        agent_cpu < 5 and rss < 60 * 1024**2,
        f"cpu={agent_cpu:.2f}% rss={rss / 1024**2:.1f} MB",
    )

    # --- processes
    later = final[1:]
    expected = 100 / cores
    tops = [(s["top_processes"]["by_cpu"] or [{}])[0] for s in later]
    rep.check(
        "busy process is #1 by CPU every cycle",
        all(t.get("pid") == busy.pid for t in tops),
        f"busy pid={busy.pid}; #1 each cycle={[t.get('pid') for t in tops]}",
    )
    vals = [t.get("cpu_percent", -1) for t in tops if t.get("pid") == busy.pid]
    rep.check(
        f"busy process CPU ~ {expected:.0f}% of machine (+/-10 pts)",
        bool(vals) and all(abs(v - expected) <= 10 for v in vals),
        str(vals),
    )
    mem_top = final[-1]["top_processes"]["by_memory"]
    hog_row = next((p for p in mem_top if p["pid"] == hog.pid), None)
    rank = [p["pid"] for p in mem_top].index(hog.pid) + 1 if hog_row else None
    rep.check(
        "1 GiB memory hog in top-5 by memory, RSS ~1 GiB (+/-10%)",
        hog_row is not None and within(hog_row["memory_rss_bytes"], HOG_BYTES, rel=0.10),
        f"rank={rank} rss={hog_row and hog_row['memory_rss_bytes']}",
    )
    rep.check(
        "process names present",
        all(p["name"] for s in final for p in s["top_processes"]["by_cpu"]),
        "",
    )

    # --- network identity and counters
    net = final[-1]["network"]
    rep.info(f"agent primary={net['primary_interface']} {net['primary_ip']}; OS={net_ref}")
    rep.check(
        "primary interface = OS default route",
        net["primary_interface"] == net_ref["iface"],
        f"{net['primary_interface']} vs {net_ref['iface']}",
    )
    rep.check(
        "primary IPv4 matches OS",
        net["primary_ip"] in net_ref["ipv4"],
        f"{net['primary_ip']} vs {net_ref['ipv4']}",
    )
    agent_if = next((i for i in net["interfaces"] if i["name"] == net_ref["iface"]), {})
    rep.check(
        "primary MAC matches OS",
        agent_if.get("mac") == net_ref["mac"],
        f"{agent_if.get('mac')} vs {net_ref['mac']}",
    )
    agent_ipv4 = sorted(
        ip for i in net["interfaces"] for ip in i["ipv4"] if not ip.startswith("127.")
    )
    rep.check(
        "all IPv4 addresses match OS",
        agent_ipv4 == net_ref["all_ipv4"],
        f"{agent_ipv4} vs {net_ref['all_ipv4']}",
    )
    m = mine.get(net_ref["iface"])
    if m and m.bytes_recv is not None:
        rep.check(
            "interface bytes recv/sent match OS (+/-2% or 2 MB)",
            within(m.bytes_recv, net_ref["rx"], rel=0.02, absolute=2 * 1024**2)
            and within(m.bytes_sent, net_ref["tx"], rel=0.02, absolute=2 * 1024**2),
            f"rx {m.bytes_recv} vs {net_ref['rx']}, tx {m.bytes_sent} vs {net_ref['tx']}",
        )
    else:
        rep.check("interface byte counters present", False, f"{net_ref['iface']} missing")
    if downloaded:
        rates = [s["network"]["recv_bps"] or 0 for s in final]
        received = final[-1]["network"]["bytes_recv"] - final[0]["network"]["bytes_recv"]
        rep.check(
            "download visible in totals and rate",
            received >= downloaded and max(rates) >= downloaded / (2 * interval),
            f"downloaded {downloaded} B in {dl_secs:.1f}s; agent saw +{received} B, "
            f"peak rx {max(rates):.0f} B/s",
        )


PHASES = {"phase1": [phase1], "phase2": [phase2]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=list(PHASES) + ["all"])
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    out = Path(args.out or tempfile.mkdtemp(prefix="sysmon-e2e-"))
    out.mkdir(parents=True, exist_ok=True)
    names = list(PHASES) if args.phase == "all" else [args.phase]
    rep = Report(args.phase)
    for name in names:
        for fn in PHASES[name]:
            fn(out, rep)
    return rep.finish()


if __name__ == "__main__":
    sys.exit(main())
