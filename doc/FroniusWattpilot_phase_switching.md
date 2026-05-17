# FroniusWattpilot – Phasenumschaltung (detailliert)

## Grundkonzept

Der Wattpilot kann in zwei Phasenmodi betrieben werden:

| `currentPhaseMode` | Bedeutung | Wattpilot-Befehl |
|---|---|---|
| `0` | Auto (Wattpilot entscheidet) | `set_phases(0)` |
| `1` | 1-phasig (nur L1) | `set_phases(1)` |
| `2` | 3-phasig (L1 + L2 + L3) | `set_phases(2)` |

`currentPhaseMode` ist eine **interne Zustandsvariable** des Services. Sie wird immer **gleichzeitig** mit dem `set_phases()`-Befehl an den Wattpilot gesetzt — also bevor der Wattpilot die Umschaltung physisch abgeschlossen hat.

---

## 1. Starterkennung beim Service-Start (`initFinalize`)

Beim Start versucht der Service den aktuellen Phasenmodus aus den Live-Messwerten abzuleiten:

```
Fahrzeug verbunden UND power2 > 0  →  currentPhaseMode = 2  (3-phasig aktiv)
Fahrzeug verbunden UND power1 > 0  →  currentPhaseMode = 1  (1-phasig aktiv)
Fahrzeug NICHT verbunden           →  currentPhaseMode = 0  + set_phases(0) [Auto]
```

Im letzten Fall wird der Wattpilot explizit auf Autoselect zurückgesetzt, damit er beim nächsten Ladestart selbst entscheiden kann. Der echte Phasenmodus wird dann beim ersten Ladevorgang durch `adjustChargeCurrent` gesetzt.

---

## 2. Manuelle Steuerung via VRM (`/SetCurrent`)

Wenn der Nutzer über VRM einen `/SetCurrent`-Wert setzt, wird die Phase implizit über die Stromstärke bestimmt:

```
Eingehender Wert (value) in Ampere (VRM schickt immer einen "Gesamt"-Wert):

value > ampLimit?
    → 3-phasig: set_phases(2), currentPhaseMode = 2
       ampPerPhase = round(value / 3)

value <= ampLimit?
    → 1-phasig: set_phases(1), currentPhaseMode = 1
       ampPerPhase = value

→ set_power(ampPerPhase)
```

**Hintergrund:** VRM kennt keine separate 3-Phasen-Einstellung. Der Nutzer gibt einen Gesamtstromwert an. Der Service interpretiert Werte über dem einphasigen Maximum (`ampLimit`) als 3-Phasen-Anforderung und teilt den Wert durch 3 für den tatsächlichen Befehl.

> **Hinweis:** In der manuellen Steuerung gilt **kein Cooldown**. Die Phasenumschaltung erfolgt sofort.

---

## 3. Automatische Phasensteuerung (`adjustChargeCurrent`)

Diese Methode wird **nur im Auto-Modus** aufgerufen, wenn der Wattpilot aktiv lädt (`modelStatus == ChargingBecauseForceStateOn / ChargingBecauseFallbackDefault`).

### Schritt 1: Gewünschten Phasenmodus berechnen

```
targetAmps > ampLimit?
    → desiredPhaseMode = 2  (3-phasig)
    sonst
    → desiredPhaseMode = 1  (1-phasig)
```

`ampLimit` ist das einphasige Hardware-Maximum des Wattpilots (z. B. 16A).

### Schritt 2: Fall A – Kein Phasenwechsel nötig (`currentPhaseMode == desiredPhaseMode`)

```
divider = 1  falls 1-phasig
divider = 3  falls 3-phasig

targetAmps = round(targetAmps / divider)
→ set_power(targetAmps)

falls currentPhaseMode == 1: wantThreePhaseSince = 0  ← Timer zurücksetzen!
falls currentPhaseMode == 2: phaseCheckHistory.append(True)
→ Rückgabe: VrmEvChargerStatus.Charging
```

### Schritt 3: Fall B1 – 1-Phase → 3-Phase (`currentPhaseMode=1, desiredPhaseMode=2`)

Die Umschaltung erfolgt nur nach **ununterbrochener** 3-Phasen-Nachfrage für `MinPhaseSwitchSeconds` (default 300s). Jeder Tick in Fall A setzt den Timer zurück.

```
wantThreePhaseSince == 0 → setze wantThreePhaseSince = now()

elapsed = now() - wantThreePhaseSince
elapsed >= MinPhaseSwitchSeconds (5 Min. stabile Nachfrage)?
    → set_phases(2), currentPhaseMode = 2
       lastThreeToOneCooldownStart = now()
       phaseCheckHistory.clear()
       wantThreePhaseSince = 0
       set_power(round(targetAmps / 3))
    sonst
    → Warte: set_power(ampLimit)   ← Maximum auf 1 Phase bis Timer abläuft
```

