"""Windows fallback backend: a write-restricted token, for machines where MXC cannot run.

The command runs under a restricted version of the user's own token whose restricting SIDs are a capability SID
of the project, the logon SID and Everyone. Windows checks restricting SIDs for write access only, so the command
reads like the user but writes only where an ACE grants one of those SIDs: the writable roots and the private
temp get an allow ACE for the capability SID, protected paths an explicit deny, which wins over the inherited
allow. Reads and the network are not confined: the level is partial and commands are still approved.

The ACEs stay on disk between sessions (the root's ACE is inherited by every file in the project, which takes a
while to write once); every one is recorded in ~/.mosscode/sandbox-acl.json, and `moss sandbox cleanup` removes
them. A launcher (LAUNCHER, standard library only) runs between MossCode and the command: it creates the token
and starts the command with CreateProcessAsUserW, inside the same Job, then exits with the command's status.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

RECORD_NAME = "sandbox-acl.json"
NO_WINDOW = 0x08000000

LAUNCHER = r"""
import ctypes
import subprocess
import sys
from ctypes import wintypes

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
DISABLE_MAX_PRIVILEGE, LUA_TOKEN, WRITE_RESTRICTED = 0x1, 0x4, 0x8
TOKEN_RIGHTS = 0x0001 | 0x0002 | 0x0008 | 0x0080  # assign primary, duplicate, query, adjust default
TOKEN_USER, TOKEN_GROUPS, TOKEN_DEFAULT_DACL = 1, 2, 6
SE_GROUP_LOGON_ID = 0xC0000000
STARTF_USESTDHANDLES = 0x100
CREATE_UNICODE_ENVIRONMENT = 0x400
CREATE_SUSPENDED = 0x4


class SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


def fail(step):
    code = ctypes.get_last_error()
    sys.stderr.write(f"MossCode 沙箱：{step} 失败（Windows 错误 {code}），命令未运行。\n")
    sys.stderr.flush()
    kernel32.ExitProcess(125)


def sid_from(text):
    sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(text, ctypes.byref(sid)):
        fail("ConvertStringSidToSid")
    return sid


def sid_text(sid):
    out = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(out)):
        fail("ConvertSidToStringSid")
    return out.value


def token_info(token, kind):
    size = wintypes.DWORD()
    advapi32.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    if not advapi32.GetTokenInformation(token, kind, buffer, size, ctypes.byref(size)):
        fail("GetTokenInformation")
    return buffer


def restricted_token(capability):
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    token = wintypes.HANDLE()
    process = wintypes.HANDLE(kernel32.GetCurrentProcess())
    if not advapi32.OpenProcessToken(process, TOKEN_RIGHTS, ctypes.byref(token)):
        fail("OpenProcessToken")
    user = sid_text(ctypes.cast(token_info(token, TOKEN_USER), ctypes.POINTER(ctypes.c_void_p))[0])
    groups = token_info(token, TOKEN_GROUPS)
    count = ctypes.cast(groups, ctypes.POINTER(wintypes.DWORD))[0]
    entries = ctypes.cast(
        ctypes.addressof(groups) + ctypes.sizeof(ctypes.c_void_p), ctypes.POINTER(SidAndAttributes * count)
    )[0]
    logon = next((sid_text(e.Sid) for e in entries if e.Attributes & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID), None)
    names = [capability, "S-1-1-0"] + ([logon] if logon else [])
    restricting = (SidAndAttributes * len(names))(*(SidAndAttributes(sid_from(n), 0) for n in names))
    new = wintypes.HANDLE()
    flags = DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED
    if not advapi32.CreateRestrictedToken(token, flags, 0, None, 0, None, len(names), restricting, ctypes.byref(new)):
        fail("CreateRestrictedToken")
    # Objects the command creates (pipes, events, its own threads) get this DACL; without the restricting SIDs in
    # it the command could not open its own objects for writing.
    sddl = "D:" + "".join(f"(A;;GA;;;{s})" for s in ["SY", user, *names[:1], *names[2:]])
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        fail("ConvertStringSecurityDescriptor")
    present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
    if not advapi32.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
        fail("GetSecurityDescriptorDacl")
    if not advapi32.SetTokenInformation(new, TOKEN_DEFAULT_DACL, ctypes.byref(dacl), ctypes.sizeof(dacl)):
        fail("SetTokenInformation")
    return new


