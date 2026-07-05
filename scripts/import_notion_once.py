#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IMPORTACIO UNICA de tasques des de Notion ("Tarees") cap a l'app Tasques.
Nomes s'ha d'executar UN COP (manualment, via workflow_dispatch a GitHub Actions).
Es pot re-executar sense risc: si la llista "Importades" ja conte tasques amb el
marcador d'aquest lot (importBatch), el script s'atura sense duplicar res.

Origen de les dades: scripts/data/notion_import_2026-07-05.json
(generat a partir de l'export CSV de la base de dades "Tarees" de Notion,
filtrant nomes les tasques amb Due Date posterior al 4/7/2026).

Secrets (variables d'entorn, via GitHub Secrets):
    FIREBASE_SERVICE_ACCOUNT  -> contingut JSON del compte de servei de Firebase
"""
import os, json, sys
import firebase_admin
from firebase_admin import credentials, firestore, auth

HERE = os.path.dirname(os.path.abspath(__file__))
OWNER_EMAIL = "joanmam@gmail.com"
LIST_NAME = "Importades"
BATCH_ID = "notion-2026-07-05"
DATA_FILE = os.path.join(HERE, "data", "notion_import_2026-07-05.json")
DRY_RUN = "--dry-run" in sys.argv


def init_db():
    env = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if env:
        cred = credentials.Certificate(json.loads(env))
    else:
        cred = credentials.Certificate(os.path.join(HERE, "service-account.json"))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


def main():
    db = init_db()
    owner = auth.get_user_by_email(OWNER_EMAIL)
    owner_uid = owner.uid
    print(f"Propietari: {OWNER_EMAIL} -> uid={owner_uid}")

    with open(DATA_FILE, encoding="utf-8") as f:
        matched = json.load(f)
    print(f"Tasques al fitxer d'importacio: {len(matched)}")

    # --- Idempotencia: si ja hi ha tasques d'aquest lot, no repeteixis ---
    already = db.collection("tasks").where("importBatch", "==", BATCH_ID).limit(1).get()
    if already:
        print(f"Aquest lot ({BATCH_ID}) ja s'ha importat abans. No es fa res mes.")
        return

    # --- Troba o crea la llista "Importades" ---
    lists_ref = db.collection("lists")
    existing = lists_ref.where("ownerId", "==", owner_uid).where("name", "==", LIST_NAME).limit(1).get()
    if existing:
        list_id = existing[0].id
        print(f"Llista '{LIST_NAME}' ja existeix: {list_id}")
    else:
        if DRY_RUN:
            list_id = "(nova-llista-dry-run)"
            print(f"[DRY] Es crearia la llista '{LIST_NAME}'")
        else:
            new_list = lists_ref.document()
            new_list.set({
                "name": LIST_NAME,
                "color": "#4f8ef7",
                "group": "Personal",
                "fields": [],
                "sharedWith": [],
                "sharedWithEmails": {},
                "ownerId": owner_uid,
                "createdAt": firestore.SERVER_TIMESTAMP,
            })
            list_id = new_list.id
            print(f"Llista '{LIST_NAME}' creada: {list_id}")

    # --- Escriu les tasques (batch unic) ---
    batch = db.batch()
    count = 0
    for t in matched:
        desc = f"Importat de Notion (Organització: {t['organitzacio']})" if t.get("organitzacio") else "Importat de Notion"
        doc = {
            "title": t["title"],
            "description": desc,
            "startDate": t["startDate"], "startTime": t["startTime"],
            "endDate": t["endDate"], "endTime": t["endTime"],
            "priority": "mitjana",
            "status": t["status"],
            "tags": t["tags"],
            "listId": list_id,
            "subtasks": [],
            "customValues": {},
            "reminderMinutes": 0,
            "recurrence": "none",
            "recurrenceEndDate": "",
            "ownerId": owner_uid,
            "ownerEmail": OWNER_EMAIL,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "importBatch": BATCH_ID,
        }
        if DRY_RUN:
            print(f"[DRY] {t['endDate']} {t['endTime']:>5}  [{t['status']:10}] {t['title']}")
        else:
            ref = db.collection("tasks").document()
            batch.set(ref, doc)
        count += 1

    if not DRY_RUN:
        batch.commit()
        print(f"Fet. {count} tasques escrites a la llista '{LIST_NAME}' ({list_id}). Lot: {BATCH_ID}")
    else:
        print(f"[DRY] {count} tasques es escriurien. Cap canvi fet.")


if __name__ == "__main__":
    main()
