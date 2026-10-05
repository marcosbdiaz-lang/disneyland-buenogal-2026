#!/usr/bin/env python3
"""
DISNEYLAND BUENOGAL 2026
Comprobador automático de horarios.

Fuente técnica: ThemeParks.wiki API v1.
La app oficial de Disneyland Paris sigue siendo la referencia final.

Estados escritos en Supabase:
- ok        -> el pase planificado aparece en los datos disponibles.
- changed   -> el evento existe, pero el pase planificado ya no aparece y hay otro horario.
- unknown   -> no podemos confirmar el horario con suficiente seguridad.
- cancelled -> solo si una fuente devuelve un estado explícito de cancelación.

Importante:
- Para fechas futuras, la API puede no publicar aún showtimes del día concreto.
- No generamos ruido con demasiada antelación.
- Dentro de las 36 h previas, si no podemos verificar el horario, marcamos "unknown"
  para que la web muestre "CONFIRMAR CON APP OFICIAL DISNEY".
"""

import os
import sys
import json
import math
import re
import unicodedata
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests

SUPABASE_URL = os.getenv(
    "SUPABASE_URL",
    "https://upgwvszzndgabasrtuuc.supabase.co"
).rstrip("/")

SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "").strip()

TRIP_ID = "disneyland-buenogal-2026"
TABLE = "disney_schedule_alerts"
PARIS = ZoneInfo("Europe/Paris")

THEMEPARKS_BASE = "https://api.themeparks.wiki/v1"

PARKS = {
    "adventure": {
        "id": "ca888437-ebb4-4d50-aed2-d227f7096968",
        "name": "Disney Adventure World",
    },
    "disneyland": {
        "id": "dae968d5-630d-4719-8b06-3d107e944401",
        "name": "Disneyland Park",
    },
}

# Relación entre event_id de Supabase y parque + alias de nombre.
EVENTS = {
    "arendelle_2026_10_09": {
        "park": "adventure",
        "aliases": ["A Celebration in Arendelle"],
    },
    "princess_cavalcade_2026_10_09": {
        "park": "adventure",
        "aliases": ["Disney Princess Cavalcade"],
    },
    "mickey_magician_2026_10_09": {
        "park": "adventure",
        "aliases": ["Mickey and the Magician"],
    },
    "together_2026_10_09": {
        "park": "adventure",
        "aliases": ["TOGETHER: a Pixar Musical Adventure", "TOGETHER a Pixar Musical Adventure"],
    },
    "cascade_2026_10_09": {
        "park": "adventure",
        "aliases": ["Disney Cascade of Lights"],
    },
    "stars_parade_2026_10_10": {
        "park": "disneyland",
        "aliases": ["Disney Stars on Parade"],
    },
    "lion_king_2026_10_10": {
        "park": "disneyland",
        "aliases": ["The Lion King: Rhythms of the Pride Lands"],
    },
    "halloween_2026_10_10": {
        "park": "disneyland",
        "aliases": [
            "Mickey's Halloween Celebration",
            "Mickey’s Halloween Celebration",
        ],
    },
    "tales_magic_2026_10_10": {
        "park": "disneyland",
        "aliases": ["Disney Tales of Magic"],
    },
    "halloween_2026_10_11": {
        "park": "disneyland",
        "aliases": [
            "Mickey's Halloween Celebration",
            "Mickey’s Halloween Celebration",
        ],
    },
}

VERIFY_HORIZON_HOURS = 36
TIMEOUT = 25

session = requests.Session()
session.headers.update({
    "User-Agent": "DISNEYLAND-BUENOGAL-2026-schedule-checker/1.0",
    "Accept": "application/json",
})


def log(msg):
    print(f"[{datetime.now(PARIS).isoformat(timespec='seconds')}] {msg}", flush=True)


