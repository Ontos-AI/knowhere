"""Security helpers retained for backward compatibility.

Authentication has largely moved to FastAPI Users.
"""

import bcrypt


def mask_api_key(api_key: str | None) -> str:
    """Return a stable masked representation for logs and internal errors."""
    if not api_key:
        return "<unset>"

    if len(api_key) <= 4:
        return "*" * len(api_key)

    if len(api_key) <= 8:
        prefix_length = 2
        suffix_length = 2
    else:
        prefix_length = 4
        suffix_length = 4

    masked_length = max(1, len(api_key) - prefix_length - suffix_length)
    return f"{api_key[:prefix_length]}{'*' * masked_length}{api_key[-suffix_length:]}"


def get_password_hash(password: str) -> str:
    """
    Generate a password hash.

    FastAPI Users has its own password flow; this helper remains only for
    backward compatibility.
    """
    password_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt()
    hashed_password_bytes = bcrypt.hashpw(password_bytes, salt)
    return hashed_password_bytes.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plain password against a hashed password.

    FastAPI Users has its own password flow; this helper remains only for
    backward compatibility.
    """
    plain_password_bytes = plain_password.encode("utf-8")
    hashed_password_bytes = hashed_password.encode("utf-8")
    try:
        return bcrypt.checkpw(plain_password_bytes, hashed_password_bytes)
    except ValueError:
        return False