# Open every protected path without FILE_SHARE_DELETE: while the handles are open nothing can delete or rename
# the path or a folder above it. DELETE is not a write access, so the token alone does not stop that.
def hold(paths):
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE,
    ]
    held = []
    for path in paths:
        # Read access: a handle with attribute access only does not take part in sharing checks. Share read and
        # write, not delete; OPEN_EXISTING; folders and links themselves.
        handle = kernel32.CreateFileW(path, 0x81, 0x3, None, 3, 0x02000000 | 0x00200000, None)
        if handle in (None, wintypes.HANDLE(-1).value):
            fail(f"打开受保护的路径 {path}")
        held.append(handle)
    return held


# A Job of the command's own: closing it ends whatever the command left running, before the held handles go.
def job_killing_on_close():
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        fail("CreateJobObject")
    limits = ctypes.create_string_buffer(144)  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION on 64-bit Windows
    ctypes.c_uint32.from_buffer(limits, 16).value = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(wintypes.HANDLE(job), 9, limits, len(limits)):
        fail("SetInformationJobObject")
    return job


def run(capability, paths, argv):
    held = hold(paths)
    token = restricted_token(capability)
    job = job_killing_on_close()
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    handles = [kernel32.GetStdHandle(n) for n in (-10, -11, -12)]
    for handle in handles:
        if handle:
            kernel32.SetHandleInformation(wintypes.HANDLE(handle), 1, 1)
    info = StartupInfo(cb=ctypes.sizeof(StartupInfo), dwFlags=STARTF_USESTDHANDLES)
    info.hStdInput, info.hStdOutput, info.hStdError = handles
    process = ProcessInformation()
    line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    ok = advapi32.CreateProcessAsUserW(
        token, None, line, None, None, True, CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED, None, None,
        ctypes.byref(info), ctypes.byref(process),
    )
    if not ok:
        code = ctypes.get_last_error()
        if code == 2:
            sys.stderr.write(f"{argv[0]}: 找不到程序\n")
            sys.stderr.flush()
            kernel32.ExitProcess(127)
        fail("CreateProcessAsUser")
    child, thread = wintypes.HANDLE(process.hProcess), wintypes.HANDLE(process.hThread)
    if not kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), child):
        kernel32.TerminateProcess(child, 125)
        fail("AssignProcessToJobObject")
    kernel32.ResumeThread(thread)
    kernel32.CloseHandle(thread)
    kernel32.WaitForSingleObject(child, wintypes.DWORD(0xFFFFFFFF))
    status = wintypes.DWORD()
    kernel32.GetExitCodeProcess(child, ctypes.byref(status))
    kernel32.CloseHandle(wintypes.HANDLE(job))
    sys.stdout.flush()
    sys.stderr.flush()
    kernel32.ExitProcess(wintypes.UINT(status.value))


args = sys.argv[1:]
if args[:1] == ["exec"] and "--" in args[2:] and len(args) > args.index("--") + 1:
    split = args.index("--")
    run(args[1], args[2:split], args[split + 1:])
