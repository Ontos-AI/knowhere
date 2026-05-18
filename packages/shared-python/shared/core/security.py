"""Security helpers retained for backward compatibility.

Authentication has largely moved to FastAPI Users.
"""

import bcrypt


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
