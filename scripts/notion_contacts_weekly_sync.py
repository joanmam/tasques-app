#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sincronitzacio AUTOMATICA (setmanal, via GitHub Actions) dels contactes marcats
"Personal" a la base de dades "Contactes" de Notion cap a la col·leccio
`contacts` de Firestore.

A diferencia de sync_contacts_notion.py (que llegia un JSON estatic preparat a
ma des d'un xat Cowork), aquest script consulta l'API publica de Notion
directament, aixi es pot executar sol cada setmana sense cap intervencio.

Idempotent: l'ID de cada document de Firestore es l'ID de la pagina de Notion,
i cada execucio sobreescriu el document sencer (aixi els canvis fets a Notion
-telefon nou, categoria canviada...- es reflecteixen). Els contactes que ja no
tenen "Personal" marcat (o que s'han esborrat de Notion) s'eliminen de
Firestore, sempre que el document tingui source == "notion" i sigui del mateix
propietari (mai toquem contactes d'altres propietaris o creats per una altra via).

Els contactes son PRIVATS per usuari (camp ownerId): nomes el propietari (OWNER_EMAIL)
els pot llegir des de l'app (veure regles de Firestore v8, contacts/{contactId}).

A mes del run setmanal programat, admet un mode "--if-requested": mira el
document Firestore syncRequests/notionContacts i, nomes si "pending" es true,
fa la sincronitzacio ara mateix (i neteja la peticio). Aixo permet un boto a
l'app ("Sincronitza contactes ara") per disparar-ho sense esperar al dilluns
ni tocar GitHub — el mateix workflow de recordatoris (cada ~10 min) ho revisa.

Secrets (variables d'entorn, via GitHub Secrets):
    NOTION_API_KEY            -> secret de la integracio interna de Notion
                                  (cal compartir la base "Contactes" amb ella)
    FIREBASE_SERVICE_ACCOUNT  -> contingut JSON del compte de servei de Firebase
"""
import os, sys, re, time
import requests
import firebase_admin
from firebase_admin import credentials, firestore, auth

HERE = os.path.dirname(os.path.abspath(__file__))
OWNER_EMAIL = "joanmam@gmail.com"
DRY_RUN = "--dry-run" in sys.argv
IF_REQUESTED = "--if-requested" in sys.argv

# ID de la data source "Contactes" (obtingut via el connector MCP de Notion el
# 15 juliol 2026; es estable mentre no es reconstrueixi la base de dades).
#
# IMPORTANT (actualitzat 20 juliol 2026): Notion ha migrat aquesta base de dades
# al model "multi-source" (una base de dades ara pot tenir diverses "data sources").
# Aixo trenca l'endpoint antic /v1/databases/{id}/query, que esperava l'ID del
# contenidor "database" i no el de la "data source" — encara que l'ID d'aquesta
# data source concreta (el que ja teniem aqui) NO ha canviat. La solucio oficial
# de Notion es fer servir el nou endpoint de data sources amb la versio d'API
# 2025-09-03, mantenint el mateix ID.
NOTION_DATA_SOURCE_ID = "28f3d60a796981f1a71d000b26063ed8"
NOTION_VERSION = "2025-09-03"


def init_db():
    env = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if env:
        import json
        cred = credentials.Certificate(json.loads(env))
    else:
        cred = credentials.Certificate(os.path.join(HERE, "..", "..", "push-sender", "service-account.json"))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


def notion_headers():
    key = os.environ.get("NOTION_API_KEY")
    if not key:
        print("ERROR: falta la variable d'entorn NOTION_API_KEY.")
        sys.exit(1)
    return {
        "Authorization": f"Bearer {key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def fetch_personal_contacts():
    """Consulta l'API de Notion i retorna totes les pagines amb Personal=true."""
    headers = notion_headers()
    url = f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query"
    body = {
        "filter": {"property": "Personal", "checkbox": {"equals": True}},
        "page_size": 100,
    }
    pages = []
    while True:
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "5"))
            print(f"Rate limited, esperant {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        pages.extend(data.get("results", []))
        if data.get("has_more"):
            body["start_cursor"] = data["next_cursor"]
        else:
            break
    return pages


def plain_title(prop):
    return "".join(t.get("plain_text", "") for t in (prop.get("title") or [])).strip()


def plain_rich_text(prop):
    return "".join(t.get("plain_text", "") for t in (prop.get("rich_text") or [])).strip()


def clean_phone(raw):
    if not raw:
        return ""
    first = raw.split(":::")[0].strip()
    first = re.sub(r"[‎‏‪-‮]", "", first)  # marques de direccio invisibles
    return re.sub(r"\s+", " ", first).strip()


def notion_id_from_page(page):
    return page["id"].replace("-", "")


def build_doc(page, owner_uid):
    props = page.get("properties", {})
    first = plain_title(props.get("First Name", {}))
    last = plain_rich_text(props.get("Last Name", {}))
    org = plain_rich_text(props.get("Organization Name", {}))
    categoria = (props.get("Categoria", {}) or {}).get("select") or {}
    phone1 = clean_phone((props.get("Phone 1 - Value", {}) or {}).get("phone_number") or "")
    phone2 = clean_phone(plain_rich_text(props.get("Phone 2 - Value", {})))
    email1 = ((props.get("E-mail 1 - Value", {}) or {}).get("email") or "").strip()
    name = f"{first} {last}".strip() or org or "(sense nom)"
    return {
        "name": name,
        "firstName": first,
        "lastName": last,
        "org": org,
        "category": categoria.get("name", "") or "",
        "phone1": phone1,
        "phone2": phone2,
        "email1": email1,
        "notionUrl": page.get("url", ""),
        "source": "notion",
        "ownerId": owner_uid,
        "syncedAt": firestore.SERVER_TIMESTAMP,
    }


def run_sync(db):
    """Fa la sincronitzacio completa Notion -> Firestore. Retorna (actualitzats, eliminats)."""
    pages = fetch_personal_contacts()
    print(f"Contactes 'Personal' trobats a Notion: {len(pages)}")

    if DRY_RUN:
        prepared = [(notion_id_from_page(p), build_doc(p, "(uid-real-en-execucio)")) for p in pages]
        for nid, doc in prepared[:10]:
            print(f"[DRY] {nid} -> {doc['name']} | {doc['phone1'] or doc['phone2'] or '(sense telefon)'}")
        print(f"[DRY] Es sincronitzarien {len(prepared)} contactes. Cap canvi fet.")
        return 0, 0

    owner_uid = auth.get_user_by_email(OWNER_EMAIL).uid
    print(f"Propietari: {OWNER_EMAIL} -> uid={owner_uid}")

    prepared = [(notion_id_from_page(p), build_doc(p, owner_uid)) for p in pages]
    current_ids = {nid for nid, _ in prepared}

    # --- Upsert dels contactes actuals ---
    batch = db.batch()
    count = 0
    for nid, doc in prepared:
        ref = db.collection("contacts").document(nid)
        batch.set(ref, doc)
        count += 1
        if count % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
    print(f"Actualitzats {count} contactes a Firestore.")

    # --- Neteja: esborra contactes de Notion (d'aquest propietari) que ja no ---
    # --- son "Personal" (o que s'han esborrat a Notion). Mai toquem contactes
    # --- d'altres propietaris ni de fonts diferents de Notion.
    existing = db.collection("contacts").where("source", "==", "notion").where("ownerId", "==", owner_uid).stream()
    to_delete = [doc.id for doc in existing if doc.id not in current_ids]
    deleted = 0
    if to_delete:
        del_batch = db.batch()
        for i, doc_id in enumerate(to_delete, 1):
            del_batch.delete(db.collection("contacts").document(doc_id))
            if i % 400 == 0:
                del_batch.commit()
                del_batch = db.batch()
        del_batch.commit()
        deleted = len(to_delete)
        print(f"Eliminats {deleted} contactes que ja no son 'Personal' a Notion.")

    return count, deleted


def run_if_requested(db):
    """Mode lleuger per al workflow de recordatoris (cada ~10 min): nomes fa la
    sincronitzacio (i truca a Notion) si hi ha una peticio pendent a Firestore."""
    ref = db.collection("syncRequests").document("notionContacts")
    snap = ref.get()
    data = snap.to_dict() if snap.exists else None
    if not data or not data.get("pending"):
        print("Cap peticio pendent de sincronitzacio de contactes Notion.")
        return

    print("Peticio pendent trobada — sincronitzant contactes de Notion ara...")
    try:
        count, deleted = run_sync(db)
        ref.set({
            "pending": False,
            "lastRunAt": firestore.SERVER_TIMESTAMP,
            "lastResult": f"{count} actualitzats, {deleted} eliminats",
        }, merge=True)
        print(f"OK  sincronitzacio a peticio completada ({count} actualitzats, {deleted} eliminats).")
    except Exception as e:
        ref.set({"pending": False, "lastError": str(e)}, merge=True)
        print(f"ERR sincronitzacio a peticio: {e}")
        raise


def main():
    if IF_REQUESTED:
        db = init_db()
        run_if_requested(db)
        return

    if DRY_RUN:
        run_sync(None)
        return

    db = init_db()
    run_sync(db)
    print("Sincronitzacio setmanal completada.")


if __name__ == "__main__":
    main()