sys.stderr.write("usage: exec <capability SID> [protected path ...] -- argv\n")
sys.exit(2)
"""


def record_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".mosscode" / RECORD_NAME


def load(home: Path | None = None) -> dict:
    try:
        data = json.loads(record_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "roots": {}}
    if not isinstance(data, dict) or not isinstance(data.get("roots"), dict):
        return {"version": 1, "roots": {}}
    return data


def save(data: dict, home: Path | None = None) -> None:
    path = record_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def new_sid() -> str:
    # The shape of a domain SID no account has; the ACL editor shows it as the raw string.
    return "S-1-5-21-" + "-".join(str(secrets.randbits(31) + 1) for _ in range(4))


def entries(policy) -> dict[Path, str]:
    """The ACEs a policy needs, path → kind: write (allow) for writable roots, protect (deny writes) for the
    protected paths that exist. Deleting and renaming are not write access to a restricted token; the launcher
    holds protected paths open for that."""
    marks = {root: "write" for root in policy.writable}
    for path in policy.protected:
        if os.path.lexists(path) and any(path.is_relative_to(root) for root in policy.writable):
            marks[path] = "protect"
    return marks


# Modify for writable roots; for protected paths every write and ACL change. Reads stay with the user's own ACEs.
MODIFY = 0x1301BF
DENY_WRITES = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000
GRANT_ACCESS, DENY_ACCESS = 1, 3
INHERIT_ALL = 0x3  # Files and folders below inherit the ACE.
FILE_OBJECT, DACL_INFORMATION = 1, 4
INHERITED_ACE = 0x10


def _api():
    import ctypes
    from ctypes import wintypes

    class Trustee(ctypes.Structure):
        _fields_ = [
            ("pMultipleTrustee", ctypes.c_void_p),
            ("MultipleTrusteeOperation", ctypes.c_int),
            ("TrusteeForm", ctypes.c_int),
            ("TrusteeType", ctypes.c_int),
            ("ptstrName", ctypes.c_void_p),
        ]

    class ExplicitAccess(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", wintypes.DWORD),
            ("grfAccessMode", ctypes.c_int),
            ("grfInheritance", wintypes.DWORD),
            ("Trustee", Trustee),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.POINTER(ctypes.c_void_p)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, pointer, ctypes.c_void_p,
        pointer,
    ]  # fmt: skip
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p,
    ]  # fmt: skip
    advapi32.SetEntriesInAclW.argtypes = [wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p, pointer]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p,
    ]  # fmt: skip
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, pointer]
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, pointer]
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.DeleteAce.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    return ctypes, wintypes, advapi32, kernel32, ExplicitAccess


def _sddl(path: Path) -> str:
    """The path's DACL as SDDL; an unknown SID shows there as its own string."""
    ctypes, wintypes, advapi32, kernel32, _ = _api()
    dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    code = advapi32.GetNamedSecurityInfoW(
        str(path), FILE_OBJECT, DACL_INFORMATION, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
    )
    if code:
        raise OSError(code, "GetNamedSecurityInfo", str(path))
    try:
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, DACL_INFORMATION, ctypes.byref(text), None
        ):
            raise OSError(ctypes.get_last_error(), "ConvertSecurityDescriptorToStringSecurityDescriptor")
        value = text.value or ""
        kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        return value
    finally:
        kernel32.LocalFree(descriptor)


def _apply(path: Path, sid: str, changes: list[tuple[int, int, int]]) -> None:
    """changes: (mode, mask, inheritance). SetNamedSecurityInfo carries inheritable ACEs to what the folder holds."""
    ctypes, _, advapi32, kernel32, ExplicitAccess = _api()
    psid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(sid, ctypes.byref(psid)):
        raise OSError(ctypes.get_last_error(), "ConvertStringSidToSid", sid)
    old, descriptor, new = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        code = advapi32.GetNamedSecurityInfoW(
            str(path), FILE_OBJECT, DACL_INFORMATION, None, None, ctypes.byref(old), None, ctypes.byref(descriptor)
        )
        if code:
            raise OSError(code, "GetNamedSecurityInfo", str(path))
        access = (ExplicitAccess * len(changes))()
        for item, (mode, mask, inheritance) in zip(access, changes, strict=True):
            item.grfAccessPermissions, item.grfAccessMode, item.grfInheritance = mask, mode, inheritance
            item.Trustee.ptstrName = psid.value  # TrusteeForm 0: a SID.
        code = advapi32.SetEntriesInAclW(len(changes), access, old, ctypes.byref(new))
        if code:
            raise OSError(code, "SetEntriesInAcl", str(path))
        code = advapi32.SetNamedSecurityInfoW(str(path), FILE_OBJECT, DACL_INFORMATION, None, None, new, None)
        if code:
            raise OSError(code, "SetNamedSecurityInfo", str(path))
    finally:
        for handle in (new, descriptor, psid):
            if handle.value:
                kernel32.LocalFree(handle)


