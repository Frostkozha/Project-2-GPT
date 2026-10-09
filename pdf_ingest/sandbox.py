"""A fail-closed Linux parser sandbox with hard writable-storage limits.

The worker gets one preopened result file. New writable opens and all filesystem
mutation are denied by seccomp; RLIMIT_FSIZE caps that only writable file. This
deliberately excludes temporary-file based OCR until a bounded adapter exists.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import math
import os
import platform
import resource

from .schema import IngestError, Limits


class _ArgumentComparison(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]


def apply_limits(limits: Limits) -> None:
    if platform.system() != "Linux" or not hasattr(resource, "RLIMIT_AS"):
        raise IngestError("RESOURCE_LIMIT", "This deployment requires Linux resource limits and seccomp.")
    if limits.memory_bytes <= 0 or limits.scratch_bytes <= 0:
        raise IngestError("RESOURCE_LIMIT", "Positive enforceable memory and scratch budgets are required.")
    try:
        settings = [(resource.RLIMIT_AS, limits.memory_bytes),
                    (resource.RLIMIT_FSIZE, limits.scratch_bytes),
                    (resource.RLIMIT_CORE, 0), (resource.RLIMIT_NOFILE, 128),
                    (resource.RLIMIT_CPU, max(1, math.ceil(limits.job_timeout_seconds)))]
        for kind, budget in settings:
            current = resource.getrlimit(kind)
            if current[1] != resource.RLIM_INFINITY:
                budget = min(budget, current[1])
            resource.setrlimit(kind, (budget, budget))
            if resource.getrlimit(kind) != (budget, budget):
                raise ValueError("Resource limit was not installed")
    except (OSError, ValueError):
        raise IngestError("RESOURCE_LIMIT", "The worker resource limits could not be enforced.") from None


def install_syscall_filter() -> None:
    """Block network, filesystem mutation, process-group escape and bypass APIs."""
    try:
        library = ctypes.util.find_library("seccomp")
        if not library:
            raise OSError("libseccomp unavailable")
        seccomp = ctypes.CDLL(library, use_errno=True)
        seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
        seccomp.seccomp_init.restype = ctypes.c_void_p
        seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
        seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
        seccomp.seccomp_rule_add.restype = ctypes.c_int
        seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
        seccomp.seccomp_load.restype = ctypes.c_int
        seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
        seccomp.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
        seccomp.seccomp_attr_set.restype = ctypes.c_int
        context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
        if not context:
            raise OSError("seccomp initialization failed")
        deny = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO
        try:
            # Trusted imports may already have native thread pools. Every
            # thread must receive the same filter before any PDF is parsed.
            if seccomp.seccomp_attr_set(context, 4, 1) != 0:  # SCMP_FLTATR_CTL_TSYNC
                raise OSError("seccomp thread synchronization unavailable")
            blocked = (
                "socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
                "sendto", "sendmsg", "sendmmsg", "recvfrom", "recvmsg", "recvmmsg",
                "openat2", "creat", "truncate", "ftruncate", "fallocate", "mkdir", "mkdirat",
                "rmdir", "rename", "renameat", "renameat2", "unlink", "unlinkat", "link", "linkat",
                "symlink", "symlinkat", "mknod", "mknodat", "chmod", "fchmod", "fchmodat", "fchmodat2",
                "chown", "fchown", "fchownat", "lchown", "utime", "utimes", "futimesat", "utimensat",
                "setxattr", "lsetxattr", "fsetxattr", "removexattr", "lremovexattr", "fremovexattr",
                "mount", "umount2", "pivot_root", "chroot", "unshare", "setns", "setsid", "setpgid",
                "ptrace", "process_vm_writev", "bpf", "io_uring_setup", "open_by_handle_at",
                "name_to_handle_at", "execve", "execveat", "swapon", "swapoff",
                "fork", "vfork", "kill", "pidfd_send_signal", "process_vm_readv",
                "tkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo", "pidfd_getfd",
            )
            for name in blocked:
                syscall = seccomp.seccomp_syscall_resolve_name(name.encode())
                if syscall >= 0 and seccomp.seccomp_rule_add(context, deny, syscall, 0) != 0:
                    raise OSError("seccomp rule failed")
            for name, flags_argument in (("open", 1), ("openat", 2)):
                syscall = seccomp.seccomp_syscall_resolve_name(name.encode())
                if syscall < 0:
                    continue
                for mask, value in ((os.O_ACCMODE, os.O_WRONLY), (os.O_ACCMODE, os.O_RDWR),
                                    (os.O_CREAT, os.O_CREAT), (os.O_TRUNC, os.O_TRUNC),
                                    (os.O_APPEND, os.O_APPEND), (os.O_TMPFILE, os.O_TMPFILE)):
                    argument = _ArgumentComparison(flags_argument, 7, mask, value)
                    if seccomp.seccomp_rule_add(context, deny, syscall, 1, argument) != 0:
                        raise OSError("seccomp writable-open rule failed")
            # Memory is per address space. Prevent multiplying it with child
            # processes while allowing bounded native library threads.
            clone = seccomp.seccomp_syscall_resolve_name(b"clone")
            if clone >= 0:
                no_thread = _ArgumentComparison(0, 7, 0x10000, 0)  # CLONE_THREAD absent
                if seccomp.seccomp_rule_add(context, deny, clone, 1, no_thread) != 0:
                    raise OSError("seccomp process-clone rule failed")
            clone3 = seccomp.seccomp_syscall_resolve_name(b"clone3")
            if clone3 >= 0 and seccomp.seccomp_rule_add(context, 0x00050000 | errno.ENOSYS, clone3, 0) != 0:
                raise OSError("seccomp clone3 rule failed")
            for name, allowed_target in (("prlimit64", 0), ("tgkill", os.getpid())):
                syscall = seccomp.seccomp_syscall_resolve_name(name.encode())
                if syscall >= 0:
                    external_target = _ArgumentComparison(0, 1, allowed_target, 0)  # SCMP_CMP_NE
                    if seccomp.seccomp_rule_add(context, deny, syscall, 1, external_target) != 0:
                        raise OSError("seccomp external-process rule failed")
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
                raise OSError("No-new-privileges failed")
            if seccomp.seccomp_load(context) != 0:
                raise OSError("seccomp installation failed")
        finally:
            seccomp.seccomp_release(context)
    except Exception:
        raise IngestError("RESOURCE_LIMIT", "The required Linux syscall sandbox could not be installed.") from None


def isolate(limits: Limits) -> None:
    os.umask(0o077)
    # Install seccomp before accepting PDF bytes; resource failure also fails closed.
    install_syscall_filter()
    apply_limits(limits)
