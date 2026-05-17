# FroniusWattpilot – Funktionale Beschreibung

## Architektur-Überblick

Der Service läuft als Victron-dbus-Dienst (`com.victronenergy.evcharger`) und vermittelt zwischen dem Fronius Wattpilot (via WebSocket), dem Victron-VRM-System (via dbus) und dem `SolarOverheadDistributor` (via MQTT). Der Haupt-Zyklus (`_update`) wird alle **5 Sekunden** ausgeführt.

---

## Betriebsmodi (`VrmEvChargerControlMode`)

| Modus | Wert | Wattpilot intern | Bedeutung |
|---|---|---|---|
| `Manual` | 0 | `Default` | VRM/Nutzer steuert direkt; Befehle werden 1:1 weitergeleitet |
| `Auto` | 1 | `ECO` | Solar-Overhead-Automatik; der Service steuert Strom und Phasen selbst |
| `Scheduled` | 2 | — | Nur intern als Trigger genutzt: weckt den Wattpilot aus dem Hibernate-Modus auf |

Moduserkennung beim Start: Ist der Wattpilot in `ECO`-Modus → `Auto`; sonst → `Manual`. Der Nutzer kann den Modus jederzeit über VRM umschalten. Bei jedem Zyklus wird der tatsächliche Wattpilot-Modus erneut geprüft (der Nutzer kann auch direkt am Gerät umschalten).

---

## Idle- und Hibernate-Mechanismus

Der Zyklus läuft normalerweise vollständig durch. Ist kein Fahrzeug verbunden, wechselt der Service in den **Idle-Modus** und prüft nur alle **5 Minuten**.

```
Fahrzeug getrennt UND carStateReady == True
    → isIdleMode = True
    → WENN HibernateMode = true: WebSocket-Verbindung wird getrennt (auto_reconnect = false)

Fahrzeug verbunden (Rückkehr aus Idle)
    → WENN verbunden + carConnected: isIdleMode = False → Normalbetrieb
    → WENN NICHT verbunden (Hibernate): wakeUpWattpilot() → reconnect → carStateReady abwarten → isIdleMode = False
```

---

## Statusmaschine: Der `_update`-Zyklus

Die entscheidende Variable ist `wattpilot.modelStatus`. Daraus wird der VRM-Status abgeleitet.

### Zustand 1 – `Disconnected` (VRM Status 0)

**Bedingung:**
- `modelStatus == NotChargingBecauseNoChargeCtrlData` (id=0) **ODER**
- `wattpilot.carConnected == False`

**Aktion:**
- Meldet VRM-Status `Disconnected`
- Setzt `noChargeSince = 0` zurück (nächste Verbindung startet frisch)

---

### Zustand 2 – Aktives Laden (`ChargingBecauseForceStateOn` id=3 oder `ChargingBecauseFallbackDefault` id=15)

Dies ist der Haupt-Ladezustand. Hier wird unterschieden ob **Auto** oder **Manual**.

**Vollladungs-Erkennung (beide Modi):**
- `wattpilot.power <= 0` → `noChargeSince += 5` (Sekunden)
- `wattpilot.power > 0` → `noChargeSince = 0`
- `noChargeSince >= 120` (2 Minuten kein Strom) → VRM-Status `Charged` (id=3) → **Ende**

**Modus Auto (`ECO`):**

```
allowance >= voltage1 * 6 (= Mindestleistung für 6A einphasig)?
│
├─ JA → targetAmps = max(allowance / voltage1, 6), begrenzt auf ampLimit * 3
│        → adjustChargeCurrent(targetAmps) aufrufen
│        → VRM-Status = Rückgabe von adjustChargeCurrent()
│
└─ NEIN (kein Überschuss mehr) → VRM-Status: StopCharging (id=24)
         onOffCooldown abgelaufen?
         ├─ JA  → set_start_stop(Off), lastOnOffTime setzen
         │        currentPhaseMode = 0, set_phases(0) [Auto]
         └─ NEIN → Lade weiter mit Minimum 6A bis Cooldown abläuft
```

