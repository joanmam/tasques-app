#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BACKFILL D'OCURRENCIES per a tasques recurrents antigues.

Motiu: fins fa poc, quan es creava una tasca recurrent nomes es generava UNA
ocurrencia (no totes les futures). L'app ara genera totes les ocurrencies
futures (fins a 1 any, o fins a "recurrenceEndDate") en el moment de crear
la tasca -- pero les tasques recurrents creades ABANS d'aquest canvi nomes
tenen aquell unic document, i mai se'ls generen les seguents.

Aquest script busca, per a cada tasca amb "recurrence" != "none" del propietari
indicat, quina es la darrera ocurrencia que ja existeix (agrupant per seriesId
si en te, o com a grup d'un sol element si no en te) i genera les que falten
fins a 1 any des d'avui (o fins a recurrenceEndDate si es mes proper).

Es IDEMPOTENT: si es torna a executar, com que sempre genera "fins al limit",
les series que ja estan completes no generen res mes (0 ocurrencies noves).

Nomes s'ha d'executar manualment (workflow_dispatch a GitHub Actions).

Secrets (variables d'entorn, via GitHub Secrets):
    FIREBASE_SERVICE_ACCOUNT  -> contingut JSON del compte de servei de Firebase
"""
import os, json, sys, calendar
from datetime import date, timedelta
import firebase_admin
from firebase_admin import credentials, firestore, auth

HERE = os.path.dirname(os.path.abspath(__file__))
OWNER_EMAIL = "joanmam@gmail.com"
DRY_RUN = "--dry-run" in sys.argv
MAX_SAFETY = 400  # mateix limit que fa servir el client per generar una serie


def init_db():
    env = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if env:
        cred = credentials.Certificate(json.loads(env))
    else:
        cred = credentials.Certificate(os.path.join(HERE, "service-account.json"))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


def ymd(d):
    return d.isoformat()


def parse_ymd(s):
    y, m, dd = s.split("-")
    return date(int(y), int(m), int(dd))


def next_occurrence(date_str, recurrence):
    """Rèplica exacta de la funció JS nextOccurrence() del client."""
    d = parse_ymd(date_str)
    if recurrence == "daily":
        d = d + timedelta(days=1)
    elif recurrence == "weekly":
        d = d + timedelta(days=7)
    elif recurrence == "monthly":
        # mateix dia del mes seguent (com Date.setMonth a JS, amb el mateix
        # desbordament si el dia no existeix al mes seguent: p.ex. 31 de gener
        # + 1 mes -> 3 de març, no 28 de febrer).
        month = d.month + 1
        year = d.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        days_in_month = calendar.monthrange(year, month)[1]
        if d.day > days_in_month:
            d = date(year, month, days_in_month) + timedelta(days=d.day - days_in_month)
        else:
            d = date(year, month, d.day)
    elif recurrence == "monthly4w":
        d = d + timedelta(days=28)
    elif recurrence == "yearly":
        # mateix desbordament que Date.setFullYear a JS (29 de febrer en any
        # de traspas + 1 any no de traspas -> 1 de març, no 28 de febrer).
        year = d.year + 1
        days_in_month = calendar.monthrange(year, d.month)[1]
        if d.day > days_in_month:
            d = date(year, d.month, days_in_month) + timedelta(days=d.day - days_in_month)
        else:
            d = date(year, d.month, d.day)
    elif recurrence == "weekdays":
        d = d + timedelta(days=1)
        while d.weekday() >= 5:  # 5=dissabte, 6=diumenge
            d = d + timedelta(days=1)
    else:
        return None
    return ymd(d)


def main():
    db = init_db()
    owner = auth.get_user_by_email(OWNER_EMAIL)
    owner_uid = owner.uid
    print(f"Propietari: {OWNER_EMAIL} -> uid={owner_uid}")
    if DRY_RUN:
        print("*** MODE DRY-RUN: no s'escriura res a Firestore ***")

    today = date.today()
    limit_default = ymd(today + timedelta(days=365))

    docs = list(db.collection("tasks").where("ownerId", "==", owner_uid).stream())
    print(f"Tasques del propietari: {len(docs)}")

    # Agrupa les tasques recurrents: per seriesId si en tenen, sino cada una es el seu propi grup.
    groups = {}
    for d in docs:
        data = d.data()
        rec = data.get("recurrence")
        if not rec or rec == "none":
            continue
        key = data.get("seriesId") or f"__solo__{d.id}"
        groups.setdefault(key, []).append((d.id, data))

    print(f"Series/tasques recurrents trobades: {len(groups)}")

    total_new = 0
    total_series_extended = 0
    batch = db.batch()
    ops_in_batch = 0

    def flush_batch():
        nonlocal batch, ops_in_batch
        if ops_in_batch and not DRY_RUN:
            batch.commit()
        batch = db.batch()
        ops_in_batch = 0

    for series_id, items in groups.items():
        # Tria la instancia mes recent (per endDate) com a referencia dels camps a clonar.
        items.sort(key=lambda x: x[1].get("endDate") or x[1].get("startDate") or "")
        ref_id, ref_data = items[-1]
        last_date = ref_data.get("endDate") or ref_data.get("startDate")
        if not last_date:
            continue
        recurrence = ref_data.get("recurrence")
        recurrence_end = ref_data.get("recurrenceEndDate") or ""
        limit_str = recurrence_end if recurrence_end else limit_default
        real_series_id = ref_data.get("seriesId")  # None si era "solo"

        # Avança des de la darrera ocurrència existent fins al límit, pero
        # nomes AFEGIM les dates d'avui en endavant (no volem inundar de
        # tasques "vençudes" si la serie fa temps que no s'havia avançat).
        today_str = ymd(today)
        new_dates = []
        cur = last_date
        safety = 0
        while safety < MAX_SAFETY:
            nx = next_occurrence(cur, recurrence)
            if not nx or nx > limit_str:
                break
            if nx >= today_str:
                new_dates.append(nx)
            cur = nx
            safety += 1

        if not new_dates:
            continue  # aquesta serie ja esta completa (o no en calen mes)

        total_series_extended += 1
        title = ref_data.get("title", "(sense titol)")
        print(f"- '{title}' (darrera: {last_date}, recurrencia: {recurrence}): "
              f"{len(new_dates)} ocurrencia(es) noves fins a {new_dates[-1]}")

        # Si la tasca no tenia seriesId (era una tasca solta amb recurrencia), li'n creem un
        # i actualitzem el document existent perque quedi enllacat amb les noves ocurrencies.
        if not real_series_id:
            real_series_id = db.collection("tasks").document().id
            if DRY_RUN:
                print(f"  [DRY] S'assignaria seriesId={real_series_id} al document existent {ref_id}")
            else:
                batch.update(db.collection("tasks").document(ref_id), {"seriesId": real_series_id})
                ops_in_batch += 1

        clone_fields = {
            k: v for k, v in ref_data.items()
            if k not in ("endDate", "createdAt", "status", "completionLogs",
                         "gcalSyncEventIds", "gcalFamilyEventId", "comments")
        }
        clone_fields["seriesId"] = real_series_id

        for nd in new_dates:
            if DRY_RUN:
                print(f"  [DRY] nova ocurrencia {nd}")
            else:
                new_ref = db.collection("tasks").document()
                doc = dict(clone_fields)
                doc["endDate"] = nd
                doc["status"] = "pendent"
                doc["createdAt"] = firestore.SERVER_TIMESTAMP
                batch.set(new_ref, doc)
                ops_in_batch += 1
            total_new += 1

        if ops_in_batch >= 400:
            flush_batch()

    flush_batch()

    if DRY_RUN:
        print(f"\n[DRY] Series afectades: {total_series_extended}. "
              f"Ocurrencies que es generarien: {total_new}. Cap canvi fet.")
    else:
        print(f"\nFet. Series esteses: {total_series_extended}. "
              f"Ocurrencies noves creades: {total_new}.")


if __name__ == "__main__":
    main()
