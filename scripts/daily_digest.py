#!/usr/bin/env python3
"""
Resum diari per CORREU ELECTRÒNIC de l'app Tasques.
Cada dia, CADA usuari de l'app (propietari o membre d'alguna llista, o
propietari/assignat d'alguna tasca) rep el seu propi correu amb:
  - Tasques i subtasques VENÇUDES (agrupades per llista)
  - Tasques i subtasques d'AVUI (agrupades per llista)
Pensat per executar-se un cop al dia des de GitHub Actions (cron extern
tipus cron-job.org a les 6:00, seguint el mateix patró que reminders.yml).

Secrets (variables d'entorn, via GitHub Secrets):
    FIREBASE_SERVICE_ACCOUNT  -> contingut JSON del compte de servei de Firebase
    GMAIL_APP_PASSWORD        -> contrasenya d'aplicació de Gmail (16 caràcters)
    GMAIL_USER (opcional)     -> adreça Gmail remitent (per defecte joanmam@gmail.com)
    ONLY_EMAIL  (opcional)    -> si es posa, nomes s'envia el resum a aquest usuari
                                 (per fer proves; a l'execucio normal es deixa buit
                                 i s'envia a tothom). Tambe es pot passar com a
                                 argument: --only=algu@example.com

Per a proves locals: posa service-account.json en aquest directori i exporta
GMAIL_APP_PASSWORD, i executa amb --dry-run per veure el contingut sense enviar
res ni marcar-ho com enviat. Combina'l amb --only=email@... per mirar el resum
d'una sola persona.
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

ONLY_EMAIL = os.environ.get("ONLY_EMAIL") or ""
for arg in sys.argv[1:]:
    if arg.startswith("--only="):
        ONLY_EMAIL = arg.split("=", 1)[1]
ONLY_EMAIL = ONLY_EMAIL.strip()


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


def bucket(store, list_id):
    return store.setdefault(list_id, {"tasks": [], "subtasks": []})


def task_line(d):
    time_prefix = f"{d.get('startTime')} " if d.get("startTime") else ""
    due = task_due_str(d)
    return f"  - {time_prefix}{d.get('title', 'Tasca')} (venç {fmt_date(due)})"


def sub_line(s):
    return f"    • {s['title']}  [{s['parent']}]"


def render_section(title_icon, title_text, by_list, list_name):
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


def build_digest_for_user(uid, today_str, lists_cache, all_tasks, list_name):
    """Calcula el resum (vençudes/avui, agrupat per llista) d'un usuari concret."""
    user_list_ids = {
        list_id for list_id, ldata in lists_cache.items()
        if ldata.get("ownerId") == uid or uid in (ldata.get("sharedWith") or [])
    }

    def belongs_to_user(d):
        if d.get("ownerId") == uid:
            return True
        if d.get("assigneeId") == uid:
            return True
        if d.get("listId") in user_list_ids:
            return True
        return False

    overdue_by_list, today_by_list = {}, {}
    counts = {"overdue_tasks": 0, "overdue_sub": 0, "today_tasks": 0, "today_sub": 0}

    for d in all_tasks:
        if not belongs_to_user(d):
            continue
        list_id = d.get("listId") or "__none__"

        if d.get("status") != "completada":
            due = task_due_str(d)
            if due and due < today_str:
                bucket(overdue_by_list, list_id)["tasks"].append(d)
                counts["overdue_tasks"] += 1
            elif due == today_str:
                bucket(today_by_list, list_id)["tasks"].append(d)
                counts["today_tasks"] += 1

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
                counts["overdue_sub"] += 1
            elif sdue == today_str:
                bucket(today_by_list, list_id)["subtasks"].append(entry)
                counts["today_sub"] += 1

    return overdue_by_list, today_by_list, counts


