# Design

## Umschaltung von 1 Phase auf 3 Phasen
Ich hätte gerne, dass die Phasenumschaltung von 1 auf 3 Phasen nur stattfindet, wenn für die Cooldown-Zeit (default: 300) bei jedem einzelnen Check currentPhaseMode != desiredPhaseMode ist. Sobald das einmal nicht ist, soll die Cooldown-Zeit zurückgesetzt werden.
Das führt dazu, dass wirklich nur umgeschalten wird, wenn die Leistung für 3 Phasen für zumindest 5 Min. stabil ist.

## Umschaltung von 3 Phasen auf 1 Phase
Hier soll der Cooldown 1h sein. Also die 3 Phasen bleiben für zumindest 1h erhalten.
Nach dem Cooldown prüfe die letzten 5 Abfragen von "currentPhaseMode == desiredPhaseMode" und wenn mind. 4 Mal (also 80%) "currentPhaseMode == desiredPhaseMode", dann Cooldown-Zeit zurücksetzen, sodass wieder mind. 1h weiter mit 3 Phasen gearbeitet wird.

# Frage:

Was passiert wenn der SoC Level der Batterie fällt, auf zB 90%, wird dann wieder alles auf die Batterie umgeleitet?