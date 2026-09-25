class PageviewsError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details if details is not None else {}

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "details": self.details,
        }