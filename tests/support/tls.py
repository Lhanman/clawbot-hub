"""测试用的 TLS：用本机的 `openssl` 命令现生成一张自签证书（只在测试里用，不进 git）。

腾讯的接口是 https，"发出去了没有"的分界在加密连接和代理隧道上有自己的路径（见
`upstream/http.py` 的 `_MarksTheRequest`），只测明文 HTTP 测不到。证书里写了本机回环地址和
`upstream.example`：后者让"经 CONNECT 代理连一个并不存在的域名"的测试也能通过主机名校验。
"""

from __future__ import annotations

import shutil
import ssl
import subprocess
from pathlib import Path

HOSTNAME = "upstream.example"

_CONFIG = f"""\
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no

[dn]
CN = {HOSTNAME}

[v3]
basicConstraints = critical,CA:TRUE
keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign
subjectAltName = IP:127.0.0.1,DNS:localhost,DNS:{HOSTNAME}
"""


def openssl_available() -> bool:
    return shutil.which("openssl") is not None


def make_certificate(directory: Path) -> tuple[Path, Path]:
    """在 `directory` 里生成证书和私钥，返回 `(证书路径, 私钥路径)`。"""
    config = directory / "openssl.cnf"
    config.write_text(_CONFIG)
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-config",
            str(config),
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    return context
