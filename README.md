# WoW Forever Patchnote

Ce dépôt suit les posts bleus de Blizzard sur **WoW Forever** et les publie en français dans un salon Discord.

Toutes les 15 minutes (à :07, :22, :37 et :52), GitHub Actions :

1. Lit le [blue tracker des forums US](https://us.forums.blizzard.com/en/wow/groups/blizzard-tracker/posts).
2. Récupère l'article complet quand le post renvoie vers une news officielle.
3. Demande à Claude (Sonnet) si le post concerne WoW Forever et s'il apporte une vraie information.
4. Réécrit le post en français, aéré et lisible.
5. Publie le message sur le webhook Discord.
6. Enregistre les identifiants des posts traités dans `state/seen.json`.

Au premier passage, les posts déjà en ligne sont marqués comme lus. Rien n'est publié.

## Sécurité

- Aucun secret n'est dans le dépôt. L'URL du webhook est un **secret GitHub Actions**.
- Aucune clé API n'est stockée : l'accès à Claude passe par la fédération d'identité GitHub, limitée à la branche `main` de ce dépôt.
- Le script n'affiche jamais ces secrets, même en cas d'erreur.
- Le workflow ne se lance que par planification ou à la main. Une pull request venant d'un fork ne peut pas le déclencher.
- Le workflow a deux jobs séparés. Le premier a le secret Discord et l'accès à Claude, mais ne peut pas écrire dans le dépôt. Le second peut écrire mais n'a aucun secret.
- Le second job n'accepte qu'un fichier d'état qui contient uniquement des numéros de posts.
- Une erreur imprévue n'affiche que son type dans le journal, jamais son contenu.
- Les dépendances Python sont figées avec leurs empreintes. Les actions GitHub sont figées par commit.
- Les messages Discord ne peuvent notifier personne : `@everyone`, `@here` et les mentions sont désactivés.
- Seuls les liens en https vers une courte liste d'hôtes officiels sont gardés : `worldofwarcraft.blizzard.com`, `worldofwarcraft.com`, `news.blizzard.com`, `us.forums.blizzard.com`, `eu.forums.blizzard.com`, `shop.battle.net`. Tous les autres liens et les invitations Discord sont retirés.
- Le script ne suit les redirections que vers les hôtes Blizzard attendus.

## Installation

### 1. Créer le webhook Discord

1. Ouvre les **Paramètres du serveur**, puis **Intégrations**, puis **Webhooks**.
2. Clique sur **Nouveau webhook**.
3. Choisis le salon de destination et donne un nom au webhook, par exemple « WoW Forever Patchnote ».
4. Clique sur **Copier l'URL du webhook**.

Ne colle jamais cette URL dans un fichier, une issue ou un message public. Toute personne qui la connaît peut écrire dans le salon.

### 2. Connecter GitHub à Claude, sans clé API (recommandé)

La fédération d'identité remplace la clé API. À chaque passage, GitHub prouve à Anthropic que la demande vient de la branche `main` de ce dépôt. Anthropic renvoie un accès valable 10 minutes au plus. Aucune clé n'est stockée, donc aucune clé ne peut fuir.

1. Ouvre la page [Workload identity](https://platform.claude.com/settings/workload-identity-federation) de la console.
2. Clique sur **Connect workload**, puis sur la tuile **GitHub Actions**.
3. Choisis un espace de travail dédié à ce projet, avec un plafond de dépense.
4. Nomme le compte de service `wow-forever-patchnote`, puis ajoute-le comme membre de l'espace dédié. Sans cela, l'accès à Claude est refusé même si la règle correspond.
5. Remplis la règle de fédération avec ces valeurs :

| Champ du formulaire | Valeur |
| --- | --- |
| Nom de la règle | `wow-forever-patchnote` |
| Organisation GitHub | `ValentinRLL` |
| Dépôt | `wow-blue-tracker` (la règle vérifie le claim `repository` = `ValentinRLL/wow-blue-tracker`) |
| Branche / référence | `refs/heads/main` |
| Événements autorisés | « N'importe quel événement » (« Push uniquement » bloquerait la planification) |
| Identifiant numérique de l'organisation | `9751045` |
| Espaces de travail | **Espaces de travail**, puis seulement l'espace dédié au projet |
| Options avancées : portée OAuth | `workspace:inference` si la console le propose, sinon `workspace:developer` |
| Options avancées : durée du jeton | `600` secondes |

Un préfixe de `sub` doit contenir les identifiants numériques. Sinon, la règle vérifie le claim `repository`.

GitHub envoie un `sub` qui contient les identifiants numériques du propriétaire et du dépôt :

```
repo:ValentinRLL@9751045/wow-blue-tracker@1472349302:ref:refs/heads/main
```

Un préfixe de `sub` sans ces identifiants, comme `repo:ValentinRLL/wow-blue-tracker`, est refusé avec la raison `match_subject_prefix`.

Si un passage est refusé, ouvre l'onglet **Historique** de la page Workload identity. Il montre la raison du refus et les claims reçus, dont `sub` et `repository`.

« N'importe quel événement » reste sûr ici. Une pull request reçoit la référence `refs/pull/...`, jamais `refs/heads/main`. Elle est donc refusée par le champ « Branche / référence ».

6. Note les 3 identifiants affichés à la fin : la règle (`fdrl_...`), le compte de service (`svac_...`) et l'organisation.

Cette règle refuse les pull requests, les forks, les autres branches et les autres dépôts.

Pour le vérifier, crée une branche de test et lance le workflow depuis elle avec **Aperçu seulement** coché. L'aperçu vérifie l'accès à Claude dès le début. Le journal doit afficher `ERREUR connexion Claude : WorkloadIdentityError`. L'historique de la page Workload identity montre la raison du refus. Supprime ensuite la branche.

Utilise un espace de travail **dédié**, jamais l'espace Default. La portée `workspace:developer` donne les mêmes droits qu'une clé API de cet espace : Messages, mais aussi Files, Skills et Managed Agents. Un espace dédié et plafonné limite ce qu'un jeton volé pourrait faire.

**Règle à respecter ensuite :** la fédération fait confiance à toute la branche `main` de ce dépôt. N'ajoute jamais d'autre workflow sur `main` avec `id-token: write`, sinon il recevrait lui aussi un accès Claude. Et ne donne à personne d'autre le droit de pousser sur `main`.

### 3. Ajouter les réglages dans GitHub

1. Dans le dépôt, ouvre **Settings**, puis **Secrets and variables**, puis **Actions**.
2. Dans l'onglet **Secrets**, clique sur **New repository secret**.
3. Crée `DISCORD_WEBHOOK_URL` avec l'URL du webhook.
4. Dans l'onglet **Variables**, crée ces 3 variables. Ce ne sont pas des secrets.
   - `ANTHROPIC_FEDERATION_RULE_ID` : l'identifiant `fdrl_...`.
   - `ANTHROPIC_SERVICE_ACCOUNT_ID` : l'identifiant `svac_...`.
   - `ANTHROPIC_ORGANIZATION_ID` : l'identifiant de l'organisation.

Si la règle couvre plusieurs espaces de travail, crée aussi `ANTHROPIC_WORKSPACE_ID`.

Le workflow ne lit aucune clé API. La variable `ANTHROPIC_API_KEY` ne sert qu'en local, pour les tests.

Option : dans l'onglet **Variables**, crée `TRACKER_SCOPE` pour changer le périmètre.

| Valeur | Effet |
| --- | --- |
| `forever_only` (défaut) | Seulement WoW Forever. |
| `forever_plus_news` | WoW Forever et les grandes annonces WoW. |
| `all` | Tout World of Warcraft. |

### 4. Régler GitHub Actions

1. Ouvre **Settings**, puis **Actions**, puis **General**.
2. Dans **Fork pull request workflows from outside collaborators**, choisis **Require approval for all external contributors**.
3. Dans **Workflow permissions**, garde **Read repository contents and packages permissions**.

### 5. Tester

1. Ouvre l'onglet **Actions**, puis le workflow **Blue tracker WoW**.
2. Clique sur **Run workflow** et coche **Aperçu seulement**.
3. Lis le journal. Il montre la réécriture des 3 derniers posts, ou « ignoré » s'ils ne concernent pas WoW Forever. Rien n'est publié ni enregistré.
4. Lance une fois **Run workflow** sans cocher la case. Ce passage marque les posts existants comme lus.

Ensuite, le workflow tourne seul toutes les 15 minutes.

## Développement local

```bash
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes --no-deps -r requirements.txt
pip install pytest
python -m pytest -q
```

Pour un aperçu réel sans publier :

```bash
DRY_RUN=true PREVIEW_COUNT=3 ANTHROPIC_API_KEY=... python -m tracker.main
```

En local, la fédération GitHub n'existe pas : utilise une clé API de test.

Pour changer une dépendance, modifie `requirements.in`, puis régénère le fichier figé :

```bash
pip install pip-tools
pip-compile --generate-hashes --strip-extras -o requirements.txt requirements.in
```

## Limites connues

- GitHub peut retarder les tâches planifiées de quelques minutes quand ses serveurs sont chargés.
- Si Blizzard bloque un jour les serveurs de GitHub, le journal affichera « forum injoignable ».
- Un post réécrit fait 8 000 caractères au plus, soit en général 5 messages Discord, jusqu'à 8 si les paragraphes sont longs. Au-delà, la fin renvoie vers le post original.
- Un passage traite 8 posts au maximum, en 14 minutes au plus. Les suivants partent au passage d'après.
- Si l'état ne peut pas être enregistré, le workflow se désactive pour éviter les doublons. Réactive-le dans l'onglet **Actions** avec **Enable workflow**.
- Si Discord coupe la connexion pendant un envoi, le post n'est pas renvoyé, pour éviter un doublon. Il peut alors manquer dans le salon.
