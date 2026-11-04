"""Windows 作业对象持有实际解释器；关闭最后一个句柄时内核清理全部成员。

venv 的 Popen.pid 可能只是启动器：先纳入启动器，再在 hello 后、加载前
验证实际 PID 的直接父进程及创建时间并纳入。不能仅 kill 启动器。
"""
import ctypes
from ctypes import wintypes as w
import os


class ProcessOwner:
    def __init__(self, process):
        self.process = process
        self.handle = None
        self.actual = None
        if os.name != 'nt':
            raise OSError('Resident model supervision currently requires Windows')
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        api = self.api
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        api.CreateJobObjectW.restype = w.HANDLE
        api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        api.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]
        api.CloseHandle.argtypes = [w.HANDLE]
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        api.OpenProcess.restype = w.HANDLE
        api.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
        api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),
                ('LimitFlags',w.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),
                ('ActiveProcessLimit',w.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',w.DWORD),('SchedulingClass',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name,ctypes.c_uint64) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class Limits(ctypes.Structure):
            _fields_ = [('BasicLimitInformation',Basic),('IoInfo',IO)] + [(name,ctypes.c_size_t) for name in ('ProcessMemoryLimit','JobMemoryLimit','PeakProcessMemoryUsed','PeakJobMemoryUsed')]
        try:
            self.handle = api.CreateJobObjectW(None, None)
            if not self.handle: raise ctypes.WinError(ctypes.get_last_error())
            limits = Limits(); limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            launcher = self._open(process.pid)
            try:
                self.created = self._created(launcher)
                self._attach(launcher)
            finally: api.CloseHandle(launcher)
        except BaseException:
            self.close()
            raise

    def _open(self, pid):
        handle = self.api.OpenProcess(0x100 | 0x1 | 0x1000 | 0x100000, False, pid)
        if not handle: raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def _created(self, handle):
        values = [w.FILETIME() for _ in range(4)]
        if not self.api.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime

    def _attach(self, handle):
        inside = w.BOOL()
        if not self.api.IsProcessInJob(handle, self.handle, ctypes.byref(inside)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not inside.value and not self.api.AssignProcessToJobObject(self.handle, handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def attach_actual(self, pid, ppid):
        if type(pid) is not int or pid <= 0 or (pid != self.process.pid and ppid != self.process.pid):
            raise ValueError('Worker ownership mismatch')
        if pid != self.process.pid and self._parent(pid) != self.process.pid:
            raise ValueError('Worker OS ownership mismatch')
        handle = self._open(pid)
        try:
            if self._created(handle) < self.created: raise ValueError('Worker creation mismatch')
            self._attach(handle)
        except BaseException:
            self.api.CloseHandle(handle)
            raise
        self.actual = handle

    def _parent(self, pid):
        # Do not trust a worker-supplied ppid. Snapshot the OS process tree;
        # keep the opened process handle and creation time check as a second fence.
        class Entry(ctypes.Structure):
            _fields_ = [('dwSize',w.DWORD),('cntUsage',w.DWORD),('th32ProcessID',w.DWORD),
                ('th32DefaultHeapID',ctypes.c_size_t),('th32ModuleID',w.DWORD),('cntThreads',w.DWORD),
                ('th32ParentProcessID',w.DWORD),('pcPriClassBase',w.LONG),('dwFlags',w.DWORD),('szExeFile',w.WCHAR*260)]
        api=self.api
        api.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];api.CreateToolhelp32Snapshot.restype=w.HANDLE
        api.Process32FirstW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
        api.Process32NextW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
        snapshot=api.CreateToolhelp32Snapshot(2,0)
        if snapshot==w.HANDLE(-1).value:raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry=Entry();entry.dwSize=ctypes.sizeof(entry)
            found=api.Process32FirstW(snapshot,ctypes.byref(entry))
            while found:
                if entry.th32ProcessID==pid:return entry.th32ParentProcessID
                found=api.Process32NextW(snapshot,ctypes.byref(entry))
            return None
        finally:api.CloseHandle(snapshot)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle); self.handle = None
        if self.actual:
            self.api.WaitForSingleObject(self.actual, 5000)
            self.api.CloseHandle(self.actual); self.actual = None
