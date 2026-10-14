"""Close-on-exit Windows Job. An instance and all its children die together."""
import ctypes
import os
from ctypes import wintypes


class BasicLimits(ctypes.Structure):
    _fields_ = [('ProcessUserTime', ctypes.c_int64), ('JobUserTime', ctypes.c_int64),
                ('Flags', wintypes.DWORD), ('MinWorkingSet', ctypes.c_size_t),
                ('MaxWorkingSet', ctypes.c_size_t), ('ActiveProcesses', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('Priority', wintypes.DWORD), ('Scheduling', wintypes.DWORD)]


class IOCounters(ctypes.Structure):
    _fields_ = [(key, ctypes.c_uint64) for key in
                ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('Basic', BasicLimits), ('IO', IOCounters), ('ProcessMemory', ctypes.c_size_t),
                ('JobMemory', ctypes.c_size_t), ('PeakProcessMemory', ctypes.c_size_t),
                ('PeakJobMemory', ctypes.c_size_t)]


class OwnedJob:
    def __init__(self, process):
        self.handle = None
        if os.name != 'nt': return
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle: raise OSError('Unable to create instance job')
        info = ExtendedLimits()
        info.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise OSError('Unable to set instance job limits')
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            self.close()
            raise OSError('Unable to supervise instance job')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
