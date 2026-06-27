class TaskFailure(RuntimeError):
    category = "engine"


class DiskSpaceError(TaskFailure):
    category = "disk"


class StalledTransferError(TaskFailure):
    category = "stalled"


class ShutdownError(TaskFailure):
    category = "shutdown"


class TorrentError(TaskFailure):
    category = "torrent"


class TorrentMetadataTimeoutError(TorrentError):
    category = "torrent_metadata_timeout"


class TorrentRemovedError(TorrentError):
    category = "torrent_removed"


class TorrentDuplicateError(TorrentError):
    category = "torrent_duplicate"


class TorrentEngineError(TorrentError):
    category = "torrent_engine"
