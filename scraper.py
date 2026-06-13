import os
import re
import time
from datetime import datetime, timezone

import psycopg2
import requests


PLAYERS = [
    "ZywOo",
    "S1mple",
    "Donk",
]

DATABASE_URL = os.environ["DATABASE_URL"]
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "contact@example.com")

HEADERS = {
    "User-Agent": f"cs2you-player-test/0.1 ({CONTACT_EMAIL})",
    "Accept-Encoding": "gzip",
}


def clean_value(value: str) -> str:
    return value.strip().replace("'''", "").strip()


def extract_field(text: str, field: str) -> str:
    pattern = rf"^\|{re.escape(field)}\s*=\s*(.*)$"
    match = re.search(pattern, text, re.MULTILINE)
    return clean_value(match.group(1)) if match else ""


def fetch_player_raw(title: str) -> str:
    url = f"https://liquipedia.net/counterstrike/{title}?action=raw"

    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    return response.text


def parse_player(title: str, raw_text: str) -> dict:
    return {
        "liquipedia_title": title,
        "alias": extract_field(raw_text, "id"),
        "real_name": extract_field(raw_text, "name"),
        "team": extract_field(raw_text, "team"),
        "country": extract_field(raw_text, "country"),
        "status": extract_field(raw_text, "status"),
        "roles": extract_field(raw_text, "roles"),
        "steam64": extract_field(raw_text, "steam64ID"),
        "faceitdb": extract_field(raw_text, "faceitdb"),
        "esea": extract_field(raw_text, "esea"),
        "source_url": f"https://liquipedia.net/counterstrike/{title}",
        "last_checked": datetime.now(timezone.utc),
    }


def upsert_player(conn, player: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.cs_players (
                liquipedia_title,
                alias,
                real_name,
                team,
                country,
                status,
                roles,
                steam64,
                faceitdb,
                esea,
                source_url,
                last_checked
            )
            VALUES (
                %(liquipedia_title)s,
                %(alias)s,
                %(real_name)s,
                %(team)s,
                %(country)s,
                %(status)s,
                %(roles)s,
                %(steam64)s,
                %(faceitdb)s,
                %(esea)s,
                %(source_url)s,
                %(last_checked)s
            )
            ON CONFLICT (liquipedia_title)
            DO UPDATE SET
                alias = EXCLUDED.alias,
                real_name = EXCLUDED.real_name,
                team = EXCLUDED.team,
                country = EXCLUDED.country,
                status = EXCLUDED.status,
                roles = EXCLUDED.roles,
                steam64 = EXCLUDED.steam64,
                faceitdb = EXCLUDED.faceitdb,
                esea = EXCLUDED.esea,
                source_url = EXCLUDED.source_url,
                last_checked = EXCLUDED.last_checked;
            """,
            player,
        )


def main():
    print("Starting Liquipedia test scrape...")

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")

    try:
        for title in PLAYERS:
            print(f"Fetching {title}...")

            raw_text = fetch_player_raw(title)
            player = parse_player(title, raw_text)

            print(
                f"Parsed: {player['alias']} | "
                f"{player['real_name']} | "
                f"{player['team']} | "
                f"{player['steam64']}"
            )

            upsert_player(conn, player)
            conn.commit()

            print(f"Saved {title}")

            time.sleep(2.5)

    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()
