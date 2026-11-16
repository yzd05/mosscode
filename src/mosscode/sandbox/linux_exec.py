"""Runs inside bubblewrap, ahead of a sandboxed command; standard library only, started with `python -I -S -c`.

`exec -- argv`: forbid new AF_UNIX sockets with seccomp, then exec argv. A read-only mount does not stop a
connect() to a socket file, so without this a command could reach the Docker daemon, ssh-agent or D-Bus.

`bridge <socket> <port> -- argv`: listen on 127.0.0.1:<port> in the command's network namespace, where only
loopback exists, and carry each connection to MossCode's proxy on <socket>; then run `exec -- argv` and exit
with its status. The bridge itself keeps AF_UNIX: it is the one process that needs it.
"""

import ctypes
import os
import platform
import socket
import struct
import subprocess
import sys
import threading

# bwrap.py prepends `SOURCE = <this text>` when it runs it with -c; the bridge hands it to the exec step.
if "SOURCE" not in globals():
    with open(__file__, encoding="utf-8") as own:
        SOURCE = own.read()

PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
RET_ALLOW = 0x7FFF0000
RET_EPERM = 0x00050000 | 1
# (audit arch, socket syscall number) per machine.
ARCHES = {
    "x86_64": (0xC000003E, 41),
    "aarch64": (0xC00000B7, 198),
}
X32_BIT = 0x40000000
IO_URING_SETUP = 425  # The same number on both.


def _op(code, k, jt=0, jf=0):
    return struct.pack("HBBI", code, jt, jf, k)


def seccomp_program(machine: str) -> bytes:
    """socket(AF_UNIX, ...) fails with EPERM; system calls from another ABI fail too, so they cannot go around it."""
    arch, number = ARCHES[machine]
    load, jeq, jge, ret = 0x20, 0x15, 0x35, 0x06
    ops = [
        _op(load, 4),  # seccomp_data.arch
        _op(jeq, arch, 1, 0),
        _op(ret, RET_EPERM),
        _op(load, 0),  # seccomp_data.nr
    ]
    if machine == "x86_64":
        ops += [_op(jge, X32_BIT, 0, 1), _op(ret, RET_EPERM)]
    ops += [
        # io_uring can open sockets without the socket system call.
        _op(jeq, IO_URING_SETUP, 0, 1),
        _op(ret, RET_EPERM),
        _op(jeq, number, 0, 3),
        _op(load, 16),  # args[0], low 32 bits
        _op(jeq, socket.AF_UNIX, 0, 1),
        _op(ret, RET_EPERM),
        _op(ret, RET_ALLOW),
    ]
    return b"".join(ops)


def install_seccomp() -> None:
    program = seccomp_program(platform.machine())
    libc = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(program)

    class Prog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    prog = Prog(len(program) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS")
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_SECCOMP")


def run(argv):
    try:
        install_seccomp()
    except (OSError, KeyError, AttributeError) as exc:
        # Fail closed: the command does not run without the filter.
        sys.stderr.write(f"MossCode 沙箱：无法安装 seccomp 过滤（{exc}），命令未运行。\n")
        sys.exit(125)
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        sys.stderr.write(f"{argv[0]}: {exc.strerror}\n")
        sys.exit(127)


def _pump(source, target):
    try:
        while data := source.recv(65536):
            target.sendall(data)
    except OSError:
        pass
    finally:
        for side in (source, target):
            try:
                side.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _serve(listener, path):
    while True:
        client, _ = listener.accept()
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(path)
        except OSError:
            client.close()
            upstream.close()
            continue
        for a, b in ((client, upstream), (upstream, client)):
            threading.Thread(target=_pump, args=(a, b), daemon=True).start()


def bridge(path, port, argv):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(64)
    threading.Thread(target=_serve, args=(listener, path), daemon=True).start()
    # Run the same way bwrap.py runs this file: with SOURCE defined, since -c code has no __file__.
    launcher = f"SOURCE = {SOURCE!r}\n{SOURCE}"
    child = subprocess.Popen([sys.executable, "-I", "-S", "-c", launcher, "exec", "--", *argv])
    while True:
        try:
            code = child.wait()
            break
        except KeyboardInterrupt:
            continue
    sys.exit(code if code >= 0 else 128 - code)


def main(args):
    if args[:2] == ["exec", "--"] and len(args) > 2:
        run(args[2:])
    elif args[:1] == ["bridge"] and len(args) > 5 and args[3] == "--":
        bridge(args[1], int(args[2]), args[4:])
    else:
        sys.stderr.write("usage: exec -- argv | bridge <socket> <port> -- argv\n")
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
