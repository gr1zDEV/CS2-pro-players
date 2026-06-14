import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import mwparserfromhell
import psycopg2
import requests


DATABASE_URL = os.environ["DATABASE_URL"]
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "contact@example.com")

HEADERS = {
    "User-Agent": f"cs2you-tournament-scraper/0.1 ({CONTACT_EMAIL})",
    "Accept-Encoding": "gzip",
}


STAGE_MAP = {
    "Stage 3 Invites": "Legends",
    "Stage 2 Invites": "Challengers",
    "Stage 1 Invites": "Contenders",
}


def title_to_url_path(title: str) -> str:
    return quote(title.replace(" ", "_"), safe="/")


def load_tournaments() -> list[str]:
    with open("tournaments.txt", "r", encoding="utf-8") as f:
        return [
            line.strip()
            for line in f.readlines()
            if line.strip() and not line.strip().startswith("#")
        ]


def fetch_raw(title: str) -> str:
    url = f"https://liquipedia.net/counterstrike/{title_to_url_path(title)}?action=raw"
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def clean_wiki_value(value: str) -> str:
    value = str(value).strip()
    value = value.replace("'''", "")
    value = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL)
    value = re.sub(r"\[\[(?:[^|\]]+\|)?([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\{\{flag\|.*?\}\}", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\{\{.*?\}\}", "", value)
    value = re.sub(r"<.*?>", "", value)
    return value.strip()


def get_param(template, names: list[str]) -> str:
    for name in names:
        if template.has(name):
            return clean_wiki_value(template.get(name).value)
    return ""


def extract_stage_section(raw_text: str, stage_title: str) -> str:
    start_patterns = [
        f"==={stage_title}===",
        f"=== {stage_title} ===",
        f"===={stage_title}====",
        f"==== {stage_title} ====",
    ]

    start = -1
    for pattern in start_patterns:
        start = raw_text.find(pattern)
        if start != -1:
            break

    if start == -1:
        return ""

    next_stage_positions = []
    for other_stage in STAGE_MAP.keys():
        if other_stage == stage_title:
            continue

        for pattern in [
            f"==={other_stage}===",
            f"=== {other_stage} ===",
            f"===={other_stage}====",
            f"==== {other_stage} ====",
        ]:
            pos = raw_text.find(pattern, start + 1)
            if pos != -1:
                next_stage_positions.append(pos)

    # Stop before Results / next major section if no next invite section.
    for pattern in ["==Results==", "== Results ==", "## Results"]:
        pos = raw_text.find(pattern, start + 1)
        if pos != -1:
            next_stage_positions.append(pos)

    end = min(next_stage_positions) if next_stage_positions else len(raw_text)
    return raw_text[start:end]


def extract_rosters_from_section(section_text: str) -> list[dict]:
    """
    Looks for Liquipedia roster/team templates that contain p1-p5/player1-player5.
    Returns:
      [{"team": "Team Vitality", "players": ["apEX", "ZywOo", ...]}]
    """
    rosters = []
    wikicode = mwparserfromhell.parse(section_text)

    for template in wikicode.filter_templates(recursive=True):
        param_names = {str(param.name).strip().lower() for param in template.params}

        has_players = any(name in param_names for name in ["p1", "player1", "p1link", "player1link"])
        if not has_players:
            continue

        team = get_param(template, ["team", "teamname", "name", "team1"])
        if not team:
            continue

        players = []
        for i in range(1, 6):
            player = (
                get_param(template, [f"p{i}link", f"player{i}link"])
                or get_param(template, [f"p{i}", f"player{i}"])
            )

            if player:
                players.append(player)

        if team and len(players) >= 3:
            rosters.append({
                "team": team,
                "players": players[:5],
            })

    return rosters


def upsert_tournament(conn, title: str) -> int:
    data = {
        "liquipedia_title": title,
        "name": title.replace("_", " ").replace("/", " / "),
        "source_url": f"https://liquipedia.net/counterstrike/{title_to_url_path(title)}",
        "last_checked": datetime.now(timezone.utc),
    }

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.tournaments (
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
            data,
        )
        return cur.fetchone()[0]


def upsert_team(conn, team_title: str, major_stage: str, major_name: str) -> int:
    data = {
        "liquipedia_title": team_title,
        "name": team_title,
        "major_stage": major_stage,
        "major_name": major_name,
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
            data,
        )
        return cur.fetchone()[0]


def find_player_id(conn, player_title: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM public.players
            WHERE lower(liquipedia_title) = lower(%s)
               OR lower(alias) = lower(%s)
            LIMIT 1;
            """,
            (player_title, player_title),
        )
        row = cur.fetchone()
        return row[0] if row else None


def upsert_player_team_membership(conn, player_id: int, team_id: int, source: str) -> None:
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


def upsert_tournament_team(conn, tournament_id: int, team_id: int, stage: str, classification: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.tournament_teams (
                tournament_id,
                team_id,
                stage,
                classification,
                last_checked
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (tournament_id, team_id)
            DO UPDATE SET
                stage = EXCLUDED.stage,
                classification = EXCLUDED.classification,
                last_checked = EXCLUDED.last_checked;
            """,
            (tournament_id, team_id, stage, classification, datetime.now(timezone.utc)),
        )


def main():
    print("Starting tournament affiliation scrape...")

    tournaments = load_tournaments()
    print(f"Loaded {len(tournaments)} tournaments")

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")

    try:
        for tournament_title in tournaments:
            print(f"\nFetching tournament: {tournament_title}")

            raw_text = fetch_raw(tournament_title)
            tournament_id = upsert_tournament(conn, tournament_title)
            major_name = tournament_title.replace("_", " ").replace("/", " / ")

            total_rosters = 0
            total_links = 0
            missing_players = []

            for stage_title, classification in STAGE_MAP.items():
                section = extract_stage_section(raw_text, stage_title)

                if not section:
                    print(f"No section found for {stage_title}")
                    continue

                rosters = extract_rosters_from_section(section)
                print(f"{stage_title} / {classification}: found {len(rosters)} rosters")

                for roster in rosters:
                    team_title = roster["team"]
                    player_titles = roster["players"]

                    team_id = upsert_team(conn, team_title, classification, major_name)
                    upsert_tournament_team(conn, tournament_id, team_id, stage_title, classification)

                    print(f"Team: {team_title} | {classification} | players: {player_titles}")

                    for player_title in player_titles:
                        player_id = find_player_id(conn, player_title)

                        if not player_id:
                            missing_players.append(player_title)
                            print(f"Missing player in DB: {player_title}")
                            continue

                        upsert_player_team_membership(
                            conn,
                            player_id,
                            team_id,
                            tournament_title,
                        )
                        total_links += 1

                    total_rosters += 1
                    conn.commit()

            print(f"\nDone tournament: {tournament_title}")
            print(f"Rosters found: {total_rosters}")
            print(f"Player-team links created/updated: {total_links}")

            if missing_players:
                unique_missing = sorted(set(missing_players))
                print("Missing players to add to players.txt:")
                for player in unique_missing:
                    print(player)

            time.sleep(2.5)

    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()
