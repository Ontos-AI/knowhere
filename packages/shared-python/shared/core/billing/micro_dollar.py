"""
MicroDollar value object for type-safe integer-based currency operations.

Standard: $1.00 = 1,000,000 micro-credits
This allows representing prices like $0.015 as 15,000 integers.

Usage:
    # Create from int (e.g., from database value)
    balance = MicroDollar(user.credits_balance)

    # Calculations
    cost = MicroDollar(15000) * 10  # 10 pages @ $0.015/page

    # Get raw int value (e.g., for database storage)
    user.credits_balance = cost.amount

    # To frontend display
    display_credits = cost.to_credit()  # Returns int for display
"""


class MicroDollar:
    """
    Value object representing currency in micro-units (1/1,000,000 of a dollar).

    All arithmetic is integer-based to prevent floating-point errors in billing.

    Example:
        price = MicroDollar(15_000)  # $0.015
        total = price * 10           # 10 pages -> 150,000 micros ($0.15)
    """

    SCALE = 1_000_000  # $1.00 = 1,000,000 micros

    def __init__(self, micro_credits: int):
        """
        Initialize with an integer amount in micro-credits.

        Args:
            micro_credits: Integer value in micro-units (1/1,000,000 of a dollar)

        Raises:
            TypeError: If micro_credits is not an integer
        """
        if not isinstance(micro_credits, int):
            raise TypeError(
                f"MicroDollar must be initialized with an integer, got {type(micro_credits).__name__}"
            )
        self._amount = micro_credits

    @property
    def amount(self) -> int:
        """The raw micro-credit value."""
        return self._amount

    # =============================================
    # Factory Methods
    # =============================================

    # shoud we call this from_credits?
    @classmethod
    def from_dollars(cls, amount: int) -> "MicroDollar":
        """
        Convert a dollar amount to MicroDollar.

        Args:
            amount: Dollar amount as int (e.g., 1 for $1.00)

        Returns:
            MicroDollar instance

        Example:
            MicroDollar.from_dollars(1) -> MicroDollar(1_000_000)
        """
        return cls(amount * cls.SCALE)

    @classmethod
    def zero(cls) -> "MicroDollar":
        """Return a zero-value MicroDollar."""
        return cls(0)

    # =============================================
    # Arithmetic Operations
    # =============================================

    def __add__(self, other: "MicroDollar") -> "MicroDollar":
        """Add two MicroDollar values."""
        if not isinstance(other, MicroDollar):
            raise TypeError(f"Cannot add MicroDollar with {type(other).__name__}")
        return MicroDollar(self._amount + other._amount)

    def __sub__(self, other: "MicroDollar") -> "MicroDollar":
        """Subtract two MicroDollar values."""
        if not isinstance(other, MicroDollar):
            raise TypeError(f"Cannot subtract MicroDollar with {type(other).__name__}")
        return MicroDollar(self._amount - other._amount)

    def __mul__(self, quantity: int) -> "MicroDollar":
        """
        Multiply by a scalar quantity (integer only).

        Example: 10 pages * $0.015/page = $0.15
        """
        if not isinstance(quantity, int):
            raise TypeError(
                f"MicroDollar can only be multiplied by an integer, got {type(quantity).__name__}"
            )
        return MicroDollar(self._amount * quantity)

    def __rmul__(self, quantity: int) -> "MicroDollar":
        """Support quantity * MicroDollar syntax."""
        return self.__mul__(quantity)

    def __floordiv__(self, divisor: int) -> "MicroDollar":
        """Integer division by a scalar."""
        if not isinstance(divisor, int):
            raise TypeError(
                f"MicroDollar can only be divided by an integer, got {type(divisor).__name__}"
            )
        return MicroDollar(self._amount // divisor)

    def __neg__(self) -> "MicroDollar":
        """Negate the value (for deductions)."""
        return MicroDollar(-self._amount)

    # =============================================
    # Comparison Operations
    # =============================================

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MicroDollar):
            return NotImplemented
        return self._amount == other._amount

    def __lt__(self, other: "MicroDollar") -> bool:
        if not isinstance(other, MicroDollar):
            return NotImplemented
        return self._amount < other._amount

    def __le__(self, other: "MicroDollar") -> bool:
        if not isinstance(other, MicroDollar):
            return NotImplemented
        return self._amount <= other._amount

    def __gt__(self, other: "MicroDollar") -> bool:
        if not isinstance(other, MicroDollar):
            return NotImplemented
        return self._amount > other._amount

    def __ge__(self, other: "MicroDollar") -> bool:
        if not isinstance(other, MicroDollar):
            return NotImplemented
        return self._amount >= other._amount

    def __hash__(self) -> int:
        return hash(self._amount)

    def __bool__(self) -> bool:
        """Return True if amount is non-zero."""
        return self._amount != 0

    # =============================================
    # Display / Formatting
    # =============================================

    def __repr__(self) -> str:
        """Developer-friendly representation."""
        return f"<MicroDollar: {self._amount} ({self._amount / self.SCALE:.2f})>"

    def __str__(self) -> str:
        """User-friendly string."""
        return f"<MicroDollar: {self._amount} ({self._amount / self.SCALE:.2f})>"

    def to_credit(self) -> float:
        """
        Convert micro-credits to display credits for frontend.

        1 display credit = 1,000,000 micro-credits = $1.00

        Returns:
            Float display credits with 4 decimal precision (e.g., 10.0005)
        """
        return round(self._amount / self.SCALE, 4)
