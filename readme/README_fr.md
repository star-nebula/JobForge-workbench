# JobForge Workbench

**Language / 语言 / 言語 / Idioma / 언어 / Langues / Sprachen:** [English](../README.md) | [简体中文](README_zh-CN.md) | [日本語](README_ja.md) | [한국어](README_ko.md) | [Français](README_fr.md) | [Deutsch](README_de.md) | [Español](README_es.md)

Atelier de recherche d'emploi local : CV → mots-clés → scraping d'offres (BOSS Zhipin) → classement par compatibilité, le tout dans un outil mono-poste intégré.

> **Plateforme : Windows 10/11 uniquement.** Le canal de scraping natif pilote Chrome de bureau via Windows UI Automation (pywinauto / UIA TextPattern) et l'automatisation clavier-souris (pyautogui / pygetwindow) ; ces bibliothèques réservées à Windows sont importées au démarrage du serveur — sur macOS/Linux, le serveur ne démarre pas. Chrome de bureau et Python 3.10+ sont également requis.

![Tableau de bord](../docs/screenshots/dashboard.png)

## Fonctionnalités

- **Profil** : import et analyse du CV, comparaison et adoption champ par champ, notation locale par règles avec suggestions d'amélioration, aperçu du CV, export PDF
- **Scraping intelligent** : récupère les offres BOSS à partir des mots-clés du CV (canal navigateur natif), avec historique de scraping et déduplication par offre
- **Marché des offres** : persistance des offres en base, récupération du JD avec nettoyage du texte, tri par compatibilité, statistiques de récupération des JD (obtenu / non obtenu / possiblement incomplet — cliquez sur les pastilles pour filtrer ; les JD possiblement incomplets peuvent être confirmés manuellement ou récupérés à nouveau depuis la fenêtre de détail). La compatibilité se fait en deux temps : **présélection par étiquettes** (règles locales sur 4 dimensions : compétences / intention / salaire / ville ; le dénominateur des compétences est constitué des étiquettes de l'offre, correspondance exacte après normalisation des termes, salaire normalisé en K, vrai 0-100 sans plancher), calculée au moment du scraping ; et **ajustement fin du JD** (un LLM lit le JD complet plus les informations dures — étiquettes / salaire / ville), produit dans la fenêtre de détail ou via l'analyse par lots ; les cartes portent un badge bleu « AI xx »
- **Capacités IA** (configuration multi-modèles, protocole compatible OpenAI, clés stockées uniquement dans le SQLite local) : génération de messages d'accroche BOSS, analyse IA de compatibilité (exécution automatique dans la fenêtre de détail + analyse par lots en un clic sur le marché), affinage du CV (comparaison diff avant adoption)
- **Pipeline de candidatures** : tableau kanban avec glisser-déposer sur 6 statuts (discovered / reviewing / applied / interviewing / rejected / offered)
- **Agenda d'entretiens** : planification des dates et notes depuis la colonne « entretien en cours »
- **Centre de messages** : synchronisation en lecture seule des conversations BOSS Zhipin (interception de réponses CDP)
- **Fenêtre de progression flottante** : reste au premier plan pendant le scraping avec boutons pause / reprise / arrêt. La fenêtre ne vole pas le focus et laisse passer les clics de souris, elle **n'interrompt donc pas une session de scraping clavier-souris en cours** ; déplacez-la en faisant glisser la poignée ⠿

## Structure du projet

```
JobForge-workbench/
├─ src/jobforge/                 # Code : paquet Python
│  ├─ server.py                  # Point d'entrée FastAPI
│  ├─ paths.py                   # Source unique des chemins du projet (code et données découplés)
│  ├─ spider.py  fetch_jd.py  fetch_jd_native.py  fetch_gate.py
│  ├─ db.py  llm.py  profile_score.py
│  └─ tools/                     # Scripts de sous-processus, lancés par le server via `python -m jobforge.tools.*`
│     └─ hud.py  messages.py  grab_cookies.py
├─ web/job-workbench.html        # Page unique du frontend (6 vues)
├─ data/                         # Données d'exécution (non versionnées) : jobs.db, cookies.json, messages.json, fichiers de porte/limitation, chrome-profile/
├─ tests/                        # Régressions pytest (`pip install -r requirements-dev.txt` d'abord, puis `venv\Scripts\python.exe -m pytest tests/` ; le nombre de tests n'est pas documenté — exécutez-les pour le voir)
├─ run.bat  setup.bat  requirements.txt  README.md
```

Tous les chemins de fichiers de données proviennent de `paths.py` ; aucun module ne les déduit de son propre `__file__` — déplacer le code n'emmène jamais les données.
Le user-data-dir du Chrome débogué via CDP (contenant la session BOSS) vit lui aussi dans le répertoire de données : `data/chrome-profile/`.

Il n'existe qu'un seul vocabulaire pour les statuts de candidature : le `STATUS_META` du frontend (les colonnes du kanban, le sélecteur de statut de la fenêtre de détail et le pipeline du tableau de bord en dérivent tous)
et le `db.VALID_STATUSES` du backend. Les deux ensembles sont identiques et chaque statut dispose d'une voie d'écriture accessible, verrouillé par
`tests/test_frontend_status_contract.py` — un jour, 6 statuts étaient déclarés mais le kanban n'affichait que 4 colonnes :
`rejected`/`offered` étaient impossibles à définir depuis l'UI et 114 offres en base n'avaient plus que deux valeurs.

Attention : les fichiers `.bat` doivent conserver des fins de ligne CRLF (`.gitattributes` déclare `*.bat text eol=crlf`) — dans la configuration « changement de page de code via `chcp` + commentaires en chinois + LF brut », cmd.exe désaligne son analyse par offset d'octets et avale silencieusement le début de la ligne `set "PYTHONPATH=..."`, ce qui se manifeste par `ModuleNotFoundError: No module named 'jobforge'` au démarrage.

Le démarrage manuel (IDE / ligne de commande) exige que `src` soit dans `PYTHONPATH`, sinon `import jobforge` échoue :

```
set PYTHONPATH=%CD%\src
venv\Scripts\python.exe -m jobforge.server
```

## Architecture

| Fichier | Rôle |
|---|---|
| `src/jobforge/server.py` | Point d'entrée FastAPI, `127.0.0.1:8080` (`--port` modifiable, pratique pour déboguer une seconde instance) |
| `src/jobforge/paths.py` | Source unique des chemins : `PROJECT_ROOT` / `DATA_DIR` / `WEB_DIR` |
| `src/jobforge/spider.py` / `fetch_jd_native.py` | Scraping de la liste d'offres et du JD (clavier/souris natif + canal UIA TextPattern) |
| `src/jobforge/fetch_jd.py` | Entrée générale de récupération du JD (natif en priorité, repli CDP) + nettoyage du texte du JD |
| `src/jobforge/tools/hud.py` | Fenêtre de progression flottante (processus indépendant ; les trois contraintes de fenêtre — premier plan + pas de vol de focus + clics traversants — sont documentées dans le commentaire d'en-tête) |
| `src/jobforge/fetch_gate.py` | Porte de pause/arrêt inter-processus : le signal est écrit dans `data/fetch_gate.json`, partagé entre le thread du server et les sous-processus de scraping |
| `src/jobforge/tools/messages.py` | Synchronisation des messages BOSS (Playwright CDP se connecte au navigateur du port 9222 et intercepte les réponses de page) |
| `src/jobforge/db.py` | SQLite (WAL) : offres / messages / profil / historique de scraping |
| `src/jobforge/profile_score.py` | Moteur de notation locale par règles du profil (13 vérifications) |
| `src/jobforge/llm.py` | Couche de capacités LLM (client chat compatible OpenAI + trois fonctions : accroche / analyse de compatibilité / affinage du CV) |
| `web/job-workbench.html` | Page unique du frontend (6 vues) |
| `src/jobforge/tools/grab_cookies.py` | Récupère les cookies de session du navigateur et écrit `data/cookies.json` |

## Utilisation

1. Double-cliquez sur `setup.bat` pour créer le venv et installer les dépendances
2. Double-cliquez sur `run.bat` pour démarrer, puis ouvrez <http://127.0.0.1:8080> dans un navigateur
3. Ouvrez Chrome de bureau et connectez-vous à zhipin.com pour activer le scraping (le scraping prend le contrôle du clavier et de la souris pendant ~8-15 s ; l'actualisation des messages exige le navigateur ouvert avec le port de débogage 9222)
4. Configurez un modèle IA via le ⚙ en haut à droite pour débloquer la génération d'accroches / l'analyse de compatibilité / l'affinage du CV (compatible avec DeepSeek, Qwen, Zhipu, Ollama ou tout service compatible OpenAI)
5. Prérequis de l'analyse IA par lots : Chrome de bureau ouvert et connecté à zhipin.com (fenêtre non minimisée) ; l'analyse prend le contrôle du clavier et de la souris, et 3 échecs consécutifs déclenchent un coupe-circuit automatique. Le server dispose d'un garde-fou d'instance unique — un second démarrage est refusé
6. La progression du scraping s'affiche dans la fenêtre flottante (à ouvrir manuellement via « 🪟 Fenêtre de progression » dans la barre supérieure ; elle s'ouvre aussi automatiquement au lancement d'un scraping) :
   - **Pause** ne s'applique qu'aux points sûrs (bordures d'offres, attentes de limitation) et ne coupe jamais une action clavier/souris en deux ; le temps de pause ne compte pas dans la limitation, aucune nouvelle attente après reprise
   - **Arrêt** prend effet en quelques secondes (tue aussi le sous-processus de scraping en cours) ; les offres déjà récupérées et les analyses IA déjà terminées sont conservées
   - La fenêtre apparaît par défaut en bas à droite de l'écran ; faites glisser ⠿ pour la déplacer. Après la fin d'une tâche, elle reste quelques secondes pour afficher le résultat puis se ferme seule ; ✕ la ferme immédiatement

## Confidentialité

`data/` (`jobs.db`, `messages.json`, `cookies.json`, `fetch_gate.json`, `hud_pos.json`, etc.) et le profil navigateur (`chrome-profile/`, emplacement canonique `data/chrome-profile`) sont exclus par `.gitignore` et ne sont jamais versionnés.
