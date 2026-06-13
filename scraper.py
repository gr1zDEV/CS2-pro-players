import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import psycopg2
import requests


DATABASE_URL = os.environ["DATABASE_URL"]
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "contact@example.com")
MAJOR_NAME = "IEM Cologne Major 2026"

HEADERS = {
    "User-Agent": f"cs2you-major-roster-test/0.1 ({CONTACT_EMAIL})",
    "Accept-Encoding": "gzip",
}


def title_to_url_path(title: str) -> str:
    return quote(title.replace(" ", "_"), safe="")


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


def load_major_teams() -> list[dict]:
    rows = []

    with open("major_teams.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            parts = line.split("|")

            if len(parts) != 3:
                print(f"Skipping bad line: {line}")
                continue

            team_title = parts[0].strip()
            major_stage = parts[1].strip()
            player_titles = [
                player.strip()
                for player in parts[2].split(",")
                if player.strip()
            ]

            rows.append({
                "team_title": team_title,
                "major_stage": major_stage,
                "player_titles": player_titles,
            })

    return rows


def upsert_team(conn, team_title: str, major_stage: str) -> int:
    team = {
        "liquipedia_title": team_title,
        "name": team_title,
        "major_stage": major_stage,
        "major_name": MAJOR_NAME,
        "source_url": f"https://liquipedia.net/counterstrike/{title_to_url_path(team_title)}",
        "last_checked": datetime.now(timezone.utc),
    }

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.teams (
                liquipedia_title,
                name,
                major_stage,
                major_name,
                source_url,
                last_checked
            )
            VALUES (
                %(liquipedia_title)s,
                %(name)s,
                %(major_stage)s,
                %(major_name)s,
                %(source_url)s,
                %(last_checked)s
            )
            ON CONFLICT (liquipedia_title)
            DO UPDATE SET
                name = EXCLUDED.name,
                major_stage = EXCLUDED.major_stage,
                major_name = EXCLUDED.major_name,
                source_url = EXCLUDED.source_url,
                last_checked = EXCLUDED.last_checked
            RETURNING id;
            """,
            team,
        )

        return cur.fetchone()[0]


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


def upsert_membership(conn, player_id: int, team_id: int, source: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.player_team_memberships (
                player_id,
                team_id,
                is_current,
                source,
                last_checked
            )
            VALUES (%s, %s, true, %s, %s)
            ON CONFLICT (player_id, team_id)
            DO UPDATE SET
                is_current = true,
                source = EXCLUDED.source,
                last_checked = EXCLUDED.last_checked;
            """,
            (
                player_id,
                team_id,
                source,
                datetime.now(timezone.utc),
            ),
        )


def main():
    print("Starting major roster scrape...")

    rows = load_major_teams()
    print(f"Loaded {len(rows)} teams from major_teams.txt")

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")

    try:
        for row in rows:
            team_title = row["team_title"]
            major_stage = row["major_stage"]
            player_titles = row["player_titles"]

            print(f"\nProcessing team: {team_title} | {major_stage}")

            try:
                team_id = upsert_team(conn, team_title, major_stage)
                conn.commit()
            except Exception as team_error:
                conn.rollback()
                print(f"Failed team {team_title}: {team_error}")
                continue

            for player_title in player_titles:
                try:
                    print(f"Fetching player: {player_title}")

                    raw_text = fetch_raw(player_title)
                    player = parse_player(player_title, raw_text)

                    if not player["alias"] and not player["steam64"]:
                        print(f"Skipping non-player page: {player_title}")
                        continue

                    player_id = upsert_player(conn, player)
                    upsert_membership(conn, player_id, team_id, "major_teams.txt")
                    conn.commit()

                    print(
                        f"Saved: {team_title} | "
                        f"{major_stage} | "
                        f"{player['alias']} | "
                        f"{player['steam64']}"
                    )

                except Exception as player_error:
                    conn.rollback()
                    print(f"Failed player {player_title}: {player_error}")

                time.sleep(2.5)

    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()
