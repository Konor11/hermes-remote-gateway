"""OS detection + dependency bootstrap for local-PC access.

`hermes remote pc setup` needs two SYSTEM packages that no Python installer can
provide:

  * an SSH **server** on the laptop (so the remote agent can SSH in), and
  * ``sshpass`` (only for the one-time key install — the server password is fed
    to ssh non-interactively; after that everything uses keys).

Hermes' plugin installer only resolves *Python* dependencies, so the system side
has to be done here: detect the distribution, map the package names, and install
what is missing (with sudo, interactively, so the password is never handled by
us).
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

#: package-manager → (install argv prefix, {role: package name})
_MANAGERS: dict[str, tuple[list[str], dict[str, str]]] = {
    "apt-get":  (["apt-get", "install", "-y"],
                 {"sshd": "openssh-server", "sshpass": "sshpass"}),
    "dnf":      (["dnf", "install", "-y"],
                 {"sshd": "openssh-server", "sshpass": "sshpass"}),
    "yum":      (["yum", "install", "-y"],
                 {"sshd": "openssh-server", "sshpass": "sshpass"}),
    "pacman":   (["pacman", "-S", "--noconfirm"],
                 {"sshd": "openssh", "sshpass": "sshpass"}),
    "zypper":   (["zypper", "--non-interactive", "install"],
                 {"sshd": "openssh", "sshpass": "sshpass"}),
    "apk":      (["apk", "add"],
                 {"sshd": "openssh", "sshpass": "sshpass"}),
    "brew":     (["brew", "install"],
                 {"sshd": "", "sshpass": "sshpass"}),  # macOS: sshd ships with the OS
    "pkg":      (["pkg", "install", "-y"],
                 {"sshd": "openssh-portable", "sshpass": "sshpass"}),
}

#: /etc/os-release ID → package manager, for hosts where several could apply.
_ID_TO_MANAGER = {
    "debian": "apt-get", "ubuntu": "apt-get", "linuxmint": "apt-get",
    "pop": "apt-get", "raspbian": "apt-get", "kali": "apt-get",
    "fedora": "dnf", "rhel": "dnf", "centos": "dnf", "rocky": "dnf",
    "almalinux": "dnf", "amzn": "dnf", "ol": "dnf",
    "arch": "pacman", "manjaro": "pacman", "endeavouros": "pacman",
    "opensuse": "zypper", "opensuse-leap": "zypper", "opensuse-tumbleweed": "zypper",
    "sles": "zypper", "alpine": "apk",
}


def _sshd_path() -> str:
    """sshd lives in /usr/sbin, which is not always on a user's PATH."""
    found = shutil.which("sshd")
    if found:
        return found
    for cand in ("/usr/sbin/sshd", "/sbin/sshd", "/usr/local/sbin/sshd"):
        if os.path.exists(cand):
            return cand
    return ""


def os_release() -> dict[str, str]:
    info: dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    info[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return info


def detect_os() -> str:
    """Human-readable distro label (used in messages and for package choice)."""
    rel = os_release()
    name = rel.get("PRETTY_NAME") or rel.get("NAME") or platform.system()
    return f"{name} ({rel.get('ID', platform.system().lower())})"


def detect_manager() -> str:
    """Pick the package manager: os-release first, then whatever is installed."""
    rel = os_release()
    for key in (rel.get("ID", "").lower(), *(rel.get("ID_LIKE", "").lower().split())):
        mgr = _ID_TO_MANAGER.get(key)
        if mgr and shutil.which(mgr):
            return mgr
    if platform.system() == "Darwin":
        return "brew" if shutil.which("brew") else ""
    for candidate in ("apt-get", "dnf", "yum", "pacman", "zypper", "apk", "pkg"):
        if shutil.which(candidate):
            return candidate
    return ""


def missing() -> list[str]:
    """Roles that are not satisfied on THIS machine (``sshd`` / ``sshpass``)."""
    gaps = []
    if not _sshd_path():
        gaps.append("sshd")
    if not shutil.which("sshpass"):
        gaps.append("sshpass")
    return gaps


def _sudo_prefix() -> list[str]:
    """Root already? Run directly. Otherwise prepend sudo (may prompt)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    return ["sudo"]


def ensure_dependencies(auto_install: bool = True, quiet: bool = False) -> tuple[bool, list[str]]:
    """Make sure sshd + sshpass exist; install what is missing.

    Returns ``(ok, notes)``. ``ok`` is False only when something is *known*
    missing afterwards — an unknown platform is reported but not treated as a
    hard failure, since the packages may well be present already.
    """
    notes: list[str] = []
    gaps = missing()
    if not gaps:
        if not quiet:
            notes.append("зависимости на месте: sshd, sshpass")
        return True, notes

    mgr = detect_manager()
    notes.append(f"ОС: {detect_os()}")
    notes.append(f"не хватает: {', '.join(gaps)}")

    if not mgr:
        notes.append("не смог определить пакетный менеджер — поставь вручную: "
                     "openssh-server и sshpass")
        return False, notes

    argv_prefix, mapping = _MANAGERS[mgr]
    pkgs = [mapping.get(role, role) for role in gaps if mapping.get(role)]
    if not pkgs:
        notes.append(f"{mgr}: пакеты для {', '.join(gaps)} не заданы — поставь вручную")
        return False, notes

    # `brew` has no -y; it never prompts for sudo either.
    if mgr == "brew":
        argv_prefix = ["brew", "install"]
    cmd = _sudo_prefix() + argv_prefix + pkgs
    notes.append(f"устанавливаю: {' '.join(cmd)}")

    if not auto_install:
        return False, notes

    try:
        # Inherit the terminal: sudo must be able to prompt for a password, and
        # we deliberately never see or store it.
        rc = subprocess.call(cmd)
    except (OSError, KeyboardInterrupt) as exc:
        notes.append(f"установка не удалась: {type(exc).__name__}: {exc}")
        return False, notes
    if rc != 0:
        notes.append(f"пакетный менеджер вернул код {rc}")
        return False, notes

    # Re-check rather than trusting the exit code.
    still = missing()
    if still:
        notes.append(f"после установки всё ещё нет: {', '.join(still)}")
        return False, notes
    notes.append("готово: sshd и sshpass установлены")
    return True, notes


def sshd_hint() -> str:
    """Platform-appropriate way to start the SSH server (shown on failure)."""
    if platform.system() == "Darwin":
        return "sshd уже в системе; включи «Remote Login» в System Settings → General → Sharing"
    if shutil.which("systemctl"):
        return "sudo systemctl enable --now ssh   (Alpine/старые: sshd)"
    if shutil.which("rc-service"):
        return "sudo rc-service sshd start"
    if shutil.which("launchctl"):
        return "sudo launchctl load -w /System/Library/LaunchDaemons/ssh.plist"
    return "запусти SSH-сервер вручную (команда зависит от системы)"


def main(argv: list[str] | None = None) -> int:
    quiet = bool(argv and "--quiet" in argv)
    ok, notes = ensure_dependencies(quiet=quiet)
    for n in notes:
        print(n)
    if not ok:
        print(sshd_hint())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
