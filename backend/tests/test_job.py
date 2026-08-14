from bridgebox.zapret.job import ProcessJobObject


class FakeKernel32:
    """Fakes just the four Win32 calls ProcessJobObject makes, so the logic
    is testable without a real Windows kernel handle."""

    def __init__(self, *, create_ok=True, set_info_ok=True, open_ok=True, assign_ok=True):
        self.create_ok = create_ok
        self.set_info_ok = set_info_ok
        self.open_ok = open_ok
        self.assign_ok = assign_ok
        self.closed_handles = []
        self.assigned = []

    def CreateJobObjectW(self, sa, name):
        return 111 if self.create_ok else 0

    def SetInformationJobObject(self, handle, cls, info, size):
        return 1 if self.set_info_ok else 0

    def OpenProcess(self, access, inherit, pid):
        return 222 if self.open_ok else 0

    def AssignProcessToJobObject(self, job_handle, process_handle):
        self.assigned.append((job_handle, process_handle))
        return 1 if self.assign_ok else 0

    def CloseHandle(self, handle):
        self.closed_handles.append(handle)
        return 1


def test_assign_succeeds_with_working_kernel32():
    kernel32 = FakeKernel32()
    job = ProcessJobObject(kernel32=kernel32)

    ok = job.assign(4242)

    assert ok is True
    assert kernel32.assigned == [(111, 222)]
    assert 222 in kernel32.closed_handles  # process handle released after assigning


def test_assign_returns_false_when_job_object_creation_failed():
    kernel32 = FakeKernel32(create_ok=False)
    job = ProcessJobObject(kernel32=kernel32)

    assert job.assign(4242) is False


def test_assign_returns_false_when_open_process_fails():
    kernel32 = FakeKernel32(open_ok=False)
    job = ProcessJobObject(kernel32=kernel32)

    assert job.assign(4242) is False


def test_assign_returns_false_when_assign_call_fails():
    kernel32 = FakeKernel32(assign_ok=False)
    job = ProcessJobObject(kernel32=kernel32)

    assert job.assign(4242) is False


def test_assign_never_raises_when_kernel32_is_unavailable():
    class BrokenKernel32:
        def __getattr__(self, name):
            raise AttributeError(name)

    job = ProcessJobObject(kernel32=BrokenKernel32())

    assert job.assign(4242) is False


def test_kill_on_close_is_set_with_the_length_and_offset_windows_expects():
    """Regression for a silent failure, not a style point.

    SetInformationJobObject validates the struct length exactly and rejects
    the whole call with ERROR_BAD_LENGTH (24) if it disagrees. A 64-byte
    buffer (the BASIC struct's size) was being passed for the EXTENDED class,
    and the flags were written at the BASIC struct's offset, so the call had
    never once succeeded - measured against the real kernel32. The class
    docstring promises this is what kills winws.exe when BridgeBox dies
    without cleanup, and it was doing nothing at all."""
    seen = {}

    class FakeKernel32:
        def CreateJobObjectW(self, a, b):
            return 4242

        def SetInformationJobObject(self, handle, klass, info, length):
            seen["class"] = klass
            seen["length"] = length
            seen["bytes"] = bytes(info._obj)
            return 1

    job = ProcessJobObject(kernel32=FakeKernel32())

    assert job._handle == 4242
    assert seen["class"] == 9  # JobObjectExtendedLimitInformation
    assert seen["length"] == 144  # sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION), x64
    # LimitFlags follows PerProcessUserTimeLimit(8) + PerJobUserTimeLimit(8).
    assert seen["bytes"][16:20] == (0x2000).to_bytes(4, "little")
    # ...and nothing was written where the two time limits live.
    assert seen["bytes"][0:16] == b"\x00" * 16


def test_a_refused_kill_on_close_leaves_no_job_handle():
    class FakeKernel32:
        def CreateJobObjectW(self, a, b):
            return 4242

        def SetInformationJobObject(self, handle, klass, info, length):
            return 0

    job = ProcessJobObject(kernel32=FakeKernel32())

    assert job._handle is None
    assert job.assign(1234) is False
