# Bot Telegram (version GitHub Actions) – Alertes économiques 3★

## Comment ça marche
Contrairement à une VM classique (boucle infinie), cette version s'exécute
**une fois toutes les 5 minutes** via un cron GitHub Actions : elle vérifie
le calendrier, envoie les alertes nécessaires, sauvegarde son état dans
`notified_events.json`, puis s'arrête. GitHub relance le script tout seul
5 minutes plus tard. Zéro serveur, zéro carte bancaire.

⚠️ **Limite à connaître** : GitHub ne garantit pas une précision à la minute
près pour les tâches planifiées (délais possibles en période de forte
charge, parfois plusieurs minutes). Pour ne jamais rater totalement une
alerte, le bot envoie dès qu'il reste **≤ 10 minutes** avant l'annonce
(au lieu d'exiger précisément 5-10 min) — donc l'alerte arrivera toujours
avant l'annonce, mais pas toujours avec exactement le même délai.

## 1. Créer le dépôt GitHub
1. Va sur https://github.com/new
2. Nom : `telegram-econ-bot` (ou ce que tu veux).
3. **Visibilité : Public.** C'est important : ça te donne des minutes
   GitHub Actions **illimitées et gratuites**. Tes identifiants restent
   protégés quand même — ils sont stockés en "secrets" chiffrés, jamais
   visibles dans le code ni dans les logs.
4. Crée le dépôt (pas besoin de README, on a déjà tout).

## 2. Envoyer les fichiers de ce dossier
Depuis ce dossier, sur ton PC :
```
git init
git add .
git commit -m "Bot alertes economiques"
git branch -M main
git remote add origin https://github.com/<ton-pseudo>/telegram-econ-bot.git
git push -u origin main
```

## 3. Ajouter tes secrets
Sur la page du dépôt GitHub :
**Settings → Secrets and variables → Actions → New repository secret**
- `BOT_TOKEN` → ton token @BotFather
- `CHAT_ID` → ton chat_id (récupéré avec `get_chat_id.py`, cf. dossier
  précédent, ou via `https://api.telegram.org/bot<TOKEN>/getUpdates`)

## 4. Autoriser le bot à sauvegarder son état
**Settings → Actions → General → Workflow permissions**
→ coche **"Read and write permissions"** → Save.
(Sans ça, le workflow ne peut pas commit `notified_events.json`, et il
renverra les mêmes alertes en boucle à chaque exécution.)

## 5. Tester tout de suite
Onglet **Actions** du dépôt → workflow **"Alertes economiques Telegram"**
→ bouton **Run workflow** (en haut à droite) pour le lancer manuellement,
sans attendre le prochain cron. Regarde les logs de l'étape "Vérifier le
calendrier et envoyer les alertes" pour confirmer que tout fonctionne.

## 6. C'est tout
Le workflow tourne désormais tout seul toutes les 5 minutes. Tu peux
suivre l'historique des exécutions dans l'onglet **Actions**.
