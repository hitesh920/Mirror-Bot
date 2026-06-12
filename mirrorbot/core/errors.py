class TaskFailure(RuntimeError):
    category = "engine"


class DiskSpaceError(TaskFailure):
    category = "disk"


class StalledTransferError(TaskFailure):
    category = "stalled"


class ShutdownError(TaskFailure):
    category = "shutdown"
