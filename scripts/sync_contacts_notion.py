#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sincronitza contactes des de la base de dades "Contactes" de Notion cap a la
col·lecció `contacts` de Firestore, perquè l'app pugui triar-los des d'una
tasca (camp Contacte -> omple contactPhone).

Sincronització MANUAL: no hi ha cap workflow programat. Es dispara demanant-ho
al Claude d'un xat Cowork, que:
  1. Consulta la data source de Notion via el connector MCP (nomes els
     contactes amb Personal = true) i escriu el resultat cru a un JSON.
  2. Executa aquest script, que llegeix aquell JSON i fa upsert a Firestore
     (contacts/{notionId}), sobreescrivint sempre el document sencer perquè
     els canvis fets a Notion (telefon nou, categoria canviada...) es reflecteixin.

L'ID de cada document és l'ID de la pagina de Notion (extret de la URL), aixi
la re-sincronitzacio es idempotent (actualitza en lloc de duplicar).

Secrets (variables d'entorn, via GitHub Secrets, o servei local per proves):
    FIREBASE_SERVICE_ACCOUNT  -> contingut JSON del compte de servei de Firebase
"""
import os, sys, json, re
import firebase_admin
from firebase_admin import credentials, firestore

HERE = os.path.dirname(os.path.abspath(__file__))
DRY_RUN = "--dry-run" in sys.argv
# Fitxer d'entrada: per defecte scripts/data/contacts_notion_raw.json, pero es pot
# passar una altra ruta com a primer argument (útil per no haver de commitejar
# el JSON cru cada vegada, ja que es un fitxer temporal de treball).
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
DATA_FILE = ARGS[0] if ARGS else os.path.join(HERE, "data", "contacts_notion_raw.json")


def init_db():
    env = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if env:
        cred = credentials.Certificate(json.loads(env))
    else:
        # Per proves locals / execucio manual: fa servir la clau del propietari
        # del repo, guardada FORA del repo public a push-sender/.
        cred = credentials.Certificate(os.path.join(HERE, "..", "..", "push-sender", "service-account.json"))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


def notion_id_from_url(url):
    # Les URLs de Notion acaben amb l'id de pagina en hexadecimal (32 caracters, sense guions)
    m = re.search(r"([0-9a-fA-F]{32})$", url or "")
    return m.group(1) if m else None


def clean_phone(raw):
    # Alguns valors venen duplicats/concatenats amb " ::: " (artefacte d'exportacio de Notion).
    # Ens quedem amb el primer, sense espais sobrants ni caracters invisibles (marques RTL etc.)
    if not raw:
        return ""
    first = raw.split(":::")[0].strip()
    first = re.sub(r"[‎‏‪-‮]", "", first)  # marques de direccio invisibles
    return re.sub(r"\s+", " ", first).strip()


def build_doc(row):
    first = (row.get("First Name") or "").strip()
    last = (row.get("Last Name") or "").strip()
    org = (row.get("Organization Name") or "").strip()
    name = f"{first} {last}".strip() or org or "(sense nom)"
    return {
        "name": name,
        "firstName": first,
        "lastName": last,
        "org": org,
        "category": row.get("Categoria") or "",
        "phone1": clean_phone(row.get("Phone 1 - Value")),
        "phone2": clean_phone(row.get("Phone 2 - Value")),
        "email1": (row.get("E-mail 1 - Value") or "").strip(),
        "notionUrl": row.get("url") or "",
        "source": "notion",
        "syncedAt": firestore.SERVER_TIMESTAMP,
    }


def main():
    with open(DATA_FILE, encoding="utf-8") as f:
        rows = json.load(f)
    print(f"Contactes al fitxer d'entrada: {len(rows)}")

    prepared = []
    skipped = 0
    for row in rows:
        nid = notion_id_from_url(row.get("url"))
        if not nid:
            skipped += 1
            continue
        prepared.append((nid, build_doc(row)))

    if skipped:
        print(f"AVIS: {skipped} files sense URL/ID vàlid, ignorades.")

    if DRY_RUN:
        for nid, doc in prepared[:10]:
            print(f"[DRY] {nid} -> {doc['name']} | {doc['phone1'] or doc['phone2'] or '(sense telèfon)'}")
        print(f"[DRY] Es sincronitzarien {len(prepared)} contactes. Cap canvi fet.")
        return

    db = init_db()
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
    print(f"Fet. {count} contactes sincronitzats a Firestore (col·lecció 'contacts').")


if __name__ == "__main__":
    main()
