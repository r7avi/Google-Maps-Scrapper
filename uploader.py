"""Optional SFTP upload of finished workbooks.

Configuration comes from the environment, never from source:

    GMAPS_SFTP_HOST        required, e.g. sftp.example.com
    GMAPS_SFTP_PORT        default 22
    GMAPS_SFTP_USER        required
    GMAPS_SFTP_KEY         path to a private key (preferred)
    GMAPS_SFTP_KEY_PASS    passphrase for the key, if any
    GMAPS_SFTP_PASSWORD    password auth (used only when no key is given)
    GMAPS_SFTP_REMOTE_DIR  default /home/scraper-output
    GMAPS_SFTP_KNOWN_HOSTS default ~/.ssh/known_hosts

The previous implementation hardcoded the host, username and password as module
constants and used ``AutoAddPolicy``, which accepts any host key presented and
therefore offers no protection against an intercepted connection. Host keys are
now verified against a known_hosts file; an unrecognised host is refused.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_REMOTE_DIR = "/home/scraper-output"


class UploadError(Exception):
    """Raised when the upload cannot be performed."""


def _known_hosts_path() -> Path:
    configured = os.environ.get("GMAPS_SFTP_KNOWN_HOSTS")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".ssh" / "known_hosts"


def upload_file(local_path: Path) -> bool:
    """Upload one file over SFTP. Returns True on success.

    Failures are logged and reported, never raised into the scraping loop -- a
    broken upload should not discard results that were scraped successfully.
    """
    try:
        _upload(local_path)
        return True
    except UploadError as exc:
        logger.error("Upload of %s failed: %s", local_path, exc)
    except Exception as exc:
        logger.error("Unexpected error uploading %s: %s", local_path, exc)
    return False


def _upload(local_path: Path) -> None:
    try:
        # Imported lazily so paramiko is only required when uploads are enabled.
        import paramiko
    except ImportError as exc:
        raise UploadError(
            "paramiko is not installed. Run: pip install -r requirements-upload.txt"
        ) from exc

    host = os.environ.get("GMAPS_SFTP_HOST", "").strip()
    user = os.environ.get("GMAPS_SFTP_USER", "").strip()
    if not host or not user:
        raise UploadError("GMAPS_SFTP_HOST and GMAPS_SFTP_USER must both be set.")

    if not local_path.exists():
        raise UploadError(f"{local_path} does not exist.")

    port = int(os.environ.get("GMAPS_SFTP_PORT", "22"))
    key_path = os.environ.get("GMAPS_SFTP_KEY", "").strip()
    password = os.environ.get("GMAPS_SFTP_PASSWORD") or None
    remote_dir = os.environ.get("GMAPS_SFTP_REMOTE_DIR", DEFAULT_REMOTE_DIR).rstrip("/")

    if not key_path and not password:
        raise UploadError(
            "Provide GMAPS_SFTP_KEY (preferred) or GMAPS_SFTP_PASSWORD."
        )

    known_hosts = _known_hosts_path()
    if not known_hosts.exists():
        raise UploadError(
            f"Host key file {known_hosts} not found. Add the server's key with "
            f"'ssh-keyscan -H {host} >> {known_hosts}' after verifying the "
            "fingerprint out of band, or point GMAPS_SFTP_KNOWN_HOSTS at it."
        )

    client = paramiko.SSHClient()
    client.load_host_keys(str(known_hosts))
    # Refuse unknown hosts rather than trusting them on first sight.
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    connect_kwargs: dict = {
        "hostname": host,
        "port": port,
        "username": user,
        "timeout": 30,
        "allow_agent": True,
    }
    if key_path:
        connect_kwargs["key_filename"] = str(Path(key_path).expanduser())
        key_pass = os.environ.get("GMAPS_SFTP_KEY_PASS")
        if key_pass:
            connect_kwargs["passphrase"] = key_pass
    else:
        connect_kwargs["password"] = password
        connect_kwargs["look_for_keys"] = False

    try:
        client.connect(**connect_kwargs)
    except paramiko.SSHException as exc:
        raise UploadError(
            f"Could not establish a verified connection to {host}: {exc}"
        ) from exc

    try:
        sftp = client.open_sftp()
        try:
            _makedirs(sftp, remote_dir)
            remote_path = f"{remote_dir}/{local_path.name}"
            sftp.put(str(local_path), remote_path)
            logger.info("Uploaded %s to %s", local_path.name, remote_path)
        finally:
            sftp.close()
    finally:
        client.close()


def _makedirs(sftp, remote_dir: str) -> None:
    """Create ``remote_dir`` and any missing parents over SFTP.

    Done through SFTP calls rather than an interpolated ``mkdir -p`` shell
    command, so a directory name can never be treated as shell syntax.
    """
    parts = [part for part in remote_dir.split("/") if part]
    current = ""
    for part in parts:
        current = f"{current}/{part}"
        try:
            sftp.stat(current)
        except IOError:
            sftp.mkdir(current)
