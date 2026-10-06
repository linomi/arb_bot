import secrets


def _password_matches(plain: str, expected: str) -> bool:
    exp = (expected or "").strip()
    if exp.startswith("$2a$") or exp.startswith("$2b$") or exp.startswith("$2y$"):
        import bcrypt
        return bcrypt.checkpw(plain.encode("utf-8"), exp.encode("utf-8"))
    return secrets.compare_digest(plain, exp)


def test_plain_password_match():
    assert _password_matches("secret", "secret")
    assert not _password_matches("secret", "other")


def test_bcrypt_password_match():
    import bcrypt
    hashed = bcrypt.hashpw(b"s3cret", bcrypt.gensalt()).decode()
    assert _password_matches("s3cret", hashed)
    assert not _password_matches("wrong", hashed)


def test_loopback_helper():
    def _is_loopback(host: str) -> bool:
        h = (host or "").strip().lower()
        return h in ("127.0.0.1", "localhost", "::1")
    assert _is_loopback("127.0.0.1")
    assert not _is_loopback("0.0.0.0")
