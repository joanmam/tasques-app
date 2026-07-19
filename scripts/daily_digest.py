#!/usr/bin/env python3
"""
Resum diari per CORREU ELECTRÒNIC de l'app Tasques.
Cada dia envia a l'usuari (per defecte joanmam@gmail.com) un correu amb:
  - Tasques i subtasques VENÇUDES (agrupades per llista)
  - Tasques i subtasques d'AVUI (agrupades per llista)
Pensat per executar-se un cop al dia des de GitHub Actions (cron extern
tipus cron-job.org a les 6:00, seguint el mateix patró que reminders.yml).

Secrets (variables d'entorn, via GitHub Secrets):
    FIREBASE_SERVICE_ACCOUNT  -> contingut JSON del compte de servei de Firebase
    GMAIL_APP_PASSWORD        -> contrasenya d'aplicació de Gmail (16 caràcters)
    GMAIL_USER (opcional)     -> adreça Gmail remitent (per defecte joanmam@gmail.com)
    DIGEST_EMAIL (opcional)   -> adreça destinatària del resum (per defecte joanmam@gmail.com)

Per a proves locals: posa service-account.json en aquest directori i exporta
GMAIL_APP_PASSWORD (i opcionalment GMAIL_USER / DIGEST_EMAIL), i executa amb --dry-run
per veure el contingut del correu sense enviar-lo ni marcar-lo com enviat.
"""
import os, sys, json, smtplib, ssl
from email.message import EmailMessage
from datetime import datetime
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, firestore, auth

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Europe/Madrid")
DRY_RUN = "--dry-run" in sys.argv
FORCE = "--force" in sys.argv          # ignora el dedup diari (per fer proves)
APP_URL = "https://joanmam.github.io/tasques-app/"

GMAIL_USER = os.environ.get("GMAIL_USER") or "joanmam@gmail.com"
GMAIL_APP_PASSWORD = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
DIGEST_EMAIL = os.environ.get("DIGEST_EMAIL") or "joanmam@gmail.com"


def init_db():
    env = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if env:
        cred = credentials.Certificate(json.loads(env))
    else:
        cred = credentials.Certificate(os.path.join(HERE, "service-account.json"))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


def task_due_str(d):
    """Mateixa convenció que taskDueStr/sortDateStr al front-end: una ocurrència
    d'una sèrie recurrent (seriesId) nomes fa servir la seva pròpia endDate,
    perquè la startDate es la de tota la sèrie (compartida, no d'aquest dia)."""
    if d.get("seriesId"):
        return d.get("endDate") or ""
    return d.get("endDate") or d.get("startDate") or ""


