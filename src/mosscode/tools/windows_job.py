"""Windows kill-on-close Job Object for one command invocation."""

import ctypes
from ctypes import wintypes


class BasicLimit(ctypes.Structure):
    _fields_ = [
        ("ProcessTime", ctypes.c_int64),
        ("JobTime", ctypes.c_int64),
        ("Flags", wintypes.DWORD),
        ("MinWorkingSet", ctypes.c_size_t),
        ("MaxWorkingSet", ctypes.c_size_t),
        ("ActiveProcesses", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IOCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in ("ReadOperations", "WriteOperations", "OtherOperations", "ReadBytes", "WriteBytes", "OtherBytes")
    ]


class ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("Basic", BasicLimit),
        ("IO", IOCounters),
        ("ProcessMemory", ctypes.c_size_t),
        ("JobMemory", ctypes.c_size_t),
        ("PeakProcessMemory", ctypes.c_size_t),
        ("PeakJobMemory", ctypes.c_size_t),
    ]


# JOB_OBJECT_UILIMIT_*: no clipboard reads (passwords sit there), no global atoms, system parameters, display
# settings, desktop switching or logoff. Not a security boundary on its own: processes started through WMI or the
# task scheduler are outside the Job.
UI_LIMITS = 0x2 | 0x8 | 0x10 | 0x20 | 0x40 | 0x80
JOB_BASIC_UI_RESTRICTIONS = 4


class WindowsJob:
    def __init__(self, pid: int, *, ui_limits: bool = False):
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        self.api = api
        self.handle = api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process = None
        try:
            if not api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            ui = wintypes.DWORD(UI_LIMITS)
            if ui_limits and not api.SetInformationJobObject(
                self.handle, JOB_BASIC_UI_RESTRICTIONS, ctypes.byref(ui), ctypes.sizeof(ui)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process = api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not api.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            self.close()
            raise
        finally:
            if process:
                api.CloseHandle(process)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
