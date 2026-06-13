import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import psycopg2
import requests


DATABASE_URL = os.environ["DATABASE_URL"]
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "contact@example.com")

HEADERS = {
    "User-Agent": f"cs2you-player-list-test/0.1 ({CONTACT_EMAIL})",
    "Accept-Encoding": "gzip",
}


def title_to_url_path(title: str) -> str:
    return quote(title.replace(" ", "_"), safe="")


def load_players() -> list[str]:
    with open("players.txt", "r", encoding="utf-8") as f:
        return [
            line.strip()
            for line in f.readlines()
            if line.strip() and not line.strip().startswith("#")
        ]


def clean_value(value: str) -> str:
    value = value.strip()
    value = value.replace("'''", "")
    value = re.sub(r"\[\[(?:[^|\]]+\|)?([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"<.*?>", "", value)
    return value.strip()


def extract_field(text: str, field: str) -> str:
    pattern = rf"^\|{re.escape(field)}\s*=\s*(.*)$"
    match = re.search(pattern, text, re.MULTILINE)
    return clean_value(match.group(1)) if match else ""


def fetch_raw(title: str) -> str:
    url = f"https://liquipedia.net/counterstrike/{title_to_url_path(title)}?action=raw"
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def extract_infobox_player_block(text: str) -> str:
    start = text.find("{{Infobox player")
    if start == -1:
        return text

    end = text.find("|team_history=", start)
    if end != -1:
        return text[start:end]

    return text[start:start + 3000]


def parse_player(title: str, raw_text: str) -> dict:
    block = extract_infobox_player_block(raw_text)

    return {
        "liquipedia_title": title,
        "alias": extract_field(block, "id"),
        "real_name": extract_field(block, "name"),
        "country": extract_field(block, "country"),
        "status": extract_field(block, "status"),
        "roles": extract_field(block, "roles"),
        "steam64": extract_field(block, "steam64ID"),
        "faceitdb": extract_field(block, "faceitdb"),
        "esea": extract_field(block, "esea"),
        "source_url": f"https://liquipedia.net/counterstrike/{title_to_url_path(title)}",
        "last_checked": datetime.now(timezone.utc),
    }


def upsert_player(conn, player: dict) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.players (
                liquipedia_title,
                alias,
                real_name,
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
                country = EXCLUDED.country,
                status = EXCLUDED.status,
                roles = EXCLUDED.roles,
                steam64 = EXCLUDED.steam64,
                faceitdb = EXCLUDED.faceitdb,
                esea = EXCLUDED.esea,
                source_url = EXCLUDED.source_url,
                last_checked = EXCLUDED.last_checked
            RETURNING id;
            """,
            player,
        )

        return cur.fetchone()[0]


def main():
    print("Starting players.txt scrape...")

    players = load_players()
    print(f"Loaded {len(players)} players from players.txt")

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")

    try:
        for title in players:
            try:
                print(f"Fetching player: {title}")

                raw_text = fetch_raw(title)
                player = parse_player(title, raw_text)

                if not player["alias"] and not player["steam64"]:
                    print(f"Skipping non-player page: {title}")
                    continue

                upsert_player(conn, player)
                conn.commit()

                print(
                    f"Saved: {player['alias']} | "
                    f"{player['real_name']} | "
                    f"{player['steam64']}"
                )

            except Exception as e:
                conn.rollback()
                print(f"Failed {title}: {e}")

            time.sleep(2.5)

    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()