**Modus Manual:**
- Keine Logik, nur VRM-Status `Charging` (id=2) melden

---

### Zustand 3 – Nicht-Laden durch Wattpilot-intern (`modelStatus` id ∈ {4, 5, 6, 16, 17, 18, 22, 24})

Dies umfasst alle Fälle wo der Wattpilot intern pausiert (Force-Off, Scheduler, Energy-Limit, Fallback, PhaseSwitch, MinPause).

**Modus Auto (`ECO`):**

```
allowance >= voltage1 * 6?
│
├─ JA  → VRM-Status: StartCharging (id=21)
│         onOffCooldown abgelaufen?
│         ├─ JA  → Phase bestimmen (targetAmps > ampLimit → 3-phasig, sonst 1-phasig)
│         │        set_phases(), set_power(targetAmps), set_start_stop(On)
│         │        lastOnOffTime setzen
│         └─ NEIN → Wartemeldung, noch nichts senden
│
└─ NEIN → VRM-Status: WaitingForSun (id=4)
           startState != Neutral? → set_start_stop(Neutral) [damit Günstig-Preis-Laden greifen kann]
```

**Modus Manual:**
- VRM-Status `Connected` (id=1)

---

### Zustand 4 – Niedrigpreis-Laden (`ChargingBecauseAwattarPriceLow` id=7)

Wattpilot lädt eigenständig wegen günstigem Strompreis — kein eigener Steuereingriff.

```
power <= 0 → noChargeSince += 5
power > 0  → noChargeSince = 0

noChargeSince >= 120 → VRM-Status: Charged (id=3)
sonst               → VRM-Status: Charging (id=2)
                       LastChargeModeLiteral = "LowPrice"
```

---

### Zustand 5 – Phasenumschaltung läuft (`NotChargingBecausePhaseSwitch` id=23)

Wattpilot meldet aktive Umschaltung. Da `currentPhaseMode` immer **einen Schritt voraus** ist (wird gesetzt bevor die Umschaltung stattfindet):

```
currentPhaseMode == 1 → VRM-Status: SwitchingTo1Phase (id=23)
currentPhaseMode == 2 → VRM-Status: SwitchingTo3Phase (id=22)
```

---

## Phasenumschaltlogik (`adjustChargeCurrent`)

Wird nur im Auto-Modus aufgerufen, wenn der Wattpilot aktiv lädt.

```
targetAmps > ampLimit (einphasiges Maximum)?
    → desiredPhaseMode = 2 (3-phasig)
    sonst
    → desiredPhaseMode = 1 (1-phasig)

currentPhaseMode == desiredPhaseMode?
    → JA:  Strom direkt anpassen (targetAmps / 1 oder / 3)
           Rückgabe: VrmEvChargerStatus.Charging

    → NEIN: Phasenwechsel nötig
            phaseSwitchCooldown abgelaufen?
            ├─ JA  → set_phases(desiredPhaseMode), set_power(targetAmps / desiredPhaseMode)
            │        currentPhaseMode = desiredPhaseMode, lastPhaseSwitchTime setzen
            │        Rückgabe: SwitchingTo3Phase oder SwitchingTo1Phase
            └─ NEIN → currentPhaseMode == 1: weiter mit ampLimit (Maximum 1-Phase)
                       currentPhaseMode == 2: weiter mit 6A (Minimum 3-Phase)
                       Rückgabe: SwitchingTo3Phase oder SwitchingTo1Phase
```

---

## Cooldown-Mechanismen

| Cooldown | Config-Key | Schützt vor |
|---|---|---|
| `minimumOnOffSeconds` | `MinOnOffSeconds` | Zu schnellem Ein-/Ausschalten |
| `minimumPhaseSwitchSeconds` | `MinPhaseSwitchSeconds` | Zu schneller Phasenumschaltung |

Während ein Cooldown aktiv ist, wird **nicht gewartet** — stattdessen wird auf das Minimum eingestellt (6A oder `ampLimit`) und der echte Befehl beim nächsten Zyklus erneut versucht.

---

