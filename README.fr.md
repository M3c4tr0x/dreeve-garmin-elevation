# dreeve-garmin-elevation

🇫🇷 Français · [🇬🇧 English](README.md)

Import automatique des sorties **Garmin Connect → [Dreeve](https://dreeve.app)**, avec le **D+ corrigé par Garmin** au lieu du D+ GPS brut de la montre. Rien d'autre ne change : cadence, type de sport (trail, route…), FC, calories et tours sont conservés.

## Le problème

Les montres Garmin sans baromètre (Forerunner 245, 55, 165…) mesurent l'altitude au GPS. Elle est bruitée, et le D+ écrit dans le `.fit` est gonflé. Dreeve reprend ce chiffre tel quel. Exemples réels : 234 m au lieu de 158, 323 m au lieu de 283.

Garmin Connect corrige l'altitude avec une carte du relief (« Corrections d'altitude »), mais seulement de son côté :

- importer le `.fit` dans Dreeve donne le D+ brut ;
- importer le TCX corrigé fait perdre la cadence, le type « trail » et d'autres données ;
- Dreeve ne corrige pas l'altitude lui-même ([#2356](https://github.com/dreeveapp/dreeve/issues/2356), refusé).

## Le principe

```
Garmin Connect ──► garmin-connector ──► garmin/inbox/<id>.fit
                                              │
                   dreeve-garmin-elevation ◄──┘  télécharge le TCX de la même sortie,
                         │                       remplace l'altitude des points du .fit,
                         ▼                       écrit le D+/D- affiché par Garmin Connect,
                   watch/ de Dreeve ──► import   recalcule le D+ des tours et le CRC
```

- **`garmin-connector`** : le [connecteur officiel Dreeve](https://github.com/dreeveapp/dreeve-garmin-connector). Il dépose les `.fit` dans une **inbox**, et non dans le dossier `watch` de Dreeve.
- **`dreeve-garmin-elevation`** (`app/fixer.py`) : passe toutes les 5 minutes sur l'inbox, corrige chaque `.fit` (`app/fit_alt_fix.py`) et le dépose dans le dossier `watch`. Il tourne dans **la même image que le connecteur** : il n'y a rien à construire.
- **Dreeve** importe le dossier `watch` toutes les 5 minutes, comme d'habitude.

**Aucune sortie n'est perdue.** Si la correction est impossible (pas d'altitude, Garmin n'a rien corrigé, fichier qui n'est pas un `.fit`, erreur), le `.fit` d'origine part tel quel dans Dreeve et le journal le signale.

## Prérequis

