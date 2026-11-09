"""Register elysium.warga-digital.com in OpenLiteSpeed.

Idempotent: adds the listener maps (Default / SSL / SSL IPv6) and the virtualHost
block, backs the config up first. Run with sudo on the host.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

CONF = Path("/usr/local/lsws/conf/httpd_config.conf")
NAME = "elysium.warga-digital.com"
LISTENERS = {"Default", "SSL", "SSL IPv6"}


def main() -> int:
    text = CONF.read_text(encoding="utf-8")
    if f"virtualHost {NAME}" in text and text.count(f"map {NAME}") >= 2:
        print("already registered")
        return 0

    backup = CONF.with_name(f"{CONF.name}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CONF, backup)

    lines = text.splitlines()
    out: list[str] = []
    current: str | None = None
    mapped: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("listener ") and stripped.endswith("{"):
            current = stripped[len("listener ") : -1].strip()
        if stripped.startswith("address") and current in LISTENERS and current not in mapped:
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}map                     {NAME} {NAME}")
            mapped.add(current)
        out.append(line)

    text = "\n".join(out) + "\n"
    if f"virtualHost {NAME}" not in text:
        text += (
            f"\nvirtualHost {NAME} {{\n"
            f"  vhRoot                  /usr/local/lsws/conf/vhosts/{NAME}\n"
            f"  configFile              $SERVER_ROOT/conf/vhosts/{NAME}/vhost.conf\n"
            f"  allowSymbolLink         0\n"
            f"  enableScript            0\n"
            f"  restrained              1\n"
            f"}}\n"
        )

    CONF.write_text(text, encoding="utf-8")
    print(f"patched listeners={sorted(mapped)} backup={backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