def _changes(path: Path, kind: str) -> list[tuple[int, int, int]]:
    inherit = INHERIT_ALL if path.is_dir() else 0
    return [(GRANT_ACCESS, MODIFY, inherit) if kind == "write" else (DENY_ACCESS, DENY_WRITES, inherit)]


def has_ace(path: Path, sid: str) -> bool:
    try:
        return f";{sid})" in _sddl(path)
    except OSError:
        return False


def revoke(path: Path, sid: str) -> None:
    """Remove the SID's own allow and deny ACEs (REVOKE_ACCESS in SetEntriesInAcl leaves deny ACEs behind)."""
    ctypes, _, advapi32, kernel32, _ = _api()
    psid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(sid, ctypes.byref(psid)):
        raise OSError(ctypes.get_last_error(), "ConvertStringSidToSid", sid)
    dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        code = advapi32.GetNamedSecurityInfoW(
            str(path), FILE_OBJECT, DACL_INFORMATION, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
        )
        if code:
            raise OSError(code, "GetNamedSecurityInfo", str(path))
        if not dacl.value:
            return
        count = ctypes.c_uint16.from_address(dacl.value + 4).value
        removed = False
        for index in reversed(range(count)):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise OSError(ctypes.get_last_error(), "GetAce", str(path))
            kind = ctypes.c_uint8.from_address(ace.value).value
            flags = ctypes.c_uint8.from_address(ace.value + 1).value
            # Allowed (0) and denied (1) ACEs: header, mask, then the SID.
            if kind in (0, 1) and not flags & INHERITED_ACE and advapi32.EqualSid(ace.value + 8, psid):
                if not advapi32.DeleteAce(dacl, index):
                    raise OSError(ctypes.get_last_error(), "DeleteAce", str(path))
                removed = True
        if removed:
            code = advapi32.SetNamedSecurityInfoW(str(path), FILE_OBJECT, DACL_INFORMATION, None, None, dacl, None)
            if code:
                raise OSError(code, "SetNamedSecurityInfo", str(path))
    finally:
        for handle in (descriptor, psid):
            if handle.value:
                kernel32.LocalFree(handle)


EVERYONE = "S-1-1-0"
INHERIT_ONLY_ACE = 0x08
# Write data, append data, generic all, generic write.
WRITE_BITS = 0x2 | 0x4 | 0x10000000 | 0x40000000


def everyone_writes(path: Path) -> bool:
    """Whether the folder's DACL lets Everyone write it: Everyone is a restricting SID, so commands may too."""
    ctypes, _, advapi32, kernel32, _ = _api()
    psid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(EVERYONE, ctypes.byref(psid)):
        return False
    dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        code = advapi32.GetNamedSecurityInfoW(
            str(path), FILE_OBJECT, DACL_INFORMATION, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
        )
        if code:
            return False
        if not dacl.value:
            return True  # A null DACL grants everyone everything.
        count = ctypes.c_uint16.from_address(dacl.value + 4).value
        for index in range(count):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                return False
            kind = ctypes.c_uint8.from_address(ace.value).value
            flags = ctypes.c_uint8.from_address(ace.value + 1).value
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            if (
                kind == 0
                and not flags & INHERIT_ONLY_ACE
                and mask & WRITE_BITS
                and advapi32.EqualSid(ace.value + 8, psid)
            ):
                return True
        return False
    finally:
        for handle in (descriptor, psid):
            if handle.value:
                kernel32.LocalFree(handle)


def open_folders(policy, *, limit: int = 300) -> list[Path]:
    """Folders outside the writable roots that Everyone may write, so a command could too.

    Not a full scan: the folders above the project, PATH entries, the home folder's own and the shared folders
    Windows sets up, where a weak ACL is common and a planted program would be run later.
    """
    candidates: dict[Path, None] = dict.fromkeys(policy.root.parents)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry.strip():
            candidates[Path(entry.strip())] = None
    for base in (Path.home(), os.environ.get("PUBLIC"), os.environ.get("ProgramData")):
        if not base:
            continue
        candidates[Path(base)] = None
        try:
            candidates.update(dict.fromkeys(p for p in Path(base).iterdir() if p.is_dir()))
        except OSError:
            continue
    found = []
    for path in list(candidates)[:limit]:
        if any(path.is_relative_to(root) for root in policy.writable) or not path.is_dir():
            continue
        if everyone_writes(path):
            found.append(path)
    return found