def fmt_date(ds):
    try:
        return datetime.strptime(ds, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return ds or ""


def main():
    db = init_db()
    now = datetime.now(TZ)
    today_str = now.strftime("%Y-%m-%d")
    today_label = now.strftime("%d/%m/%Y")

    if not DRY_RUN and not GMAIL_APP_PASSWORD:
        print("ERROR: falta el secret GMAIL_APP_PASSWORD")
        sys.exit(1)

    # --- Dedup: nomes un resum per dia (per si el trigger extern es dispara 2 cops) ---
    digest_log_ref = db.collection("dailyDigestLog").document(today_str)
    if not FORCE and not DRY_RUN and digest_log_ref.get().exists:
        print(f"Ja s'ha enviat el resum d'avui ({today_str}). Sortint.")
        return

    # --- Trobar l'usuari destinatari ---
    try:
        joan_uid = auth.get_user_by_email(DIGEST_EMAIL).uid
    except Exception as e:
        print(f"ERROR: no s'ha trobat cap usuari amb l'email {DIGEST_EMAIL}: {e}")
        sys.exit(1)

    # --- Llistes: cache + saber quines son "de Joan" (propietari o membre) ---
    lists_cache = {}
    joan_list_ids = set()
    for doc in db.collection("lists").stream():
        ldata = doc.to_dict() or {}
        lists_cache[doc.id] = ldata
        if ldata.get("ownerId") == joan_uid or joan_uid in (ldata.get("sharedWith") or []):
            joan_list_ids.add(doc.id)

    def list_name(list_id):
        ldata = lists_cache.get(list_id)
        return (ldata or {}).get("name") or "Sense llista"

    def belongs_to_joan(d):
        if d.get("ownerId") == joan_uid:
            return True
        if d.get("assigneeId") == joan_uid:
            return True
        if d.get("listId") in joan_list_ids:
            return True
        return False

    overdue_by_list = {}   # list_id -> {"tasks":[...], "subtasks":[...]}
    today_by_list = {}

    def bucket(store, list_id):
        return store.setdefault(list_id, {"tasks": [], "subtasks": []})

    n_overdue_tasks = n_overdue_sub = n_today_tasks = n_today_sub = 0

    for doc in db.collection("tasks").stream():
        d = doc.to_dict() or {}
        if not belongs_to_joan(d):
            continue
        list_id = d.get("listId") or "__none__"

        # --- Tasca principal ---
        if d.get("status") != "completada":
            due = task_due_str(d)
            if due and due < today_str:
                bucket(overdue_by_list, list_id)["tasks"].append(d)
                n_overdue_tasks += 1
            elif due == today_str:
                bucket(today_by_list, list_id)["tasks"].append(d)
                n_today_tasks += 1

        # --- Subtasques ---
        for s in (d.get("subtasks") or []):
            if s.get("done"):
                continue
            title = (s.get("title") or "").strip()
            if not title:
                continue
            sdue = s.get("due") or s.get("start") or ""
            if not sdue:
                continue
            entry = {"title": title, "due": sdue, "parent": d.get("title") or "Tasca"}
            if sdue < today_str:
                bucket(overdue_by_list, list_id)["subtasks"].append(entry)
                n_overdue_sub += 1
            elif sdue == today_str:
                bucket(today_by_list, list_id)["subtasks"].append(entry)
                n_today_sub += 1

    def task_line(d):
        time_prefix = f"{d.get('startTime')} " if d.get("startTime") else ""
        due = task_due_str(d)
        return f"  - {time_prefix}{d.get('title', 'Tasca')} (venç {fmt_date(due)})"

    def sub_line(s):
        return f"    • {s['title']}  [{s['parent']}]"

    def render_section(title_icon, title_text, by_list):
        if not by_list:
            return ""
        lines = [f"{title_icon} {title_text}"]
        for list_id, data in sorted(by_list.items(), key=lambda kv: list_name(kv[0]).lower()):
            if not data["tasks"] and not data["subtasks"]:
                continue
            lines.append(f"\n[{list_name(list_id)}]")
            for d in sorted(data["tasks"], key=lambda x: task_due_str(x)):
                lines.append(task_line(d))
            for s in sorted(data["subtasks"], key=lambda x: x["due"]):
                lines.append(sub_line(s))
        lines.append("")
        return "\n".join(lines)

    total = n_overdue_tasks + n_overdue_sub + n_today_tasks + n_today_sub

    body_parts = [f"Resum de tasques — {today_label}\n"]
    overdue_section = render_section("⚠️", "VENÇUDES", overdue_by_list)
    today_section = render_section("📅", "AVUI", today_by_list)
    if overdue_section:
        body_parts.append(overdue_section)
    if today_section:
        body_parts.append(today_section)
    if total == 0:
        body_parts.append("Cap tasca vençuda ni per avui. 🎉\n")
    body_parts.append(f"Obre l'app: {APP_URL}")
    body = "\n".join(body_parts)

    subject = f"📋 Resum de tasques — {today_label}" + (f" ({total})" if total else "")

    print(body)
    print(f"--- Total: {n_overdue_tasks} tasques vençudes, {n_overdue_sub} subtasques vençudes, "
          f"{n_today_tasks} tasques avui, {n_today_sub} subtasques avui ---")

    if DRY_RUN:
        print("[DRY RUN] No s'envia ni es marca res.")
        return

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        msg = EmailMessage()
        msg["From"] = f"Tasques <{GMAIL_USER}>"
        msg["To"] = DIGEST_EMAIL
        msg["Subject"] = subject
        msg.set_content(body)
        smtp.send_message(msg)

    digest_log_ref.set({
        "sentAt": firestore.SERVER_TIMESTAMP,
        "date": today_str,
        "overdueTasks": n_overdue_tasks,
        "overdueSubtasks": n_overdue_sub,
        "todayTasks": n_today_tasks,
        "todaySubtasks": n_today_sub,
    })
    print(f"OK  resum enviat a {DIGEST_EMAIL}")


if __name__ == "__main__":
    main()
