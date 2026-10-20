class Problem(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str, status: int = 409) -> None:
    if not condition:
        raise Problem(status, code)
