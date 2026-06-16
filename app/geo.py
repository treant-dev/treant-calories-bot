"""Resolve coordinates to an IANA timezone via a keyless API (keeps the package light)."""
import httpx


def timezone_for(latitude, longitude):
    """Return an IANA timezone name (e.g. 'Europe/Stockholm') for the coordinates."""
    resp = httpx.get(
        "https://timeapi.io/api/timezone/coordinate",
        params={"latitude": latitude, "longitude": longitude},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["timeZone"]
