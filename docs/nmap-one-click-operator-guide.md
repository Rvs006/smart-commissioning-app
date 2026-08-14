# Nmap on a field laptop

This guide applies when the project uses Nmap and Nmap is already installed on
the Windows field laptop by IT or the approved software process. The Smart
Commissioning App does not package, install, or download Nmap or Npcap.

## What the engineer does

1. Open **IP Discovery** and select the project and site.
2. Choose **Operator-managed Nmap** only when it is shown as confirmed.
3. Select one of the displayed fixed profiles, then use the normal authorized
   scan controls.

The screen intentionally has no Nmap path, executable, flag, script, command,
or licence fields. Those details are not part of the engineer's job.

## What the global administrator does

When the app finds an eligible local installation that has not yet been
recorded, a global administrator sees **Approve detected Nmap** in IP Discovery.
They select it once for that project and site. The app records the signed local
installation and the approved profiles. Concurrent approval requests converge
on the same recorded authority.

The app asks for approval again only if the detected installed files change. If
Nmap is unavailable or does not match the recorded approval, use the built-in
TCP-connect scanner or ask the global administrator to review the installation.

## Before a live scan

Load the approved IP register, choose the wired building-network interface,
run a dry run first, then tick the explicit authorization control before a live
scan. Nmap approval does not replace site scan authorization.