## Allowance (MQTT-Eingang)

Der `SolarOverheadDistributor` liefert via MQTT (`es-ESS/SolarOverheadDistributor/Requests/Wattpilot/Allowance`) die aktuell verfügbare Solar-Leistung in Watt. Dieser Wert ist das zentrale Steuerungssignal für den Auto-Modus. Die **Mindestschwelle** für einen Ladestart ist immer `voltage1 * 6` (typisch ~1380W bei 230V).

---

## Vollständiges Zustandsdiagramm

```
[Kein Fahrzeug / NoCtrlData]  →  Disconnected (0)

[Fahrzeug verbunden, Auto-Modus, nicht ladend, id∈{4,5,6,16,17,18,22,24}]
    allowance < min  →  WaitingForSun (4)
    allowance >= min →  StartCharging (21)  →  [Start-Befehl nach Cooldown]

[Fahrzeug verbunden, Auto-Modus, ladend, id=3/15]
    allowance >= min  →  Charging (2) oder SwitchingToXPhase (22/23)
    allowance < min   →  StopCharging (24)  →  [Stop-Befehl nach Cooldown]
    noCharge >= 120s  →  Charged (3)

[Fahrzeug verbunden, Manual-Modus, nicht ladend]  →  Connected (1)
[Fahrzeug verbunden, Manual-Modus, ladend]        →  Charging (2)

[Niedrigpreis-Laden, id=7]
    ladend           →  Charging (2), LastChargeMode="LowPrice"
    noCharge >= 120s →  Charged (3)

[Phasenumschaltung läuft, id=23]
    currentPhaseMode=1  →  SwitchingTo1Phase (23)
    currentPhaseMode=2  →  SwitchingTo3Phase (22)
```

---

## VRM-Statuswerte Referenz

| VRM-Status | Wert | Bedeutung |
|---|---|---|
| `Disconnected` | 0 | Kein Fahrzeug verbunden |
| `Connected` | 1 | Fahrzeug verbunden, lädt nicht (Manual) |
| `Charging` | 2 | Lädt aktiv |
| `Charged` | 3 | Vollgeladen (kein Strom seit ≥2 Min.) |
| `WaitingForSun` | 4 | Auto-Modus, kein ausreichender PV-Überschuss |
| `StartCharging` | 21 | Ladestart wird eingeleitet |
| `SwitchingTo3Phase` | 22 | Phasenumschaltung auf 3-phasig läuft |
| `SwitchingTo1Phase` | 23 | Phasenumschaltung auf 1-phasig läuft |
| `StopCharging` | 24 | Ladestopp wird eingeleitet |

## WattpilotModelStatus Referenz

| id | Wattpilot-Status | Behandlung |
|---|---|---|
| 0 | `NotChargingBecauseNoChargeCtrlData` | → `Disconnected` |
| 3 | `ChargingBecauseForceStateOn` | → Haupt-Ladezustand |
| 4 | `NotChargingBecauseForceStateOff` | → Start-Logik (Auto) / `Connected` (Manual) |
| 5 | `NotChargingBecauseScheduler` | → Start-Logik (Auto) / `Connected` (Manual) |
| 6 | `NotChargingBecauseEnergyLimit` | → Start-Logik (Auto) / `Connected` (Manual) |
| 7 | `ChargingBecauseAwattarPriceLow` | → Niedrigpreis-Laden, keine Steuerung |
| 15 | `ChargingBecauseFallbackDefault` | → Haupt-Ladezustand |
| 16–18 | `NotChargingBecauseFallback*` | → Start-Logik (Auto) / `Connected` (Manual) |
| 22 | `NotChargingBecauseSimulateUnplugging` | → Start-Logik (Auto) / `Connected` (Manual) |
| 23 | `NotChargingBecausePhaseSwitch` | → `SwitchingTo1Phase` / `SwitchingTo3Phase` |
| 24 | `NotChargingBecauseMinPauseDuration` | → Start-Logik (Auto) / `Connected` (Manual) |
| sonstige | — | Warning-Log, keine Aktion |
