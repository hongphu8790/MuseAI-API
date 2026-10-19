class MuseError(RuntimeError):
    """Base error for the reverse-engineered Muse client."""


class MuseVideoNotCreated(MuseError):
    """Muse finished replying without producing a video or an active video task."""

    def __init__(self, muse_text: str, session_id: str | None = None):
        self.muse_text = muse_text
        self.session_id = session_id
        super().__init__(
            "Muse đã hoàn tất trả lời nhưng không trả về video. "
            + ("Phản hồi Muse: " + muse_text if muse_text else "Muse không trả về nội dung.")
        )


class MuseAuthError(MuseError):
    pass


class MuseProtocolError(MuseError):
    pass


class MuseRpcError(MuseError):
    def __init__(self, message: str, *, status: int | None = None, body: bytes | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
