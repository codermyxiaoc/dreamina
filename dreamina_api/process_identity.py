"""Read-only process birth evidence; absence is never guessed from a PID alone."""
import os
from pathlib import Path
import sys


def process_snapshot(pid):
    if type(pid) is not int or pid<=0:return None
    if sys.platform.startswith('linux'):
        directory=Path('/proc')/str(pid)
        try:
            raw=(directory/'cmdline').read_bytes()
            if len(raw)>65536:return None
            argv=[part.decode('utf-8','strict') for part in raw.split(b'\0') if part]
        except FileNotFoundError:return None  # hidepid and exit races are both possible.
        except (OSError,UnicodeError):return None
        result={'argv':argv}
        try:
            stat=(directory/'stat').read_text().rsplit(')',1)[1].split()
            ticks=int(stat[19]);boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            namespace=os.readlink(directory/'ns/pid')
            result['identity']='linux:'+boot+':'+namespace+':'+str(ticks)
            uptime=next(line.split()[1] for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
            result['created_at']=int(uptime)+ticks/os.sysconf('SC_CLK_TCK')
        except (OSError,ValueError,IndexError,StopIteration):pass
        return result
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return None
        try:
            created,exited,kernel_time,user_time=(wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(exited),ctypes.byref(kernel_time),ctypes.byref(user_time)):return None
            ticks=(created.dwHighDateTime<<32)|created.dwLowDateTime
            return {'identity':'windows:'+str(ticks),'created_at':ticks/10_000_000-11_644_473_600}
        finally:kernel.CloseHandle(handle)
    return None
