"""S7CommPlus connection probe for S7 Plus troubleshooting.

Tries every combination of TLS and connection role against a controller and
writes a detailed log, so you can see where the controller drops the session.

Usage (on any PC that can reach the PLC):

    pip install s7commplus==0.2.0
    python s7probe.py 10.0.20.71 [--password SECRET] [--browse]

Send the printed summary and the file s7probe.log to the integration author.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from s7commplus import AsyncClient


async def attempt(host: str, port: int, use_tls: bool, role: str, password: str | None,
                  browse: bool) -> str:
    client = AsyncClient()
    try:
        await asyncio.wait_for(
            client.connect(host, port, use_tls=use_tls, connection_type=role,
                           password=password or None),
            timeout=20,
        )
    except Exception as err:  # noqa: BLE001
        return f"FAILED to connect: {type(err).__name__}: {err}"
    try:
        details = [f"OK  protocol V{client.protocol_version}", f"TLS active={client.tls_active}"]
        try:
            details.append(f"protection level={client.protection_level}")
        except Exception:  # noqa: BLE001
            pass
        try:
            details.append(f"CPU={await asyncio.wait_for(client.get_cpu_state(), 10)}")
        except Exception as err:  # noqa: BLE001
            details.append(f"CPU state not available ({type(err).__name__})")
        if browse:
            try:
                tags = await asyncio.wait_for(client.browse(), 180)
                details.append(f"browse: {len(tags)} tags, e.g. {[t['name'] for t in tags[:5]]}")
            except Exception as err:  # noqa: BLE001
                details.append(f"browse FAILED: {type(err).__name__}: {err}")
        return ", ".join(details)
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), 5)
        except Exception:  # noqa: BLE001
            pass


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("host")
    parser.add_argument("--port", type=int, default=102)
    parser.add_argument("--password")
    parser.add_argument("--browse", action="store_true", help="also read the tag catalog")
    args = parser.parse_args()

    logging.basicConfig(
        filename="s7probe.log", level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ok = False
    for use_tls in (False, True):
        for role in ("hmi", "es", "pg"):
            logging.info("==== TLS=%s role=%s ====", use_tls, role)
            result = await attempt(args.host, args.port, use_tls, role, args.password, args.browse)
            ok |= result.startswith("OK")
            print(f"TLS={'on ' if use_tls else 'off'} role={role:3}  {result}")
    print("\nDetailed log: s7probe.log")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
