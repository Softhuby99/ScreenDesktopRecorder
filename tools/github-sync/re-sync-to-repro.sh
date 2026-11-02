#!/bin/bash
# Synct den aktuellen Stand aus dem Arbeitsordner in das eigentliche
# Git-Repo (Unterordner ScreenDesktopRecorder/) und pusht ihn.
#
# Vorher lag der Fehler darin, dass dieses Skript in
#   .../HomeLab/1_Projects/Screenrecorder/          <- KEIN Git-Repo
# ausgefuehrt wurde statt im tatsaechlichen Repo
#   .../HomeLab/1_Projects/Screenrecorder/ScreenDesktopRecorder/
# Da /mnt ein eigener Mountpunkt ist, sucht Git (ohne
# GIT_DISCOVERY_ACROSS_FILESYSTEM) beim Hochlaufen durch die
# Elternordner nicht darueber hinaus und findet daher gar kein .git -
# daher "fatal: not a git repository ... Stopping at filesystem boundary".
set -e

REPO_DIR="/mnt/Home/Stephan/Priv/HomeLab/1_Projects/Screenrecorder/ScreenDesktopRecorder"
cd "$REPO_DIR"

# git add -A statt einer manuell gepflegten Dateiliste, damit neue
# Dateien (z. B. optimizer.py) nicht mehr von Hand ergaenzt werden
# muessen. .gitignore filtert venv/.venv/__pycache__/*.bak etc. bereits
# zuverlaessig heraus.
git add -A

# WICHTIG: Frueher wurde bei "nichts Neues zu committen" hier sofort mit
# exit 0 abgebrochen - dadurch blieb ein bereits committeter, aber noch
# nicht gepushter Commit aus einem vorherigen Lauf (z. B. weil der Push
# damals fehlschlug) fuer immer ungepusht liegen, und es gab NIE wieder
# einen Build, obwohl das Skript scheinbar anstandslos durchlief ("Keine
# Aenderungen zum Committen." sah nach Erfolg aus). Jetzt wird nur der
# commit-Schritt uebersprungen, gepusht wird trotzdem immer - so kommt
# ein liegen gebliebener Commit beim naechsten Lauf garantiert nach.
if git diff --cached --quiet; then
    echo "Keine neuen Aenderungen zum Committen (es wird trotzdem gepusht -"
    echo "falls von einem frueheren Lauf noch ein ungepushter Commit offen ist)."
else
    # Zeitstempel in build_info.py schreiben, damit der Fenstertitel der
    # fertigen .exe zweifelsfrei zeigt, welcher Stand drin steckt (die
    # von Hand gepflegte APP_VERSION wurde dafuer zu oft vergessen).
    # BEWUSST erst hier, innerhalb des else-Zweigs: wuerde bei jedem
    # Skriptlauf gestempelt, aenderte sich build_info.py immer, und es
    # entstuende bei jedem Lauf ein leerer Commit samt sinnlosem
    # CI-Build.
    # Zeitzone bewusst MIT im Stempel: der Dateizeitstempel der fertigen
    # .exe kommt aus dem GitHub-Runner (UTC) und wird von Windows beim
    # Entpacken unveraendert angezeigt - ohne Zeitzonen-Angabe sieht der
    # Stempel dann gegenueber der Datei um Stunden "falsch" aus, obwohl
    # beides derselbe Build ist.
    STAMP="$(date '+%Y-%m-%d %H:%M %Z')"
    {
        printf '"""\r\n'
        printf 'build_info.py - AUTOMATISCH ERZEUGT von re-sync-to-repro.sh.\r\n'
        printf 'Nicht von Hand bearbeiten, wird bei jedem Sync ueberschrieben.\r\n'
        printf 'Der Stempel erscheint im Fenstertitel und zeigt, welcher Stand\r\n'
        printf 'in einer gebauten .exe/Binary tatsaechlich steckt.\r\n'
        printf '"""\r\n'
        printf '\r\n'
        printf 'BUILD_STAMP = "%s"\r\n' "$STAMP"
    } > build_info.py
    git add build_info.py
    echo "Build-Stempel gesetzt: $STAMP"

    COMMIT_MSG="${1:-Sync: aktueller Stand aus ScreenRecPro}"
    git commit -m "$COMMIT_MSG"
fi

# Der Push wird bewusst NICHT mehr von "set -e" stillschweigend
# abgefangen: zuvor blieb ein fehlgeschlagener Push (z. B. abgelaufene
# HTTPS-Anmeldedaten) leicht unbemerkt, weil das Terminal einfach ohne
# klare Erfolgs-/Fehlermeldung endete - der Commit lag dann zwar lokal
# vor, wurde aber nie zu GitHub übertragen, wodurch auch nie ein
# Actions-Build angestoßen wurde (Push ist der einzige Trigger).
echo "→ Push zu $(git remote get-url origin) ..."
if git push; then
    echo "✓ Push erfolgreich - GitHub Actions sollte jetzt einen Build starten."
else
    status=$?
    echo ""
    echo "✗ Push FEHLGESCHLAGEN (Exit-Code $status)."
    echo "  Der Commit ist lokal vorhanden, aber NICHT auf GitHub - deshalb"
    echo "  läuft auch kein Build. Häufigste Ursache bei HTTPS-Remotes:"
    echo "  abgelaufenes/fehlendes Zugangstoken. Prüfen mit:"
    echo "    git push -v"
    echo "  und ggf. Anmeldedaten erneuern (z. B. git credential-manager,"
    echo "  oder auf ein SSH-Remote umstellen)."
    exit "$status"
fi
