# ✅ Tasques

Context del projecte **tasques-app** per a Claude Code. Respon sempre en **català**. L'usuari és en Joan (masculí).

## Què és

App personal de gestió de tasques (PWA) amb llistes compartides, vistes Llista / Calendari / Matriu / Gantt / Seguiment / Subtasques, entrada per veu, recurrències, contactes (sincronitzats de Notion) i **recordatoris + resum diari per correu**.

- Repo: https://github.com/joanmam/tasques-app (públic)
- Producció: **https://joanmam-tasques.netlify.app** (Netlify, projecte `joanmam-tasques`, connectat a `github.com/joanmam/tasques-app`; auto publishing de `main` activat, `netlify.toml` amb publish = "."). Cada pujada a `main` es desplega sola; per comprovar-ho, mira a Netlify → Deploys que el commit publicat (`main@xxxxxxx`) coincideixi amb l'últim d'`origin/main`.
- Projecte Firebase: `tasques-app` (Firestore `(default)`, europe-west3, mode natiu).

## Arquitectura (IMPORTANT)

- **Tota l'app és un sol fitxer: `index.html`** (~4300 línies). React 18 + Babel standalone + Firebase compat 10.12 carregats per CDN; el codi va dins `<script type="text/babel">` (a partir de ~línia 368).
- **No hi ha pas de build.** Es publica l'arrel del repo tal qual.
- `src/`, `vite.config.js`, `package.json`, `eslint.config.js`, `public/` són restes de la plantilla Vite inicial: **no s'utilitzen**. No els editis pensant que afecten l'app.
- PWA: `manifest.json`, `sw.js` (network-first, cau `tasques-cache-v2`), `icon-192/512/maskable.png`.
- Versió visible a la barra lateral (`v4.8` ara, ~línia 1607 d'`index.html`): **puja-la a mà a cada canvi** perquè en Joan sàpiga quina versió té el mòbil.

### Components principals d'`index.html`
`LoginScreen`, `App` (estat global, ~l.853), `TaskItem`, `DetailPanel`, `NewTaskModal`, `ListSettingsModal`, `CalendarView`, `MatrixView`, `GanttView` (~l.3969), `SeguimentView`, `SubtasksView`, `ContactPicker`, `AssignSection`, `WatcherSection`.
Helpers de recurrència: `RECURRENCE_OPTIONS`, `RECURRENCE_INTERVAL_TYPES`, `nextOccurrence(dateStr, recurrence, interval)`, `recurrenceLabel(task)`.

### Firestore (col·leccions)
`tasks`, `lists` (amb `ownerId`, `sharedWith`), `users`, `userSettings`, `completionLogs`, `contacts` (privats per `ownerId`, només escriu l'Admin SDK), `pushLog` (dedup de recordatoris), `pushSubscriptions` (llegat), `digestRequests`, `syncRequests`.
Regles vigents: `../firestore-rules-v8.txt` (fora del repo; es copien a mà a la consola de Firebase).

Camps de tasca rellevants: `startDate`/`startTime`, `endDate`/`endTime`, `reminderMinutes`, `recurrence`, `recurrenceInterval` (per defecte 1), `assigneeId`, `watchers`, `url`, subtasques.

## Scripts i GitHub Actions (`scripts/`, Python)

| Workflow | Script | Quan |
|---|---|---|
| `reminders.yml` | `send_reminders.py` (+ `daily_digest.py --process-requests`, `notion_contacts_weekly_sync.py --if-requested`) | cada ~10 min, disparat per **cron-job.org** via `workflow_dispatch`; el `schedule:` és només backup |
| `daily-digest.yml` | `daily_digest.py` | 6:00 Europe/Madrid via cron-job.org (input `only_email` per proves) |
| `notion-contacts-sync.yml` | `notion_contacts_weekly_sync.py` | dilluns |
| `backfill-recurring-once.yml`, `import-notion-once.yml` | scripts d'un sol ús | manual |
| `deploy.yml` | — | desactivat (només manual) |

- Correus via Gmail SMTP (`GMAIL_APP_PASSWORD`, `GMAIL_USER`). Email del destinatari amb `auth.get_user(uid).email`.
- Recordatori = (`startDate`+`startTime`) − `reminderMinutes` (si no hi ha inici, s'usa la fi). Finestra ~25 min. Idempotent gràcies a `pushLog`.
- Routing: llista no compartida → propietari; llista compartida → propietari + `sharedWith`.
- Secrets: `FIREBASE_SERVICE_ACCOUNT`, `GMAIL_APP_PASSWORD` (`VAPID_JSON` llegat).
- ⚠️ El PAT de GitHub que fa servir cron-job.org **caduca el 30/12/2026**. Guies: `../recordatoris-cron-extern.md`, `../resum-diari-cron-extern.md`.

### Lògica duplicada JS ↔ Python
Si canvies `nextOccurrence` o la finestra de dates (`inDateWindow`) a `index.html`, actualitza també la rèplica a `scripts/backfill_recurring_occurrences.py` / `scripts/daily_digest.py`. Han de donar exactament el mateix resultat.

### Dependències Python fixades
`scripts/requirements.txt` fixa `google-cloud-firestore==2.28.1`, `google-api-core==2.34.0`, `google-auth==2.56.3` per una regressió de Google (error fals "Invalid database id (default)", 24/08/2026). **No els treguis** sense provar abans amb un `workflow_dispatch`. Si torna a fallar tot de cop sense canvis de codi, sospita primer de releases noves a PyPI.

## Com treballar-hi

- **Abans de donar un canvi per bo, verifica:**
  1. Que el bloc `<script type="text/babel">` transpila sense errors (p. ex. extreure'l i passar-lo per `@babel/preset-react` amb node).
  2. Proves node/python de la lògica tocada (dates, recurrències, Gantt) amb dades sintètiques. Compte amb les hores: compara dates a mitjanit (`setHours(0,0,0,0)`) — ja hi va haver un bug de decalatge d'un dia al Gantt.
  3. Per als scripts Python, prova amb `firebase_admin` mockejat; no toquis la Firestore real sense permís.
- Tot el text de la UI és en **català**.
- Edita amb canvis puntuals; no reescriguis `index.html` sencer.
- **No facis `git push`** ni commits sense que en Joan ho demani. Normalment ell puja els canvis (git a Windows o "Add file → Upload files" al web de GitHub). Després d'una pujada pel web cal `git pull` al clone local.
- El clone local pot estar desincronitzat del remot (hi ha hagut pujades pel web). Fes `git fetch` + `git status` abans de res.
- Problemes coneguts a Windows: `.git/index.lock` penjat → `Remove-Item .git\index.lock -Force`; error de credencials → `git config --global credential.helper manager`.
- **Mai** pugis secrets: `service-account.json`, `vapid_keys.json`, `push-sender/` (fora del repo). No commitegis `Subtasques.xlsx` ni `index.html.restored`.

## Pendents / idees
- Renovar el PAT de cron-job.org abans del 30/12/2026.
- Revisar de tant en tant si es poden treure els pins de Google a `requirements.txt`.
- Si tornen els "No jobs were run" de GitHub Actions, valorar `concurrency:` a `reminders.yml`.
