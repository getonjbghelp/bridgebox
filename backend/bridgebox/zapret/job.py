from __future__ import annotations

import ctypes
import logging

logger = logging.getLogger(__name__)

# JOBOBJECT_EXTENDED_LIMIT_INFORMATION.BasicLimitInformation.LimitFlags value
# that tells Windows to terminate every process in the job the moment the job
# handle closes (i.e. when this Python process exits or dies, for any reason).
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_ALL_ACCESS = 0x1F0FFF

# sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION) on x64:
#   JOBOBJECT_BASIC_LIMIT_INFORMATION  64
#   IO_COUNTERS (6 x ULONGLONG)        48
#   4 x SIZE_T (process/job mem limits) 32
# SetInformationJobObject validates this length exactly and fails the whole
# call with ERROR_BAD_LENGTH (24) if it disagrees - which is what a 64-byte
# buffer here did, silently disabling kill-on-close entirely.
_EXTENDED_LIMIT_INFORMATION_SIZE = 144

# LimitFlags sits after PerProcessUserTimeLimit (8) + PerJobUserTimeLimit (8).
# Writing it at offset 8 set a time limit's low dword instead of the flags.
_LIMIT_FLAGS_OFFSET = 16


class ProcessJobObject:
    """A Windows Job Object with "kill on close" set, so any process assigned
    to it dies the instant this Python process does - crash, `taskkill /F`,
    normal exit, doesn't matter. This is the one mechanism that survives a
    hard kill of BridgeBox itself: window.events.closing / atexit handlers
    can't run at all if the process is killed outright, but the OS enforces
    a job object's kill-on-close regardless (see PRD "чтобы zapret мог
    закрываться при закрытии приложения").

    kernel32 is injected for testability - defaults to the real Win32 API via
    ctypes.windll on Windows."""

    def __init__(self, *, kernel32=None):
        self._kernel32 = kernel32 if kernel32 is not None else _real_kernel32()
        self._handle = None
        if self._kernel32 is not None:
            self._handle = self._create()

    def _create(self):
        try:
            handle = self._kernel32.CreateJobObjectW(None, None)
        except (AttributeError, OSError):
            return None
        if not handle:
            return None
        if not self._set_kill_on_close(handle):
            return None
        return handle

    def _set_kill_on_close(self, handle) -> bool:
        # Only LimitFlags is set; the rest of the struct can stay zeroed -
        # Windows only enforces the limits whose flag bit is actually set. But
        # the struct still has to be the full declared size, and the flags
        # still have to land at their real offset: both were wrong here, so
        # this call had never once succeeded (measured: returns 0 with
        # GetLastError()==24 ERROR_BAD_LENGTH), leaving winws.exe able to
        # outlive a hard kill of BridgeBox exactly as the class docstring
        # promises it cannot.
        info = ctypes.create_string_buffer(_EXTENDED_LIMIT_INFORMATION_SIZE)
        ctypes.memmove(
            ctypes.byref(info, _LIMIT_FLAGS_OFFSET),
            ctypes.byref(ctypes.c_uint32(_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)),
            4,
        )
        try:
            ok = self._kernel32.SetInformationJobObject(
                handle, _JOBOBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), len(info)
            )
        except (AttributeError, OSError):
            return False
        if not ok:
            logger.warning(
                "SetInformationJobObject refused kill-on-close - winws.exe will not "
                "be terminated automatically if BridgeBox dies without cleanup"
            )
        return bool(ok)

    def assign(self, pid: int) -> bool:
        """Assign a process to this job. Best-effort: returns False on any
        failure rather than raising, since this is defense-in-depth on top
        of the normal stop() path, not something that should block startup."""
        if not self._handle:
            return False
        try:
            process_handle = self._kernel32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
            if not process_handle:
                return False
            try:
                ok = self._kernel32.AssignProcessToJobObject(self._handle, process_handle)
            finally:
                self._kernel32.CloseHandle(process_handle)
        except (AttributeError, OSError):
            return False
        return bool(ok)


def _real_kernel32():
    try:
        return ctypes.windll.kernel32  # type: ignore[attr-defined]
    except AttributeError:
        return None
