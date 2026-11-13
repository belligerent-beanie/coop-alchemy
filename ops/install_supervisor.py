"""Make the supervisor (ops/supervise.py) start by itself when you log in: macOS and Windows, from the same command.

    python ops/install_supervisor.py install [supervise.py options …] [-- extra server options]
    python ops/install_supervisor.py remove [--data-dir DIR]
    python ops/install_supervisor.py start          start it now (after a `supervise.py stop`)
    python ops/install_supervisor.py show [...]      print what install would register, changing nothing

`install` takes the same options as `supervise.py run` (--data-dir, --port, --backup-at, …, and server options after `--`) and
registers exactly that command, then starts it:

  macOS    a LaunchAgent, ~/Library/LaunchAgents/com.coopalchemy.supervisor.plist: starts at login, and launchd starts the
           supervisor again if it ever crashes (not after a deliberate `supervise.py stop`).
  Windows  a scheduled task, CoopAlchemy-Supervisor: starts at logon, restarts on failure, no 72-hour time limit, no console
           window. Installing also removes the four tasks the old ops/install_tasks.ps1 registered (start, watchdog, backup,
           summary), since the supervisor does all of that now and two watchers would fight.

Both run only while you are logged in (like the old Windows tasks): after a reboot, the game comes back when you log in.
Linux servers run the game in Docker, where the supervisor is the container's entry point (see the Dockerfile).
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
LABEL = "com.coopalchemy.supervisor"
TASK = "CoopAlchemy-Supervisor"
OLD_TASKS = ("CoopAlchemy-Start", "CoopAlchemy-Watchdog", "CoopAlchemy-Backup", "CoopAlchemy-Summary")


def supervisor_command(options: list[str]) -> list[str]:
    python = sys.executable
    if os.name == "nt" and python.lower().endswith("python.exe"):
        quiet = python[:-len("python.exe")] + "pythonw.exe"   # no console window at logon
        if Path(quiet).exists():
            python = quiet
    return [python, str(ROOT / "ops" / "supervise.py"), "run", *options]


def data_dir_of(options: list[str]) -> Path:
    if "--data-dir" in options and options.index("--data-dir") + 1 < len(options):
        return Path(options[options.index("--data-dir") + 1]).resolve()
    return ROOT / "data"


# ---- macOS ----

def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def plist_for(command: list[str], data_dir: Path) -> bytes:
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": command,
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},   # again after a crash, not after a deliberate stop
        "ThrottleInterval": 30,
        "ProcessType": "Interactive",              # not slowed down as a background job
        "StandardOutPath": str(data_dir / "supervisor.out.log"),
        "StandardErrorPath": str(data_dir / "supervisor.out.log"),
    })


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def mac_install(command: list[str], data_dir: Path) -> int:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{LABEL}")   # an older version of it, if any
    path.write_bytes(plist_for(command, data_dir))
    done = launchctl("bootstrap", domain, str(path))
    if done.returncode != 0:
        print(f"launchctl could not load it: {done.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"Installed {path}\nIt runs now and at every login. Check it with: python ops/supervise.py status --data-dir {data_dir}")
    return 0


def mac_remove() -> int:
    launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    path = plist_path()
    if path.exists():
        path.unlink()
        print(f"Removed {path}; the supervisor and the server it ran are stopped.")
    else:
        print("It was not installed.")
    return 0


def mac_start() -> int:
    done = launchctl("kickstart", f"gui/{os.getuid()}/{LABEL}")
    print("Started." if done.returncode == 0 else f"Could not start it: {done.stderr.strip() or 'is it installed?'}")
    return done.returncode


# ---- Windows ----

def task_xml(command: list[str]) -> str:
    program, arguments = command[0], subprocess.list2cmdline(command[1:])
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Keeps the Co-op Alchemy server running (ops/supervise.py)</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Hidden>true</Hidden>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(program)}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(str(ROOT))}</WorkingDirectory></Exec>
  </Actions>
</Task>
"""


def schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True)


def win_install(command: list[str], data_dir: Path) -> int:
    data_dir.mkdir(parents=True, exist_ok=True)
    for old in OLD_TASKS:   # the old watchdog would restart the server behind the supervisor's back
        if schtasks("/Delete", "/TN", old, "/F").returncode == 0:
            print(f"Removed the old task {old}")
    xml = data_dir / "supervisor-task.xml"
    xml.write_text(task_xml(command), encoding="utf-16")
    made = schtasks("/Create", "/TN", TASK, "/XML", str(xml), "/F")
    if made.returncode != 0:
        print(f"Could not register the task: {made.stderr.strip() or made.stdout.strip()}", file=sys.stderr)
        return 1
    schtasks("/Run", "/TN", TASK)
    print(f"Registered the task {TASK}: it runs now and at every logon.\n"
          f"Check it with: python ops\\supervise.py status --data-dir {data_dir}")
    return 0


def win_remove(data: Path) -> int:
    subprocess.run([sys.executable, str(ROOT / "ops" / "supervise.py"), "stop", "--data-dir", str(data)], capture_output=True)
    gone = schtasks("/Delete", "/TN", TASK, "/F")
    print("Removed the task." if gone.returncode == 0 else "It was not installed.")
    return 0


def win_start() -> int:
    done = schtasks("/Run", "/TN", TASK)
    print("Started." if done.returncode == 0 else f"Could not start it: {done.stderr.strip()}")
    return done.returncode


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("install", "remove", "start", "show"):
        print(__doc__)
        return 2
    action, options = argv[0], argv[1:]
    command = supervisor_command(options)
    data_dir = data_dir_of(options)
    mac, windows = sys.platform == "darwin", os.name == "nt"
    if action == "show":
        print("Command:", subprocess.list2cmdline(command) if windows else " ".join(command))
        print(task_xml(command) if windows else plist_for(command, data_dir).decode())
        return 0
    if not (mac or windows):
        print("On Linux, run the game in Docker: the supervisor is the container's entry point.", file=sys.stderr)
        return 1
    if action == "install":
        return mac_install(command, data_dir) if mac else win_install(command, data_dir)
    if action == "remove":
        return mac_remove() if mac else win_remove(data_dir)
    return mac_start() if mac else win_start()


if __name__ == "__main__":
    sys.exit(main())
