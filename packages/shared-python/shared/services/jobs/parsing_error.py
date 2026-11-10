"""A stale parser must stop without failing or refunding another owner."""


class ParsingOwnerError(RuntimeError):
    pass
