# SvenbotSystemdiagnose
Svenbot ist ein in Python geschriebenes Windows-11-Diagnose- und Sicherheitsanalyse-Tool. Der gesamte Code wurde vollständig von ChatGPT generiert. Ziel: Lernen und Verständnis für Windows, nicht Produktiveinsatz. Läuft komplett offline, ist portable, speichert nichts extern und zeigt mit Humor auf, was dein System preisgibt.

# Svenbot

Ein offline-basiertes Windows-11-Diagnose- und Sicherheitsanalyse-Tool.  
Ziel: Das Verhalten von Windows verstehen, typische Angriffsmuster nachvollziehen und Sicherheit praktisch begreifen.  
Der gesamte Code wurde vollständig mit ChatGPT erstellt.

---

## 1. Sinn und Funktion

Svenbot wurde ausschließlich entwickelt, um Windows und dessen sicherheitsrelevante Mechanismen besser kennenzulernen.  
Das Tool soll helfen zu verstehen, wie Malware agiert, welche Prozesse im Hintergrund laufen und wie man potenziell gefährliche Aktivitäten erkennt.

Ziel ist es, den Benutzer aktiv zum Nachdenken zu bringen:
- Warum wird etwas angezeigt?
- Ist es sicher oder verdächtig?
- Wie würde man es manuell überprüfen?

Das Projekt dient nur zu Bildungszwecken.  
Es ist kein fertiges Produkt und enthält:
- Funktionen, die nicht immer wie beabsichtigt arbeiten
- Viele False Positives (absichtlich, um Denken zu fördern)
- Keine Garantie auf Stabilität oder Korrektheit

Svenbot ersetzt keine professionelle Sicherheitssoftware.  
Es läuft vollständig offline, speichert keine Daten extern und sammelt keine Nutzerdaten.

---

## 2. Installation und Nutzung

Eigenständige Erstellung mit PyInstaller

#### Voraussetzungen:
- Python 3.10 oder neuer
- Module: psutil, pywin32, Crypto, geoip2
- Im Projektordner müssen vorhanden sein:
- `facion.ico` (Programmsymbol)
- `GeoLite2-City.mmdb` (lokale GeoIP-Datenbank von MaxMind)

#### Empfohlener Build-Befehl:
pyinstaller --onefile --console --clean --name Svenbot --icon=facion.ico --add-data "GeoLite2-City.mmdb;." --hidden-import=Crypto --hidden-import=Crypto.Cipher sve6.0.py

Nach erfolgreicher Erstellung befindet sich die ausführbare Datei im Ordner:
dist\Svenbot.exe

#### Hinweise:
- Die GeoLite2-Datenbank kann direkt von der offiziellen MaxMind-Webseite heruntergeladen werden.
- Das Icon (`facion.ico`) muss im selben Verzeichnis liegen.
- Antivirenprogramme können Svenbot fälschlicherweise als verdächtig erkennen, da es Systeminformationen ausliest.  
  Dies ist ein typisches False Positive und kein Hinweis auf Schadcode.

---

### Start und Bedienung

Svenbot ist ein reines Konsolenprogramm.  
Nach dem Start erscheint ein Menü mit verschiedenen Analysefunktionen:
- System- und Sicherheitsüberwachung
- Prozess- und Portanalyse
- Registry- und Taskplanerprüfung
- Lokale Forensik- und Exportfunktionen

Die Navigation erfolgt über einfache Tasteneingaben.

---

## 3. Sicherheit und Haftung

Svenbot ist ausschließlich für den Einsatz auf eigenen oder autorisierten Systemen gedacht.  
Eine Verwendung auf fremden Geräten ohne Zustimmung ist untersagt.

Der Autor übernimmt keine Haftung für Schäden, Datenverluste oder Gesetzesverstöße, die durch unsachgemäße Nutzung entstehen.  
Alle Analysen erfolgen lokal und dienen ausschließlich Lern- und Diagnosezwecken.

---

## 4. Lizenz

- Python Standardbibliothek – PSF License  
- psutil – BSD License  
- pywin32 – MIT License  
- geoip2 – Apache 2.0  
- Crypto (PyCryptodome) – BSD License  
- GeoLite2 (MaxMind) – kostenlose Lizenz, lokale Nutzung  

---

## 5. Fazit

Svenbot ist kein Virenscanner, kein Produkt und kein Allheilmittel.  
Es ist ein Lernwerkzeug, um zu verstehen, was im eigenen System passiert.  
Der beste Schutz beginnt mit Wissen über das, was wirklich läuft.