class Grants:
    """The ACEs this session has checked, so each path's DACL is read once per session at most."""

    def __init__(self, home: Path | None = None):
        self.home = home
        self.checked: set[tuple[str, str, str]] = set()

    def ensure(self, policy) -> str:
        """The project's capability SID, after making sure every ACE the policy needs is on disk."""
        from . import SandboxFailure

        data = load(self.home)
        key = str(policy.root)
        changed = key not in data["roots"]
        entry = data["roots"].setdefault(key, {"sid": new_sid(), "paths": {}})
        sid = entry["sid"]
        for path, kinds in entries(policy).items():
            mark = (sid, str(path), kinds)
            if mark in self.checked:
                continue
            if entry["paths"].get(str(path)) != kinds or not has_ace(path, sid):
                try:
                    if str(path) in entry["paths"]:
                        revoke(path, sid)
                    _apply(path, sid, _changes(path, kinds))
                except OSError as exc:
                    raise SandboxFailure(f"无法为 {path} 设置访问控制（Windows 错误 {exc.errno}）。") from None
                entry["paths"][str(path)] = kinds
                changed = True
            self.checked.add(mark)
        if changed:
            save(data, self.home)
        return sid


def cleanup(root: Path | None = None, *, home: Path | None = None) -> list[str]:
    """Remove the recorded ACEs, of one project or all; returns what was done, one line each."""
    data = load(home)
    lines = []
    for key in list(data["roots"]):
        if root is not None and Path(key) != root:
            continue
        entry = data["roots"][key]
        failed = {}
        for path, kinds in entry.get("paths", {}).items():
            if not os.path.lexists(path):
                continue
            try:
                revoke(Path(path), entry["sid"])
            except OSError:
                failed[path] = kinds
                lines.append(f"未能移除：{path}")
        if failed:
            entry["paths"] = failed
        else:
            del data["roots"][key]
            lines.append(f"已移除：{key}（{len(entry.get('paths', {}))} 处）")
    save(data, home)
    return lines


def command(argv: list[str], cwd: Path, policy, *, python: str, grants: Grants | None = None) -> list[str]:
    sid = (grants or _grants_for_session()).ensure(policy)
    held = [str(path) for path, kind in entries(policy).items() if kind == "protect"]
    return [python, "-I", "-S", "-c", LAUNCHER, "exec", sid, *held, "--", *argv]


_session: Grants | None = None


def _grants_for_session() -> Grants:
    global _session
    if _session is None:
        _session = Grants()
    return _session


def check() -> str:
    """An empty string when a write-restricted command runs here and cannot write where no ACE allows it."""
    if os.name != "nt":
        return "受限令牌只用于 Windows。"
    from . import safe_python

    python = safe_python()
    if python is None:
        return "找不到用来启动沙箱的 Python 解释器。"
    with tempfile.TemporaryDirectory(prefix="mosscode-token-") as folder:
        probe = Path(folder) / "probe"
        code = f"open({str(probe)!r}, 'w')"
        argv = [python, "-I", "-S", "-c", LAUNCHER, "exec", new_sid(), "--", python, "-I", "-S", "-c", code]
        try:
            done = subprocess.run(argv, capture_output=True, timeout=60, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"受限令牌自检失败：{exc}。"
        stderr = done.stderr.decode("utf-8", "replace")
        if "MossCode 沙箱" in stderr:
            return stderr.strip().splitlines()[-1]
        if done.returncode == 0 or probe.exists():
            return "受限令牌自检失败：命令写入了没有授权的目录。"
        if "PermissionError" not in stderr:
            return "受限令牌自检失败：" + (stderr.strip()[-300:] or f"退出码 {done.returncode}") + "。"
    return ""
