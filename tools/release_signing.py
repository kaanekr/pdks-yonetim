"""Local publisher signing. Private RSA key is DPAPI-encrypted outside the repository."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT/'windows_app/assets/update_public_key.pem'


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, decrypt=False):
    crypt = ctypes.WinDLL('Crypt32.dll', use_last_error=True)
    kernel = ctypes.WinDLL('Kernel32.dll', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, result = Blob(len(data), buffer), Blob()
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError('Windows signing key protection failed; no key was published.')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(result.data, 0, result.size)
        kernel.LocalFree(result.data)
        ctypes.memset(buffer, 0, len(buffer))


def key_path():
    return Path(os.environ['LOCALAPPDATA'])/'PDKS_ReleaseKeys'/'github-release-rsa.dpapi'


def load_key(initialize=False):
    path = key_path()
    if path.exists():
        key = serialization.load_der_private_key(_dpapi(path.read_bytes(), decrypt=True), None)
    else:
        if not initialize:
            raise RuntimeError('Release signing key is missing; do not silently rotate trust.')
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        raw = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        encrypted = _dpapi(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as file:
            file.write(encrypted)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 2048:
        raise RuntimeError('Release signing key has an unsupported format.')
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    if PUBLIC.exists() and PUBLIC.read_bytes() != public:
        raise RuntimeError('Signing key does not match pinned public key; no trust rotation performed.')
    return key


def sign(data):
    return load_key().sign(data, padding.PKCS1v15(), hashes.SHA256())


def initialize():
    key = load_key(initialize=True)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    PUBLIC.parent.mkdir(parents=True, exist_ok=True)
    if not PUBLIC.exists():
        PUBLIC.write_bytes(public)
    header = ROOT/'include/UpdatePublicKey.h'
    text = '#pragma once\n#include <pgmspace.h>\nstatic const char PDKS_UPDATE_PUBLIC_KEY[] PROGMEM = R"KEY(' + public.decode('ascii') + ')KEY";\n'
    if header.exists() and header.read_text(encoding='utf-8') != text:
        raise RuntimeError('Existing public firmware key differs; refusing overwrite.')
    if not header.exists():
        header.write_text(text, encoding='utf-8')
    print('Public trust-key SHA256:', hashlib.sha256(public).hexdigest())
    print('Private key: protected with Windows DPAPI outside the project; never print/upload it.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--init-key', action='store_true', required=True)
    parser.parse_args()
    initialize()
