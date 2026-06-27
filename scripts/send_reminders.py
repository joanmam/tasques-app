#!/usr/bin/env python3
"""
Enviador de recordatoris push per a l'app Tasques.
Llegeix les tasques de Firestore amb recordatori actiu, calcula quines vençen
dins de la finestra actual i envia una notificacio web push al/s dispositiu/s
subscrit/s. Pensat per executar-se periodicament des de GitHub Actions.

Secrets (com a variables d'entorn, via GitHub Secrets):
    FIREBASE_SERVICE_ACCOUNT  -> contingut JSON del compte de servei de Firebase
    VAPID_JSON                -> { "public": "...", "private": "...", "sub": "mailto:..." }

Per a proves locals pots posar els fitxers service-account.json i vapid_keys.json
al mateix directori en comptes de les variables d'entorn.
"""
import json, os, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, firestore
from pywebpush import webpush, WebPushException

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Europe/Madrid")
WINDOW_MIN = 25          # ampli per absorbir el retard del cron de GitHub Actions
DRY_RUN = "--dry-run" in sys.argv

def load_cfg():
    env = os.environ.get("VAPID_JSON")
    if env:
        return json.loads(env)
    with open(os.path.join(HERE, "vapid_keys.json"), encoding="utf-8") as f:
        return json.load(f)

def init_db():
    env = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if env:
        cred = credentials.Certificate(json.loads(env))
    else:
        cred = credentials.Certificate(os.path.join(HERE, "service-account.json"))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()

def reminder_label(mins):
    if mins < 60:   return f"{mins} min"
    if mins == 60:  return "1 hora"
    if mins < 1440: return f"{mins // 60} hores"
    if mins == 1440: return "1 dia"
    return f"{mins // 1440} dies"

def main():
    cfg = load_cfg()
    db = init_db()
    now = datetime.now(TZ)

    subs = {}
    def get_sub(uid):
        if uid not in subs:
            doc = db.collection("pushSubscriptions").document(uid).get()
            subs[uid] = doc.to_dict().get("subscription") if doc.exists else None
        return subs[uid]

    due = 0
    tasks = db.collection("tasks").where("reminderMinutes", ">", 0).stream()
    for t in tasks:
        d = t.to_dict()
        if d.get("status") == "completada" or not d.get("endDate"):
            continue
        mins = int(d.get("reminderMinutes") or 0)
        if mins <= 0:
            continue
        end_time = d.get("endTime") or "23:59"
        try:
            end_dt = datetime.fromisoformat(f"{d['endDate']}T{end_time}").replace(tzinfo=TZ)
        except ValueError:
            continue
        rem_dt = end_dt - timedelta(minutes=mins)
        diff = (now - rem_dt).total_seconds()
        if not (0 <= diff < WINDOW_MIN * 60):
            continue

        log_id = f"{t.id}_{int(rem_dt.timestamp())}"
        log_ref = db.collection("pushLog").document(log_id)
        if log_ref.get().exists:
            continue

        uid = d.get("ownerId")
        sub = get_sub(uid) if uid else None
        if not sub:
            continue

        payload = json.dumps({
            "title": f"⏰ {d.get('title','Tasca')}",
            "body": f"Venç en {reminder_label(mins)}",
            "tag": t.id,
            "url": "./",
        })
        due += 1
        if DRY_RUN:
            print(f"[DRY] {d.get('title')} -> {uid} (rem {rem_dt})")
            continue
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=cfg["private"],
                vapid_claims={"sub": cfg["sub"]},
            )
            log_ref.set({"sentAt": firestore.SERVER_TIMESTAMP, "taskId": t.id, "uid": uid})
            print(f"OK  {d.get('title')} -> {uid}")
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):
                db.collection("pushSubscriptions").document(uid).delete()
                subs[uid] = None
                print(f"GONE subscripcio caducada esborrada ({uid})")
            else:
                print(f"ERR {d.get('title')}: {e}")

    print(f"Fet. Recordatoris dins de finestra: {due}. Hora: {now.isoformat()}")

if __name__ == "__main__":
    main()
