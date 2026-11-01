"""Shared public update protocol. Only a verified publisher signature authorises bytes."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

CURRENT_VERSION = '1.5.4'
CURRENT_SEQUENCE = 10504
REPOSITORY = 'kaanekr/pdks-yonetim'
FIRMWARE_TARGET = 'nodemcuv2_sd_soft_tx'
PUBLIC_KEY = Path(__file__).resolve().parent/'assets/update_public_key.pem'
MANIFEST_NAME = 'update-manifest.json'
SIGNATURE_NAME = 'update-manifest.sig'
WINDOWS_NAME = 'PDKS_Yonetim.exe'
FIRMWARE_NAME = 'PDKS_Firmware_TX_SD.signed.bin'


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class ReleaseInfo:
    manifest: dict
    raw: bytes
    signature: bytes


def version_sequence(version):
    if not isinstance(version, str) or not re.fullmatch(r'\d{1,2}\.\d{1,2}\.\d{1,2}', version):
        raise UpdateError('Sürüm biçimi geçersiz.')
    major, minor, patch = map(int, version.split('.'))
    if version != f'{major}.{minor}.{patch}':
        raise UpdateError('Sürüm biçimi geçersiz.')
    return major*10000 + minor*100 + patch


def verify_signature(data, signature, public_key=None):
    try:
        key = serialization.load_pem_public_key(public_key or PUBLIC_KEY.read_bytes())
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size != 2048 or len(signature) != 256:
            raise ValueError()
        key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError, TypeError, OSError):
        raise UpdateError('Güncellemenin yayıncı imzası doğrulanamadı; hiçbir şey yüklenmedi.') from None


def firmware_envelope(artifact):
    version, sequence = artifact['version'], artifact['sequence']
    if sequence != version_sequence(version) or artifact['target'] != FIRMWARE_TARGET:
        raise UpdateError('Firmware sürümü/hedefi uyuşmuyor.')
    size, digest = artifact['size'], artifact['sha256']
    if type(size) is not int or not 260 < size <= 1024*1024 or not re.fullmatch('[0-9a-f]{64}', digest):
        raise UpdateError('Firmware boyutu/özeti geçersiz.')
    return f'PDKS-FW1\n{FIRMWARE_TARGET}\n{version}\n{sequence}\n{size}\n{digest}\n'.encode('ascii')


def verify_artifact(path, artifact):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size != artifact['size']:
        raise UpdateError('Güncelleme dosyasının boyutu uyuşmuyor.')
    digest = hashlib.sha256()
    with path.open('rb') as file:
        while chunk := file.read(1024*1024):
            digest.update(chunk)
    if digest.hexdigest() != artifact['sha256']:
        raise UpdateError('Güncelleme dosyasının SHA256 özeti uyuşmuyor.')
    return path
