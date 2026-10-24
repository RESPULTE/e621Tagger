"""Reserve the loop image physically and prevent trim from punching its holes."""

import os
import shutil
import subprocess
from pathlib import Path


def main():
    image = Path("/var/lib/homelab-monitor/telemetry.ext4")
    storage = Path("/srv/observability")
    if image.is_symlink() or not storage.is_mount():
        raise RuntimeError("Managed telemetry mount is unavailable")
    target = 24 * 1024**3
    if image.stat().st_size != target:
        raise RuntimeError("Managed telemetry image must be exactly 24 GiB; refusing resize")
    source = subprocess.check_output(["findmnt", "-n", "-o", "SOURCE", "--target", str(storage)], text=True).strip()
    if not source.startswith("/dev/loop") or not source[9:].isdigit():
        raise RuntimeError("Expected a managed loop device")
    backing = subprocess.check_output(["losetup", "-n", "-O", "BACK-FILE", source], text=True).strip()
    if Path(backing).resolve() != image.resolve():
        raise RuntimeError("Loop device backing file does not match telemetry image")
    queue = Path("/sys/block") / Path(source).name / "queue/discard_max_bytes"
    queue.write_text("0\n")
    if queue.read_text().strip() != "0":
        raise RuntimeError("Could not disable discard for monitoring loop device")
    metadata = image.stat()
    deficit = max(0, target - metadata.st_blocks * 512)
    if shutil.disk_usage("/").free - deficit < 40 * 1024**3:
        raise RuntimeError("Insufficient root reserve to allocate telemetry image")
    subprocess.run(["fallocate", "-l", str(target), str(image)], check=True)
    with image.open("rb") as file:
        os.fsync(file.fileno())
    if image.stat().st_blocks * 512 < target:
        raise RuntimeError("Telemetry image is still sparse")


if __name__ == "__main__":
    main()
