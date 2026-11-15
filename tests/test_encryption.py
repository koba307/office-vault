from app.core.encryption import decrypt_secret, encrypt_secret


def test_encrypt_decrypt_roundtrip():
    plain = "super-secret-password"
    enc = encrypt_secret(plain)
    assert enc != plain
    assert decrypt_secret(enc) == plain


def test_empty():
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""