import os
import re
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from dotenv import load_dotenv

load_dotenv()

STEAM_API_KEY = os.getenv("STEAM_API_KEY")
STEAM_API = "https://api.steampowered.com"

app = FastAPI(title="Steam Account Calculator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Утилиты ----------

def resolve_vanity(vanity: str) -> str | None:
    """Превращает custom URL (например /gaben) в SteamID64."""
    url = f"{STEAM_API}/ISteamUser/ResolveVanityURL/v1/"
    params = {"key": STEAM_API_KEY, "vanityurl": vanity}
    r = requests.get(url, params=params, timeout=15).json()
    if r.get("response", {}).get("success") == 1:
        return r["response"]["steamid"]
    return None


def parse_steam_input(raw: str) -> str | None:
    """Принимает ссылку на профиль, SteamID64 или custom URL — возвращает SteamID64."""
    raw = raw.strip()

    # Чистый SteamID64
    if re.fullmatch(r"7656119\d{10}", raw):
        return raw

    # Ссылка вида steamcommunity.com/profiles/76561198...
    m = re.search(r"steamcommunity\.com/profiles/(7656119\d{10})", raw)
    if m:
        return m.group(1)

    # Ссылка вида steamcommunity.com/id/customname
    m = re.search(r"steamcommunity\.com/id/([^/?#]+)", raw)
    if m:
        return resolve_vanity(m.group(1))

    # Просто написали custom name
    if re.fullmatch(r"[A-Za-z0-9_-]{2,64}", raw):
        return resolve_vanity(raw)

    return None


# ---------- Steam API ----------

def get_player_summary(steam_id: str) -> dict:
    url = f"{STEAM_API}/ISteamUser/GetPlayerSummaries/v2/"
    params = {"key": STEAM_API_KEY, "steamids": steam_id}
    r = requests.get(url, params=params, timeout=15).json()
    players = r.get("response", {}).get("players", [])
    if not players:
        raise HTTPException(404, "Игрок не найден или профиль приватный")
    return players[0]


def get_owned_games(steam_id: str) -> dict | None:
    url = f"{STEAM_API}/IPlayerService/GetOwnedGames/v1/"
    params = {
        "key": STEAM_API_KEY,
        "steamid": steam_id,
        "include_appinfo": True,
        "include_played_free_games": True,
    }
    r = requests.get(url, params=params, timeout=20).json()
    resp = r.get("response", {})
    if "games" not in resp:
        return None  # Библиотека скрыта
    return resp


def get_player_achievements_count(steam_id: str, app_id: int) -> dict | None:
    url = f"{STEAM_API}/ISteamUserStats/GetPlayerAchievements/v1/"
    params = {"key": STEAM_API_KEY, "steamid": steam_id, "appid": app_id}
    try:
        r = requests.get(url, params=params, timeout=10).json()
    except Exception:
        return None
    ps = r.get("playerstats", {})
    if not ps.get("success"):
        return None
    ach = ps.get("achievements", [])
    if not ach:
        return None
    return {
        "total": len(ach),
        "unlocked": sum(1 for a in ach if a.get("achieved") == 1),
    }


def get_game_schema_price(app_id: int) -> float | None:
    """
    Ориентировочная цена игры в USD через Steam Store API.
    Возвращает None, если игра бесплатная / нет цены / недоступна в регионе.
    """
    url = "https://store.steampowered.com/api/appdetails"
    params = {"appids": app_id, "cc": "us", "l": "en"}
    try:
        r = requests.get(url, params=params, timeout=10).json()
    except Exception:
        return None

    data = r.get(str(app_id), {})
    if not data.get("success"):
        return None
    d = data.get("data", {})
    if d.get("is_free"):
        return 0.0
    price = d.get("price_overview")
    if not price:
        return None
    return price.get("final", 0) / 100  # в центах


# ---------- Оценка ----------

def estimate_value(games: list, achievements_stats: dict) -> dict:
    """
    Ориентировочная формула:
      - базовая стоимость библиотеки: сумма цен игр (в USD) × 0.3
        (аккаунты продаются заметно дешевле суммы магазина)
      - бонус за часы: 1 час = 1.2 руб. (условно, для "прокачки")
      - бонус за достижения: 1% завершённости = 5 руб.
      - фикс за возраст аккаунта (не считаем — нет данных в summary)

    Курс USD->RUB берём условно = 90 (можно поменять).
    """
    USD_TO_RUB = 90

    total_hours = sum(g.get("playtime_forever", 0) for g in games) / 60
    total_games = len(games)

    # --- Стоимость библиотеки ---
    # Берём топ-20 самых дорогих игр, чтобы не долбить Store API 500 раз
    top_games = sorted(
        games, key=lambda g: g.get("playtime_forever", 0), reverse=True
    )[:20]

    lib_usd = 0.0
    priced_count = 0
    for g in top_games:
        price = get_game_schema_price(g["appid"])
        if price:
            lib_usd += price
            priced_count += 1

    # Экстраполируем на всю библиотеку
    if priced_count > 0:
        avg_price = lib_usd / priced_count
        estimated_lib_usd = avg_price * total_games
    else:
        estimated_lib_usd = 0.0

    lib_rub = estimated_lib_usd * USD_TO_RUB * 0.3  # 30% от суммы магазина

    # --- Часы ---
    hours_rub = total_hours * 1.2

    # --- Достижения ---
    ach_total = achievements_stats.get("total", 0)
    ach_unlocked = achievements_stats.get("unlocked", 0)
    if ach_total > 0:
        pct = (ach_unlocked / ach_total) * 100
    else:
        pct = 0
    ach_rub = pct * 5

    total = lib_rub + hours_rub + ach_rub

    return {
        "total_hours": round(total_hours, 1),
        "total_games": total_games,
        "estimated_lib_usd": round(estimated_lib_usd, 2),
        "lib_rub": round(lib_rub),
        "hours_rub": round(hours_rub),
        "ach_rub": round(ach_rub),
        "ach_pct": round(pct, 1),
        "ach_unlocked": ach_unlocked,
        "ach_total": ach_total,
        "total_rub": round(total),
        "price_range": [round(total * 0.7), round(total * 1.3)],
    }


# ---------- Роут ----------

@app.get("/api/analyze")
def analyze(steam: str):
    if not STEAM_API_KEY:
        raise HTTPException(500, "STEAM_API_KEY не задан в .env")

    steam_id = parse_steam_input(steam)
    if not steam_id:
        raise HTTPException(400, "Не удалось распознать SteamID / ссылку")

    summary = get_player_summary(steam_id)

    owned = get_owned_games(steam_id)
    if owned is None:
        return {
            "profile": {
                "steam_id": steam_id,
                "name": summary.get("personaname"),
                "avatar": summary.get("avatarfull"),
                "profile_url": summary.get("profileurl"),
                "visibility": "private",
            },
            "error": "Библиотека игр скрыта. Оценка невозможна.",
        }

    games = owned.get("games", [])

    # Считаем достижения для топ-3 игр по времени
    top3 = sorted(games, key=lambda g: g.get("playtime_forever", 0), reverse=True)[:3]
    ach_total = 0
    ach_unlocked = 0
    for g in top3:
        stats = get_player_achievements_count(steam_id, g["appid"])
        if stats:
            ach_total += stats["total"]
            ach_unlocked += stats["unlocked"]

    ach_stats = {"total": ach_total, "unlocked": ach_unlocked}
    estimate = estimate_value(games, ach_stats)

    top_games_out = sorted(
        games, key=lambda g: g.get("playtime_forever", 0), reverse=True
    )[:10]
    top_games_out = [
        {
            "name": g.get("name"),
            "hours": round(g.get("playtime_forever", 0) / 60, 1),
            "appid": g["appid"],
        }
        for g in top_games_out
    ]

    return {
        "profile": {
            "steam_id": steam_id,
            "name": summary.get("personaname"),
            "avatar": summary.get("avatarfull"),
            "profile_url": summary.get("profileurl"),
            "visibility": "public",
        },
        "estimate": estimate,
        "top_games": top_games_out,
    }


@app.get("/")
def root():
    return FileResponse("index.html")
