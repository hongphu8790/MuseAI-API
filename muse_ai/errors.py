class MuseError(RuntimeError):
    """Base error for the reverse-engineered Muse client."""


class MuseAuthError(MuseError):
    pass


class MuseProtocolError(MuseError):
    pass


class MuseRpcError(MuseError):
    def __init__(self, message: str, *, status: int | None = None, body: bytes | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
