import hashlib

def hash_string(input: str) -> str:
    sha256_hash = hashlib.sha256(input.encode('utf-8')).hexdigest()
    return sha256_hash

def remove_keys(obj, keys_to_remove):
    if isinstance(obj, dict):
        return {k: remove_keys(v, keys_to_remove) for k, v in obj.items() if k not in keys_to_remove}
    elif isinstance(obj, list):
        return [remove_keys(item, keys_to_remove) for item in obj]
    else:
        return obj
