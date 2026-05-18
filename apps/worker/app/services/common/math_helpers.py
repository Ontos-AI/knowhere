"""Math utilities shared across services."""


def min_max_normalize(value, min_val, max_val):
    """
    Apply min-max normalization.

    Args:
        value: Value to normalize.
        min_val: Minimum value.
        max_val: Maximum value.

    Returns:
        Normalized value in the range [0, 1].
    """
    if max_val == min_val:
        return 0  # Avoid division by zero
    normalized = (value - min_val) / (max_val - min_val)
    return max(0, min(1, normalized))  # Clip to [0, 1]
