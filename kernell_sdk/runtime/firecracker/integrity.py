"""
Kernell OS SDK — Supply Chain Integrity Verifier
══════════════════════════════════════════════════
Verifies SHA-256 hashes of critical runtime artifacts at boot time.
If ANY artifact fails verification, the node refuses to start (fail-close).

Usage:
    from kernell_sdk.runtime.firecracker.integrity import verify_artifacts

    manifest = {
        "/opt/kernell/vmlinux": "sha256:abc123...",
        "/opt/kernell/rootfs.ext4": "sha256:def456...",
    }
    verify_artifacts(manifest)  # raises IntegrityError on mismatch
"""
import hashlib
import os
import logging

logger = logging.getLogger("kernell.integrity")


class IntegrityError(Exception):
    """Raised when a runtime artifact fails hash verification."""
    pass


def sha256_file(path: str, chunk_size: int = 65536) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def verify_artifacts(manifest: dict[str, str]) -> None:
    """
    Verify all artifacts in the manifest against their expected hashes.
    
    Args:
        manifest: Dict mapping absolute file paths to expected "sha256:<hex>" strings.
        
    Raises:
        IntegrityError: If any file is missing or its hash doesn't match.
        FileNotFoundError: If a manifest file doesn't exist.
    """
    for path, expected_hash in manifest.items():
        if not os.path.exists(path):
            raise IntegrityError(
                f"SUPPLY_CHAIN_VIOLATION: Artifact missing: {path}"
            )

        actual_hash = sha256_file(path)

        if actual_hash != expected_hash:
            raise IntegrityError(
                f"SUPPLY_CHAIN_VIOLATION: Hash mismatch for {path}. "
                f"Expected {expected_hash}, got {actual_hash}. "
                f"The artifact may have been tampered with."
            )

        logger.info("artifact_verified", path=path, hash=actual_hash[:32])

    logger.info("all_artifacts_verified", count=len(manifest))


def generate_manifest(*paths: str) -> dict[str, str]:
    """
    Helper to generate a manifest from a list of file paths.
    Run this once after building your artifacts, then pin the output.
    """
    manifest = {}
    for path in paths:
        manifest[path] = sha256_file(path)
    return manifest
