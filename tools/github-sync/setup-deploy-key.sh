#!/bin/bash
# Einmaliges Setup: erzeugt einen dedizierten SSH-Schluessel NUR fuer den
# Push zu ScreenDesktopRecorder (kein Wiederverwenden eines evtl. schon
# vorhandenen privaten GitHub-Schluessels - ein Deploy Key sollte pro
# Repo eigenstaendig und mit "Allow write access" streng auf genau
# dieses eine Repo begrenzt sein).
#
# Der private Schluessel verlaesst diesen Rechner NIE - nur der
# OEFFENTLICHE Teil (…_screenrecorder.pub) wird am Ende ausgegeben und
# muss von Hand auf GitHub eingetragen werden.
set -e

KEY_PATH="$HOME/.ssh/id_ed25519_screenrecorder"
REPO_URL="https://github.com/Softhuby99/ScreenDesktopRecorder"

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

if [ -f "$KEY_PATH" ]; then
    echo "Schluessel existiert bereits ($KEY_PATH) - ueberspringe Erstellung."
else
    # -N "": bewusst OHNE Passphrase, sonst fragt SSH bei jedem Push
    # trotzdem wieder nach etwas - Schutz kommt hier stattdessen aus der
    # Dateiberechtigung (chmod 600) und dem engen Scope des Deploy Keys.
    ssh-keygen -t ed25519 -N "" -C "deploy-key-screendesktoprecorder" -f "$KEY_PATH"
fi
chmod 600 "$KEY_PATH"
chmod 644 "$KEY_PATH.pub"

SSH_CONFIG="$HOME/.ssh/config"
touch "$SSH_CONFIG"
chmod 600 "$SSH_CONFIG"

if grep -q "^Host github-screenrecorder$" "$SSH_CONFIG" 2>/dev/null; then
    echo "SSH-Config-Eintrag existiert bereits - unveraendert gelassen."
else
    {
        echo ""
        echo "Host github-screenrecorder"
        echo "    HostName github.com"
        echo "    User git"
        echo "    IdentityFile $KEY_PATH"
        echo "    IdentitiesOnly yes"
    } >> "$SSH_CONFIG"
    echo "SSH-Config-Eintrag 'github-screenrecorder' ergaenzt."
fi

echo ""
echo "=================================================================="
echo " NAECHSTER SCHRITT (von Hand, einmalig):"
echo "=================================================================="
echo "1) Oeffnen:  $REPO_URL/settings/keys/new"
echo "2) Titel frei waehlbar, z. B. 'sync-script'"
echo "3) 'Allow write access' UNBEDINGT anhaken (sonst nur Lesezugriff)"
echo "4) Diesen OEFFENTLICHEN Schluessel dort einfuegen:"
echo ""
cat "$KEY_PATH.pub"
echo ""
echo "=================================================================="
echo " Danach zum Testen:"
echo "   ssh -T git@github-screenrecorder"
echo " (Meldet 'successfully authenticated' - eine Shell bekommt man"
echo " darueber nie, das ist bei GitHub so gewollt.)"
echo ""
echo " Erst wenn das klappt: Bescheid geben, dann stelle ich die Remote-"
echo " URL im Repo von HTTPS auf diesen SSH-Alias um."
echo "=================================================================="
