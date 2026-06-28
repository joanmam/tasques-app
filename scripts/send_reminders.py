#!/usr/bin/env python3
"""
Enviador de recordatoris per CORREU ELECTRÒNIC per a l'app Tasques.
Llegeix les tasques de Firestore amb recordatori actiu, calcula quines toquen
ara i envia un correu a cada persona implicada (propietari de la tasca + propietari
i membres de les llistes compartides). Pensat per executar-se periòdicament des de
GitHub Actions.

Secrets (variables d'entorn, via GitHub Secrets):
    FIREBASE_SERVICE_ACCOUNT  -> contingut JSON del compte de servei de Firebase
    GMAIL_APP_PASSWORD        -> contrasenya d'aplicació de Gmail (16 caràcters)
    GMAIL_USER (opcional)     -> adreça Gmail remitent (per defecte joanmam@gmail.com)

Per a proves locals: posa service-account.json en aquest directori i exporta
GMAIL_APP_PASSWORD (i opcionalment GMAIL_USER).
"""
import os, sys, json, smtplib, ssl
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, firestore, auth

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Europe/Madrid")
WINDOW_MIN = 25          # ampli per absorbir el retard del cron de GitHub Actions
DRY_RUN = "--dry-run" in sys.argv
APP_URL = "https://joanmam.github.io/tasques-app/"

GMAIL_USER = os.environ.get("GMAIL_USER") or "joanmam@gmail.com"
GMAIL_APP_PASSWORD = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")


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
    if mins < 60:    return f"{mins} min"
    if mins == 60:   return "1 hora"
    if mins < 1440:  return f"{mins // 60} hores"
    if mins == 1440: return "1 dia"
    return f"{mins // 1440} dies"


def main():
    db = init_db()
    now = datetime.now(TZ)

    if not DRY_RUN and not GMAIL_APP_PASSWORD:
        print("ERROR: falta el secret GMAIL_APP_PASSWORD")
        sys.exit(1)

    emails = {}
    def get_email(uid):
        if uid not in emails:
            try:
                emails[uid] = auth.get_user(uid).email
            except Exception:
                emails[uid] = None
        return emails[uid]

    lists_cache = {}
    def get_list(list_id):
        if list_id not in lists_cache:
            doc = db.collection("lists").document(list_id).get()
            lists_cache[list_id] = doc.to_dict() if doc.exists else None
        return lists_cache[list_id]

    def recipients_for(task):
        # Propietari de la tasca + propietari i membres de la llista (si en té).
        uids = set()
        if task.get("ownerId"):
            uids.add(task["ownerId"])
        list_id = task.get("listId")
        if list_id:
            ldata = get_list(list_id)
            if ldata:
                if ldata.get("ownerId"):
                    uids.add(ldata["ownerId"])
                for u in (ldata.get("sharedWith") or []):
                    uids.add(u)
        return uids

    smtp = {"conn": None}
    def send_email(to_addr, subject, body):
        if smtp["conn"] is None:
            smtp["conn"] = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context())
            smtp["conn"].login(GMAIL_USER, GMAIL_APP_PASSWORD)
        msg = EmailMessage()
        msg["From"] = f"Tasques <{GMAIL_USER}>"
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)
        smtp["conn"].send_message(msg)

    due = 0
    tasks = db.collection("tasks").where("reminderMinutes", ">", 0).stream()
    for t in tasks:
        d = t.to_dict()
        if d.get("status") == "completada":
            continue
        mins = int(d.get("reminderMinutes") or 0)
        if mins <= 0:
            continue
        # El recordatori es calcula respecte a l'INICI de la tasca (o la fi si no hi ha inici).
        if d.get("startDate"):
            base_date = d["startDate"]
            base_time = d.get("startTime") or "09:00"
        elif d.get("endDate"):
            base_date = d["endDate"]
            base_time = d.get("endTime") or "23:59"
        else:
            continue
        try:
            base_dt = datetime.fromisoformat(f"{base_date}T{base_time}").replace(tzinfo=TZ)
        except ValueError:
            continue
        rem_dt = base_dt - timedelta(minutes=mins)
        diff = (now - rem_dt).total_seconds()
        if not (0 <= diff < WINDOW_MIN * 60):
            continue

        rem_ts = int(rem_dt.timestamp())
        title = d.get("title", "Tasca")
        when = base_dt.strftime("%d/%m/%Y a les %H:%M")
        subject = f"⏰ Recordatori: {title}"
        body = (
            f"La tasca «{title}» comença el {when} (d'aquí {reminder_label(mins)}).\n\n"
            f"Obre l'app: {APP_URL}\n"
        )

        # Envia a tots els destinataris, amb control de duplicats independent per persona.
        for uid in recipients_for(d):
            log_id = f"{t.id}_{rem_ts}_{uid}"
            log_ref = db.collection("pushLog").document(log_id)
            if log_ref.get().exists:
                continue
            addr = get_email(uid)
            if not addr:
                continue
            due += 1
            if DRY_RUN:
                print(f"[DRY] {title} -> {addr}")
                continue
            try:
                send_email(addr, subject, body)
                log_ref.set({"sentAt": firestore.SERVER_TIMESTAMP, "taskId": t.id, "uid": uid, "email": addr})
                print(f"OK  {title} -> {addr}")
            except Exception as e:
                print(f"ERR {title} -> {addr}: {e}")

    if smtp["conn"] is not None:
        try:
            smtp["conn"].quit()
        except Exception:
            pass

    print(f"Fet. Recordatoris dins de finestra: {due}. Hora: {now.isoformat()}")


if __name__ == "__main__":
    main()
