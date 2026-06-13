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
    "User-Agent": f"cs2you-team-roster-test/0.1 ({CONTACT_EMAIL})",
    "Accept-Encoding": "gzip",
}

SKIP_PLAYER_TITLES = {
    "Counter-Strike",
    "Counter-Strike 2",
    "Counter-Strike: Global Offensive",
    "Portal:Teams",
    "Portal:Players",
}


def load_lines(filename: str) -> list[str]:
    with open(filename, "r", encoding="utf-8") as f:
        return [
            line.strip()
            for line in f.readlines()
            if line.strip() and not line.strip().startswith("#")
        ]


def title_to_url_path(title: str) -> str:
    return quote(title.replace(" ", "_"), safe="")


def fetch_raw(title: str) -> str:
    path = title_to_url_path(title)
    url = f"https://liquipedia.net/counterstrike/{path}?action=raw"

    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    return response.text


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


def extract_infobox_player_block(text: str) -> str:
    start = text.find("{{Infobox player")
    if start == -1:
        return text

    # Good enough for the simple top infobox fields.
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


def extract_team_roster_section(raw_text: str) -> str:
    markers = [
        "==Player Roster==",
        "==Current Roster==",
        "==Roster==",
        "==Active==",
    ]

    lower_text = raw_text.lower()

    best_index = -1
    for marker in markers:
        idx = lower_text.find(marker.lower())
        if idx != -1:
            best_index = idx
            break

    if best_index == -1:
        # Fallback: team pages often keep roster templates in the top half.
        return raw_text[:8000]

    next_section = raw_text.find("\n==", best_index + 5)
    if next_section == -1:
        return raw_text[best_index:best_index + 8000]

    return raw_text[best_index:next_section]


def extract_player_titles_from_team(raw_text: str) -> list[str]:
    section = extract_team_roster_section(raw_text)

    titles = set()

    # Match normal wiki links: [[ZywOo]], [[Ropz|ropz]]
    for match in re.finditer(r"\[\[([^|\]#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", section):
        title = match.group(1).strip()

        if not title:
            continue

        if title.startswith("File:") or title.startswith("Category:"):
            continue

        if title in SKIP_PLAYER_TITLES:
            continue

        # Skip obvious team/event/admin pages.
        bad_words = [
            "Tournament",
            "Matches",
            "Results",
            "Statistics",
            "Team",
            "Roster",
            "Standings",
            "Portal",
            "Help:",
            "Template:",
        ]
        if any(word in title for word in bad_words):
            continue

        titles.add(title)

    return sorted(titles)


def upsert_team(conn, team_title: str) -> int:
    team = {
        "liquipedia_title": team_title,
        "name": team_title,
        "source_url": f"https://liquipedia.net/counterstrike/{title_to_url_path(team_title)}",
        "last_checked": datetime.now(timezone.utc),
    }

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.teams (
                liquipedia_title,
                name,
                source_url,
                last_checked
            )
            VALUES (
                %(liquipedia_title)s,
                %(name)s,
                %(source_url)s,
                %(last_checked)s
            )
            ON CONFLICT (liquipedia_title)
            DO UPDATE SET
                name = EXCLUDED.name,
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
            (player_id, team_id, source, datetime.now(timezone.utc)),
        )


def main():
    print("Starting team roster scrape...")

    teams = load_lines("teams.txt")
    print(f"Loaded {len(teams)} teams from teams.txt")

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")

    try:
        for team_title in teams:
            print(f"\nFetching team: {team_title}")

            try:
                team_raw = fetch_raw(team_title)
                team_id = upsert_team(conn, team_title)

                player_titles = extract_player_titles_from_team(team_raw)

                print(f"Found player candidates for {team_title}: {player_titles}")

                for player_title in player_titles:
                    try:
                        print(f"Fetching player: {player_title}")

                        player_raw = fetch_raw(player_title)
                        player = parse_player(player_title, player_raw)

                        # Avoid saving pages that are not real player pages.
                        if not player["alias"] and not player["steam64"]:
                            print(f"Skipping non-player page: {player_title}")
                            continue

                        player_id = upsert_player(conn, player)
                        upsert_membership(conn, player_id, team_id, team_title)

                        conn.commit()

                        print(
                            f"Saved {player['alias']} | "
                            f"{team_title} | "
                            f"{player['steam64']}"
                        )

                    except Exception as player_error:
                        conn.rollback()
                        print(f"Failed player {player_title}: {player_error}")

                    time.sleep(2.5)

            except Exception as team_error:
                conn.rollback()
                print(f"Failed team {team_title}: {team_error}")

            time.sleep(2.5)

    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()