def normalize_name(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = value.replace("’", "'")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def supabase_headers():
    if not SUPABASE_SECRET_KEY:
        raise RuntimeError(
            "Falta el secret de GitHub SUPABASE_SECRET_KEY. "
            "Añade una clave sb_secret_... en Settings > Secrets and variables > Actions."
        )
    # Las nuevas sb_secret_ se envían en apikey. No las enviamos como Bearer JWT.
    return {
        "apikey": SUPABASE_SECRET_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def fetch_rows():
    url = f"{SUPABASE_URL}/rest/v1/{TABLE}"
    params = {
        "trip_id": f"eq.{TRIP_ID}",
        "select": "trip_id,event_id,event_name,event_date,planned_time,live_time,status,message,source_url,updated_at",
        "order": "event_date.asc,event_id.asc",
    }
    r = session.get(url, headers=supabase_headers(), params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def patch_row(event_id, payload):
    url = f"{SUPABASE_URL}/rest/v1/{TABLE}"
    params = {
        "trip_id": f"eq.{TRIP_ID}",
        "event_id": f"eq.{event_id}",
    }
    headers = supabase_headers()
    headers["Prefer"] = "return=minimal"
    r = session.patch(url, headers=headers, params=params, json=payload, timeout=TIMEOUT)
    r.raise_for_status()


def fetch_park_live(park_key):
    park = PARKS[park_key]
    url = f"{THEMEPARKS_BASE}/entity/{park['id']}/live"
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json(), url


def walk_objects(obj):
    """Yield every dict nested anywhere inside JSON."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_objects(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_objects(value)


def show_candidates(payload):
    """
    Extrae objetos que parecen shows.
    Funciona con liveData o con estructuras anidadas similares.
    """
    out = []
    seen = set()

    for obj in walk_objects(payload):
        name = obj.get("name") or obj.get("title")
        if not name:
            continue

        entity_type = normalize_name(
            obj.get("entityType") or obj.get("entity_type") or obj.get("type")
        )
        has_showtimes = isinstance(
            obj.get("showtimes") or obj.get("showTimes") or obj.get("times"),
            list
        )

        if entity_type == "show" or has_showtimes:
            key = (
                obj.get("id") or obj.get("entityId") or obj.get("entity_id") or "",
                normalize_name(name),
            )
            if key not in seen:
                seen.add(key)
                out.append(obj)
    return out


def match_show(candidates, aliases):
    wanted = {normalize_name(x) for x in aliases}
    # Primero exacto normalizado.
    for obj in candidates:
        if normalize_name(obj.get("name") or obj.get("title")) in wanted:
            return obj

    # Segundo, contención razonable para pequeñas variaciones.
    for obj in candidates:
        actual = normalize_name(obj.get("name") or obj.get("title"))
        for w in wanted:
            if len(w) >= 10 and (w in actual or actual in w):
                return obj
    return None


def parse_iso_datetime(value):
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=PARIS)
        return dt.astimezone(PARIS)
    except Exception:
        return None


def extract_showtimes(show_obj, target_date):
    arrays = []
    for key in ("showtimes", "showTimes", "times"):
        value = show_obj.get(key)
        if isinstance(value, list):
            arrays.extend(value)

    found = []
    for item in arrays:
        if isinstance(item, str):
            # ISO completo
            dt = parse_iso_datetime(item)
            if dt and dt.date() == target_date:
                found.append(dt.strftime("%H:%M"))
                continue
            # Hora simple (solo la aceptamos como perteneciente al target si el feed no aporta fecha)
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", item.strip())
            if m:
                found.append(f"{int(m.group(1)):02d}:{m.group(2)}")
            continue

        if not isinstance(item, dict):
            continue

        raw = (
            item.get("startTime")
            or item.get("start_time")
            or item.get("start")
            or item.get("startDateTime")
            or item.get("datetime")
            or item.get("dateTime")
        )

        dt = parse_iso_datetime(raw)
        if dt:
            if dt.date() == target_date:
                found.append(dt.strftime("%H:%M"))
            continue

        if raw:
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(raw).strip())
            if m:
                found.append(f"{int(m.group(1)):02d}:{m.group(2)}")

    return sorted(set(found))


def minutes(hhmm):
    try:
        h, m = map(int, hhmm.split(":"))
        return h * 60 + m
    except Exception:
        return None


def nearest_time(times, planned):
    p = minutes(planned)
    if p is None or not times:
        return times[0] if times else None

    valid = [(abs(minutes(t) - p), t) for t in times if minutes(t) is not None]
    return min(valid)[1] if valid else times[0]


def explicit_cancelled(show_obj):
    status = normalize_name(show_obj.get("status"))
    return status in {"cancelled", "canceled"}


def hours_until(target_date, now):
    target = datetime.combine(target_date, datetime.min.time(), tzinfo=PARIS)
    return (target - now).total_seconds() / 3600


def evaluate(row, park_payload, source_url, now):
    event_id = row["event_id"]
    cfg = EVENTS.get(event_id)

    if not cfg:
        return {
            "status": "unknown",
            "live_time": row.get("live_time"),
            "message": "Evento sin configuración en el comprobador automático. Confirmar con app oficial Disney.",
            "source_url": source_url,
        }

    target_date = date.fromisoformat(row["event_date"])
    planned = row.get("planned_time")
    candidates = show_candidates(park_payload)
    show = match_show(candidates, cfg["aliases"])

    # Si la fuente ya contiene showtimes de la fecha objetivo, podemos verificar aunque sea futura.
    if show:
        times = extract_showtimes(show, target_date)
        if times:
            if planned in times:
                return {
                    "status": "ok",
                    "live_time": planned,
                    "message": None,
                    "source_url": source_url,
                }

            new_time = nearest_time(times, planned)
            return {
                "status": "changed",
                "live_time": new_time,
                "message": "El pase previsto ya no aparece en los horarios disponibles.",
                "source_url": source_url,
            }

        if explicit_cancelled(show) and target_date == now.date():
            return {
                "status": "cancelled",
                "live_time": None,
                "message": "La fuente técnica indica cancelación. Confirmar también en la app oficial Disney.",
                "source_url": source_url,
            }

    # Fechas ya pasadas: dejamos de generar avisos.
    if target_date < now.date():
        return {
            "status": "ok",
            "live_time": row.get("live_time") or planned,
            "message": None,
            "source_url": source_url,
        }

    remaining = hours_until(target_date, now)

    # Mucha antelación: no declaramos una duda solo porque el feed live aún no publique ese día.
    if remaining > VERIFY_HORIZON_HOURS:
        return {
            "status": "ok",
            "live_time": row.get("live_time") or planned,
            "message": None,
            "source_url": source_url,
        }

    # Dentro de las 36 h: si no podemos verificar la fecha concreta, preferimos avisar.
    if target_date > now.date():
        return {
            "status": "unknown",
            "live_time": row.get("live_time") or planned,
            "message": "La fuente automática todavía no permite confirmar con seguridad el horario de ese día.",
            "source_url": source_url,
        }

    # Día del evento.
    if show is None:
        return {
            "status": "unknown",
            "live_time": row.get("live_time") or planned,
            "message": "El evento no aparece claramente en la fuente automática de hoy.",
            "source_url": source_url,
        }

    return {
        "status": "unknown",
        "live_time": row.get("live_time") or planned,
        "message": "El evento aparece, pero no se han podido confirmar sus horas de hoy.",
        "source_url": source_url,
    }


def main():
    now = datetime.now(PARIS)
    log("Iniciando comprobación automática.")

    rows = fetch_rows()
    log(f"Supabase: {len(rows)} eventos a revisar.")

    # Solo descargamos cada parque una vez por ejecución.
    park_cache = {}
    park_errors = {}

    for key in PARKS:
        try:
            park_cache[key] = fetch_park_live(key)
            log(f"Fuente OK: {PARKS[key]['name']}.")
        except Exception as exc:
            park_errors[key] = str(exc)
            log(f"AVISO: no se pudo consultar {PARKS[key]['name']}: {exc}")

    changed_count = 0
    unknown_count = 0
    cancelled_count = 0

    for row in rows:
        event_id = row["event_id"]
        cfg = EVENTS.get(event_id)
        checked_at = datetime.now(PARIS).isoformat()

        if not cfg:
            result = {
                "status": "unknown",
                "live_time": row.get("live_time"),
                "message": "Evento sin configuración automática. Confirmar con app oficial Disney.",
                "source_url": None,
            }
        elif cfg["park"] in park_errors:
            target_date = date.fromisoformat(row["event_date"])
            remaining = hours_until(target_date, now)

            # No convertimos una caída de API en 10 alertas varios días antes.
            if target_date >= now.date() and remaining <= VERIFY_HORIZON_HOURS:
                result = {
                    "status": "unknown",
                    "live_time": row.get("live_time") or row.get("planned_time"),
                    "message": "No se ha podido consultar la fuente automática. Confirmar con app oficial Disney.",
                    "source_url": f"{THEMEPARKS_BASE}/entity/{PARKS[cfg['park']]['id']}/live",
                }
            else:
                # Conserva el estado previo si estamos lejos del viaje.
                result = {
                    "status": row.get("status") or "ok",
                    "live_time": row.get("live_time") or row.get("planned_time"),
                    "message": row.get("message"),
                    "source_url": row.get("source_url"),
                }
        else:
            payload, source_url = park_cache[cfg["park"]]
            result = evaluate(row, payload, source_url, now)

        # updated_at se actualiza SIEMPRE: la web lo usa como "última comprobación".
        payload = {
            "live_time": result.get("live_time"),
            "status": result["status"],
            "message": result.get("message"),
            "source_url": result.get("source_url"),
            "updated_at": checked_at,
        }

        patch_row(event_id, payload)

        if result["status"] == "changed":
            changed_count += 1
        elif result["status"] == "unknown":
            unknown_count += 1
        elif result["status"] == "cancelled":
            cancelled_count += 1

        log(
            f"{row['event_name']}: {row.get('planned_time')} -> "
            f"{result.get('live_time')} [{result['status']}]"
        )

    log(
        f"Fin. changed={changed_count}, unknown={unknown_count}, "
        f"cancelled={cancelled_count}, total={len(rows)}."
    )

    # No hacemos fallar el workflow por una alerta de horario.
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log(f"ERROR FATAL: {exc}")
        raise