def main():
    db = init_db()
    now = datetime.now(TZ)
    today_str = now.strftime("%Y-%m-%d")
    today_label = now.strftime("%d/%m/%Y")

    if not DRY_RUN and not GMAIL_APP_PASSWORD:
        print("ERROR: falta el secret GMAIL_APP_PASSWORD")
        sys.exit(1)

    # --- Carrega llistes i tasques un sol cop (es reutilitzen per a tots els usuaris) ---
    lists_cache = {}
    for doc in db.collection("lists").stream():
        lists_cache[doc.id] = doc.to_dict() or {}

    def list_name(list_id):
        ldata = lists_cache.get(list_id)
        return (ldata or {}).get("name") or "Sense llista"

    all_tasks = [doc.to_dict() or {} for doc in db.collection("tasks").stream()]

    # --- Qui rep resum: tothom que aparegui com a propietari/assignat d'una tasca,
    #     o propietari/membre d'una llista ---
    uids = set()
    for ldata in lists_cache.values():
        if ldata.get("ownerId"):
            uids.add(ldata["ownerId"])
        for u in (ldata.get("sharedWith") or []):
            uids.add(u)
    for d in all_tasks:
        if d.get("ownerId"):
            uids.add(d["ownerId"])
        if d.get("assigneeId"):
            uids.add(d["assigneeId"])

    if ONLY_EMAIL:
        try:
            only_uid = auth.get_user_by_email(ONLY_EMAIL).uid
        except Exception as e:
            print(f"ERROR: no s'ha trobat cap usuari amb l'email {ONLY_EMAIL}: {e}")
            sys.exit(1)
        uids = {only_uid}
        print(f"[Mode prova] nomes s'envia a {ONLY_EMAIL}")

    smtp_conn = None

    def get_smtp():
        nonlocal smtp_conn
        if smtp_conn is None:
            ctx = ssl.create_default_context()
            smtp_conn = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx)
            smtp_conn.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        return smtp_conn

    sent = 0
    for uid in uids:
        try:
            addr = auth.get_user(uid).email
        except Exception:
            addr = None
        if not addr:
            continue

        # Dedup per usuari i dia (per si el trigger extern es dispara 2 cops)
        digest_log_ref = db.collection("dailyDigestLog").document(f"{today_str}_{uid}")
        if not FORCE and not DRY_RUN and digest_log_ref.get().exists:
            print(f"[skip] {addr}: ja enviat avui")
            continue

        overdue_by_list, today_by_list, c = build_digest_for_user(
            uid, today_str, lists_cache, all_tasks, list_name
        )
        total = c["overdue_tasks"] + c["overdue_sub"] + c["today_tasks"] + c["today_sub"]

        body_parts = [f"Resum de tasques — {today_label}\n"]
        overdue_section = render_section("⚠️", "VENÇUDES", overdue_by_list, list_name)
        today_section = render_section("📅", "AVUI", today_by_list, list_name)
        if overdue_section:
            body_parts.append(overdue_section)
        if today_section:
            body_parts.append(today_section)
        if total == 0:
            body_parts.append("Cap tasca vençuda ni per avui. 🎉\n")
        body_parts.append(f"Obre l'app: {APP_URL}")
        body = "\n".join(body_parts)

        subject = f"📋 Resum de tasques — {today_label}" + (f" ({total})" if total else "")

        print(f"===== {addr} =====")
        print(body)
        print(f"--- {c['overdue_tasks']} tasques vençudes, {c['overdue_sub']} subtasques vençudes, "
              f"{c['today_tasks']} tasques avui, {c['today_sub']} subtasques avui ---")

        if DRY_RUN:
            continue

        try:
            msg = EmailMessage()
            msg["From"] = f"Tasques <{GMAIL_USER}>"
            msg["To"] = addr
            msg["Subject"] = subject
            msg.set_content(body)
            get_smtp().send_message(msg)
            digest_log_ref.set({
                "sentAt": firestore.SERVER_TIMESTAMP,
                "date": today_str,
                "uid": uid,
                "email": addr,
                **{k: v for k, v in c.items()},
            })
            sent += 1
            print(f"OK  resum enviat a {addr}")
        except Exception as e:
            print(f"ERR resum a {addr}: {e}")

    if smtp_conn is not None:
        try:
            smtp_conn.quit()
        except Exception:
            pass

    if DRY_RUN:
        print("[DRY RUN] No s'ha enviat ni marcat res.")
    else:
        print(f"Fet. Resums enviats: {sent}.")


if __name__ == "__main__":
    main()
