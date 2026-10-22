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
import subprocess
import sys
import tempfile
import time
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
        "== Host ==" in console and "== Resources ==" in console,
        "Host and Resources blocks printed",
    )
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
        # Load average is emulated on Windows; see what a fresh process gets.
        la = run(
            [
                sys.executable,
                "-c",
                "import psutil,time;a=psutil.getloadavg();time.sleep(6);"
                "print(a, psutil.getloadavg())",
            ]
        )
        rep.info(f"getloadavg first call / after 6 s in a fresh process: {la.stdout.strip()}")
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
    busy = run([sys.executable, "-m", "sysmon", "--once", "--output-dir", str(out)]).stdout
    for p in procs:
        p.join()
    cpu_line = next((ln for ln in busy.splitlines() if "CPU" in ln and "%" in ln), "")
    try:
        cpu = float(cpu_line.split(":")[1].split("%")[0])
    except (IndexError, ValueError):
        cpu = -1.0
    rep.check("CPU % rises under full load (> 60%)", cpu > 60, f"{cpu}% with {n} busy procs")


PHASES = {"phase1": [phase1]}


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