### Schritt 4: Fall B2 – 3-Phase → 1-Phase (`currentPhaseMode=2, desiredPhaseMode=1`)

```
phaseCheckHistory.append(False)   ← 1-Phase würde reichen diesen Tick

SoC < SoCProtectionThreshold?
    → SOFORTIGER SWITCH: set_phases(1), currentPhaseMode = 1
       (Batterieschutz überschreibt alle Cooldowns)

SoC >= SoCProtectionThreshold:
    cooldownElapsed = now() - lastThreeToOneCooldownStart

    cooldownElapsed < MinPhaseSwitchSecondsThreeToOne (1h)?
        → Warte: set_power(6)   ← Minimum auf 3 Phasen

    Cooldown abgelaufen:
        threePhaseRatio = sum(phaseCheckHistory) / len(phaseCheckHistory)

        threePhaseRatio >= 0.8 (≥80% der letzten 10 Min. war 3-Phase nötig)?
            → Verlängern: lastThreeToOneCooldownStart = now()
               phaseCheckHistory.clear()
               set_power(6)
        sonst
            → SWITCH: set_phases(1), currentPhaseMode = 1
               wantThreePhaseSince = 0
               phaseCheckHistory.clear()
               set_power(targetAmps)
```

> **Wichtig:** Wenn `desiredPhaseMode != enteringPhaseMode` (d.h. eine Umschaltung läuft oder ansteht), gibt die Methode `SwitchingTo3Phase` bzw. `SwitchingTo1Phase` zurück — auch wenn der Befehl noch nicht gesendet wurde. VRM zeigt also die beabsichtigte Richtung an.

---

## 4. Phasenmodus bei Ladestart (`NotChargingBecause*` → Ladestart)

Wenn im Auto-Modus ein neuer Ladevorgang gestartet wird, wird der Phasenmodus **vor dem Start-Befehl** festgelegt:

```
targetAmps = max(allowance / voltage1, 6), begrenzt auf ampLimit * 3

targetAmps > ampLimit?
    → currentPhaseMode = 2, set_phases(2)   ← 3-phasig
    sonst
    → currentPhaseMode = 1, set_phases(1)   ← 1-phasig

→ set_power(targetAmps)
→ set_start_stop(On)
→ lastOnOffTime = now()
```

---

## 5. Phasenmodus bei Ladestopp (Auto-Modus, kein Überschuss)

Wenn kein PV-Überschuss mehr vorhanden ist und der Ladevorgang gestoppt wird:

```
→ set_start_stop(Off)
→ currentPhaseMode = 0
→ set_phases(0)   ← zurück auf Auto, damit Günstigpreis-Laden oder manueller Start korrekt starten
→ lastOnOffTime = now()
```

---

## 6. Asymmetrische Cooldown-Mechanismen

Die zwei Phasenrichtungen haben vollständig unabhängige, unterschiedliche Logiken:

| Richtung | Mechanismus | Config-Key | Default |
|---|---|---|---|
| 1→3 Phase | Stabilitäts-Timer (`wantThreePhaseSince`) | `MinPhaseSwitchSeconds` | 300s (5 Min.) |
| 3→1 Phase | Schutzfenster + History-Check | `MinPhaseSwitchSecondsThreeToOne` | 3600s (1h) |
| SoC-Schutz | Sofortiger 3→1 Bypass | `SoCProtectionThreshold` | 50% |

### 1→3: Stabilitäts-Timer (`wantThreePhaseSince`)

Der Timer startet beim ersten Tick mit `desiredPhaseMode=2` während `currentPhaseMode=1`. Er wird bei **jedem Tick in Fall A** (kein Wechsel nötig, 1-Phase ausreichend) **auf 0 zurückgesetzt**. Nur wenn der Timer ununterbrochen für `MinPhaseSwitchSeconds` läuft, wird auf 3-Phase umgeschalten.

```
Tick 1: desiredPhaseMode=2 → wantThreePhaseSince = now()     Timer startet
Tick 2: desiredPhaseMode=2 → elapsed = 5s, noch 295s
...
Tick 10: desiredPhaseMode=1 → wantThreePhaseSince = 0         RESET!
Tick 11: desiredPhaseMode=2 → wantThreePhaseSince = now()     Timer startet neu
...
Tick 70: desiredPhaseMode=2 → elapsed = 300s → SWITCH
```

### 3→1: Schutzfenster + History-Check

`lastThreeToOneCooldownStart` wird beim Eintritt in 3-Phase gesetzt. Nach Ablauf der 1h wird `phaseCheckHistory` ausgewertet (Rolling Window, 120 Ticks = 10 Min.):

