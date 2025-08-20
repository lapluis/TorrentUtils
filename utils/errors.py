class TorrentNotReadyError(Exception): pass


class BdecodeError(ValueError): pass


class PieceSizeTooSmall(ValueError): pass


class PieceSizeUncommon(ValueError): pass


class EmptySourceSize(ValueError): pass
