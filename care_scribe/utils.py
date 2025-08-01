import hashlib

def hash_string(input: str) -> str:
    sha256_hash = hashlib.sha256(input.encode('utf-8')).hexdigest()
    return sha256_hash