- **≥ 80% True** (3-Phase war meist noch nötig) → Schutzfenster um 1h verlängern
- **< 80% True** → auf 1-Phase wechseln

### SoC-Schutz (Sofort-Bypass für 3→1)

Fällt der Batterie-SoC unter `SoCProtectionThreshold`, wird **sofort** auf 1-Phase gewechselt — unabhängig vom laufenden Schutzfenster. Die Batterie (10 kWh) soll unterhalb von 50% nicht für den Wattpilot belastet werden.

### Was passiert wenn Cooldown läuft aber `currentPhaseMode == desiredPhaseMode`?

Fall A wird **vor** den Cooldown-Checks geprüft. Keiner der Timer-Werte (`wantThreePhaseSince`, `lastThreeToOneCooldownStart`) wird berührt. Beim 1→3-Timer gilt zusätzlich: Fall A setzt `wantThreePhaseSince = 0` aktiv zurück.

Beim nächsten Tick mit `currentPhaseMode != desiredPhaseMode` gilt sofort wieder die normale Logik — bei 1→3 beginnt der Stabilitäts-Timer von vorn, bei 3→1 läuft das Schutzfenster weiter.

---

## 7. StepSize im SolarOverheadDistributor

Der Phasenmodus beeinflusst auch, wie der `SolarOverheadDistributor` die Überschussleistung zuweist. Der Service meldet via MQTT die `StepSize`, also die kleinste sinnvolle Leistungsänderung:

```
currentPhaseMode == 2 (3-phasig):
    StepSize = voltage1 + voltage2 + voltage3   (≈ 690W bei 3 × 230V)

currentPhaseMode != 2 (1-phasig oder Auto):
    StepSize = voltage1                          (≈ 230W)
```

Dadurch kann der Distributor die Leistung in phasengerechten Schritten zuweisen.

---

## 8. Phasenmode-Anzeige (`reportPhaseMode`)

Der aktuelle Phasenmodus wird auch im CustomName des Overhead-Requests sichtbar gemacht:

```
power > 0 UND currentPhaseMode == 1  →  CustomName = "Fronius Wattpilot (1)"
power > 0 UND currentPhaseMode == 2  →  CustomName = "Fronius Wattpilot (3)"
sonst                                →  CustomName = "Fronius Wattpilot"
```

---

## Zusammenfassung: Wann wird welche Phase gesetzt?

| Auslöser | Bedingung | Ergebnis |
|---|---|---|
| Service-Start, Fahrzeug lädt auf 3 Phasen | `power2 > 0` | `currentPhaseMode = 2`, `lastThreeToOneCooldownStart = now()` |
| Service-Start, Fahrzeug lädt auf 1 Phase | `power1 > 0` | `currentPhaseMode = 1` |
| Service-Start, kein Ladevorgang | — | `currentPhaseMode = 0`, `set_phases(0)` |
| VRM `/SetCurrent` > `ampLimit` | Manuell | `set_phases(2)`, `currentPhaseMode = 2` (sofort) |
| VRM `/SetCurrent` ≤ `ampLimit` | Manuell | `set_phases(1)`, `currentPhaseMode = 1` (sofort) |
| Auto-Modus, Ladestart | `targetAmps > ampLimit` | `set_phases(2)`, `currentPhaseMode = 2` |
| Auto-Modus, Ladestart | `targetAmps <= ampLimit` | `set_phases(1)`, `currentPhaseMode = 1` |
| Auto-Modus, 1→3 Phase | 3-Phasen-Nachfrage ≥ 5 Min. stabil | `set_phases(2)`, `currentPhaseMode = 2` |
| Auto-Modus, 1→3 Phase | 3-Phasen-Nachfrage < 5 Min. stabil | `set_power(ampLimit)`, kein Wechsel |
| Auto-Modus, 3→1 Phase, SoC < Schwellenwert | beliebig | Sofort `set_phases(1)`, `currentPhaseMode = 1` |
| Auto-Modus, 3→1 Phase, Schutzfenster aktiv | SoC ≥ Schwellenwert | `set_power(6)`, kein Wechsel |
| Auto-Modus, 3→1 Phase, Fenster abgelaufen | History < 80% 3-Phase | `set_phases(1)`, `currentPhaseMode = 1` |
| Auto-Modus, 3→1 Phase, Fenster abgelaufen | History ≥ 80% 3-Phase | Fenster +1h verlängert, `set_power(6)` |
| Auto-Modus, Ladestopp (kein Überschuss) | SoC < Schwellenwert oder nicht auf 3-Phase | `set_phases(0)`, `currentPhaseMode = 0` |
| Auto-Modus, Allowance < Minimum, 3-phasig | SoC ≥ Schwellenwert + Fenster aktiv | `set_power(6)`, kein Stopp |
