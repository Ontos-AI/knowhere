"""
Billing calculator for per-page pricing in micro-credits.

Per-page model: Each page costs a fixed amount in micro-credits.
$1.00 = 1,000,000 micro-credits
Default: $0.015/page = 15,000 micro-credits/page
"""

from shared.core.config import settings

from .micro_dollar import MicroDollar


class BillingCalculator:
    """
    Per-page billing calculator.

    Calculates document processing costs in micro-credits.
    Uses settings.MICRO_DOLLARS_PER_PAGE for pricing.

    Example:
        calc = BillingCalculator()
        cost = calc.calculate_page_cost(10)  # Returns MicroDollar
        print(cost.amount)  # 150,000 micro-credits
    """

    def __init__(self, price_per_page_micros: int | None = None):
        """
        Initialize with per-page pricing.

        Args:
            price_per_page_micros: Price per page in micro-credits.
                                   Defaults to settings.MICRO_DOLLARS_PER_PAGE
        """
        self._price_per_page = price_per_page_micros or settings.MICRO_DOLLARS_PER_PAGE

    @property
    def price_per_page(self) -> MicroDollar:
        """Price per page as MicroDollar."""
        return MicroDollar(self._price_per_page)

    def calculate_page_cost(self, page_count: int) -> MicroDollar:
        """
        Calculate cost for processing pages.

        Args:
            page_count: Number of pages to process

        Returns:
            MicroDollar representing total cost

        Example:
            calc.calculate_page_cost(10)  # -> MicroDollar(150000)
        """
        return MicroDollar(page_count * self._price_per_page)

    def format_description(self, page_count: int, filename: str | None = None) -> str:
        """
        Generate billing description for a document.

        Args:
            page_count: Number of pages processed
            filename: Optional filename

        Returns:
            Human-readable description string

        Example:
            "Document processing: report.pdf (10 pages @ $0.0150/page)"
        """
        price_dollars = self._price_per_page / 1_000_000
        file_name = filename or "file"
        return f"Document processing: {file_name} ({page_count} pages @ ${price_dollars:.4f}/page)"
