# Tasques

Aplicació personal de gestió de tasques amb llistes compartides, vistes (llista, calendari, matriu, Gantt), entrada per veu i **recordatoris per correu electrònic**.

- **App en producció:** https://joanmam.github.io/tasques-app/
- **Repositori:** https://github.com/joanmam/tasques-app

## Com està feta

- **Una sola pàgina `index.html`** amb React + Babel compilats al navegador (no hi ha pas de build de Vite; es publica servint l'arrel del repo).
- **Firebase:** Authentication (login), Firestore (tasques, llistes) i Storage (adjunts).
- **PWA:** `manifest.json` + `sw.js` (service worker) + icones. Es pot instal·lar al mòbil.
- **GitHub Pages** per a l'allotjament i **GitHub Actions** per al desplegament i els recordatoris.

## Desplegament

El desplegament és automàtic: qualsevol `push` a la branca `main` dispara el workflow `.github/workflows/deploy.yml`, que publica tot el repo a GitHub Pages.

```bash
git add index.html               # i els fitxers que toqui
git commit -m "descripció del canvi"
git push origin main
```

El número de versió visible a la barra lateral (p. ex. `v4.4`) es puja a mà a `index.html` a cada canvi, per saber fàcilment quina versió té carregada el mòbil.

### Notes / problemes coneguts en fer commit (Windows)

- Si surt `Unable to create '.git/index.lock': File exists`, esborra el bloqueig:
  `Remove-Item ".git\index.lock" -Force`
- Si el `push` diu `'credential-manager-core' is not a git command`, canvia el gestor de credencials:
  `git config --global credential.helper manager`

## Recordatoris per correu electrònic

Els recordatoris s'envien per **correu** (Gmail). Es va provar abans amb notificacions push del navegador, però es va descartar perquè el web push donava molts problemes (permisos, una sola subscripció per compte, missatges descartats en silenci). El correu és molt més fiable i no cal configurar res al mòbil.

### Com funciona

1. El workflow `.github/workflows/reminders.yml` executa `scripts/send_reminders.py` **cada ~10 minuts** (i també es pot llançar a mà amb *Run workflow*).
2. L'script busca a Firestore les tasques amb recordatori, calcula quines toquen ara i envia un correu als destinataris.
3. L'adreça de cada destinatari s'obté automàticament de Firebase Authentication (`auth.get_user(uid).email`).

### Quan s'envia un recordatori

El moment del recordatori es calcula respecte a l'**hora d'inici** de la tasca:

```
moment_recordatori = (startDate + startTime)  −  reminderMinutes
```

Si la tasca no té data d'inici, es fa servir la de fi (`endDate`/`endTime`) com a alternativa. L'script envia el correu si l'hora actual cau dins d'una finestra de ~25 minuts a partir d'aquest moment (per absorbir el retard del cron de GitHub). Cada recordatori s'envia un sol cop per persona (control de duplicats a la col·lecció `pushLog`).

> Nota: l'**entrega** del correu és fiable, però l'**hora** depèn del `schedule` de GitHub Actions, que pot retardar-se uns minuts.

### A qui arriba

- Tasca en una llista **no compartida** → només al correu del compte propietari de la tasca.
- Tasca en una llista **compartida** → al propietari de la llista **i** a tots els comptes amb qui es comparteix.

### Secrets (GitHub → Settings → Secrets and variables → Actions)

El workflow necessita dos *repository secrets*:

- `FIREBASE_SERVICE_ACCOUNT` — contingut JSON del compte de servei de Firebase.
- `GMAIL_APP_PASSWORD` — contrasenya d'aplicació de Gmail (16 lletres). Cal tenir la verificació en 2 passos activada al compte de Google per generar-la a https://myaccount.google.com/apppasswords.

Opcionalment, `GMAIL_USER` (remitent) si no és `joanmam@gmail.com`, que és el valor per defecte.

*(El secret `VAPID_JSON`, de l'època del push, ja no es fa servir.)*

### Carpeta `push-sender/` (fora del repo)

Conté `service-account.json` per a proves locals i **no s'ha de pujar mai** (està al `.gitignore`):

```bash
cd push-sender
export GMAIL_APP_PASSWORD="..."          # la contrasenya d'aplicació
python send_reminders.py --dry-run        # llista què s'enviaria, sense enviar
```

### Provar que arriben els correus

1. Crea una tasca amb **hora d'inici = ara** i recordatori "5 minuts abans" (així el moment del recordatori queda just enrere, dins de la finestra).
2. Ves a **Actions → Recordatoris push → Run workflow** i, quan acabi, mira el log del pas *"Enviar recordatoris vençuts"*:
   - `OK <títol> -> <correu>` → enviat correctament (revisa també la carpeta de spam el primer cop).
   - `Fet. Recordatoris dins de finestra: 0` → cap recordatori toca ara (revisa hores).
   - `ERROR: falta el secret GMAIL_APP_PASSWORD` o `ERR ...` → problema amb el secret o l'adreça.

## Nota sobre el front-end de notificacions push

`index.html` encara conté el botó ON/OFF de notificacions i `sw.js` manté el handler de push, restes de quan els recordatoris eren push. Ara **no s'usen per enviar** (l'enviament és per correu), però es deixen per si en el futur es vol recuperar el push. El service worker segueix sent útil perquè fa que el mòbil carregui sempre l'última versió (estratègia *network-first*).

## Estructura

```
index.html                      App sencera (React + Babel)
manifest.json, sw.js, icon-*    PWA (service worker + icones)
scripts/send_reminders.py       Enviador de recordatoris per correu (GitHub Actions)
scripts/requirements.txt        Dependències de l'script
.github/workflows/deploy.yml    Desplegament a GitHub Pages
.github/workflows/reminders.yml Cron de recordatoris (cada ~10 min)
push-sender/                    service-account.json per a proves locals (NO al repo)
```
