"""用数据目录中的独立主密钥加密服务商凭据，数据库和审计记录只接触密文。"""

import os
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from mycomfy.database.prompt_models import PromptProvider
from mycomfy.errors import ApiError


def _cipher(data_root, *, initialize=False):
    """仅在首次保存凭据时创建 0600 主密钥，始终从已完整发布的文件读取。"""
    path = Path(data_root) / "prompt-providers.key"
    try:
        if initialize and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再用硬链接发布。并发首次配置只会采用最先发布的一把密钥。
            with NamedTemporaryFile(dir=path.parent) as draft:
                draft.write(Fernet.generate_key())
                draft.flush()
                os.fsync(draft.fileno())
                try:
                    os.link(draft.name, path)
                except FileExistsError:
                    pass
                descriptor = os.open(path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise ValueError
            key = stream.read(45)
            if len(key) != 44:
                raise ValueError
            return Fernet(key)
    except (OSError, ValueError):
        raise ApiError(
            503, "PROMPT_PROVIDER_SECRET_UNAVAILABLE", "Provider key storage is unavailable"
        ) from None


def encrypt_key(db, data_root, plaintext):
    """加密新提供的 API key。库内已有密文时，缺失的主密钥须通过备份恢复。"""
    existing = db.scalar(
        select(PromptProvider.id).where(PromptProvider.encrypted_api_key.is_not(None)).limit(1)
    )
    cipher = _cipher(data_root, initialize=existing is None)
    return cipher.encrypt(plaintext.encode("ascii")).decode("ascii")


def decrypt_key(data_root, ciphertext):
    """仅在服务端调用上游时解密，损坏或不匹配的密钥返回固定错误。"""
    try:
        return _cipher(data_root).decrypt(ciphertext).decode("ascii")
    except (InvalidToken, UnicodeError):
        raise ApiError(
            503, "PROMPT_PROVIDER_SECRET_UNAVAILABLE", "Provider key storage is unavailable"
        ) from None
