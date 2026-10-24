# PEA Watch

Ce que fait l'outil, une fois par jour ouvré :

1. Il mesure la **baisse du MSCI World depuis son plus haut** (via CW8, en euros).
2. Il en déduit l'apport du mois : ×1 normalement, ×2 à −10 %, ×3 à −20 %, ×4 à −30 %.
3. Il calcule **quoi acheter** (nombre de parts et prix limite) pour ramener le portefeuille vers 80 % DCAM / 20 % CL2.
4. Il écrit une page `index.html` et **t'envoie un mail** dans trois cas :
   - le jour de l'apport mensuel (le `contribution_day` du config, ou le jour ouvré suivant) ;
   - le jour où un palier de baisse est franchi ;
   - le jour où une ligne sort de sa fourchette (±5 points).

Tout se règle dans `config.json` : apport, jour, ETF, cibles, paliers, fourchette.
**Après chaque achat, mets à jour tes quantités** (`qty`, ou le secret `PEA_POSITIONS`).

## Mail (Gmail)
Crée un *mot de passe d'application* sur myaccount.google.com → Sécurité → Validation en deux étapes → Mots de passe des applications.
Variables : `SMTP_USER` (ton adresse), `SMTP_PASS` (le mot de passe d'application), `MAIL_TO` (le destinataire).
Pour vérifier l'envoi : `python pea_watch.py --test-mail`.

## Option A : GitHub (gratuit, PC éteint OK, page consultable sur téléphone)
1. Crée un dépôt GitHub et pousse le contenu de ce dossier à sa racine.
2. Settings → Secrets → Actions : ajoute `SMTP_USER`, `SMTP_PASS`, `MAIL_TO` et `PEA_POSITIONS` (par exemple `{"DCAM.PA": 1200, "CL2.PA": 60}`).
3. Settings → Pages → Source : **GitHub Actions**.
4. Actions → PEA Watch → *Run workflow* pour un premier essai.

La page Pages est publique, donc les montants y sont masqués (`HIDE_AMOUNTS=1`). Les mails, eux, contiennent tout.
Si le dépôt est public, laisse les `qty` à 0 dans `config.json` et passe tes quantités par le secret.

## Option B : ton PC Windows
Renseigne `run_windows.bat`, puis crée une tâche dans le Planificateur de tâches : du lundi au vendredi à 16h, action = ce fichier.
La page est alors `out/index.html` (ouvre-la dans ton navigateur). Le PC doit être allumé à 16h.

## Tests
```
python pea_watch.py --dry-run                   # rien n'est envoyé, les mails s'affichent
python pea_watch.py --dry-run --force-monthly   # simule le mail d'achat du mois
```

Les règles sont fixées à l'avance : ce n'est pas un conseil en investissement. Les données Yahoo peuvent avoir un jour de retard.