- Une instance **Dreeve** qui tourne sous Docker, avec son dossier `watch`.
- **Docker Compose v2** (`docker compose`).
- Un compte **Garmin Connect** et une montre **sans baromètre**. Avec un baromètre, l'altitude est déjà bonne : la passerelle n'apporte rien de plus que le connecteur seul.
- Sur Garmin Connect, l'option **« Corrections d'altitude »** active (sur le web : ouvrir une activité, puis l'engrenage). Sans elle, le TCX est identique au `.fit` et les sorties passent sans correction.

## Installation

### 1. Trouver le dossier `watch` de Dreeve

C'est le dossier de l'hôte monté sur `/var/www/watch` dans le conteneur Dreeve :
```bash
docker inspect <conteneur-dreeve> -f '{{range .Mounts}}{{if eq .Destination "/var/www/watch"}}{{.Source}}{{end}}{{end}}'
```
Remplace `<conteneur-dreeve>` par le nom du conteneur Dreeve (`docker ps` le donne).

### 2. Récupérer ce dépôt et le configurer

```bash
git clone <url-de-ce-depot> dreeve-garmin-elevation && cd dreeve-garmin-elevation
cp .env.example .env
```
Dans `.env`, renseigne :
- `GARMIN_EMAIL` : l'adresse du compte Garmin ;
- `DREEVE_WATCH_DIR` : le chemin trouvé à l'étape 1 ;
- `SINCE` : laisse `now` si Dreeve contient déjà ton historique (voir « Anciennes sorties »).

### 3. Se connecter à Garmin, une seule fois

Le mot de passe est lu sans écho. Il n'est écrit ni dans `.env`, ni dans le compose, ni dans l'historique du shell. Le code MFA est demandé si le compte en a un.
```bash
read -rsp 'Mot de passe Garmin : ' GP; echo
GARMIN_PASSWORD="$GP" docker compose run --rm -e GARMIN_PASSWORD garmin-connector login
unset GP
```
La session est enregistrée dans `garmin/tokens/`. Les deux conteneurs la réutilisent ensuite sans mot de passe.

> ⚠️ **Ne relance pas le login en boucle.** Garmin limite vite les connexions (`429 IP rate limited`). En cas de 429, laisse la commande aller au bout : la bibliothèque essaie d'autres méthodes de connexion et finit souvent par passer. Si elle échoue quand même, attends plusieurs heures avant de réessayer.

### 4. Démarrer

```bash
docker compose up -d
```

### 5. Vérifier à la prochaine sortie

Après une synchronisation de la montre, compte jusqu'à environ 1 h 15 : le connecteur passe toutes les heures, `dreeve-garmin-elevation` attend 5 minutes, puis Dreeve importe dans les 5 minutes.
```bash
docker compose logs -f garmin-connector dreeve-garmin-elevation
```
La ligne attendue :
```
25091328234.fit : altitude corrigee (4638 points, 12 tours), D+ 323 -> 283 m (Garmin)
```
Le premier chiffre est le D+ brut de la montre. Le second, celui de Garmin Connect, est celui qui arrive dans Dreeve.

## Lire le journal de `dreeve-garmin-elevation`

| Ligne | Signification |
|---|---|
| `altitude corrigee … D+ x -> y m (Garmin)` | Tout va bien. |
| `… (TCX)` au lieu de `(Garmin)` | Le résumé Garmin n'était pas disponible : le D+ a été recalculé sur le TCX. Il est correct, mais peut différer de quelques mètres de l'appli. |
| `pas d'altitude dans le TCX, transmis tel quel` | Séance sans GPS (renforcement, home trainer…). C'est normal. |
| `TCX identique au FIT … transmis tel quel` | Garmin n'a pas corrigé l'altitude. Vérifie l'option « Corrections d'altitude ». |
| `session Garmin en pause … (essai non compte)` | Garmin a refusé ou limité un appel. Le fichier attend la fin de la pause (1 h). |
| `transmis SANS correction` | Échec après plusieurs essais, ou au bout de 12 h. La sortie est dans Dreeve avec le D+ brut : voir « Réimporter une sortie ». |

## Exploitation

- **Réimporter une sortie** (passée sans correction, par exemple) :
  1. La supprimer dans l'admin Dreeve, sinon il ignore le doublon.
  2. Récupérer son `.fit` d'origine sur Garmin Connect (web) : ouvrir l'activité, puis l'engrenage, puis « Exporter le fichier d'origine », et dézipper.
  3. Le renommer `<identifiant Garmin>.fit`, avec le nombre qui figure dans l'URL de l'activité, puis le déposer dans `garmin/inbox/`. Il est traité au bout de 5 à 10 minutes.
- **Chaussures, nom, texte et photos** : rien de tout ça n'est rempli automatiquement. Il faut les ajouter dans Dreeve après chaque sortie.
- **Session Garmin perdue** : le journal du connecteur signale une erreur d'authentification. Refais l'étape 3, **une seule fois**, puis lance `docker compose restart`.
- **Mise à jour** : `docker compose pull && docker compose up -d`. Les deux conteneurs utilisent la même image. Après une mise à jour, vérifie dans le journal qu'une sortie est bien traitée : une nouvelle version de la bibliothèque `garminconnect` peut changer son API.
- **Droits sur les fichiers** : les conteneurs tournent en root, comme l'image Dreeve. Si ton Dreeve tourne avec un autre utilisateur, vérifie qu'il peut lire et supprimer les fichiers déposés dans `watch`.

## Anciennes sorties

Avec `SINCE=now`, seules les sorties enregistrées après le premier démarrage sont récupérées. Pour corriger des sorties déjà présentes dans Dreeve, deux possibilités :
- les **réimporter** une à une (voir plus haut) ;
- les supprimer de Dreeve, mettre une date dans `SINCE` **avant le premier démarrage**, puis démarrer. `SINCE` n'est lu qu'une fois : ensuite, il faut vider `garmin/state/`. Le connecteur étale les téléchargements pour ne pas se faire bloquer par Garmin.

Attention : supprimer une sortie de Dreeve efface aussi ce qui y a été ajouté à la main (chaussures, texte, photos).

## Réglages (`docker-compose.yml`, service `dreeve-garmin-elevation`)

| Variable | Défaut | Rôle |
|---|---|---|
| `FIX_INTERVAL` | 300 | Secondes entre deux passages. |
| `FIX_MIN_AGE` | 300 | Âge minimal d'un `.fit` avant traitement. |
| `FIX_MAX_ATTEMPTS` | 6 | Échecs réels de téléchargement avant de transmettre sans correction. Les pauses imposées par Garmin ne comptent pas. |
| `FIX_GIVE_UP_AGE` | 43200 | Au-delà de cet âge en secondes (12 h), le `.fit` part tel quel quoi qu'il arrive. |
| `FIX_AUTH_BACKOFF` | 3600 | Durée de la pause après un refus de Garmin. |
| `FIX_THRESHOLD_M` | 3 | Seuil en mètres du recalcul du D+ des tours. |

Pour les réglages du connecteur (types d'activités, rythme, etc.), voir la [documentation Dreeve](https://docs.dreeve.app).

## Sécurité

- `dreeve-garmin-elevation` ne se connecte **jamais** avec un mot de passe. Il reprend la session du connecteur à partir d'une copie de ses jetons, montés en lecture seule. À chaque passage qui a un fichier à traiter, il repart des jetons du moment, que le connecteur renouvelle de son côté.
- Écriture atomique dans `watch` (fichier `.part`, puis renommage) : Dreeve ne lit jamais un fichier à moitié écrit.
- `garmin/tokens/` donne accès au compte Garmin. Ne le partage pas, et ne le commite pas : il est dans `.gitignore`.

## Retour arrière

```bash
docker compose down
```
Dreeve n'est pas modifié : le dépôt manuel de fichiers dans `watch` continue de fonctionner. Les sorties déjà importées gardent leur D+ corrigé.

## Utilisation sans Docker

`app/fit_alt_fix.py` corrige aussi un fichier à la main (Python 3, sans dépendance) :
```bash
python app/fit_alt_fix.py sortie.fit sortie.tcx sortie_corrigee.fit
```
Le TCX s'exporte depuis Garmin Connect (activité, puis engrenage, puis « Exporter au format TCX »). Dans ce mode, le D+ est recalculé sur le TCX, sans le chiffre exact de l'appli Garmin.

## Développement

Ce projet a été écrit avec l'aide de [Claude Code](https://claude.com/claude-code) (assistant IA d'Anthropic), puis relu et testé sur mes propres sorties.

## Licence

[GPL-3.0](LICENSE), comme le [connecteur Garmin de Dreeve](https://github.com/dreeveapp/dreeve-garmin-connector).
